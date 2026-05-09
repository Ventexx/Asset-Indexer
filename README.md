# Asset-Indexer

A desktop application for browsing, searching, and editing structured data paired with visual assets, organized into a fast and intuitive interface, with native support for Windows and Linux.

![Cover Image](ScreenshotCover.png)

---

## Table of Contents

- [Overview](#overview)  
- [Features](#features)  
- [Getting Started](#getting-started)  
  - [Installation](#installation)  
  - [Uninstall](#uninstall)  
- [Usage](#usage)  
- [Contribution Guidelines](#contribution-guidelines)  
- [Contact](#contact)  
- [License](#license)

---

## Overview

Asset Indexer is a desktop application that scans folders of image + JSON pairs and turns them into a searchable, visual data library. It allows you to quickly find, preview, copy, and edit structured data in an organized way.
It features native support for Windows and Linux, with partial Wayland support.

---

## Features

* 🗂️ **Folder-based indexing**: Automatically scans directories for `.png` + `.json` pairs, builds a structured SQLite database, and organises results into collapsible, nested folder sections.

* ⚡ **Smart incremental indexing**: On subsequent loads, a file-cache diff detects only added, changed, or deleted files so unchanged assets are skipped entirely — making reloads near-instant for large libraries.

* 🔍 **Instant search with folder-only mode**: Filter assets by name across all indexed content. Append ` f` to a query (e.g. `characters f`) to search folder names instead of asset names. Scroll position and expanded folders are restored automatically when the search is cleared.

* 🖼️ **Thumbnail grid with lazy loading**: Assets are displayed in a responsive grid of rounded thumbnail cards. Images load in the background off the main thread so the UI never freezes. Thumbnails are cached in memory after the first load.

* 🔎 **Full-screen image viewer**: Left-click any card to open a full-screen overlay viewer. Navigate between images in the same folder using arrow keys or the on-screen Prev / Next buttons. Right-click the image to access the card's context menu directly from the viewer. Click outside the image or press Escape to close.

* 🖱️ **Drag & drop**: Drag any card out of the app to copy the image file path to another application as a file drop.

* 📋 **Per-field copy menu**: Right-click a card to copy any JSON field value individually. A `LORA` badge is shown for assets flagged with `"lora": true`. The `lora` key itself is excluded from copy actions.

* 🏷️ **Quick tag editing**: Right-click a card and choose "Add Tag" to append a tag to the asset's `tags` JSON field without opening the full editor.

* ✏️ **Inline JSON editor**: Open a floating, draggable editor to view and modify the full JSON file linked to any card. Changes are saved back to disk and reflected immediately in the UI.

* 🧩 **Folder metadata tags (`!F-<folder>.json`)**: Right-click a folder header to add a tag or open the JSON editor for the folder's meta file. When a folder has a meta file, a **Copy** button appears next to the folder header for one-click copying of its stored value (e.g. a LoRA snippet).

* 🗒️ **Notes window**: A separate floating, non-modal window for storing reusable text snippets (e.g. prompts, LoRA weights, settings). Notes are organised into optional categories displayed as collapsible folder sections. Click any note card to copy its value to the clipboard. Supports search, inline JSON editing, and an optional A-Z sort.

* 🗃️ **Multiple databases**: Add and manage multiple indexed folders. Switch between them via the Change Database dialog. Removing a database deletes only the index file — your original asset folder is never touched.

* ⚡ **Startup scripts**: Register Python scripts that run automatically on app launch (or on "Reload with Scripts"). Scripts can be given custom arguments and reordered via up/down buttons. All script execution happens in a background thread with a visible progress indicator.

* 🧪 **Dev mode**: Launch with `--dev` to use a safe in-memory dataset. No files are read or written; all changes vanish on exit. The real database and preferences are left completely untouched.

* ⌨️ **Keyboard shortcuts**: Press `/` from anywhere in the app to instantly focus the search bar and clear it. Use Left / Right arrow keys to navigate in the image viewer.

---

## Getting Started

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Ventexx/Asset-Indexer.git
   cd Asset-Indexer
   ```

2. Start the app using the provided script (recommended):

   - **Windows**:
     ```bash
     start.bat
     ```

   - **Linux / macOS**:
     ```bash
     chmod +x start.sh
     ./start.sh
     ```

   This will automatically:
   - Create a virtual environment  
   - Install all dependencies  
   - Launch the application  

---

#### Manual setup (optional)

If you prefer to set things up manually:

1. Install dependencies:
   ```bash
   pip install PySide6
   ```

2. Run the application:
   ```bash
   python app.py
   ```


---

### Uninstall

To fully remove the application:

1. Delete the project folder.

2. Remove application data stored on your system:
   - **Linux / macOS**:  
     ~/.asset_indexer/
   - **Windows**:  
     C:\Users\<YourUser>\.asset_indexer\

This directory contains:
- Indexed databases (`.db` files)  
- File caches (`*_file_cache.json`)
- Preferences (`prefs.json`)  
- Notes (`notes.json`)
- Startup scripts config (`startup_scripts.json`)  

---

## Usage

### 1. Add a folder
Open the app, click **≡ → Add Folder**, and select a directory containing `.png` images with matching `.json` files. The app indexes the folder automatically and displays all assets grouped by subfolder.

### 2. Browse & search
Use the search bar (or press `/` to jump to it) to filter assets by name. Results update after a short debounce as you type. Clear the search to return to exactly where you were — expanded folders and scroll position are restored.

To search by folder name instead of asset name, end your query with ` f`. For example, typing `characters f` shows everything inside folders whose name contains "characters".

### 3. Explore folder sections
Click a folder header to expand it and reveal its thumbnail grid. Folders nest to reflect your directory structure. Each folder remembers its expanded state across search refreshes.

### 4. View an image full-screen
Left-click any card to open it in the full-screen viewer. Navigate to the previous or next image in the same folder with the **‹ / ›** buttons or the **Left / Right** arrow keys. Press **Escape** or click outside the image to close.

### 5. Copy prompt fields
Right-click a card to see a context menu listing every field in the linked JSON as a "Copy …" action. Click any entry to copy its value to the clipboard.

### 6. Add a quick tag
Right-click a card and choose **Add Tag** to append a value to that asset's `tags` field without opening the full JSON editor.

### 7. Edit JSON inline
Right-click a card and choose **Edit JSON…** to open the floating JSON editor. Edit the content directly and click **Save** to write the changes to disk and update the index.

### 8. Drag images out
Click and drag any card out of the app window to copy the image as a file drop into another application (e.g. a file manager or image editor).

### 9. Use folder tags
Right-click an expanded folder header to either:
- **Add Tag** — creates or updates an `!F-<FolderName>.json` meta file with a new tag, making a **Copy** button appear next to the folder header.
- **Edit JSON…** — opens the full JSON editor for the folder's meta file.

Once a folder has a meta file, click its **Copy** button to instantly copy the stored value (e.g. a LoRA snippet) to the clipboard.

### 10. Use the Notes window
Click the 📋 button in the top-right of the main window to open the Notes panel. Click **+** to create a new note with a name, value, and optional category path (e.g. `Styles/Lighting`). Notes are grouped into collapsible folder sections. Click any note card to copy its value. Right-click a note card to copy or edit its raw JSON.

### 11. Manage databases
Click **≡ → Change Database** to switch between indexed folders or remove one from the list. Removing a database deletes the index file only — your source folder is not affected. Click **≡ → Add Folder** to index a new folder at any time.

### 12. Reload the index
Click **≡ → Reload Database** to re-index the active folder. Choose:
- **without Scripts** — re-indexes immediately (uses the file cache diff to skip unchanged files).
- **with Scripts** — runs your configured startup scripts first, then re-indexes.

### 13. Manage startup scripts
Click **≡ → Startup Scripts** to add, remove, edit, or reorder Python scripts that run on each app launch. Each script can be given a name and optional command-line arguments.

💡 **Tip:**  
To safely test the app without modifying real data, launch it in dev mode:
```bash
python app.py --dev
```

---

## Contribution Guidelines

Your contributions are welcome!

[Conventional Commits](https://www.conventionalcommits.org/)

---

## Contact

* **Maintainer**: Ventexx ([ven.private@outlook.de](mailto:ven.private@outlook.de))

---

## License

This work is licensed under a  
[Creative Commons Attribution-NonCommercial 4.0 International License](LICENSE).

You may use, modify, and share this software for non-commercial purposes, provided that appropriate credit is given.

Disclaimer: This software is provided "as is", without warranty of any kind. The author is not liable for any damages or issues arising from its use.
