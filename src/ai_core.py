import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import requests
import json
import threading
from datetime import datetime
import os
import re
from collections import defaultdict
import markdown
from xml.etree import ElementTree as ET

mymarkdown_system_prompt="""
You are an intelligent autocomplete engine for Markdown documents of all kinds.
Your goal is to anticipate and suggest relevant Markdown syntax and common text patterns based on the user's current input. 
Provide completions that are syntactically correct, contextually appropriate, and designed to improve the efficiency of writing Markdown. 
Prioritize common Markdown elements like headings, lists, bold/italic text, links, images, and code blocks.
Your task is to provide concise and highly relevant autocomplete suggestions for Markdown syntax. 
Focus on accelerating the user's typing by anticipating common Markdown patterns and structures when they fit the precise context. 

Do not repeat any input verbatim.
Do not repeat the first part of the line you are completing.

Completions should be only the missing words, never type what is already typed.

Below is Relevant Documents, if any.
 
"""

class MultiDocumentRetriever:
    def __init__(self, verbose=True):
        self.vectorizer = TfidfVectorizer(stop_words='english')
        self.doc_lines = {}  # {doc_uid: [line1, line2, ...]}
        self.corpus_lines = []
        self.corpus_line_origins = []  # List of doc_uids for each line in corpus_lines
        self.corpus_vectors = None
        self.lock = threading.Lock()
        self.verbose = verbose
    def _clean_chunk_text(self, text_chunk):
        """Cleans an extracted text chunk by stripping leading/trailing whitespace."""
        return text_chunk.strip()

    def _extract_text_blocks(self, markdown_text):
        """
        Parses Markdown text into semantic blocks (paragraphs, headings, lists, code blocks, tables).
        Each block's text content is then cleaned and returned as a list of strings.
        """
        blocks = []
        # Configure Markdown to output ElementTree, using the same extensions as the editor
        md = markdown.Markdown(extensions=['tables', 'fenced_code', 'codehilite'])
        
        # Convert markdown to HTML first. The Markdown library processes the text
        # and returns the HTML string.
        html_content = md.convert(markdown_text)
        
        # Then, parse the generated HTML into an ElementTree.
        # This is the correct way to get an ElementTree object from the HTML output.
        try:
            root = ET.fromstring(html_content)
        except ET.ParseError as e:
            # Handle cases where the generated HTML might not be perfectly well-formed for strict ET parsing.
            if self.verbose:
                self.raglog(f"Error parsing generated HTML into ElementTree: {e}")
            # If HTML parsing fails, fall back to basic newline splitting immediately.
            if markdown_text.strip():
                raw_paragraphs = re.split(r'\n{2,}', markdown_text)
                for para in raw_paragraphs:
                    cleaned_chunk = self._clean_chunk_text(para)
                    if cleaned_chunk:
                        blocks.append(cleaned_chunk)
            return blocks # Return the blocks from fallback and skip ElementTree iteration

        # Walk the ElementTree to extract text from relevant block-level elements
        for element in root.iter():
            text_content = ""
            tag = element.tag
            
            # Paragraphs, headings, blockquotes, list items
            if tag in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'li']:
                # Extract text from the current element and its children (if any)
                text_content = (element.text or "")
                for child in element:
                    text_content += (child.text or "")
                    text_content += (child.tail or "") # Important for text following child elements

                cleaned_chunk = self._clean_chunk_text(text_content)
                if cleaned_chunk:
                    blocks.append(cleaned_chunk)

            # Code blocks (pre tag typically contains a code tag)
            elif tag == 'pre':
                code_element = element.find('code') # Get the code child within pre
                if code_element is not None and code_element.text:
                    cleaned_chunk = self._clean_chunk_text(code_element.text)
                    if cleaned_chunk:
                        blocks.append(cleaned_chunk)
            
            # Tables (collect all cell text into one block for the entire table)
            elif tag == 'table':
                table_text_parts = []
                for child in element.iter(): # Iterate through all descendants of the table
                    if child.tag in ['th', 'td']:
                        if child.text:
                            table_text_parts.append(child.text.strip())
                    if child.tag == 'tr': # Add a newline or separator after each row for better readability
                        table_text_parts.append("\n") # Add a newline character
                if table_text_parts:
                    # Join all table cell contents into a single chunk, removing empty lines from newlines
                    cleaned_chunk = self._clean_chunk_text(" ".join(filter(None, table_text_parts)))
                    if cleaned_chunk:
                        blocks.append(cleaned_chunk)


        # Fallback for documents that are just plain text or have structures not explicitly handled.
        # If no specific markdown blocks were found but there's content,
        # fall back to splitting by double newlines as a basic paragraph detection.
        if not blocks and markdown_text.strip():
            raw_paragraphs = re.split(r'\n{2,}', markdown_text)
            for para in raw_paragraphs:
                cleaned_chunk = self._clean_chunk_text(para)
                if cleaned_chunk:
                    blocks.append(cleaned_chunk)

        return blocks

    def raglog(self, msg):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]  [RAG] {msg}")

    def _rebuild_corpus(self):
        """Rebuilds the entire corpus from all tracked documents."""
        if self.verbose:
            self.raglog(f"Rebuilding corpus from {len(self.doc_lines)} documents...")

        self.corpus_lines = []
        self.corpus_line_origins = []
            
        for doc_uid, blocks in self.doc_lines.items():
            # Blocks are already cleaned by _extract_text_blocks, so no need for _clean_chunk_text here
            self.corpus_lines.extend(blocks)
            self.corpus_line_origins.extend([doc_uid] * len(blocks))

        if not self.corpus_lines:
            self.corpus_vectors = None
            if self.verbose:
                self.raglog(f"Corpus is empty, cleared vectors.")
            return

        try:
            self.corpus_vectors = self.vectorizer.fit_transform(self.corpus_lines)
            if self.verbose:
                self.raglog(f"Corpus rebuilt and vectorized successfully. Total lines: {len(self.corpus_lines)}")
                self.raglog(f"--- Vectorized Lines START ---")
                for line in self.corpus_lines:
                    print(f"[RAG]   '{line}'")
                self.raglog(f"--- Vectorized Lines END ---")
        except ValueError as e:
            if "empty vocabulary" in str(e):
                if self.verbose:
                    self.raglog(f"Corpus contains only stop words. Vectorization skipped.")
            else:
                if self.verbose:
                    self.raglog(f"Could not vectorize corpus. Error: {e}")
            self.corpus_vectors = None

    def add_or_update_document(self, doc_uid, text):
        """Processes a document's text, adds/updates it in the collection, and rebuilds the corpus."""
        with self.lock:
            if self.verbose:
                self.raglog(f"Updating document '{doc_uid}'...")
            # Use the new _extract_text_blocks method to get semantic chunks
            blocks = self._extract_text_blocks(text)
            self.doc_lines[doc_uid] = blocks
            if self.verbose:
                self.raglog(f"Document '{doc_uid}' stored with {len(blocks)} semantic blocks.")
            self._rebuild_corpus()

    def remove_document(self, doc_uid):
        """Removes a document from the collection and rebuilds the corpus."""
        with self.lock:
            if doc_uid in self.doc_lines:
                if self.verbose:
                    self.raglog(f"Removing document '{doc_uid}' from corpus.")
                del self.doc_lines[doc_uid]
                self._rebuild_corpus()
            elif self.verbose:
                self.raglog(f"Attempted to remove doc '{doc_uid}', but it was not in the corpus.")

    def retrieve_relevant_lines(self, query_text, n=5, current_doc_uid=None):
        """Retrieves the top N most relevant lines from ALL documents based on the query."""
        if self.verbose:
            self.raglog(f"MultiDocumentRetriever query: '{query_text[:80]}...'")

        with self.lock:
            if not query_text or self.corpus_vectors is None or not self.corpus_lines:
                if self.verbose:
                    reason = "empty query" if not query_text else "no corpus vectors" if self.corpus_vectors is None else "empty corpus"
                    self.raglog(f"No retrieval. Reason: {reason}")
                return ""
            try:
                query_vector = self.vectorizer.transform([query_text.strip()])
            except ValueError:
                return ""

            similarities = cosine_similarity(query_vector, self.corpus_vectors).flatten()
            sorted_indices = np.argsort(similarities)[::-1]
            
            relevant_items = []
            query_lines = {line.strip() for line in query_text.split('\n\n') if line.strip()}
            
            for i in sorted_indices:
                line = self.corpus_lines[i]
                if line not in query_lines and line not in [item[0] for item in relevant_items]:
                    doc_uid = self.corpus_line_origins[i]
                    relevant_items.append((line, doc_uid))
                    if len(relevant_items) >= n:
                        break
            
            if not relevant_items:
                if self.verbose:
                    self.raglog(f"No relevant lines found across all documents.")
                return ""

            grouped_lines = defaultdict(list)
            for line, doc_uid in relevant_items:
                grouped_lines[doc_uid].append(line)

            context_parts = []
            for doc_uid, lines in grouped_lines.items():
                doc_name = os.path.basename(doc_uid) if doc_uid.startswith('/') else doc_uid.replace("unsaved-", "New Doc ")
                context_parts.append(f"--- If needed, Here is relevant context from Retrieved Document '{doc_name}' ---")
                context_parts.extend(lines)

            context = "\n".join(context_parts)
            if self.verbose:
                self.raglog(f"Retrieved Relevant Context:\n{context}")
            
            return f"{context}\n------------ End of Retreived Document --------------------"
