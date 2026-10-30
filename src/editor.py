import os
import gi

gi.require_version('GtkSource', '3.0')  # NEW: Require GtkSource specifically in this file
from gi.repository import Gtk, Gdk, Gio, GLib, Pango, PangoCairo, WebKit2, GtkSource  # MODIFIED: Add GtkSource
import markdown  # For Markdown to HTML conversion
import json
import re  # Import the regular expression module
import requests
import threading
from datetime import datetime
from pygments.formatters import HtmlFormatter
from text_buffer_manager import UndoRedoTextBuffer
from ai_core import MultiDocumentRetriever, mymarkdown_system_prompt


class MyMarkdownEditor(Gtk.ApplicationWindow):
    def __init__(self, app):
        Gtk.ApplicationWindow.__init__(self, application=app, title="mymarkdown")
        self.app = app  # Store reference to the application instance
        self.set_default_size(800, 600)

        # New: Document management
        # Stores dict of {file_path_or_uid: {'buffer': UndoRedoTextBuffer,
        #                                     'tree_iter': Gtk.TreeIter,
        #                                     'parent_iter': Gtk.TreeIter or None,
        #                                     'is_unsaved': bool,
        #                                     'initial_load_done': bool}}
        self.open_documents = {}
        self.current_file_path = None  # Stores the path/uid of the currently active document
        self.document_uid_counter = 0  # For unique IDs for unsaved documents

        self.verbose_logging_enabled = True  # Initial state
        self.completion_enabled = True
        self.rag_enabled = False

        # NEW: For managing cancellation of Ollama completion requests
        self._suppress_next_completion_trigger = False  # NEW: Flag to prevent immediate re-trigger after preview dismissal
        self.completion_cancellation_event = threading.Event()
        # NEW: Flag to indicate if a completion thread is currently active
        self.active_completion_thread_running = False
        self.last_recalled_completion_text = None  # NEW: Store the last dismissed completion for recall

        self.highlight_timeout_id = None  # New: For debouncing syntax highlighting updates
        self.highlight_delay_ms = 100  # New: Delay for highlighting updates

        # NEW: For debouncing LLM completion requests
        self.completion_timeout_id = None
        self.completion_delay_ms = 500  # Adjust as needed
        self.retriever_update_delay_ms = 2000  # Delay for updating the RAG index


        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(200)

        self.raw_view_scrolled_window = Gtk.ScrolledWindow()
        self.raw_view_scrolled_window.set_hexpand(True)
        self.raw_view_scrolled_window.set_vexpand(True)
        self.stack.add_named(self.raw_view_scrolled_window, "raw_view")

        self.rendered_view_scrolled_window = Gtk.ScrolledWindow()
        self.rendered_view_scrolled_window.set_hexpand(True)
        self.rendered_view_scrolled_window.set_vexpand(True)
        self.stack.add_named(self.rendered_view_scrolled_window, "rendered_view")

        self.side_by_side_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.side_by_side_paned.set_hexpand(True)
        self.side_by_side_paned.set_vexpand(True)

        self.side_by_side_textview_scrolled_window = Gtk.ScrolledWindow()
        self.side_by_side_textview_scrolled_window.set_hexpand(True)
        self.side_by_side_textview_scrolled_window.set_vexpand(True)

        self.side_by_side_webview_scrolled_window = Gtk.ScrolledWindow()
        self.side_by_side_webview_scrolled_window.set_hexpand(True)
        self.side_by_side_webview_scrolled_window.set_vexpand(True)

        self.side_by_side_paned.pack1(self.side_by_side_textview_scrolled_window, True, False)
        self.side_by_side_paned.pack2(self.side_by_side_webview_scrolled_window, True, False)
        self.side_by_side_paned.set_position(self.get_default_size().width // 2)

        self.stack.add_named(self.side_by_side_paned, "side_by_side_view")

        # New: Welcome screen
        self.welcome_label = Gtk.Label()
        self.welcome_label.set_halign(Gtk.Align.CENTER)
        self.welcome_label.set_valign(Gtk.Align.CENTER)
        self.welcome_label.set_line_wrap(True)
        self.welcome_label.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.welcome_label, "welcome_view")

        # New: Create the main paned for file list and editor content
        self.main_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.main_paned.set_position(200)  # Initial width for file list panel

        # New: TreeView for file list
        # Column definitions: [display_name, file_path_or_uid, is_directory, is_unsaved, buffer_ref]
        self.tree_store = Gtk.TreeStore(str, str, bool, bool, object)
        self.file_tree_view = Gtk.TreeView(model=self.tree_store)
        self.file_tree_view.set_headers_visible(False)  # Hide column headers
        self.file_tree_view.set_enable_tree_lines(True)

        # Create a CellRendererText to render the file/directory name
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Name", renderer, text=0)  # Column 0 is display_name
        self.file_tree_view.append_column(column)

        # Connect selection handler
        self.file_tree_view.get_selection().set_mode(Gtk.SelectionMode.BROWSE)  # Single selection
        self.file_tree_view.get_selection().connect("changed", self._on_file_tree_selection_changed)

        self.file_tree_view_scrolled_window = Gtk.ScrolledWindow()
        self.file_tree_view_scrolled_window.set_hexpand(False)
        self.file_tree_view_scrolled_window.set_vexpand(True)
        self.file_tree_view_scrolled_window.add(self.file_tree_view)
        self.main_paned.pack1(self.file_tree_view_scrolled_window, False, True)  # Not resizeable, shrinkable

        # Move retriever_update_timeout_id initialization here
        self.current_font_size_pt = 12

        # NEW: Statusbar at the bottom - Initialized early
        self.statusbar = Gtk.Statusbar()
        self.statusbar_context_id = self.statusbar.get_context_id("app-status")
        self.status_timeout_id = None # To manage message auto-clearing

        self.document_retriever = MultiDocumentRetriever(verbose=self.verbose_logging_enabled)

        self.textview = GtkSource.View()  # MODIFIED: Use GtkSource.View for line numbers and features
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD)
        self.textview.set_show_line_numbers(True)  # NEW: Enable line numbers
        self.buffer = None  # Will be set by _switch_to_file
        # self.textview.set_buffer(self.buffer) # This will be set by _switch_to_file

        self.textview.set_left_margin(10)
        self.textview.set_right_margin(10)
        self.textview.set_pixels_above_lines(5)
        self.textview.set_pixels_below_lines(5)
        self._apply_font_size(self.textview, self.current_font_size_pt)
        self.textview.add_events(Gdk.EventMask.SCROLL_MASK)
        self.webview = None  # NEW: Initialize webview to None for lazy loading

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(200)

        self.raw_view_scrolled_window = Gtk.ScrolledWindow()
        self.raw_view_scrolled_window.set_hexpand(True)
        self.raw_view_scrolled_window.set_vexpand(True)
        self.stack.add_named(self.raw_view_scrolled_window, "raw_view")

        self.rendered_view_scrolled_window = Gtk.ScrolledWindow()
        self.rendered_view_scrolled_window.set_hexpand(True)
        self.rendered_view_scrolled_window.set_vexpand(True)
        self.stack.add_named(self.rendered_view_scrolled_window, "rendered_view")

        self.side_by_side_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.side_by_side_paned.set_hexpand(True)
        self.side_by_side_paned.set_vexpand(True)

        self.side_by_side_textview_scrolled_window = Gtk.ScrolledWindow()
        self.side_by_side_textview_scrolled_window.set_hexpand(True)
        self.side_by_side_textview_scrolled_window.set_vexpand(True)

        self.side_by_side_webview_scrolled_window = Gtk.ScrolledWindow()
        self.side_by_side_webview_scrolled_window.set_hexpand(True)
        self.side_by_side_webview_scrolled_window.set_vexpand(True)

        self.side_by_side_paned.pack1(self.side_by_side_textview_scrolled_window, True, False)
        self.side_by_side_paned.pack2(self.side_by_side_webview_scrolled_window, True, False)
        self.side_by_side_paned.set_position(self.get_default_size().width // 2)

        self.stack.add_named(self.side_by_side_paned, "side_by_side_view")

        # New: Welcome screen
        self.welcome_label = Gtk.Label()
        self.welcome_label.set_halign(Gtk.Align.CENTER)
        self.welcome_label.set_valign(Gtk.Align.CENTER)
        self.welcome_label.set_line_wrap(True)
        self.welcome_label.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.welcome_label, "welcome_view")

        # New: Create the main paned for file list and editor content
        self.main_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.main_paned.set_position(200)  # Initial width for file list panel

        # New: TreeView for file list
        # Column definitions: [display_name, file_path_or_uid, is_directory, is_unsaved, buffer_ref]
        self.tree_store = Gtk.TreeStore(str, str, bool, bool, object)
        self.file_tree_view = Gtk.TreeView(model=self.tree_store)
        self.file_tree_view.set_headers_visible(False)  # Hide column headers
        self.file_tree_view.set_enable_tree_lines(True)

        # Create a CellRendererText to render the file/directory name
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Name", renderer, text=0)  # Column 0 is display_name
        self.file_tree_view.append_column(column)

        # Connect selection handler
        self.file_tree_view.get_selection().set_mode(Gtk.SelectionMode.BROWSE)  # Single selection
        self.file_tree_view.get_selection().connect("changed", self._on_file_tree_selection_changed)

        self.file_tree_view_scrolled_window = Gtk.ScrolledWindow()
        self.file_tree_view_scrolled_window.set_hexpand(False)
        self.file_tree_view_scrolled_window.set_vexpand(True)
        self.file_tree_view_scrolled_window.add(self.file_tree_view)
        self.main_paned.pack1(self.file_tree_view_scrolled_window, False, True)  # Not resizeable, shrinkable

        # Move retriever_update_timeout_id initialization here
        self.retriever_update_timeout_id = None

        self.main_paned.pack2(self.stack, True, True)  # Editor content is resizeable, shrinkable
        # self.add(self.main_paned) # REMOVED: Replaced by VBox below

        # NEW: Create a VBox to hold the main_paned and the statusbar
        main_vbox = Gtk.VBox(False, 0)
        main_vbox.pack_start(self.main_paned, True, True, 0) # Main content takes most space
        main_vbox.pack_end(self.statusbar, False, False, 0) # Statusbar at the bottom
        self.add(main_vbox) # Add the VBox to the window

        self._set_display_mode_raw()  # Set initial view and parent textview
        self.markdown_converter = markdown.Markdown(extensions=['tables', 'fenced_code', 'codehilite'])
        self.pygments_css = None  # Initialize to None for lazy loading

        self.render_timeout_id = None
        self.render_delay_ms = 500

        # Connect text buffer changed signal will be done in _switch_to_file
        # self.buffer.connect("changed", self.on_text_changed)
        self.textview.connect("key-press-event", self.on_key_press)
        self.textview.connect("scroll-event", self.on_scroll_event)

        # Connect the delete-event signal for save-on-quit handling
        self.connect("delete-event", self.on_delete_event)

        # New: Start with a new, empty document
        self._new_document()  # This will set the initial buffer, retriever, etc.
        self._update_status("Welcome!", 3000) # NEW: Initial status message

    def on_delete_event(self, window, event):
        """
        Handles the window close request (e.g., via 'X' button or Alt+F4).
        It initiates the application quit process which includes save prompts.
        Returns True to prevent the window from destroying itself immediately.
        """
        print("Window delete-event detected. Initiating application quit sequence.")
        self.app.quit()  # This will trigger _on_app_quit_activated in MyMarkdownApp
        return True  # Return True to prevent the default window destruction

    def _update_status(self, message, timeout_ms=5000):
        """
        Updates the status bar with a message.
        The message will clear automatically after timeout_ms milliseconds if timeout_ms > 0.
        """
        if self.status_timeout_id:
            GLib.source_remove(self.status_timeout_id)
            self.status_timeout_id = None

        # Always pop any existing message from this context before pushing a new one
        self.statusbar.pop(self.statusbar_context_id)
        self.statusbar.push(self.statusbar_context_id, message)
        
        if self.verbose_logging_enabled: # Also print to console for verbose mode
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [STATUS] {message}")

        if timeout_ms > 0:
            self.status_timeout_id = GLib.timeout_add(timeout_ms, self._clear_status)

    def _clear_status(self):
        """Clears the status bar message."""
        self.statusbar.pop(self.statusbar_context_id)
        self.status_timeout_id = None
        return GLib.SOURCE_REMOVE # Only run once

    def _prompt_to_save_document(self, doc_uid_to_check):
        """
        Prompts the user to save a specific document if it's unsaved.
        Returns True if the user cancels the operation (i.e., wants to stop closing/switching),
        False otherwise (i.e., user chose Save or Discard, so operation can proceed).
        """
        doc_data = self.open_documents.get(doc_uid_to_check)
        if not doc_data or not doc_data['is_unsaved']:
            # No need to save or document not found/already saved
            return False

        display_name = os.path.basename(doc_uid_to_check) if doc_uid_to_check.startswith("/") else doc_uid_to_check.replace("unsaved-", "New Document ")

        dialog = Gtk.MessageDialog(
            parent=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, # We'll add custom buttons
            message_format=f"Do you want to save changes to '{display_name}' before closing?"
        )
        dialog.add_button("Save", Gtk.ResponseType.YES)
        dialog.add_button("Discard", Gtk.ResponseType.NO)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.set_default_response(Gtk.ResponseType.YES) # Pre-select Save

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Prompting to save: {doc_uid_to_check}")
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            # If current_file_path is the one being prompted, _save_file_content/as will update its status
            # If it's another document being prompted during shutdown, _save_file_content/as will also handle it.
            if doc_uid_to_check.startswith("/"): # Already has a file path
                self._save_file_content(doc_uid_to_check) # This method updates 'is_unsaved'
            else: # Unsaved new document, needs Save As
                self._save_file_as() # This method also handles updating doc_uid and 'is_unsaved'

            # Re-check if it's still unsaved after attempting to save (e.g., user cancelled Save As dialog)
            if self.open_documents.get(doc_uid_to_check, {}).get('is_unsaved', False):
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Save operation was cancelled or failed for: {doc_uid_to_check}. Aborting current action.")
                self._update_status(f"Save of '{display_name}' cancelled.", 3000) # NEW
                return True # Treat as a cancellation if save wasn't successful
            return False # Save was successful or discarded, proceed with the original action
        elif response == Gtk.ResponseType.NO:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Discarding changes for: {doc_uid_to_check}. Proceeding.")
            # Mark as not unsaved so it's not prompted again if it's removed later in a batch operation.
            if doc_data: # Ensure doc_data is still valid
                doc_data['is_unsaved'] = False
                self._update_tree_view_row_label(doc_uid_to_check)
            self._update_status(f"Changes to '{display_name}' discarded.", 3000) # NEW
            return False # Discard chosen, proceed
        elif response == Gtk.ResponseType.CANCEL:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Save prompt cancelled for: {doc_uid_to_check}. Aborting current action.")
            self._update_status(f"Operation cancelled.", 3000) # NEW
            return True # User cancelled, abort the original action (e.g., quit, switch)
        return False # Should not be reached

    def _ensure_webview_initialized(self):  # NEW METHOD
        """Initializes WebKit2.WebView if it hasn't been already."""
        if self.webview is None:
            if self.verbose_logging_enabled:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Lazily initializing WebKit2.WebView.")
            self.webview = WebKit2.WebView()
            self.webview.set_hexpand(True)
            self.webview.set_vexpand(True)

    def _setup_pygments_css(self):
        try:
            self.pygments_css = HtmlFormatter(style='dracula').get_style_defs('.codehilite')
        except ImportError:
            print("Pygments not found. Code highlighting will not work.")
            self.pygments_css = ""
            self._update_status("Pygments not found. Code highlighting disabled.", 5000) # NEW
        except Exception as e:
            print(f"Error generating Pygments CSS: {e}")
            self.pygments_css = ""
            self._update_status(f"Pygments CSS error: {e}", 5000) # NEW

    def _regenerate_completion(self):
        """Clears existing completion and triggers a new one immediately."""
        self._cleanup_completion_preview_safe()  # Clear any existing preview
        self.trigger_completion()  # Request a new completion

    def on_text_changed(self, buffer):
        # If self.buffer is None, it means no document is currently active or it's during a transition.
        # In such cases, we should not proceed with completion triggers or UI updates tied to the active buffer.
        if self.buffer is None:
            return False

        # NEW: Check if this change is programmatic (e.g., initial load, undo/redo, completion insert/delete)
        # In such cases, we update UI elements but do NOT trigger new completions or RAG updates.
        if self.buffer._ignore_history:
            # Mark the current document as unsaved if it wasn't already (applies to programmatic changes too)
            if self.current_file_path:
                doc_data = self.open_documents.get(self.current_file_path)
                if doc_data and not doc_data['is_unsaved']:
                    doc_data['is_unsaved'] = True
                    self._update_tree_view_row_label(self.current_file_path)
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Marked {self.current_file_path} as unsaved due to programmatic change.")

            # Still trigger render and highlight updates (debounced)
            if self.render_timeout_id:
                GLib.source_remove(self.render_timeout_id)
                self.render_timeout_id = None
            self.render_timeout_id = GLib.timeout_add(self.render_delay_ms, self._trigger_render_update)

            if self.highlight_timeout_id:
                GLib.source_remove(self.highlight_timeout_id)
                self.highlight_timeout_id = None
            self.highlight_timeout_id = GLib.timeout_add(self.highlight_delay_ms, self._trigger_highlight_update)

            # Crucially, do NOT trigger completion or RAG update for programmatic changes
            return False

        # NEW: Check suppression flag set by on_key_press when a preview was dismissed by typing
        if self._suppress_next_completion_trigger:
            self._suppress_next_completion_trigger = False  # Reset flag for next normal typing
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Suppressing immediate re-trigger due to overwrite key event.")
            # Still update unsaved status, rendering, highlighting, but DO NOT trigger completion
            if self.current_file_path:
                doc_data = self.open_documents.get(self.current_file_path)
                if doc_data and not doc_data['is_unsaved']:
                    doc_data['is_unsaved'] = True
                    self._update_tree_view_row_label(self.current_file_path)

            if self.render_timeout_id:
                GLib.source_remove(self.render_timeout_id)
            self.render_timeout_id = GLib.timeout_add(self.render_delay_ms, self._trigger_render_update)

            if self.highlight_timeout_id:
                GLib.source_remove(self.highlight_timeout_id)
            self.highlight_timeout_id = GLib.timeout_add(self.highlight_delay_ms, self._trigger_highlight_update)
            return False  # Crucially, stop here, don't trigger_completion()

        # NEW: Cancel any ongoing completion when text changes, as we're about to request a new one
        self._cancel_pending_completion_request()

        # New: Mark the current document as unsaved
        if self.current_file_path:
            doc_data = self.open_documents.get(self.current_file_path)
            if doc_data and not doc_data['is_unsaved']:
                doc_data['is_unsaved'] = True
                self._update_tree_view_row_label(self.current_file_path)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Marked {self.current_file_path} as unsaved.")

        if self.render_timeout_id:
            GLib.source_remove(self.render_timeout_id)
            self.render_timeout_id = None

        if self.highlight_timeout_id:
            GLib.source_remove(self.highlight_timeout_id)
            self.highlight_timeout_id = None

        # Debounce completion requests
        if self.completion_timeout_id:
            GLib.source_remove(self.completion_timeout_id)
        self.completion_timeout_id = GLib.timeout_add(self.completion_delay_ms, self.trigger_completion)

        if self.retriever_update_timeout_id:
            GLib.source_remove(self.retriever_update_timeout_id)
        self.retriever_update_timeout_id = GLib.timeout_add(self.retriever_update_delay_ms,
                                                            self._trigger_retriever_update)

        self.render_timeout_id = GLib.timeout_add(self.render_delay_ms, self._trigger_render_update)
        self.highlight_timeout_id = GLib.timeout_add(self.highlight_delay_ms, self._trigger_highlight_update)

        return False

    def on_key_press(self, widget, event):
        """Handles key press events for the text view."""

        has_active_preview = self.buffer is not None and self.buffer.completion_start_mark and self.buffer.completion_end_mark
        is_modifier_key = (
                event.keyval in [Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
                                 Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R]
        )

        # # NEW: Handle Ctrl+; for emoji picker
        # if event.keyval == Gdk.KEY_semicolon and event.state & Gdk.ModifierType.CONTROL_MASK:
        #     print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Detected Ctrl+;. Triggering emoji picker.")
        #     self.textview.insert_emoji()
        #     return True # Event handled

        # 1. Handle acceptance of completion (Ctrl+I or Tab) when a preview is active
        if has_active_preview and \
                ((event.keyval == Gdk.KEY_Tab) or \
                 (event.keyval == Gdk.KEY_i and event.state & Gdk.ModifierType.CONTROL_MASK)):
            self._accept_completion()
            return True  # Event handled (completion accepted)

        # 2. If a completion preview is active and the key is NOT Tab/Ctrl+I:
        if has_active_preview:
            if is_modifier_key:
                # Allow modifier keys to pass through without dismissing completion
                # (e.g., Ctrl+C for copy, Ctrl+V for paste, Ctrl+Z for undo, etc.)
                return False  # Allow the TextView to process the modifier key
            else:
                # For any other (non-modifier, non-acceptance) key, dismiss the completion
                # and let the key press proceed (e.g., typing 'a' dismisses and inserts 'a').
                self._suppress_next_completion_trigger = True  # NEW: Set flag to prevent double trigger
                self._cancel_pending_completion_request()
                return False  # Allow the key press to be processed by TextView

        # 3. If no completion preview is active, allow all key presses to proceed normally
        # without any cancellation logic. New completions are triggered by on_text_changed.
        return False

    def trigger_completion(self):
        # NEW: Immediately cancel any previously active completion request
        # This ensures only one request is in flight at a time and the latest user input wins.
        self._cancel_pending_completion_request()

        # Reset the timeout ID (this is primarily for manual regeneration, not auto-trigger anymore)
        # self.completion_timeout_id = None # Redundant, already handled by _cancel_pending_completion_request

        # Only trigger completion if the feature is enabled
        if not self.completion_enabled:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Completion is disabled. Not triggering.")
            self._update_status("AI Completion is disabled.", 3000) # NEW
            return False

        # Get the current text
        if self.buffer is None:
            return False
        insert_iter = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start_iter = self.buffer.get_start_iter()
        end_iter = self.buffer.get_end_iter()

        prefix_text = self.buffer.get_text(start_iter, insert_iter, False)
        suffix_text = self.buffer.get_text(insert_iter, end_iter, False)

        # Removed rstrip, keep original
        prefix_text = prefix_text

        # NEW: Clear the cancellation event for this new request
        self.completion_cancellation_event.clear()
        # NEW: Mark that a new completion request is active
        self.active_completion_thread_running = True

        # NEW: Update status bar
        self._update_status("Sending completion request...", 0) # 0 timeout to keep until new status replaces it

        # Run the API call in a separate thread to avoid blocking the UI
        # NEW: Pass the cancellation event to the thread
        thread = threading.Thread(target=self._run_ollama_completion,
                                  args=(prefix_text, suffix_text, self.completion_cancellation_event))
        thread.daemon = True  # Allow the program to exit even if thread is running
        thread.start()

        # Reset the timeout ID as this function is now called by GLib.timeout_add
        self.completion_timeout_id = None
        return False  # Ensure the timeout is only called once

    def _trigger_render_update(self):
        """Called by timeout to trigger a rendered view update if in rendered mode."""
        self.render_timeout_id = None  # Reset timeout ID

        if self.buffer is None:
            return False
        if self.current_view_mode in ["rendered", "side-by-side"]:
            # Get current text on the UI thread and schedule the actual update
            start_iter = self.buffer.get_start_iter()
            end_iter = self.buffer.get_end_iter()
            current_text = self.buffer.get_text(start_iter, end_iter, False)
            GLib.idle_add(self._update_rendered_view, current_text)
        return False  # Ensure the timeout is only called once

    def _update_rendered_view(self, markdown_text=None):
        if self.buffer is None:
            return False
        """Converts markdown to HTML and displays it in the webview.
        Can be called with specific text or will fetch from current buffer."""
        if markdown_text is None:
            start_iter = self.buffer.get_start_iter()
            end_iter = self.buffer.get_end_iter()
            markdown_text = self.buffer.get_text(start_iter, end_iter, False)

        if self.pygments_css is None:
            print("setting up pygments css")
            self._setup_pygments_css()

        html_content = self.markdown_converter.convert(markdown_text)

        # Wrap HTML content with dark mode CSS
        full_html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                {self.pygments_css} /* Pygments styles for code highlighting */
                body {{
                    font-family: sans-serif;
                    background-color: #1e1e1e;
                    color: #d4d4d4;
                    margin: 20px;
                    line-height: 1.6;
                }}
                h1, h2, h3, h4, h5, h6 {{
                    color: #569cd6; /* Blueish for headers */
                }}
                a {{
                    color: #4ec9b0; /* Teal for links */
                }}
                pre {{
                    background-color: #252526;
                    padding: 10px;
                    border-radius: 5px;
                    overflow-x: auto;
                    color: #dcdcdc;
                }}
                code {{
                    background-color: #333;
                    padding: 2px 4px;
                    border-radius: 3px;
                }}
                blockquote {{
                    border-left: 4px solid #6a737d;
                    padding-left: 10px;
                    color: #8b949e;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                }}
                th, td {{
                    border: 1px solid #444;
                    padding: 8px;
                    text-align: left;
                }}
                th {{
                    background-color: #333;
                }}
            </style>
        </head>
        <body>
            {html_content}
        </body>
        </html>
        """
        if self.webview is None:
            return GLib.SOURCE_REMOVE
        self.webview.load_html(full_html_content, "file:///")
        return GLib.SOURCE_REMOVE  # Required for GLib.idle_add (removed after execution)

    def _trigger_highlight_update(self):  # NEW METHOD
        """Called by timeout to trigger a syntax highlighting update."""
        if self.buffer is None:
            return GLib.SOURCE_REMOVE
        self.highlight_timeout_id = None  # Reset timeout ID
        GLib.idle_add(self.buffer.highlight_syntax)  # Call highlight_syntax on the main UI thread
        return GLib.SOURCE_REMOVE  # Ensure the timeout is only called once

    def _cleanup_completion_preview_safe(self):
        # self._clear_completion_pending = False # Removed, no longer needed

        # Access marks from the *active* buffer
        if self.buffer and self.buffer.completion_start_mark and self.buffer.completion_end_mark:
            # NEW: Restore original suffix if it exists
            if self.buffer.original_suffix_for_preview:
                if self.verbose_logging_enabled:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Restoring original suffix: '{self.buffer.original_suffix_for_preview}'")
                self.buffer._ignore_history = True
                try:
                    # Retrieve a fresh iterator from the mark immediately before insertion
                    insert_iter = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
                    self.buffer.insert(insert_iter, self.buffer.original_suffix_for_preview)
                finally:
                    self.buffer._ignore_history = False
                self.buffer.original_suffix_for_preview = None # Clear after restoring

            # Retrieve fresh iterators for get_text and delete operations
            current_start_iter = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
            current_end_iter = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)

            if current_start_iter.compare(current_end_iter) < 0:  # Only store and delete if there's actual text between marks
                # NEW: Store the content of the completion preview before it's deleted
                preview_content = self.buffer.get_text(current_start_iter, current_end_iter, False)
                if preview_content:  # Only store if there is actual text
                    self.last_recalled_completion_text = preview_content
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Stored last completion for recall: '{preview_content}'")

                self.buffer._ignore_history = True
                try:
                    # Retrieve fresh iterators immediately before deletion
                    delete_start_iter = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
                    delete_end_iter = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)
                    self.buffer.delete(delete_start_iter, delete_end_iter)
                finally:
                    self.buffer._ignore_history = False

            # Delete marks only if they are still valid (not None)
            # These checks are robust as marks themselves track their positions.
            if self.buffer.completion_start_mark:
                self.buffer.delete_mark(self.buffer.completion_start_mark)
            if self.buffer.completion_end_mark:
                self.buffer.delete_mark(self.buffer.completion_end_mark)

            # Clear marks on the active buffer (set to None)
            self.buffer.completion_start_mark = None
            self.buffer.completion_end_mark = None
        return GLib.SOURCE_REMOVE

    def _cancel_pending_completion_request(self):  # NEW METHOD
        """Signals any active completion thread to stop and cleans up UI preview."""
        if self.active_completion_thread_running:
            print("[COMPLETION] Cancelling active completion request...")
            self.completion_cancellation_event.set()  # Signal the worker thread to cancel
            self.active_completion_thread_running = False  # NEW: Immediately mark as not running on UI thread
            # The worker thread will handle resetting self.active_completion_thread_running via GLib.idle_add.

        # Always clear any visual preview immediately
        self._cleanup_completion_preview_safe()
        self._update_status("Completion cancelled.", 3000) # NEW

    def _accept_completion(self):
        """Accepts the currently displayed AI completion text, making it part of the main document."""
        # If there's no active completion preview, do nothing.
        if not (self.buffer.completion_start_mark and self.buffer.completion_end_mark):
            return

        start_iter = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
        end_iter = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)

        # For logging purposes
        accepted_text = self.buffer.get_text(start_iter, end_iter, False)
        # If the preview is empty for some reason, just clean it up.
        if not accepted_text:
            println("not accepted, will cancel")
            self._cancel_pending_completion_request()
            return

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Accepted completion: '{accepted_text}'")

        # NEW: Clear the original suffix, as the completion is now permanent.
        self.buffer.original_suffix_for_preview = None

        # 1. Make the text permanent by removing the preview tag.
        self.buffer.remove_tag(self.buffer.completion_tag, start_iter, end_iter)

        # 2. Move cursor to the end of the newly accepted text.
        self.buffer.place_cursor(end_iter)

        # 3. Stop any further text from being streamed.
        if self.active_completion_thread_running:
            self.completion_cancellation_event.set()
            # From the UI thread's perspective, the completion is no longer active.
            self.active_completion_thread_running = False

        # 4. Clean up the preview marks, as the text is no longer a preview.
        self.buffer.delete_mark(self.buffer.completion_start_mark)
        self.buffer.delete_mark(self.buffer.completion_end_mark)
        self.buffer.completion_start_mark = None
        self.buffer.completion_end_mark = None

        # 5. Trigger a new completion request from the new cursor position.
        self.trigger_completion()
        self._update_status("Completion accepted.", 3000) # NEW

    def _set_display_mode_raw(self):
        """Sets the display mode to Raw (editing Text View)."""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Display mode set to: Raw")
        # Ensure textview is removed from its current parent (if any) and added to raw_view_scrolled_window
        # Ensure textview is removed from its current parent (if any)
        if self.textview.get_parent():
            self.textview.get_parent().remove(self.textview)

        # Ensure the target scrolled window is empty before adding
        if self.raw_view_scrolled_window.get_child():
            self.raw_view_scrolled_window.remove(self.raw_view_scrolled_window.get_child())

        self.raw_view_scrolled_window.add(self.textview)
        self.raw_view_scrolled_window.show_all()  # Ensure the new content is visible

        self.current_view_mode = "raw"
        self.stack.set_visible_child_name("raw_view")
        self._update_status("Display mode: Raw", 2000) # NEW

    def _set_display_mode_rendered(self):
        """Sets the display mode to Rendered (HTML Web View)."""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Display mode set to: Rendered")
        self._ensure_webview_initialized()  # NEW: Ensure webview is initialized
        # Ensure webview is removed from its current parent (if any) and added to rendered_view_scrolled_window
        # Ensure webview is removed from its current parent (if any)
        if self.webview.get_parent():
            self.webview.get_parent().remove(self.webview)

        # Ensure the target scrolled window is empty before adding
        if self.rendered_view_scrolled_window.get_child():
            self.rendered_view_scrolled_window.remove(self.rendered_view_scrolled_window.get_child())

        self.rendered_view_scrolled_window.add(self.webview)
        self.rendered_view_scrolled_window.show_all()  # Ensure the new content is visible

        self.current_view_mode = "rendered"
        self.stack.set_visible_child_name("rendered_view")
        self._update_rendered_view()  # Update immediately when switching to rendered mode
        self._update_status("Display mode: Rendered", 2000) # NEW

    def _set_display_mode_side_by_side(self):
        """Sets the display mode to Side-by-Side (editing Text View and HTML Web View)."""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Display mode set to: Side-by-Side")
        self._ensure_webview_initialized()  # NEW: Ensure webview is initialized
        # Ensure textview is removed from its current parent and added to side_by_side_textview_scrolled_window
        # Ensure textview is removed from its current parent (if any)
        if self.textview.get_parent():
            self.textview.get_parent().remove(self.textview)
        # Ensure webview is removed from its current parent (if any)
        if self.webview.get_parent():
            self.webview.get_parent().remove(self.webview)

        # Ensure the specific scrolled windows within the paned are empty before adding
        if self.side_by_side_textview_scrolled_window.get_child():
            self.side_by_side_textview_scrolled_window.remove(self.side_by_side_textview_scrolled_window.get_child())
        if self.side_by_side_webview_scrolled_window.get_child():
            self.side_by_side_webview_scrolled_window.remove(self.side_by_side_webview_scrolled_window.get_child())

        self.side_by_side_textview_scrolled_window.add(self.textview)
        self.side_by_side_webview_scrolled_window.add(self.webview)

        self.side_by_side_paned.show_all()  # Ensure the new content is visible

        self.current_view_mode = "side-by-side"
        self.stack.set_visible_child_name("side_by_side_view")
        self._update_rendered_view()  # Update immediately when switching to side-by-side mode
        self._update_status("Display mode: Side-by-Side", 2000) # NEW

    def _trigger_retriever_update(self):
        self.retriever_update_timeout_id = None

        if self.rag_enabled and self.current_file_path:
            # Get the content of the document, EXCLUDING any active completion preview.
            buffer_for_rag = self.buffer
            start_iter = buffer_for_rag.get_start_iter()
            end_iter = buffer_for_rag.get_end_iter()

            # Use the entire buffer content for the update
            current_text = buffer_for_rag.get_text(start_iter, end_iter, False)

            doc_uid = self.current_file_path
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [RAG] Triggering RAG index update for '{doc_uid}' (text length: {len(current_text)})...")
            # The update runs in a background thread to not block the UI
            threading.Thread(target=lambda: self.document_retriever.add_or_update_document(doc_uid, current_text),
                             daemon=True).start()
            self._update_status("Updating RAG index...", 3000) # NEW
        elif not self.rag_enabled:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [RAG] RAG is disabled. Not triggering document update.")
            self._update_status("RAG is disabled. Not updating index.", 3000) # NEW

        return GLib.SOURCE_REMOVE

    def _create_new_document_entry(self, file_path=None, initial_content="", activate=True, parent_iter=None):
        """
        Creates a new document entry (buffer, list item) and adds it to open_documents.
        If file_path is None, it creates a new unsaved document.
        Returns the unique ID/file_path of the newly created document.
        """
        if file_path is None:
            # Generate a unique ID for unsaved documents
            doc_uid = f"unsaved-{self.document_uid_counter}"
            self.document_uid_counter += 1
            display_name = f"New Document {self.document_uid_counter}"
            is_unsaved = True
        else:
            doc_uid = file_path
            display_name = os.path.basename(file_path)
            is_unsaved = False  # Will be set to True if content is changed

        # If document already exists, just switch to it (don't duplicate the entry)
        if doc_uid in self.open_documents:
            if activate:
                self._switch_to_file(doc_uid)
            return doc_uid

        # MODIFIED: Pass the verbose_logging_enabled state to the new buffer
        new_buffer = UndoRedoTextBuffer(verbose=self.verbose_logging_enabled)

        # Connect buffer's changed signal to our general handler
        # This connection is for *all* buffers, allowing us to manage them
        new_buffer.connect("changed", self.on_text_changed)

        # Set initial content, suppressing history for initial load
        if initial_content:
            new_buffer._ignore_history = True
            try:
                new_buffer.set_text(initial_content)
            finally:
                new_buffer._ignore_history = False

        # Add to TreeStore: [display_name, file_path_or_uid, is_directory, is_unsaved, buffer_ref]
        tree_iter = self.tree_store.append(parent_iter, [display_name, doc_uid, False, is_unsaved, new_buffer])
        self.file_tree_view.expand_all()  # Expand all nodes by default for now

        self.open_documents[doc_uid] = {
            'buffer': new_buffer,
            'tree_iter': tree_iter,  # Store the Gtk.TreeIter for direct access
            'parent_iter': parent_iter,  # Store parent for easier updates if path changes
            'is_unsaved': is_unsaved,
            # Set initial_load_done to False if it's a file path and content is empty (lazy loading)
            'initial_load_done': bool(initial_content) or not bool(file_path)
        }

        # MODIFIED: Thread the initial RAG indexing for non-empty documents
        if initial_content and self.rag_enabled:
            # Pass a copy of initial_content and doc_uid to the thread to avoid race conditions
            # with future buffer changes, ensuring the initial content is what's indexed.
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [RAG] Starting initial RAG indexing for '{doc_uid}' in background...")
            threading.Thread(
                target=lambda doc=doc_uid, content=initial_content: self.document_retriever.add_or_update_document(doc,
                                                                                                                   content),
                daemon=True).start()
            self._update_status(f"Indexing '{display_name}' for RAG...", 3000) # NEW
        elif initial_content and not self.rag_enabled:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [RAG] RAG is disabled. Not performing initial indexing for '{doc_uid}'.")
            self._update_status(f"RAG disabled. Not indexing '{display_name}'.", 3000) # NEW

        self._update_tree_view_row_label(doc_uid)  # Apply initial unsaved indicator

        if activate:
            self._switch_to_file(doc_uid)

        return doc_uid

    def _update_tree_view_row_label(self, doc_uid):
        """Updates the label (display name) in the TreeStore for a given document UID."""
        doc_data = self.open_documents.get(doc_uid)
        if doc_data:
            display_name = os.path.basename(doc_uid) if doc_uid.startswith("/") else doc_uid.replace("unsaved-",
                                                                                                     "New Document ")
            if doc_data['is_unsaved']:
                self.tree_store.set(doc_data['tree_iter'], 0, f"{display_name} *")
            else:
                self.tree_store.set(doc_data['tree_iter'], 0, display_name)

    def _new_document(self):
        """Action handler for creating a new, empty document."""
        # Check if the currently active document needs saving before creating a new one
        if self.current_file_path and self.open_documents.get(self.current_file_path, {}).get('is_unsaved', False):
            if self._prompt_to_save_document(self.current_file_path):
                # User cancelled saving the current document, so abort creating a new one
                return

        # Check if there's an existing 'New Document' that's empty and unsaved
        # (This check should come AFTER the prompt for the *current* document)
        for uid, doc_data in self.open_documents.items():
            if uid.startswith("unsaved-") and \
                    self.tree_store.get_value(doc_data['tree_iter'], 3) and \
                    self.tree_store.get_value(doc_data['tree_iter'], 4).get_char_count() == 0:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Switching to existing empty new document: {uid}")
                self._switch_to_file(uid)
                self._update_status(f"Switched to existing new document: {uid.replace('unsaved-', 'New Document ')}", 3000) # NEW
                return

        new_doc_uid = self._create_new_document_entry(initial_content="", activate=True)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Created new document: {new_doc_uid}")
        self._update_status(f"Created new document: {new_doc_uid.replace('unsaved-', 'New Document ')}", 3000) # NEW

    def _switch_to_file(self, file_path_or_uid):
        """Switches the main editor view to the specified file/document."""
        if file_path_or_uid == self.current_file_path:
            return  # Already active

        doc_data = self.open_documents.get(file_path_or_uid)
        if not doc_data:
            print(f"Error: Document {file_path_or_uid} not found in open_documents.")
            self._update_status(f"Error: Document '{file_path_or_uid}' not found.", 5000) # NEW
            return

        # NEW: Immediately cancel any ongoing completion when switching documents
        self._cancel_pending_completion_request()

        # This will internally disconnect the 'changed' signal from the old buffer
        # and connect it to the new one, as it's the TextView's buffer property changing.
        self.textview.set_buffer(doc_data['buffer'])
        self.buffer = doc_data['buffer']  # Update our reference to the active buffer

        # Update current_file_path and window title
        self.current_file_path = file_path_or_uid
        display_name = os.path.basename(file_path_or_uid) if file_path_or_uid.startswith(
            "/") else file_path_or_uid.replace("unsaved-", "New Document ")
        self.set_title(f"mymarkdown - {display_name}")

        # Update DocumentRetriever reference
        # Visually select the row in the tree view
        selection = self.file_tree_view.get_selection()
        selection.select_iter(doc_data['tree_iter'])

        # Trigger rendered view and retriever update
        # Ensure rendered view updates if active
        if self.current_view_mode in ["rendered", "side-by-side"]:
            self._update_rendered_view()

        # Trigger an initial document retriever update if content hasn't been loaded/processed yet
        # This ensures 'vectorize once on load' per file.
        if not doc_data['initial_load_done']:
            # If it's a real file path and content hasn't been loaded, load it now
            if file_path_or_uid.startswith("/") and doc_data['buffer'].get_char_count() == 0:
                try:
                    with open(file_path_or_uid, 'r', encoding='utf-8') as f:
                        content = f.read()
                    doc_data['buffer']._ignore_history = True
                    try:
                        doc_data['buffer'].set_text(content)
                    finally:
                        doc_data['buffer']._ignore_history = False
                    print(f"Lazily loaded content for: {file_path_or_uid}")
                    self._update_status(f"Loaded '{display_name}'.", 3000) # NEW
                except FileNotFoundError:
                    print(f"Error: Lazily loaded file not found: {file_path_or_uid}. Clearing buffer.")
                    doc_data['buffer'].set_text("")
                    self._update_status(f"Error: File '{display_name}' not found.", 5000) # NEW
                except Exception as e:
                    print(f"Error reading lazily loaded file {file_path_or_uid}: {e}. Clearing buffer.")
                    doc_data['buffer'].set_text("")
                    self._update_status(f"Error loading '{display_name}': {e}", 5000) # NEW

            doc_data['initial_load_done'] = True  # Mark as fully loaded/processed now
            self._trigger_retriever_update()  # Update RAG index after lazy load

        # After switching buffer, trigger immediate highlight update
        GLib.idle_add(self.buffer.highlight_syntax)

        # NEW: After loading/switching, ensure the correct editor view is displayed
        if self.current_view_mode == "raw":
            self._set_display_mode_raw()
        elif self.current_view_mode == "rendered":
            self._set_display_mode_rendered()
        elif self.current_view_mode == "side-by-side":
            self._set_display_mode_side_by_side()
        # No 'else' for 'welcome_view' because switching to a document implies we want to see an editor.

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Switched to document: {file_path_or_uid}")
        self._update_status(f"Switched to document: {display_name}", 3000) # NEW

    def _on_file_tree_selection_changed(self, selection):
        """Called when the selection in the file tree view changes."""
        model, tree_iter = selection.get_selected()
        if tree_iter and model:
            is_directory = model.get_value(tree_iter, 2)  # Column 2 is is_directory
            if not is_directory:
                new_file_path_or_uid = model.get_value(tree_iter, 1)

                # If the user selects the currently active file, do nothing.
                if new_file_path_or_uid == self.current_file_path:
                    return

                # Check if the currently active document needs saving before switching
                if self.current_file_path and self.open_documents.get(self.current_file_path, {}).get('is_unsaved', False):
                    # Store original selection's path to restore it if the user cancels
                    original_selection_path = self.tree_store.get_path(self.open_documents[self.current_file_path]['tree_iter'])

                    if self._prompt_to_save_document(self.current_file_path):
                        # User cancelled saving the current document.
                        # Restore the previous selection in the tree view and abort the switch.
                        GLib.idle_add(lambda: selection.select_path(original_selection_path) and False) # False for GLib.idle_add
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Document switch cancelled by user.")
                        self._update_status("Document switch cancelled.", 3000) # NEW
                        return
                
                # If no unsaved changes or changes were saved/discarded, proceed with the switch
                self._switch_to_file(new_file_path_or_uid)
            else:
                # For directories, just expand/collapse
                path = model.get_path(tree_iter)
                if self.file_tree_view.row_expanded(path):
                    self.file_tree_view.collapse_row(path)
                else:
                    self.file_tree_view.expand_row(path, False)

    def _open_file_content(self, file_path):
        """Opens the specified file and loads its content into a new or existing buffer."""
        # Check if the file is already open. If so, just switch to it.
        if file_path in self.open_documents:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] File {file_path} is already open. Switching to it.")
            self._switch_to_file(file_path)
            return

        # Check if the currently active document needs saving before opening a new file
        if self.current_file_path and self.open_documents.get(self.current_file_path, {}).get('is_unsaved', False):
            if self._prompt_to_save_document(self.current_file_path):
                # User cancelled saving the current document, so abort opening the new file
                return

        # When opening a file explicitly via dialog, load its content immediately
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Create a new document entry and switch to it
            self._create_new_document_entry(file_path=file_path, initial_content=content, activate=True)
            
            # After opening, if content was read from a file, it's considered saved
            if self.current_file_path and self.current_file_path == file_path:
                doc_data = self.open_documents[file_path]
                doc_data['is_unsaved'] = False
                self._update_tree_view_row_label(file_path) # Update label to remove '*'
                self._update_status(f"Opened file: {os.path.basename(file_path)}", 3000) # NEW

        except FileNotFoundError:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: File not found: {file_path}")
            self._update_status(f"Error: File '{os.path.basename(file_path)}' not found.", 5000) # NEW
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error opening file {file_path}: {e}")
            self._update_status(f"Error opening '{os.path.basename(file_path)}': {e}", 5000) # NEW

    def _save_file_content(self, file_path):
        """Saves the current buffer content to the specified file path."""
        # Ensure we're operating on the currently active buffer
        content = self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # Update document tracking:
            # If we were editing an unsaved document, update its entry to the new file_path
            old_uid = self.current_file_path
            doc_data = self.open_documents.pop(old_uid, None)  # Remove old entry

            if doc_data:
                # Update the display name and remove '*'
                doc_data['is_unsaved'] = False
                self.tree_store.set(doc_data['tree_iter'], 0, os.path.basename(file_path))  # Update display name
                self.tree_store.set(doc_data['tree_iter'], 1, file_path)  # Update full path/uid
                self.tree_store.set(doc_data['tree_iter'], 3, False)  # Update is_unsaved status
                # Now add it back with the new file_path as the key
                self.open_documents[file_path] = doc_data
                self.current_file_path = file_path  # Update active path

            print(f"File saved: {file_path}")
            self.set_title(f"mymarkdown - {os.path.basename(file_path)}")
            self._update_status(f"File saved: {os.path.basename(file_path)}", 3000) # NEW

        except Exception as e:
            print(f"Error saving file {file_path}: {e}")
            self._update_status(f"Error saving '{os.path.basename(file_path)}': {e}", 5000) # NEW

    def _save_file_direct(self):
        """Saves the file directly if a path is known, otherwise calls save-as."""
        if self.current_file_path and self.current_file_path.startswith('/'):  # Check if it's a real file path
            self._save_file_content(self.current_file_path)
        else:  # It's an unsaved document or no document active
            self._save_file_as()

    def _save_file_as(self):
        """Opens a file chooser dialog to save the current content to a new file."""
        dialog = Gtk.FileChooserDialog(
            title="Save File As",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        dialog.set_do_overwrite_confirmation(True)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            file_path = dialog.get_filename()
            if file_path:
                self._save_file_content(file_path)
                self._update_status(f"File saved as: {os.path.basename(file_path)}", 3000) # NEW
        else: # NEW: If user cancels Save As dialog
            self._update_status("Save As cancelled.", 3000)

        dialog.destroy()

    def _open_file_dialog(self):
        """Opens a file chooser dialog to select and open a markdown file."""
        dialog = Gtk.FileChooserDialog(
            title="Open File",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )

        # Add a filter for Markdown files
        filter_md = Gtk.FileFilter()
        filter_md.set_name("Markdown files (*.md, *.markdown)")
        filter_md.add_pattern("*.md")
        filter_md.add_pattern("*.markdown")
        dialog.add_filter(filter_md)

        # Add a filter for all files
        filter_any = Gtk.FileFilter()
        filter_any.set_name("All files (*)")
        filter_any.add_pattern("*")
        dialog.add_filter(filter_any)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            file_path = dialog.get_filename()
            if file_path:
                self._open_file_content(file_path)
        else: # NEW: If user cancels Open dialog
            self._update_status("Open file cancelled.", 3000)
        dialog.destroy()

    def _run_ollama_completion(self, prefix_text, suffix_text, cancellation_event):  # Modified signature
        api_base = self.app.ollama_api_base
        model = self.app.ollama_model

        if not model:
            print("OLLAMA_MODEL environment variable not set. Cannot perform AI completion.")
            GLib.idle_add(self._update_status, "OLLAMA_MODEL not set. Completion disabled.", 5000) # NEW
            return

        effective_system_prompt = mymarkdown_system_prompt

        # Add retrieved context if RAG is enabled
        retrieved_context = ""
        if self.rag_enabled and self.document_retriever:
            # Retrieve relevant lines based on the current prefix_text from all documents
            retrieved_context = self.document_retriever.retrieve_relevant_lines(prefix_text,
                                                                                current_doc_uid=self.current_file_path)
            if retrieved_context:
                effective_system_prompt = retrieved_context + "\n\n" + effective_system_prompt

        if suffix_text:
            # Include suffix_text in the system prompt to give the LLM full document context
            # Place this *after* RAG context to keep the RAG context at the top.
            effective_system_prompt += f"\n\nNote: The document continues with: '{suffix_text}'"

        if self.verbose_logging_enabled:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] \n--- LLM Request Details START ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Model: {model}")
            if retrieved_context:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Retrieved Context START ---")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {retrieved_context}")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Retrieved Context END ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- System Prompt START ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {effective_system_prompt}")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- System Prompt END ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- User Prompt (Prefix) START ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] '{prefix_text}'")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- User Prompt (Prefix) END ---")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Suffix Text: '{suffix_text}'")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- LLM Request Details END ---\n")

        url = f"{api_base}/api/generate"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": model,
            "system": effective_system_prompt,
            "prompt": prefix_text,  # LLM will complete from this point
            "options": {"stop": ["\n\n", "```"]},  # Use common markdown stop sequences
            "stream": True,
            "think": False
        }

        full_completion_text = ""  # To accumulate the entire response for logging
        initial_completion_received = False

        try:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Requesting streaming completion for model: {model} from {url}...")
            # Use stream=True and iterate over response.iter_lines()
            with requests.post(url, headers=headers, json=payload, stream=True, timeout=30,verify="/etc/ssl/certs/ca-certificates.crt") as response:
                response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)

                for line in response.iter_lines():
                    if cancellation_event.is_set():
                        print(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Streaming cancelled by user input.")
                        break  # Exit the loop if cancelled

                    if line:
                        try:
                            json_data = json.loads(line.decode('utf-8'))
                            completion_chunk = json_data.get("response", "")

                            # MODIFIED LOGIC for chunk processing
                            if completion_chunk:
                                if not initial_completion_received:
                                    # Handle last_line echo: If LLM repeats the last line of the user's input (especially for markdown formatting), remove it.
                                    lines = prefix_text.split('\n\n')
                                    last_line_before_cursor = lines[-1] if lines else ""
                                    if completion_chunk.startswith(last_line_before_cursor):
                                        completion_chunk = completion_chunk[len(last_line_before_cursor):]

                                    # Intelligent leading space handling: Remove leading space only if prefix ends with alphanumeric and completion starts with space + alphanumeric.
                                    if prefix_text and prefix_text[-1].isalnum() and \
                                            completion_chunk.startswith(' ') and len(completion_chunk) > 1 and \
                                            completion_chunk[1].isalnum():
                                        completion_chunk = completion_chunk.lstrip()  # Remove ONLY leading space for this specific case

                                    # Removed completion_chunk.rstrip() as per audit.md

                                    initial_completion_received = True

                                full_completion_text += completion_chunk  # Accumulate for full logging

                                # Schedule the UI update on the main GTK thread
                                GLib.idle_add(self._append_completion_chunk_to_ui, completion_chunk, cancellation_event)

                            if json_data.get("done"):
                                if self.verbose_logging_enabled:
                                    print(
                                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Stream finished.")
                                break  # End of stream
                        except json.JSONDecodeError as e:
                            print(
                                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] JSON decoding error: {e}, Line: {line.decode('utf-8')}")
                            break  # Break on invalid JSON
                        except Exception as e:
                            print(
                                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Error processing chunk: {e}")
                            break  # Break on other errors

        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error during Ollama API call: {e}")
            GLib.idle_add(self._update_status, f"Ollama API error: {e}", 5000) # NEW
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] An unexpected error occurred in streaming: {e}")
            GLib.idle_add(self._update_status, f"Completion failed: {e}", 5000) # NEW
        finally:
            if self.verbose_logging_enabled:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- LLM Full Response (Accumulated) START ---")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] '{full_completion_text}'")
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- LLM Full Response (Accumulated) END ---")

            # NEW: Always reset the active flag on the UI thread when the thread finishes
            GLib.idle_add(lambda: setattr(self, 'active_completion_thread_running', False))
            # If the completion wasn't cancelled or accepted, show 'finished'
            # Note: _update_status is safe to call from any thread via GLib.idle_add
            if not cancellation_event.is_set() and not self.buffer.completion_start_mark: # if not explicitly cancelled and no preview left
                GLib.idle_add(self._update_status, "Completion finished.", 3000) # NEW

    # MODIFIED: Renamed and refactored for streaming chunks
    def _append_completion_chunk_to_ui(self, completion_chunk, cancellation_event):
        """Appends a new chunk of completion text to the buffer and updates the preview marks/tag."""
        # NEW: Final check for cancellation before displaying on UI thread
        if cancellation_event.is_set():
            print("[COMPLETION] Display cancelled by user input during UI scheduling of chunk.")
            self._cleanup_completion_preview_safe()  # Ensure marks are fully cleared
            return False

        if not completion_chunk:
            return False

        if not self.buffer:
            return False
        self.buffer._ignore_history = True
        try:
            if not self.buffer.completion_start_mark:
                # This is the first chunk: Identify and temporarily remove text to the right of cursor
                initial_cursor_iter = self.buffer.get_iter_at_mark(self.buffer.get_insert())
                
                # Get text from cursor to end of line
                end_of_line_iter = initial_cursor_iter.copy()
                end_of_line_iter.forward_to_line_end()
                
                # Store original suffix and delete it
                self.buffer.original_suffix_for_preview = self.buffer.get_text(initial_cursor_iter, end_of_line_iter, False)
                if self.buffer.original_suffix_for_preview:
                    if self.verbose_logging_enabled:
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Stored and deleting original suffix for preview: '{self.buffer.original_suffix_for_preview}'")
                    self.buffer.delete(initial_cursor_iter, end_of_line_iter)
                else:
                    if self.verbose_logging_enabled:
                        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] No suffix to store for preview.")

                # Insert the first chunk of the completion
                self.buffer.insert(initial_cursor_iter, completion_chunk)
                
                # Create marks for the new preview text
                self.buffer.completion_start_mark = self.buffer.create_mark("completion_start", initial_cursor_iter, True)
                end_of_completion_iter = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark).copy()
                end_of_completion_iter.forward_chars(len(completion_chunk))
                self.buffer.completion_end_mark = self.buffer.create_mark("completion_end", end_of_completion_iter, False)
                
                # Move cursor back to the start of the preview so user can type over it
                self.buffer.place_cursor(self.buffer.get_iter_at_mark(self.buffer.completion_start_mark))
                self._update_status("Completion preview: typing to accept or Esc/any key to dismiss", 0) # NEW: Update status for new preview
            else:
                # Subsequent chunk: get current end of preview, insert chunk there, and move end mark
                end_iter_of_current_preview = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)
                self.buffer.insert(end_iter_of_current_preview, completion_chunk)
                # Update the end mark's position
                new_end_of_completion_iter = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark).copy()
                new_end_of_completion_iter.forward_chars(len(completion_chunk))
                self.buffer.move_mark(self.buffer.completion_end_mark, new_end_of_completion_iter)

            # Always reapply the tag to the *entire* current preview range, which might have grown
            start_iter_for_tag = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
            end_iter_for_tag = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)
            self.buffer.apply_tag(self.buffer.completion_tag, start_iter_for_tag, end_iter_for_tag)

        finally:
            self.buffer._ignore_history = False
            # Schedule scroll to end of completion on UI thread
            GLib.idle_add(self._scroll_to_completion_end)

        return False  # Required for GLib.idle_add

    # NEW METHOD: _insert_completion_preview
    def _insert_completion_preview(self, text_to_insert):
        """
        Inserts a given text as a completion preview at the current cursor position.
        This is used for recalling previous completions.
        """
        if not text_to_insert:
            return False

        # Ensure any existing preview is cleared first
        self._cleanup_completion_preview_safe()

        self.buffer._ignore_history = True
        try:
            # Get current cursor position
            initial_cursor_iter = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            
            # Get text from cursor to end of line, store, and delete it
            end_of_line_iter = initial_cursor_iter.copy()
            end_of_line_iter.forward_to_line_end()
            
            self.buffer.original_suffix_for_preview = self.buffer.get_text(initial_cursor_iter, end_of_line_iter, False)
            if self.buffer.original_suffix_for_preview:
                if self.verbose_logging_enabled:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Stored and deleting original suffix for recalled preview: '{self.buffer.original_suffix_for_preview}'")
                self.buffer.delete(initial_cursor_iter, end_of_line_iter)
            else:
                if self.verbose_logging_enabled:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] No suffix to store for recalled preview.")

            # Insert the text
            self.buffer.insert(initial_cursor_iter, text_to_insert)

            # Create new start and end marks for this preview
            self.buffer.completion_start_mark = self.buffer.create_mark("completion_start", initial_cursor_iter, True)
            end_of_completion_iter = initial_cursor_iter.copy()
            end_of_completion_iter.forward_chars(len(text_to_insert))
            self.buffer.completion_end_mark = self.buffer.create_mark("completion_end", end_of_completion_iter, False)

            # Apply the completion tag
            start_iter_for_tag = self.buffer.get_iter_at_mark(self.buffer.completion_start_mark)
            end_iter_for_tag = self.buffer.get_iter_at_mark(self.buffer.completion_end_mark)
            self.buffer.apply_tag(self.buffer.completion_tag, start_iter_for_tag, end_iter_for_tag)

            # Move cursor back to the start of the preview so user can type over it
            self.buffer.place_cursor(self.buffer.get_iter_at_mark(self.buffer.completion_start_mark))

            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Inserted recalled completion preview.")
            self._update_status("Recalled last completion. Tab/Ctrl+I to accept.", 0) # NEW

        finally:
            self.buffer._ignore_history = False
            # Schedule scroll to end of completion on UI thread
            GLib.idle_add(self._scroll_to_completion_end)
        return False

    # NEW METHOD: _recall_last_completion
    def _recall_last_completion(self):
        """Attempts to re-insert the last stored completion as a preview."""
        if not self.buffer:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] No active buffer to recall completion to.")
            self._update_status("No active document.", 3000) # NEW
            return

        if self.last_recalled_completion_text:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Attempting to recall last completion.")
            # Schedule the insertion on the main GTK thread
            GLib.idle_add(self._insert_completion_preview, self.last_recalled_completion_text)
            self.last_recalled_completion_text = None  # Clear it after recalling to prevent multiple recalls of the same text
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] No last completion to recall.")
            self._update_status("No last completion to recall.", 3000) # NEW

    def _show_welcome_screen(self):
        """Displays the welcome message and ensures no document is active."""
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Displaying welcome screen.")
        self.welcome_label.set_markup(
            "<big><b>Welcome!</b></big>\n\nSelect a file from the sidebar to begin editing, or go to File > New to create a new document.")
        self.current_file_path = None  # Indicate no file is active
        self.textview.set_buffer(UndoRedoTextBuffer())  # Explicitly clear and detach the old buffer
        self.buffer = None  # Clear internal reference to the active buffer
        self.set_title("mymarkdown")  # Reset window title
        self.stack.set_visible_child_name("welcome_view")
        self.file_tree_view.get_selection().unselect_all()  # Ensure nothing is selected in the file tree view
        self._cancel_pending_completion_request()  # Cancel any pending completion
        self._update_status("Welcome to mymarkdown!", 3000) # NEW

    def remove_document_by_uid(self, doc_uid):
        """
        Removes a document from the tracking lists and UI.
        This function cleans up the buffer, tree view entry, and RAG index entry.
        """
        doc_data = self.open_documents.get(doc_uid)
        if not doc_data:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Warning: Attempted to remove non-existent document UID: {doc_uid}")
            return

        # 1. Remove from RAG retriever
        self.document_retriever.remove_document(doc_uid)

        # 2. Remove from TreeView
        if doc_data['tree_iter']:
            self.tree_store.remove(doc_data['tree_iter'])

        # 3. If it's the currently active document, detach its buffer and reset UI state
        if self.current_file_path == doc_uid:
            self.textview.set_buffer(UndoRedoTextBuffer())  # Set to a temporary blank buffer
            self.buffer = None  # Clear internal reference to the active buffer
            self.current_file_path = None  # Clear active file path
            self.set_title("mymarkdown")  # Reset window title
            self._cancel_pending_completion_request()  # Clear any active completion preview
            self._update_status("Document closed.", 3000) # NEW

        # 4. Remove from open_documents dictionary
        del self.open_documents[doc_uid]
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Document '{doc_uid}' removed.")
        return True # Successfully removed

    def _scroll_to_completion_end(self):
        """Scrolls the textview to make the end of the completion preview visible."""
        if self.buffer and self.buffer.completion_end_mark:
            # We want the *end* of the completion to be visible, so align it to the bottom of the viewport
            # within_margin=0.0 means no extra padding around the mark.
            # use_align=True, xalign=0.0 (left), yalign=1.0 (bottom)
            self.textview.scroll_to_mark(self.buffer.completion_end_mark, 0.0, True, 0.0, 1.0)
            if self.verbose_logging_enabled:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Scrolled to end of preview.")
            return GLib.SOURCE_REMOVE
        elif self.verbose_logging_enabled:
            # This can happen if the completion was cancelled or accepted before the idle_add ran.
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [COMPLETION] Could not scroll, completion_end_mark is None.")
        return GLib.SOURCE_REMOVE

    def _update_cursor_position(self):
        """Moves the cursor to the completion end mark if it exists."""
        # Access mark from the *active* buffer
        if self.buffer.completion_end_mark:  # Was self.completion_end_mark
            self.buffer.place_cursor(self.buffer.get_iter_at_mark(self.buffer.completion_end_mark))
        return False

    def _apply_font_size(self, textview, font_size_pt):
        """Applies the given font size to the textview."""
        font_desc = Pango.FontDescription(None)  # Create a new description
        font_desc.set_size(int(font_size_pt * Pango.SCALE))  # Set size in Pango units
        # Using override_font for GTK3, as modify_font is deprecated and can be problematic.
        textview.override_font(font_desc)  # Apply the font description
        self._update_status(f"Font size: {font_size_pt}pt", 1000) # NEW

    def on_scroll_event(self, widget, event):
        """Handle scroll events for text resizing (Ctrl + Mousewheel)."""
        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Scroll event detected. Raw event state: {event.state}. Direction: {event.direction}. Delta Y: {event.delta_y}. Current font size: {self.current_font_size_pt}pt")

        if (event.state & Gdk.ModifierType.CONTROL_MASK):
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Control key detected in scroll event state.")
            # Determine scroll direction and adjust font size
            if event.direction == Gdk.ScrollDirection.UP:
                self.current_font_size_pt += 1
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Scrolled UP. New font size will be: {self.current_font_size_pt}pt")
            elif event.direction == Gdk.ScrollDirection.DOWN:
                self.current_font_size_pt = max(1, self.current_font_size_pt - 1)  # Prevent size from going below 1
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Scrolled DOWN. New font size will be: {self.current_font_size_pt}pt")
            elif event.direction == Gdk.ScrollDirection.SMOOTH:
                # For smooth scrolling, use event.delta_y
                # A positive delta_y typically means scrolling down (towards bottom of content)
                # A negative delta_y typically means scrolling up (towards top of content)
                if event.delta_y < 0:  # Scrolled up
                    self.current_font_size_pt += 1
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Scrolled UP (smooth, delta_y={event.delta_y}). New font size will be: {self.current_font_size_pt}pt")
                elif event.delta_y > 0:  # Scrolled down
                    self.current_font_size_pt = max(1, self.current_font_size_pt - 1)
                    print(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG on_scroll_event] Scrolled DOWN (smooth, delta_y={event.delta_y}). New font size will be: {self.current_font_size_pt}pt")
                # If delta_y is 0, no significant vertical scroll, do nothing.

            self._apply_font_size(self.textview, self.current_font_size_pt)  # Apply the new font size
            return True  # Event handled, stop propagation
        return False  # Event not handled
