# MyMarkdown
A simple markdown editor with ollama completions.
In this document you will find detailed information about using MyMarkdown. Including, real-time suggestions, exporting capabilities, and customization options.

# Real-time Suggestions
When you stop typing, the editor will instantly begin completions based on the context. Press `Tab` to accept, or `ctrl+Tab` to regenerate.
# Features
  - real-time auto-complete using ollama
  - markdown-first editor (not for other syntax)- intuitive interface for creating markdown documents.
# Installation
It is a single python file, so it can be placed in any directory in your PATH.
# Dependencies
  - python 3.8 or higher
  - ollama
  - sudo pacman -S python-gobject gtk3 gobject-introspection gtksourceview3
  - uv venv
  - uv pip install -r requirements.txt
# Getting Started
To begin using MyMarkdown, simply open the editor and start typing! The editor will leverage ollama to provide real-time suggestions as you write.

## Basic Usage
The editor is designed to be intuitive, focusing solely on markdown syntax.  You can easily create headers, lists, and formatted text.

## Troubleshooting
If you encounter any issues, please consult the GitHub repository for the latest updates and known problems.

 ## Common Problems
*   **Ollama Not Found:** Ensure OLLAMA_HOST and OLLAMA_MODEL are set correctly.
*   **Suggestions Not Appearing:** Verify that your ollama instance is running.
*   **Slow Suggestions:** This can be caused by network latency or a large ollama model.

 # Advanced Usage
Explore the editor's advanced features, such as creating tables and code blocks.

## Keyboard Shortcuts
Utilize the following keyboard shortcuts for a more efficient workflow.
    * Ctrl/Cmd + S: Save
    * Ctrl/Cmd + C: Copy
    * Ctrl/Cmd + V: Paste
    * Ctrl/Cmd + Z: Undo
    * Ctrl/Cmd + Y: Redo

## Customization
The editor can be customized through settings, allowing you to adjust the font size, theme, and other preferences.

## Exporting
You can export your markdown documents in various formats, including HTML, PDF, and DOCX.
