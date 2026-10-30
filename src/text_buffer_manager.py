import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0') # NEW: Require GtkSource
# Removed gi.require_version('PangoCairo', '1.0') - not explicitly used in this file.
from gi.repository import Gtk, Pango, GtkSource # MODIFIED: Add GtkSource
from pygments.formatters import HtmlFormatter
from pygments.lexers import MarkdownLexer # Import MarkdownLexer directly
from pygments.token import Token # Import Token for hierarchical lookup
from pygments.styles import get_style_by_name # Import get_style_by_name
from datetime import datetime

# New class definition for Undo/Redo functionality
class UndoRedoTextBuffer(GtkSource.Buffer): # MODIFIED: Inherit from GtkSource.Buffer
    def __init__(self, verbose=True, *args, **kwargs): # MODIFIED: Add verbose argument
        super().__init__(*args, **kwargs)
        self.verbose = verbose # NEW: Store verbose flag
        # Removed custom history management (self.history, self.current_history_index)
        self._ignore_history = False # Flag to suppress history recording for non-user edits
        self.completion_start_mark = None # Marks now belong to the buffer
        self.completion_end_mark = None   # Marks now belong to the buffer
        self.completion_tag = self.create_tag("completion_preview", # New: Tag belongs to the buffer
                                                foreground="#4EC9B0", # Teal color, distinct from main text
                                                editable=False,       # Make it not editable directly
                                                weight=Pango.Weight.LIGHT) # Lighter weight

        # NEW: Store text that the completion preview temporarily replaces
        self.original_suffix_for_preview = None 

        # Syntax highlighting related attributes
        self.highlighting_tags = {}
        self.lexer = MarkdownLexer() 
        self._setup_syntax_highlighting_tags() # Call to setup tags

    def _setup_syntax_highlighting_tags(self):
        """Creates Gtk.TextTags based on a Pygments style."""
        style = get_style_by_name('dracula') # Use Dracula style for consistency
        
        for token_type, style_entry in style:
            tag_name = str(token_type) # Use the full token type path as tag name (e.g., 'Token.Keyword')
            
            props = {}
            if style_entry['color']:
                props['foreground'] = '#' + style_entry['color']
            if style_entry['bgcolor']:
                props['background'] = '#' + style_entry['bgcolor']
            if style_entry['bold']:
                props['weight'] = Pango.Weight.BOLD
            if style_entry['italic']:
                props['style'] = Pango.Style.ITALIC
            if style_entry['underline']:
                props['underline'] = Pango.Underline.SINGLE

            self.highlighting_tags[tag_name] = self.create_tag(tag_name, **props)
            
    def highlight_syntax(self):
        """Applies syntax highlighting to the entire buffer content."""
        # Use _ignore_history to prevent highlighting from being recorded as undo/redo actions
        self._ignore_history = True 
        try:
            start_iter = self.get_start_iter()
            end_iter = self.get_end_iter()

            # Remove all highlighting tags before re-applying
            for tag_name in self.highlighting_tags:
                tag = self.highlighting_tags[tag_name]
                self.remove_tag(tag, start_iter, end_iter)

            full_text = self.get_text(start_iter, end_iter, False)
            current_offset = 0

            # Lex the text and apply tags
            for token_type, value in self.lexer.get_tokens(full_text):
                if value: # Only apply tags if there's actual text for the token
                    # Find the most specific tag for the current token_type
                    tag_to_apply = None
                    current_pygments_token = token_type
                    # Traverse up the Pygments token hierarchy (e.g., from Token.Keyword.Declaration to Token.Keyword)
                    # until a matching Gtk.TextTag is found or we reach the root Token.
                    while current_pygments_token is not Token and tag_to_apply is None:
                        tag_name = str(current_pygments_token)
                        tag_to_apply = self.highlighting_tags.get(tag_name)
                        current_pygments_token = current_pygments_token.parent # Move up

                    if tag_to_apply:
                        tag_start_iter = self.get_iter_at_offset(current_offset)
                        tag_end_iter = self.get_iter_at_offset(current_offset + len(value))
                        self.apply_tag(tag_to_apply, tag_start_iter, tag_end_iter)
                current_offset += len(value)
        finally:
            self._ignore_history = False


    # Removed _clear_redo_history, insert, delete, and set_text overrides.
    # GtkSource.Buffer handles these internally with its undo manager.

    def undo(self):
        """Performs an undo operation using GtkSource.Buffer's undo manager."""
        self._ignore_history = True # Suppress recording this undo operation
        try:
            super().undo() # Call the base class's undo method
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Performed undo.")
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error during undo: {e}")
        finally:
            self._ignore_history = False # Reset flag

    def redo(self):
        """Performs a redo operation using GtkSource.Buffer's undo manager."""
        self._ignore_history = True # Suppress recording this redo operation
        try:
            super().redo() # Call the base class's redo method
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Performed redo.")
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error during redo: {e}")
        finally:
            self._ignore_history = False # Reset flag
