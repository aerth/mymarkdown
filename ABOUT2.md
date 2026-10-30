This application is a GTK-based Markdown editor with the following key features:

*   **Markdown Editing and Previewing:** It allows users to write Markdown text and can display a live rendered HTML preview, either in a dedicated view or side-by-side with the editor.
*   **Multi-Document Management:** It includes a file tree on the side to open and manage multiple files and directories. It tracks unsaved changes for each open document.
*   **AI-Powered Text Completion:** It connects to an Ollama-compatible API to provide real-time, streaming text completions as the user types. The completions appear as a preview that can be accepted or dismissed.
*   **Retrieval-Augmented Generation (RAG):** To improve AI suggestions, it can analyze the content of the current document to find relevant context and provide it to the language model when requesting a completion.
*   **Syntax Highlighting:** The text editor view provides syntax highlighting for the Markdown source.
*   **Custom Undo/Redo:** It implements its own undo and redo functionality for text changes.
