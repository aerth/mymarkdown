# About mymarkdown

`mymarkdown` is a modern Markdown editor for Linux X11 environments, leveraging Python and GTK.

## Features

*   **Markdown Editing:** Efficient Markdown authoring and editing capabilities.
*   **AI Assistance:** AI-powered writing assistance, utilizing OLLAMA-compatible large language models configured via `OLLAMA_API_BASE` and `OLLAMA_MODEL` environment variables. Each AI mode can be toggled on or off.
    *   **AI Mode 1 (Predictive Completion):** Automatically suggests text completions after a brief pause in typing. These suggestions appear as a subtle, non-distracting preview. Users can insert the current preview into the document using `Ctrl+I`.
*   **Display Modes:** Switch between Raw, Rendered, and Side-by-Side views of your Markdown.
    *   **Rendered Mode:** Displays your Markdown as a dark-mode HTML document.
*   **Multi-File Editing:** Dedicated multi-file editing panel, allowing efficient management and editing of multiple documents within a single session.
*   **File Opening:** Open specific Markdown files directly from the command line, e.g., `mymarkdown file.md`.
*   **Directory Support:** Seamless project file access by launching the editor with a specified directory, e.g., `mymarkdown /path/to/directory`.
