#!/usr/bin/env python3

import os

os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1" 
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
gi.require_version('PangoCairo', '1.0')
gi.require_version('GtkSource', '3.0') # NEW: Require GtkSource
import signal
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, Gtk  # Keep GLib and Gtk for application base

if TYPE_CHECKING:
    from editor import MyMarkdownEditor

class MyMarkdownApp(Gtk.Application):
    def __init__(self):
        # Use NON_UNIQUE flag to allow multiple instances of the application to run,
        # each with its own window.
        Gtk.Application.__init__(self, application_id="arpa.home.MyMarkdown", 
                                 flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)
        # With NON_UNIQUE, each instance will manage its own window.
        # self.window will store the *current* instance's main window.
        self.window: MyMarkdownEditor | None = None
        self._is_shutting_down = False  # Initialize here to ensure it's always set
        self.ollama_api_base = os.getenv("OLLAMA_HOST", "http://localhost:11434") # Default Ollama API base
        self.ollama_model = os.getenv("OLLAMA_MODEL") # Ollama model name, e.g., "llama2"

        # NEW: Centralized mapping for toggle settings
        # Maps action_name -> (window_attribute_name, display_name_for_logging)
        self.toggle_setting_map = {
            "toggle-completion": ("completion_enabled", "AI Completion"),
            "toggle-rag": ("rag_enabled", "AI RAG"),
            "toggle-verbose": ("verbose_logging_enabled", "Verbose LLM/RAG Logging"),
            "toggle-line-numbers": ("line_numbers_enabled", "Line Numbers"), # Future use, for consistency
        }

    # NEW: Generic callback for all boolean toggle actions
    def _on_toggle_setting_activated(self, action, value):
        action_name = action.get_name()
        new_state = value.get_boolean()
        action.set_state(value)

        # Retrieve the attribute name and a human-readable name from the map
        attribute_name, display_name = self.toggle_setting_map.get(action_name, (None, action_name))

        if self.window and attribute_name:
            current_setting_value = getattr(self.window, attribute_name, None) # Get current value, default None
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {display_name} toggle for window: {current_setting_value} -> {new_state}")
            setattr(self.window, attribute_name, new_state)

            # Handle specific side effects based on the setting
            if attribute_name == "completion_enabled" and not new_state:
                self.window._cancel_pending_completion_request()
            elif attribute_name == "rag_enabled" and new_state:
                self.window._trigger_retriever_update()
            elif attribute_name == "rag_enabled" and not new_state:
                if self.window.retriever_update_timeout_id:
                    GLib.source_remove(self.window.retriever_update_timeout_id)
                    self.window.retriever_update_timeout_id = None
            elif attribute_name == "verbose_logging_enabled":
                if self.window.document_retriever:
                    self.window.document_retriever.verbose = new_state
            # NEW: Call specific method for line numbers if it exists
            elif attribute_name == "line_numbers_enabled":
                # The actual implementation of enabling/disabling line numbers on GtkSource.View
                # will be in editor.py (e.g., self.window._set_line_numbers_visibility(new_state))
                # For now, we'll just ensure the attribute is set.
                self.window._set_line_numbers_visibility(new_state)


    def do_activate(self):
        # If no window exists, or the existing one is not yet shown, create it.
        # This prevents creating multiple windows on subsequent 'do_activate' calls
        # if the first one hasn't finished its setup.
        if not self.window:
            # NEW: Lazily import MyMarkdownEditor here
            from editor import MyMarkdownEditor
            self.window = MyMarkdownEditor(self)
        self.window.show_all()

    def do_shutdown(self):
        """
        Executed when the application is shutting down.
        It ensures all unsaved documents are handled (saved or discarded) before
        allowing the application to fully quit.
        Prompts the user to save any unsaved documents before allowing the application to quit.
        """
        if self._is_shutting_down: # NEW: Prevent re-entry to avoid recursion
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Shutdown already in progress. Skipping redundant call.")
            return
        self._is_shutting_down = True # NEW: Set flag to indicate shutdown is in progress

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Application shutting down. Checking for unsaved documents...")
        
        # Get a list of UIDs for unsaved documents. Create a copy as document removal might modify open_documents.
        unsaved_uids_to_process = [] # NEW: Initialize an empty list
        if self.window and self.window.open_documents: # NEW: Only check if window and documents exist
            unsaved_uids_to_process = [uid for uid, data in list(self.window.open_documents.items()) if data['is_unsaved']]

        for doc_uid in unsaved_uids_to_process:
            # Check if the document still exists and is still unsaved (could have been handled by a previous prompt)
            if doc_uid in self.window.open_documents and self.window.open_documents[doc_uid]['is_unsaved']:
                # The _prompt_to_save_document method will display a dialog.
                # It returns True if the user selected "Cancel", preventing the shutdown.
                if self.window._prompt_to_save_document(doc_uid):
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Shutdown cancelled by user for document: {doc_uid}")                    
                    self._is_shutting_down = False # NEW: Reset flag if shutdown is aborted
                    return 
            elif doc_uid in self.window.open_documents and not self.window.open_documents[doc_uid]['is_unsaved']:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Document {doc_uid} was saved/discarded by a previous prompt. Continuing shutdown.")


        # If we reach here, all unsaved documents were either saved or discarded, or there were no unsaved documents.
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] All unsaved documents handled. Proceeding with final application shutdown.")
        Gtk.Application.do_shutdown(self) # MODIFIED: Call base class's do_shutdown directly

    def do_open(self, files, n_files, hint):
        from editor import MyMarkdownEditor
 
        # Ensure the window exists. MyMarkdownEditor.__init__ will create a "New Document 1" by default.
        if not self.window:
            self.window = MyMarkdownEditor(self)
        self.window.show_all()
 
        initial_empty_doc_uid = None
        # Check if the current document is the *initial empty* one (before any user edits)
        if (self.window.current_file_path and
            self.window.current_file_path.startswith("unsaved-") and
            self.window.open_documents.get(self.window.current_file_path) and
            self.window.open_documents[self.window.current_file_path]['buffer'].get_char_count() == 0):
            initial_empty_doc_uid = self.window.current_file_path
 
        files_to_process = [] # Stores (file_path, parent_tree_iter) for markdown files to be opened/added
        processed_file_paths = set() # To prevent duplicate markdown files from being added
 
        for file_uri in files:
            file_path = file_uri.get_path()
            if not file_path or not os.path.exists(file_path):
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Skipping non-existent path: {file_path}")
                continue
 
            if os.path.isdir(file_path):
                # Always find or create the top-level directory node first
                dir_iter = None
                tree_iter_cursor = self.window.tree_store.get_iter_first()
                while tree_iter_cursor:
                    node_path = self.window.tree_store.get_value(tree_iter_cursor, 1)
                    is_dir_node = self.window.tree_store.get_value(tree_iter_cursor, 2)
                    parent_of_node = self.window.tree_store.iter_parent(tree_iter_cursor) 
                    # Ensure it's a root node that matches the path
                    if is_dir_node and node_path == file_path and parent_of_node is None:
                        dir_iter = tree_iter_cursor
                        break
                    tree_iter_cursor = self.window.tree_store.iter_next(tree_iter_cursor)
                
                if dir_iter is None:
                    # Add the directory as a new top-level node
                    dir_display_name = os.path.basename(file_path)
                    dir_iter = self.window.tree_store.append(None, [dir_display_name, file_path, True, False, None])
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Added directory to sidebar: {file_path}")
                
                # Now, find markdown files within this directory and its subdirectories
                if dir_iter: # Ensure we have a valid directory iter before proceeding
                    for root, _, filenames in os.walk(file_path):
                        for filename in filenames:
                            sub_file_path = os.path.join(root, filename)
                            if filename.lower().endswith(('.md', '.markdown')) and os.path.isfile(sub_file_path):
                                if sub_file_path not in processed_file_paths: # Prevent duplicate file entries
                                    files_to_process.append((sub_file_path, dir_iter)) # Associate with the directory node
                                    processed_file_paths.add(sub_file_path)
            elif os.path.isfile(file_path) and file_path.lower().endswith(('.md', '.markdown')):
                if file_path not in processed_file_paths: # Prevent duplicate file entries
                    files_to_process.append((file_path, None)) # No parent for top-level file
                    processed_file_paths.add(file_path)
            else:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Skipping non-markdown file or non-directory: {file_path}")
        
        # --- NEW LOGIC: Prompt for saving the *current* document if applicable ---
        # This occurs before processing the new files from CLI arguments.
        if self.window.current_file_path and self.window.open_documents.get(self.window.current_file_path, {}).get('is_unsaved', False):
            # If the current document is the initial empty one *and* it's actually empty,
            # we want to discard it without prompting, as it's being replaced by CLI files.
            # Otherwise, prompt for saving.
            if self.window.current_file_path == initial_empty_doc_uid and \
               self.window.open_documents[self.window.current_file_path]['buffer'].get_char_count() == 0:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLI args provided, silently discarding initial empty document {initial_empty_doc_uid}.")
                # Use remove_document_by_uid with prompt_on_unsaved=False
                self.window.remove_document_by_uid(initial_empty_doc_uid)
            else:
                # It's an unsaved non-empty document (could be unsaved-X or a modified file).
                # Prompt the user to save it.
                if self.window._prompt_to_save_document(self.window.current_file_path):
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLI open cancelled by user. Aborting file processing.")
                    return # User cancelled, abort the entire do_open operation
        # --- END NEW LOGIC ---

        # Now, process the collected valid markdown paths
        if files_to_process:
            # If the initial empty document was *not* discarded above (e.g., user modified it
            # and then chose to discard it in the prompt, or it was never empty),
            # this check might be redundant but serves as a failsafe to remove the original empty doc
            # if it's still there and new files are loaded.
            if self.window.current_file_path == initial_empty_doc_uid and initial_empty_doc_uid in self.window.open_documents:
                # This line means the initial empty doc somehow survived the previous check (e.g., not current, or not unsaved, etc.)
                # But if we are now processing CLI files, we still want to remove it.
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CLI args provided, removing initial empty document {initial_empty_doc_uid}.")
                self.window.remove_document_by_uid(initial_empty_doc_uid)
 
            # Sort files_to_process by path for consistent order (optional)
            files_to_process.sort(key=lambda x: x[0])

            first_file_path, first_parent_iter = files_to_process[0]
            
            # Open and activate the first valid markdown file from CLI args
            if first_file_path not in self.window.open_documents:
                try:
                    with open(first_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    self.window._create_new_document_entry(
                        file_path=first_file_path, 
                        initial_content=content, 
                        activate=True, 
                        parent_iter=first_parent_iter
                    )
                except Exception as e:
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error opening first CLI file {first_file_path}: {e}")
            else:
                # If the first file is already open, just switch to it.
                self.window._switch_to_file(first_file_path)

            # Add any remaining valid markdown files to the sidebar without activating them
            for file_path, parent_iter in files_to_process[1:]: # Start from the second file
                if file_path not in self.window.open_documents: # Only add if not already open
                    # Do not read file content here for lazy loading
                    self.window._create_new_document_entry(
                        file_path=file_path, 
                        initial_content="", # Lazy load
                        activate=False, 
                        parent_iter=parent_iter
                    )
        else:
            # If no new markdown files were found to activate, and no documents are currently open
            # (e.g., app started with only directories containing no markdown files or all files already open),
            # then show the welcome screen. Otherwise, maintain the current view (e.g., "New Document 1").
            if not self.window.open_documents: 
                self.window._show_welcome_screen()

        # Ensure all added directories are expanded to show their contents immediately
        self.window.file_tree_view.expand_all()

    def do_startup(self):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting up...")
        Gtk.Application.do_startup(self)

        # Create 'new' action
        new_action = Gio.SimpleAction.new("new", None)
        new_action.connect("activate", lambda a, v: self.window._new_document())
        self.add_action(new_action)
        self.set_accels_for_action("app.new", ["<Primary>n"])

        # Create 'save' action
        save_action = Gio.SimpleAction.new("save", None)
        save_action.connect("activate", lambda a, v: self.window._save_file_direct())
        self.add_action(save_action)
        self.set_accels_for_action("app.save", ["<Primary>s"])

        # Create 'save-as' action
        save_as_action = Gio.SimpleAction.new("save-as", None)
        save_as_action.connect("activate", lambda a, v: self.window._save_file_as())
        self.add_action(save_as_action)
        self.set_accels_for_action("app.save-as", ["<Primary><Shift>S"])

        # Create 'open' action
        open_action = Gio.SimpleAction.new("open", None)
        open_action.connect("activate", lambda a, v: self.window._open_file_dialog())
        self.add_action(open_action)
        self.set_accels_for_action("app.open", ["<Primary>o"]) # Add Ctrl+O accelerator

        # Create 'quit' action
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda a, v: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

        # Create 'File' menu
        file_menu = Gio.Menu.new()
        file_menu.append("New", "app.new") # Add New menu item
        file_menu.append("Open...", "app.open")
        file_menu.append("Save", "app.save")
        file_menu.append("Save All", "app.save-all") # Placeholder for future
        file_menu.append("Save As...", "app.save-as")
        file_menu.append("Quit", "app.quit") # Add Quit menu item

        # Create 'View' actions and menu
        raw_mode_action = Gio.SimpleAction.new("view-raw", None)
        raw_mode_action.connect("activate", lambda a, v: self.window._set_display_mode_raw())
        self.add_action(raw_mode_action)

        rendered_mode_action = Gio.SimpleAction.new("view-rendered", None)
        rendered_mode_action.connect("activate", lambda a, v: self.window._set_display_mode_rendered())
        self.add_action(rendered_mode_action)

        side_by_side_mode_action = Gio.SimpleAction.new("view-side-by-side", None)
        side_by_side_mode_action.connect("activate", lambda a, v: self.window._set_display_mode_side_by_side())
        self.add_action(side_by_side_mode_action)

        view_menu = Gio.Menu.new()
        view_menu.append("Raw", "app.view-raw")
        view_menu.append("Rendered", "app.view-rendered")
        view_menu.append("Side-by-Side", "app.view-side-by-side")

        menubar = Gio.Menu.new()
        # Create 'Completion' actions and menu
        regen_completion_action = Gio.SimpleAction.new("regen-completion", None)
        regen_completion_action.connect("activate", lambda a, v: self.window._regenerate_completion())
        self.add_action(regen_completion_action)
        self.set_accels_for_action("app.regen-completion", ["<Primary>Tab"]) # Add Ctrl+Tab accelerator

        # NEW: Create 'Recall Last Completion' action
        recall_completion_action = Gio.SimpleAction.new("recall-completion", None)
        recall_completion_action.connect("activate", lambda a, v: self.window._recall_last_completion())
        self.add_action(recall_completion_action)
        self.set_accels_for_action("app.recall-completion", ["<Primary><Alt>c"]) # Set accelerator: Ctrl+Alt+C

        # Note: boolvtype = GLib.VariantType.new("b") can cause UI rendering issues
        # with checkbox menu items in some GTK/PyGObject environments.
        # Using None as a workaround to allow checkboxes to function.
        boolvtype = None 

        # Create 'Toggle Completion' action
        toggle_completion_action = Gio.SimpleAction.new_stateful("toggle-completion",
                                                                  boolvtype,
                                                                  GLib.Variant.new_boolean(True)) # Default to enabled (checked)
        
        # Connect to the generic class method
        toggle_completion_action.connect("change-state", self._on_toggle_setting_activated)
        self.add_action(toggle_completion_action)
        toggle_completion_action.set_enabled(True) # Ensure it's enabled


        # Create 'Toggle RAG' action
        toggle_rag_action = Gio.SimpleAction.new_stateful("toggle-rag",
                                                           boolvtype, 
                                                           GLib.Variant.new_boolean(False)) # Default to DISABLED (unchecked)
        
        # Connect to the generic class method
        toggle_rag_action.connect("change-state", self._on_toggle_setting_activated)
        self.add_action(toggle_rag_action)
        toggle_rag_action.set_enabled(True) # Ensure it's enabled


        # Create 'Toggle Verbose Logging' action
        toggle_verbose_action = Gio.SimpleAction.new_stateful("toggle-verbose",
                                                               boolvtype,
                                                               GLib.Variant.new_boolean(False)) # Default to disabled (unchecked)
        
        # Connect to the generic class method
        toggle_verbose_action.connect("change-state", self._on_toggle_setting_activated)
        self.add_action(toggle_verbose_action)
        toggle_verbose_action.set_enabled(True) # Ensure it's enabled


        completion_menu = Gio.Menu.new()
        completion_menu.append("Regenerate", "app.regen-completion")
        completion_menu.append("Recall Last", "app.recall-completion") # NEW: Add the new "Recall Last" item
        completion_menu.append("Completion Enabled", "app.toggle-completion") # Add the new "Enabled" item
        completion_menu.append("RAG Enabled", "app.toggle-rag") # Add the new "RAG Enabled" item
        completion_menu.append("Verbose Logging", "app.toggle-verbose") # Add the new "Verbose Logging" item

        menubar.append_submenu("File", file_menu)

        # Create 'Edit' actions and menu
        undo_action = Gio.SimpleAction.new("undo", None)
        # Connect to the custom UndoRedoTextBuffer's undo method
        undo_action.connect("activate", lambda a, v: self.window.buffer.undo())
        self.add_action(undo_action)
        self.set_accels_for_action("app.undo", ["<Primary>z"])

        redo_action = Gio.SimpleAction.new("redo", None)
        # Connect to the custom UndoRedoTextBuffer's redo method
        redo_action.connect("activate", lambda a, v: self.window.buffer.redo())
        self.add_action(redo_action)
        self.set_accels_for_action("app.redo", ["<Primary><Shift>Z"])

        edit_menu = Gio.Menu.new()
        edit_menu.append("Undo", "app.undo")
        edit_menu.append("Redo", "app.redo")
        menubar.append_submenu("Edit", edit_menu)
        menubar.append_submenu("View", view_menu)
        menubar.append_submenu("Completion", completion_menu)

        self.set_menubar(menubar)

def sigint_handler(signum, frame):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] \nSIGINT ({signum}) received. Initiating application quit...")
    app.quit() # This will trigger _on_app_quit_activated

if __name__ == "__main__":
    app = MyMarkdownApp()

    # Handle SIGINT to allow clean exit with Ctrl+C, funneling through app.quit()
    signal.signal(signal.SIGINT, sigint_handler)

    exit_status = app.run(sys.argv) # Explicitly pass sys.argv for argument processing
    sys.exit(exit_status)
