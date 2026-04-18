from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ruff: noqa
# Uncomment next 2 lines to force XWayland if Wayland causes issues:
# import os
# os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import QMimeData, QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDrag, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ── Dev Mode ───────────────────────────────────────────────────────────────────
# DEV_MODE is activated ONLY by passing --dev as a command-line argument.
#
# CONTRACT - read this before touching anything below:
#   • DEV_MODE is a READ-ONLY boolean set once at import time from sys.argv.
#   • It is COMPLETELY ISOLATED from all production code paths.
#   • It NEVER writes to prefs, databases, scripts files, or any disk state.
#   • It NEVER overwrites "last_db" so the user's real session is preserved.
#   • The fake data and DevDatabase class defined in the _DEV section below
#     are the ONLY things that change when DEV_MODE is True.
#   • If DEV_MODE is False, none of the dev-mode symbols are ever referenced.
#   • The "dev" database CANNOT be opened through the normal Open Database
#     dialog - it only exists in memory while the flag is active.
#
# To start in dev mode:   python app.py --dev
# Normal start:           python app.py
# ──────────────────────────────────────────────────────────────────────────────
DEV_MODE: bool = "--dev" in sys.argv

APP_NAME = "Asset Indexer"
APP_ORG = "AssetIndexer"
APP_DIR = Path.home() / ".asset_indexer"
ICON_PATH = Path(__file__).parent / "Icon.png"
PREFS_FILE = APP_DIR / "prefs.json"
NOTES_FILE = APP_DIR / "notes.json"

# 2:3 card image area
THUMB_W = 106
THUMB_H = 159
NAME_H = 16
COLS = 6


# ── Prefs ──────────────────────────────────────────────────────────────────────


def _load_prefs() -> dict:
    try:
        if PREFS_FILE.exists():
            return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_prefs(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Startup Scripts ────────────────────────────────────────────────────────────

SCRIPTS_FILE = APP_DIR / "startup_scripts.json"


def _load_scripts() -> list[dict]:
    """Return list of {name, path, args} dicts."""
    try:
        if SCRIPTS_FILE.exists():
            data = json.loads(SCRIPTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_scripts(scripts: list[dict]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPTS_FILE.write_text(json.dumps(scripts, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Pixmap Cache ───────────────────────────────────────────────────────────────

_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(path: str) -> QPixmap:
    if path not in _PIXMAP_CACHE:
        pix = QPixmap(path)
        if not pix.isNull():
            scaled = pix.scaled(
                THUMB_W,
                THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (scaled.width() - THUMB_W) // 2
            y = (scaled.height() - THUMB_H) // 2
            pix = scaled.copy(x, y, THUMB_W, THUMB_H)
        _PIXMAP_CACHE[path] = pix
    return _PIXMAP_CACHE[path]


# ── Index logic (thread-safe, opens its own connection) ───────────────────────


def _run_index(
    db_path: Path,
    folder: Path,
    full_rebuild: bool = False,
    progress_cb=None,  # callable(current, total, msg) or None
) -> tuple[int, int]:
    """
    Opens a *fresh* SQLite connection on the calling thread, indexes `folder`,
    and closes the connection before returning.  Safe to call from any thread.
    Returns (total_assets, changes).
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if full_rebuild:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM folder_meta")
            conn.commit()

        # Ensure folder_meta exists (for DBs created before this feature)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folder_meta (
                folder_key  TEXT    PRIMARY KEY,
                copy_value  TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.commit()

        existing: set[str] = {
            r[0] for r in conn.execute("SELECT image_path FROM assets").fetchall()
        }

        candidates = [
            png
            for png in sorted(folder.rglob("*.png"))
            if png.with_suffix(".json").exists()
        ]
        total = len(candidates)
        found: set[str] = set()
        changes = 0

        for i, png in enumerate(candidates, 1):
            jpath = png.with_suffix(".json")
            try:
                raw = jpath.read_text(encoding="utf-8", errors="ignore")
                json.loads(raw)
            except Exception:
                raw = "{}"

            rel_folder = str(png.parent.relative_to(folder))
            if rel_folder == ".":
                rel_folder = ""

            key = str(png)
            found.add(key)

            if key not in existing:
                conn.execute(
                    "INSERT INTO assets(name, folder, image_path, json_path, json_data)"
                    " VALUES(?,?,?,?,?)",
                    (png.stem, rel_folder, key, str(jpath), raw),
                )
                changes += 1
            else:
                conn.execute(
                    "UPDATE assets SET json_data=?, folder=? WHERE image_path=?",
                    (raw, rel_folder, key),
                )

            if progress_cb:
                progress_cb(i, total, f"Indexing ({i}/{total})")

        stale = existing - found
        for p in stale:
            conn.execute("DELETE FROM assets WHERE image_path=?", (p,))
        changes += len(stale)

        # ── Scan for !F-[FolderName].json files and upsert into folder_meta ──
        # Walk every directory under `folder` (including root) and look for a
        # file matching !F-<dirname>.json (case-insensitive on the stem).
        seen_folder_keys: set[str] = set()
        for dir_path in sorted(folder.rglob("*")):
            if not dir_path.is_dir():
                continue
            expected_stem = f"!F-{dir_path.name}"
            meta_file = dir_path / f"{expected_stem}.json"
            if not meta_file.exists():
                # Try case-insensitive match on Windows-style paths
                matches = [
                    f
                    for f in dir_path.iterdir()
                    if f.suffix.lower() == ".json"
                    and f.stem.lower() == expected_stem.lower()
                ]
                meta_file = matches[0] if matches else None
            if meta_file and meta_file.exists():
                try:
                    raw = meta_file.read_text(encoding="utf-8", errors="ignore")
                    data = json.loads(raw)
                    # Take the first (and for now only) value
                    copy_value = str(next(iter(data.values()))) if data else ""
                except Exception:
                    copy_value = ""
                rel = str(dir_path.relative_to(folder)).replace("\\", "/")
                if rel == ".":
                    rel = ""
                seen_folder_keys.add(rel)
                conn.execute(
                    "INSERT INTO folder_meta(folder_key, copy_value)"
                    " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                    (rel, copy_value),
                )
        # Also check the root folder itself for a matching !F-<rootname>.json
        root_stem = f"!F-{folder.name}"
        root_meta = folder / f"{root_stem}.json"
        if not root_meta.exists():
            matches = [
                f
                for f in folder.iterdir()
                if f.suffix.lower() == ".json" and f.stem.lower() == root_stem.lower()
            ]
            root_meta = matches[0] if matches else None
        if root_meta and root_meta.exists():
            try:
                raw = root_meta.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw)
                copy_value = str(next(iter(data.values()))) if data else ""
            except Exception:
                copy_value = ""
            seen_folder_keys.add("")
            conn.execute(
                "INSERT INTO folder_meta(folder_key, copy_value)"
                " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                ("", copy_value),
            )
        # Remove stale folder_meta rows for folders that no longer have an F-*.json
        existing_fk = {
            r[0] for r in conn.execute("SELECT folder_key FROM folder_meta").fetchall()
        }
        for stale_fk in existing_fk - seen_folder_keys:
            conn.execute("DELETE FROM folder_meta WHERE folder_key=?", (stale_fk,))

        conn.commit()

        db_total: int = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        return db_total, changes
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# ██  DEV-MODE SANDBOX  ████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════════════
#
# Everything between the two ══ banners is EXCLUSIVELY for --dev mode.
# NOTHING in this block is referenced from production code paths.
# NOTHING in this block reads from or writes to disk (no DB, no prefs, no files).
#
# Structure:
#   _DEV_FAKE_ASSETS   - hardcoded list of fake asset dicts (no real images/JSON)
#   _DEV_FOLDER_META   - hardcoded folder copy-tag values
#   DevDatabase        - in-memory stub that mimics the Database API exactly
#                        but operates only on _DEV_FAKE_ASSETS; all mutating
#                        methods (update_json, delete) are no-ops.
# ──────────────────────────────────────────────────────────────────────────────

# Fake assets: three folders, a handful of entries each.
# image_path values are intentionally non-existent - _apply_pixmap will show "?".
# json_data contains realistic example JSON so the context-menu copy actions
# and the Edit JSON dialog both work (they just don't persist anything).
_DEV_FAKE_ASSETS: list[dict] = [
    # ── Folder: Characters ────────────────────────────────────────────────────
    {
        "id": 1,
        "name": "hero_warrior",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/hero_warrior.png",
        "json_path": "/dev/null/Characters/hero_warrior.json",
        "json_data": json.dumps(
            {
                "prompt": "a heroic warrior in gleaming plate armor, cinematic lighting",
                "negative_prompt": "blurry, low quality",
                "model": "stable-diffusion-xl",
                "steps": 30,
                "cfg_scale": 7.5,
            },
            indent=2,
        ),
    },
    {
        "id": 2,
        "name": "rogue_elf",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/rogue_elf.png",
        "json_path": "/dev/null/Characters/rogue_elf.json",
        "json_data": json.dumps(
            {
                "prompt": "nimble elven rogue, emerald cloak, forest background",
                "negative_prompt": "ugly, deformed",
                "model": "stable-diffusion-xl",
                "steps": 25,
                "cfg_scale": 6.0,
            },
            indent=2,
        ),
    },
    {
        "id": 3,
        "name": "dark_mage",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/dark_mage.png",
        "json_path": "/dev/null/Characters/dark_mage.json",
        "json_data": json.dumps(
            {
                "prompt": "dark sorcerer holding a glowing orb, dramatic shadows",
                "negative_prompt": "cartoon, anime",
                "model": "stable-diffusion-xl",
                "steps": 40,
                "cfg_scale": 8.0,
            },
            indent=2,
        ),
    },
    # ── Folder: Landscapes ────────────────────────────────────────────────────
    {
        "id": 4,
        "name": "misty_valley",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/misty_valley.png",
        "json_path": "/dev/null/Landscapes/misty_valley.json",
        "json_data": json.dumps(
            {
                "prompt": "sweeping misty valley at dawn, volumetric fog, epic scale",
                "negative_prompt": "oversaturated, flat",
                "model": "stable-diffusion-xl",
                "steps": 35,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
    {
        "id": 5,
        "name": "crystal_cave",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/crystal_cave.png",
        "json_path": "/dev/null/Landscapes/crystal_cave.json",
        "json_data": json.dumps(
            {
                "prompt": "underground crystal cave, bioluminescent glow, reflections",
                "negative_prompt": "dark, muddy colors",
                "model": "stable-diffusion-xl",
                "steps": 30,
                "cfg_scale": 7.5,
            },
            indent=2,
        ),
    },
    {
        "id": 6,
        "name": "sky_fortress",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/sky_fortress.png",
        "json_path": "/dev/null/Landscapes/sky_fortress.json",
        "json_data": json.dumps(
            {
                "prompt": "floating sky fortress above clouds, golden hour light",
                "negative_prompt": "low detail, blurry",
                "model": "stable-diffusion-xl",
                "steps": 40,
                "cfg_scale": 8.5,
            },
            indent=2,
        ),
    },
    # ── Folder: Items ─────────────────────────────────────────────────────────
    {
        "id": 7,
        "name": "magic_sword",
        "folder": "Items",
        "image_path": "/dev/null/Items/magic_sword.png",
        "json_path": "/dev/null/Items/magic_sword.json",
        "json_data": json.dumps(
            {
                "prompt": "ancient enchanted sword with glowing blue runes, product shot",
                "negative_prompt": "hands, people",
                "model": "stable-diffusion-xl",
                "steps": 28,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
    {
        "id": 8,
        "name": "potion_red",
        "folder": "Items",
        "image_path": "/dev/null/Items/potion_red.png",
        "json_path": "/dev/null/Items/potion_red.json",
        "json_data": json.dumps(
            {
                "prompt": "crimson health potion in ornate glass vial, studio lighting",
                "negative_prompt": "background clutter",
                "model": "stable-diffusion-xl",
                "steps": 25,
                "cfg_scale": 6.5,
            },
            indent=2,
        ),
    },
    {
        "id": 9,
        "name": "ancient_tome",
        "folder": "Items",
        "image_path": "/dev/null/Items/ancient_tome.png",
        "json_path": "/dev/null/Items/ancient_tome.json",
        "json_data": json.dumps(
            {
                "prompt": "weathered spellbook with arcane symbols, candle light",
                "negative_prompt": "modern, clean",
                "model": "stable-diffusion-xl",
                "steps": 32,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
]

# Folder copy-tag values (simulates !F-<Name>.json in each folder)
_DEV_FOLDER_META: dict[str, str] = {
    "Characters": "<lora:char_pack_v2:0.8>",
    "Landscapes": "<lora:landscape_v3:0.9>",
    "Items": "<lora:items_v1:0.7>",
}


class DevDatabase:
    """
    ──────────────────────────────────────────────────────────────────────────
    DEV-ONLY in-memory database stub.  Mirrors the public API of Database so
    the rest of the UI code works without any special-casing inside widgets.

    RULES (do not break these):
      • Never reads from or writes to any file or SQLite database.
      • update_json() only mutates the in-memory list - changes vanish on exit.
      • delete() is a silent no-op (cards appear to stay; a refresh restores).
      • get_folder_meta() returns values from the hardcoded _DEV_FOLDER_META dict.
      • This class MUST NOT be instantiated outside of DEV_MODE code paths.
    ──────────────────────────────────────────────────────────────────────────
    """

    def __init__(self) -> None:
        # Work on a shallow copy so multiple resets don't stack mutations
        self._assets: list[dict] = [dict(a) for a in _DEV_FAKE_ASSETS]
        self.path = Path("/dev/null/dev_mode.db")  # sentinel - never accessed
        self.name = "[DEV MODE]"

    # ── API surface (matches Database) ────────────────────────────────────

    def search(self, query: str, limit: int = 2000) -> list[dict]:
        """Filter fake assets by name, case-insensitive substring match."""
        q = query.lower()
        results = (
            [a for a in self._assets if q in a["name"].lower()]
            if q
            else list(self._assets)
        )
        return results[:limit]

    def get_folder_meta(self, folder_key: str) -> Optional[str]:
        """Return the hardcoded copy-tag for a folder, or None."""
        return _DEV_FOLDER_META.get(folder_key)

    def update_json(self, image_path: str, new_json_text: str) -> None:
        """
        DEV NO-OP: Updates the in-memory asset only.
        The Edit JSON dialog will show a Save confirmation, but nothing is
        written to disk and changes reset the moment the panel refreshes.
        """
        for asset in self._assets:
            if asset["image_path"] == image_path:
                asset["json_data"] = new_json_text
                break  # in-memory only, not persisted

    def delete(self, image_path: str) -> None:
        """DEV NO-OP: silently ignores delete requests."""
        pass  # intentional no-op - dev mode does not mutate state visibly

    def close(self) -> None:
        """DEV NO-OP: nothing to close."""
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ██  END DEV-MODE SANDBOX  ████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════════════


# ── Database ───────────────────────────────────────────────────────────────────


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                folder      TEXT    NOT NULL DEFAULT '',
                image_path  TEXT    NOT NULL UNIQUE,
                json_path   TEXT    NOT NULL,
                json_data   TEXT    NOT NULL DEFAULT '{}'
            )
        """)
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(assets)").fetchall()
        }
        if "folder" not in cols:
            self._conn.execute(
                "ALTER TABLE assets ADD COLUMN folder TEXT NOT NULL DEFAULT ''"
            )
        # Folder-level metadata table (stores F-[Name].json value per folder path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS folder_meta (
                folder_key  TEXT    PRIMARY KEY,
                copy_value  TEXT    NOT NULL DEFAULT ''
            )
        """)
        self._conn.commit()

    def index(self, folder: Path, full_rebuild: bool = False) -> tuple[int, int]:
        """Blocking index on the *calling* thread's connection. Returns (total, changes)."""
        return _run_index(self.path, folder, full_rebuild, progress_cb=None)

    def search(self, query: str, limit: int = 2000) -> list[dict]:
        q = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE name LIKE ? ORDER BY folder, name LIMIT ?",
            (q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_folder_meta(self, folder_key: str) -> Optional[str]:
        """Return the copy_value for a folder if an F-[Name].json was indexed, else None."""
        row = self._conn.execute(
            "SELECT copy_value FROM folder_meta WHERE folder_key=?", (folder_key,)
        ).fetchone()
        return row[0] if row else None

    def update_json(self, image_path: str, new_json_text: str) -> None:
        self._conn.execute(
            "UPDATE assets SET json_data=? WHERE image_path=?",
            (new_json_text, image_path),
        )
        self._conn.commit()

    def delete(self, image_path: str) -> None:
        self._conn.execute("DELETE FROM assets WHERE image_path=?", (image_path,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── Database Manager ───────────────────────────────────────────────────────────


class DatabaseManager:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._roots_file = APP_DIR / "roots.json"
        self._roots: dict[str, Path] = {}
        self._dbs: dict[str, Database] = {}
        self._load()

    def _load(self) -> None:
        if self._roots_file.exists():
            try:
                data = json.loads(self._roots_file.read_text(encoding="utf-8"))
                self._roots = {k: Path(v) for k, v in data.items()}
            except Exception:
                pass

    def _save(self) -> None:
        self._roots_file.write_text(
            json.dumps({k: str(v) for k, v in self._roots.items()}, indent=2),
            encoding="utf-8",
        )

    def names(self) -> list[str]:
        return sorted(self._roots.keys())

    def root_for(self, name: str) -> Optional[Path]:
        return self._roots.get(name)

    def get(self, name: str) -> Optional[Database]:
        if name not in self._roots:
            return None
        if name not in self._dbs:
            self._dbs[name] = Database(APP_DIR / f"{name}.db")
        return self._dbs[name]

    def unload(self, name: str) -> None:
        """Close and remove a loaded database from memory without deleting it."""
        db = self._dbs.pop(name, None)
        if db:
            db.close()

    def add_folder(self, folder: Path) -> str:
        name = folder.name
        base, n = name, 1
        while name in self._roots and self._roots[name] != folder:
            name = f"{base}_{n}"
            n += 1
        self._roots[name] = folder
        self._save()
        return name

    def remove(self, name: str) -> None:
        """Remove a database entry and delete its .db file if it exists."""
        # Close and unload from memory first
        db = self._dbs.pop(name, None)
        if db:
            db.close()
        # Delete the .db file if it exists (gracefully skip if already gone)
        db_file = APP_DIR / f"{name}.db"
        try:
            if db_file.exists():
                db_file.unlink()
        except Exception:
            pass
        # Remove from roots registry and persist
        self._roots.pop(name, None)
        self._save()

    def close_all(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()


# ── Loading Overlay ────────────────────────────────────────────────────────────


class LoadingOverlay(QWidget):
    """
    A full-panel overlay that shows a spinner animation and a progress message.
    Parent it to the ResultsPanel (or any widget) and call show()/hide().
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setObjectName("loadingOverlay")

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(12)

        self._dots_lbl = QLabel("◌")
        self._dots_lbl.setObjectName("loadingDots")
        self._dots_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._msg_lbl = QLabel("Loading...")
        self._msg_lbl.setObjectName("loadingMsg")
        self._msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self._dots_lbl)
        lay.addWidget(self._msg_lbl)

        self._frame = 0
        self._frames = ["◜", "◝", "◞", "◟"]
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(120)

        self.hide()

    def set_message(self, msg: str) -> None:
        self._msg_lbl.setText(msg)

    def _tick(self) -> None:
        self._dots_lbl.setText(self._frames[self._frame % len(self._frames)])
        self._frame += 1

    def resizeEvent(self, event) -> None:
        # Always fill parent
        p = self.parent()
        if isinstance(p, QWidget):
            self.setGeometry(p.rect())
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        p = self.parent()
        if isinstance(p, QWidget):
            self.setGeometry(p.rect())
        super().showEvent(event)


# ── Index Worker ───────────────────────────────────────────────────────────────


class IndexWorker(QThread):
    """Indexes a folder in a background thread using its own SQLite connection."""

    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(int)  # final total

    def __init__(self, db_path: Path, folder: Path, full_rebuild: bool = False):
        super().__init__()
        self._db_path = db_path
        self._folder = folder
        self._full_rebuild = full_rebuild

    def run(self) -> None:
        def _cb(current, total, msg):
            self.progress.emit(current, total, msg)

        total, _ = _run_index(self._db_path, self._folder, self._full_rebuild, _cb)
        self.finished.emit(total)


# ── Thumbnail Card ─────────────────────────────────────────────────────────────


class _DraggableDialog(QDialog):
    """Base for frameless dialogs that are draggable and remember position."""

    _PREFS_KEY: str = ""  # subclasses set this

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos: Optional[QPoint] = None

    def _restore_pos(self) -> None:
        key = self._PREFS_KEY
        if not key:
            return
        prefs = _load_prefs()
        pos = prefs.get(key)
        if pos and len(pos) == 2:
            self.move(pos[0], pos[1])

    def _save_pos(self) -> None:
        key = self._PREFS_KEY
        if not key:
            return
        prefs = _load_prefs()
        prefs[key] = [self.x(), self.y()]
        _save_prefs(prefs)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        self._save_pos()
        super().mouseReleaseEvent(event)


class EditJsonDialog(_DraggableDialog):
    """Frameless dialog to view and edit the JSON file linked to a card."""

    _PREFS_KEY = "edit_json_pos"
    W, H = 500, 460

    def __init__(
        self, asset: dict, db: Optional[Database], parent=None, focus_key: str = ""
    ):
        super().__init__(parent)
        self._asset = asset
        self._db = db
        self._focus_key = focus_key
        self.setWindowTitle("Edit JSON")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Shadow container - transparent, just for the drop-shadow effect
        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Accent header bar (matches status bar style) ──────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Edit JSON")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        # ── Editor area with inset border ─────────────────────────────────
        editor_wrap = QWidget()
        editor_wrap.setObjectName("editEditorWrap")
        ew_lay = QVBoxLayout(editor_wrap)
        ew_lay.setContentsMargins(10, 8, 10, 6)

        self._editor = QPlainTextEdit()
        self._editor.setObjectName("jsonEditor")

        # Load raw JSON - in dev mode the json_path is a fake sentinel, so skip disk
        json_path = asset.get("json_path", "")
        raw = ""
        if not DEV_MODE and json_path and Path(json_path).exists():
            try:
                raw = Path(json_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                raw = asset.get("json_data", "{}")
        else:
            raw = asset.get("json_data", "{}")

        try:
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            pass

        self._editor.setPlainText(raw)
        ew_lay.addWidget(self._editor)
        lay.addWidget(editor_wrap, 1)

        # ── Footer: cancel left, save right ──────────────────────────────
        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save)

        self._restore_pos()

        # Scroll to and subtly highlight the focused key after the dialog paints
        if focus_key:
            QTimer.singleShot(0, self._apply_focus_highlight)

    def _apply_focus_highlight(self) -> None:
        from PySide6.QtGui import QTextCharFormat, QTextCursor

        doc = self._editor.document()
        # Search for the key as it appears in pretty-printed JSON: "key":
        search = f'"{self._focus_key}":'
        found = doc.find(search)
        if found.isNull():
            return

        # Extend selection to the end of that line to cover the value too
        line_end = QTextCursor(found)
        line_end.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )

        # Scroll editor to show this line, centred if possible
        self._editor.setTextCursor(line_end)
        self._editor.ensureCursorVisible()
        # Move cursor to start of match so we don't leave a huge selection visible
        plain_cursor = QTextCursor(found)
        plain_cursor.clearSelection()
        self._editor.setTextCursor(plain_cursor)

        # Build the extra selection (amber-tinted background, no border noise)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(200, 170, 80, 38))

        sel = QTextEdit.ExtraSelection()
        sel.cursor = line_end
        sel.format = fmt
        self._editor.setExtraSelections([sel])

        # Clear highlight on first interaction (key press or click)
        def _clear_highlight() -> None:
            self._editor.setExtraSelections([])
            try:
                self._editor.cursorPositionChanged.disconnect(_clear_highlight)
            except RuntimeError:
                pass

        self._editor.cursorPositionChanged.connect(_clear_highlight)

    def _save(self) -> None:
        new_text = self._editor.toPlainText().strip()
        try:
            json.loads(new_text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, APP_NAME, f"Invalid JSON:\n{e}")
            return

        # ── DEV MODE: skip ALL disk I/O; only update the in-memory stub ───────
        if not DEV_MODE:
            json_path = self._asset.get("json_path", "")
            if json_path:
                try:
                    Path(json_path).write_text(new_text, encoding="utf-8")
                except Exception as exc:
                    QMessageBox.critical(
                        self, APP_NAME, f"Could not write file:\n{exc}"
                    )
                    return

        if self._db:
            self._db.update_json(self._asset["image_path"], new_text)

        self.accept()


class AddTagDialog(_DraggableDialog):
    """Frameless dialog for adding a tag to a folder or an entry."""

    _PREFS_KEY = "add_tag_pos"
    W, H = 360, 148

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tag_text: str = ""
        self.setWindowTitle("Add Tag")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Add Tag")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body: single text field ───────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(0)

        self._tag_edit = QLineEdit()
        self._tag_edit.setObjectName("scriptArgsEdit")
        self._tag_edit.setPlaceholderText("Enter tag...")
        self._tag_edit.setFixedHeight(24)
        self._tag_edit.returnPressed.connect(self._accept)
        b_lay.addWidget(self._tag_edit)
        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer: Save (left), Cancel (right) ───────────────────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        save_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)

        self._restore_pos()
        QTimer.singleShot(0, self._tag_edit.setFocus)

    def _accept(self) -> None:
        self.tag_text = self._tag_edit.text().strip()
        self.accept()


class ThumbnailCard(QWidget):
    deleted = Signal(str)
    edited = Signal(str)  # emitted after a successful JSON edit (image_path)

    CARD_W = THUMB_W
    CARD_H = THUMB_H + 4 + NAME_H

    def __init__(
        self,
        asset: dict,
        db: Optional[Database] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.asset = asset
        self._db = db
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._drag_start_pos: Optional[QPoint] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("cardImage")
        self._img_lbl.setFixedSize(THUMB_W, THUMB_H)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setScaledContents(False)
        lay.addWidget(self._img_lbl)

        self._name_lbl = QLabel(asset["name"])
        self._name_lbl.setObjectName("cardName")
        self._name_lbl.setFixedHeight(NAME_H)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setMaximumWidth(THUMB_W)
        lay.addWidget(self._name_lbl)

        QTimer.singleShot(0, self._apply_pixmap)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_start_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            dist = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def _start_drag(self) -> None:
        image_path = self.asset.get("image_path", "")
        if not image_path:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(image_path)])
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Use the thumbnail as the drag pixmap so the user sees what they're dragging
        pix = _load_pixmap(image_path)
        if not pix.isNull():
            drag.setPixmap(
                pix.scaled(
                    THUMB_W // 2,
                    THUMB_H // 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        drag.exec(Qt.DropAction.CopyAction)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def _apply_pixmap(self) -> None:
        from PySide6.QtGui import QPainterPath

        pix = _load_pixmap(self.asset["image_path"])
        if not pix.isNull():
            rounded = QPixmap(THUMB_W, THUMB_H)
            rounded.fill(Qt.GlobalColor.transparent)
            p = QPainter(rounded)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, THUMB_W, THUMB_H, 7, 7)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            self._img_lbl.setPixmap(rounded)
        else:
            self._img_lbl.setText("?")

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainterPath, QPen

        super().paintEvent(event)
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Highlight background behind name label
            name_y = THUMB_H + 4
            p.setBrush(QColor(123, 142, 232, 30))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(0, name_y, self.CARD_W, NAME_H, 3, 3)

            # Border around image
            pen = QPen(QColor(140, 160, 255, 140))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.addRoundedRect(1, 1, THUMB_W - 2, THUMB_H - 2, 7, 7)
            p.drawPath(path)
            p.end()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setObjectName("cardMenu")

        try:
            data: dict = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            data = {}

        for key, value in data.items():
            text = str(value).strip()
            if not text:
                continue
            label = "Copy " + " ".join(w.capitalize() for w in key.split("_"))
            act = menu.addAction(label)
            act.setData(text)

        if data:
            menu.addSeparator()

        add_tag_act = menu.addAction("Add Tag")
        menu.addSeparator()
        edit_act = menu.addAction("Edit JSON...")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is edit_act:
            dlg = EditJsonDialog(self.asset, self._db, self)
            if dlg.exec():
                # Refresh asset json_data from db so copies are updated
                self.edited.emit(self.asset["image_path"])
        elif chosen is add_tag_act:
            self._add_tag()
        elif chosen.data():
            QApplication.clipboard().setText(chosen.data())

    def _add_tag(self) -> None:
        dlg = AddTagDialog(self)
        if not dlg.exec():
            return
        tag = dlg.tag_text
        if not tag:
            return

        if DEV_MODE:
            # In dev mode just update in-memory json_data
            try:
                data = json.loads(self.asset.get("json_data", "{}"))
            except Exception:
                data = {}
            existing = data.get("tags", "")
            data["tags"] = f"{existing}, {tag}" if existing else tag
            new_text = json.dumps(data, indent=2, ensure_ascii=False)
            self.asset["json_data"] = new_text
            if self._db:
                self._db.update_json(self.asset["image_path"], new_text)
            self.edited.emit(self.asset["image_path"])
            return

        json_path = self.asset.get("json_path", "")
        if not json_path:
            return
        try:
            raw = (
                Path(json_path).read_text(encoding="utf-8", errors="ignore")
                if Path(json_path).exists()
                else "{}"
            )
            data = json.loads(raw)
        except Exception:
            data = {}

        existing = data.get("tags", "")
        data["tags"] = f"{existing}, {tag}" if existing else tag
        new_text = json.dumps(data, indent=2, ensure_ascii=False)

        try:
            Path(json_path).write_text(new_text, encoding="utf-8")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
            return

        if self._db:
            self._db.update_json(self.asset["image_path"], new_text)
        self.asset["json_data"] = new_text
        self.edited.emit(self.asset["image_path"])


# ── Folder Section ─────────────────────────────────────────────────────────────


class FolderSection(QWidget):
    card_deleted = Signal(str)
    card_edited = Signal(str)
    folder_tagged = Signal(str)  # emitted when a folder tag is written (folder_key)

    def __init__(
        self,
        title: str,
        depth: int = 0,
        copy_value: str = "",
        folder_key: str = "",
        root_folder: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth
        self._child_sections: list[FolderSection] = []
        self._copy_value = copy_value
        self._folder_key = folder_key
        self._root_folder = root_folder

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(4)

        self._title = title
        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(24 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(
            lambda pos: self._on_header_context_menu(self._header.mapToGlobal(pos))
        )

        hc_lay.addWidget(self._header)

        # Copy button - only created when this folder has an !F-*.json
        self._copy_btn: Optional[QToolButton] = None
        if copy_value:
            btn = QToolButton()
            btn.setText("Copy")
            btn.setObjectName("folderCopyBtn")
            btn.setToolTip("Copy Folder Tag")
            btn.setVisible(False)  # shown only when expanded
            btn.setFixedHeight(16)
            btn.adjustSize()
            btn.clicked.connect(self._copy_meta_value)
            hc_lay.addWidget(btn)
            self._copy_btn = btn

        hc_lay.addStretch()
        outer.addWidget(header_container)

        # ── Body ─────────────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        self._card_widget = QWidget()
        self._card_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

        self._cards: list[ThumbnailCard] = []
        self._current_cols: int = COLS
        self._db_ref: Optional[Database] = None

    def set_db(self, db: Optional[Database]) -> None:
        self._db_ref = db

    def add_card(self, asset: dict, db: Optional[Database] = None) -> None:
        card = ThumbnailCard(asset, db or self._db_ref)
        card.deleted.connect(self.card_deleted)
        card.edited.connect(self.card_edited)
        # Add to grid immediately using a reasonable default col count;
        # actual layout is corrected when the section is expanded/shown.
        i = len(self._cards)
        self._cards.append(card)
        self._card_grid.addWidget(card, i // COLS, i % COLS)

    def _relayout_cards(self) -> None:
        """Re-flow cards into the grid based on current available width."""
        avail_w = self._card_widget.width()
        if avail_w < THUMB_W:
            return  # not laid out yet, skip
        cols = max(1, avail_w // (THUMB_W + 6))

        # Only rebuild if column count changed
        if cols == self._current_cols:
            return
        self._current_cols = cols

        # takeAt removes from the layout but does NOT reparent the widget
        while self._card_grid.count():
            self._card_grid.takeAt(0)

        for i, card in enumerate(self._cards):
            self._card_grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Guard: only reflow when body is visible and we actually have cards
        if self._expanded and self._cards:
            self._relayout_cards()

    def add_child_section(self, sec: "FolderSection") -> None:
        self._child_sections.append(sec)
        sec.card_deleted.connect(self.card_deleted)
        sec.card_edited.connect(self.card_edited)
        sec.folder_tagged.connect(self.folder_tagged)
        self._body_lay.addWidget(sec)

    def has_cards(self) -> bool:
        return len(self._cards) > 0

    def _copy_meta_value(self) -> None:
        if self._copy_value:
            QApplication.clipboard().setText(self._copy_value)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        if self._copy_btn is not None:
            self._copy_btn.setVisible(self._expanded)
        if self._expanded and self._cards:
            self._current_cols = 0  # force reflow on next call
            QTimer.singleShot(0, self._relayout_cards)

    def _on_header_context_menu(self, global_pos) -> None:
        # Context menu only available when folder is expanded
        if not self._expanded:
            return

        menu = QMenu(self)
        menu.setObjectName("cardMenu")

        add_tag_act = menu.addAction("Add Tag")
        menu.addSeparator()
        edit_json_act = menu.addAction("Edit JSON...")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is add_tag_act:
            self._add_folder_tag()
        elif chosen is edit_json_act:
            self._edit_folder_json()

    def _get_folder_dir(self) -> Optional[Path]:
        """Return the actual filesystem directory for this folder section."""
        if self._root_folder is None:
            return None
        if self._folder_key:
            return self._root_folder / self._folder_key
        return self._root_folder

    def _add_folder_tag(self) -> None:
        dlg = AddTagDialog(self)
        if not dlg.exec():
            return
        tag = dlg.tag_text
        if not tag:
            return

        if DEV_MODE:
            self.folder_tagged.emit(self._folder_key)
            return

        folder_dir = self._get_folder_dir()
        if folder_dir is None or not folder_dir.exists():
            QMessageBox.warning(self, APP_NAME, "Could not locate folder on disk.")
            return

        # The meta file for a folder named "Foo" is: Foo/!F-Foo.json
        folder_name = folder_dir.name
        meta_filename = f"!F-{folder_name}.json"
        meta_path = folder_dir / meta_filename

        try:
            if meta_path.exists():
                raw = meta_path.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw)
            else:
                data = {}
        except Exception:
            data = {}

        existing = data.get("tags", "")
        data["tags"] = f"{existing}, {tag}" if existing else tag

        try:
            meta_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
            return

        # Re-index the folder meta and update copy_value so Copy button appears
        if self._db_ref and self._root_folder:
            try:
                conn = sqlite3.connect(str(self._db_ref.path))
                conn.row_factory = sqlite3.Row
                copy_val = str(next(iter(data.values()))) if data else ""
                conn.execute(
                    "INSERT INTO folder_meta(folder_key, copy_value)"
                    " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                    (self._folder_key, copy_val),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        self.folder_tagged.emit(self._folder_key)

    def _edit_folder_json(self) -> None:
        """Open Edit JSON for the folder's !F-*.json meta file."""
        if DEV_MODE:
            return

        folder_dir = self._get_folder_dir()
        if folder_dir is None or not folder_dir.exists():
            QMessageBox.warning(self, APP_NAME, "Could not locate folder on disk.")
            return

        folder_name = folder_dir.name
        meta_filename = f"!F-{folder_name}.json"
        meta_path = folder_dir / meta_filename

        try:
            if meta_path.exists():
                raw = meta_path.read_text(encoding="utf-8", errors="ignore")
            else:
                raw = "{}"
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            raw = "{}"

        # Build a fake asset dict so EditJsonDialog can work
        fake_asset = {
            "image_path": "",
            "json_path": str(meta_path),
            "json_data": raw,
            "name": meta_filename,
        }
        dlg = EditJsonDialog(fake_asset, None, self)
        if dlg.exec():
            # Re-index folder meta in DB
            if self._db_ref:
                try:
                    new_raw = (
                        meta_path.read_text(encoding="utf-8", errors="ignore")
                        if meta_path.exists()
                        else "{}"
                    )
                    data = json.loads(new_raw)
                    copy_val = str(next(iter(data.values()))) if data else ""
                    conn = sqlite3.connect(str(self._db_ref.path))
                    conn.row_factory = sqlite3.Row
                    conn.execute(
                        "INSERT INTO folder_meta(folder_key, copy_value)"
                        " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                        (self._folder_key, copy_val),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            self.folder_tagged.emit(self._folder_key)


# ── Results Panel ──────────────────────────────────────────────────────────────


class ResultsPanel(QScrollArea):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("resultsPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(1)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)

        self._db: Optional[Database] = None
        self._root_folder: Optional[Path] = None

        # Loading overlay (child of the viewport so it covers the scroll area)
        self._overlay = LoadingOverlay(self.viewport())

    def set_db(
        self, db: Optional[Database], root_folder: Optional[Path] = None
    ) -> None:
        self._db = db
        self._root_folder = root_folder
        self.refresh()

    def refresh(self, query: str = "") -> None:
        assets = self._db.search(query) if self._db else []
        if assets:
            # Show a message BEFORE the UI freezes while rendering all thumbnails.
            # processEvents() flushes it to screen before the heavy _populate() call.
            self.show_loading("Rendering images... this may take a moment")
            QApplication.processEvents()
        self._populate(assets)
        self.hide_loading()

    def show_loading(self, msg: str = "Loading...") -> None:
        self._overlay.set_message(msg)
        self._overlay.show()
        self._overlay.raise_()

    def update_loading(self, msg: str) -> None:
        self._overlay.set_message(msg)

    def hide_loading(self) -> None:
        self._overlay.hide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep overlay in sync
        self._overlay.setGeometry(self.viewport().rect())

    def _get_expanded_keys(self) -> set[str]:
        """Recursively collect folder_key of every currently expanded FolderSection."""
        keys: set[str] = set()

        def _collect(layout) -> None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, FolderSection):
                    if w._expanded:
                        keys.add(w._folder_key)
                    # Recurse into the section's body to catch child sections
                    _collect(w._body_lay)

        _collect(self._layout)
        return keys

    def _populate(self, assets: list[dict]) -> None:
        # Snapshot which folders are open so we can restore them after rebuild
        expanded_keys = self._get_expanded_keys()

        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (w := item.widget()):
                w.deleteLater()

        all_folders: list[str] = []
        seen: set[str] = set()
        for asset in assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            if fk not in seen:
                seen.add(fk)
                all_folders.append(fk)
            parts = fk.split("/")
            for depth in range(1, len(parts)):
                ancestor = "/".join(parts[:depth])
                if ancestor not in seen:
                    seen.add(ancestor)
                    idx = all_folders.index(fk)
                    all_folders.insert(idx, ancestor)

        all_folders.sort()

        sections: dict[str, FolderSection] = {}
        for fk in all_folders:
            parts = fk.split("/") if fk else []
            depth = len(parts)
            title = parts[-1] if parts else "(root)"
            copy_value = self._db.get_folder_meta(fk) if self._db else None
            sec = FolderSection(
                title,
                depth=depth,
                copy_value=copy_value or "",
                folder_key=fk,
                root_folder=self._root_folder,
            )
            sec.set_db(self._db)
            sec.card_deleted.connect(self._on_card_deleted)
            sec.card_edited.connect(self._on_card_edited)
            sec.folder_tagged.connect(self._on_folder_tagged)
            sections[fk] = sec

            if depth <= 1:
                self._layout.addWidget(sec)
            else:
                parent_key = "/".join(parts[:-1])
                if parent_key in sections:
                    sections[parent_key].add_child_section(sec)
                else:
                    self._layout.addWidget(sec)

        for asset in assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            if fk in sections:
                sections[fk].add_card(asset, self._db)

        # Re-open any folder that was expanded before the refresh
        for fk, sec in sections.items():
            if fk in expanded_keys:
                sec._toggle()

        self._layout.addStretch()

    def _on_card_deleted(self, image_path: str) -> None:
        if self._db:
            self._db.delete(image_path)
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]

    def _on_card_edited(self, image_path: str) -> None:
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]

    def _on_folder_tagged(self, folder_key: str) -> None:
        # Reload the whole view so the Copy button appears after a new tag
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]


# ── Open Database Dialog ───────────────────────────────────────────────────────


class ConfirmRemoveDialog(_DraggableDialog):
    """Styled confirmation dialog used by OpenDatabaseDialog's Remove action."""

    _PREFS_KEY = ""  # not persisted
    W, H = 300, 160

    def __init__(self, db_name: str, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Remove Database")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body ──────────────────────────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(16, 12, 16, 8)
        b_lay.setSpacing(6)

        main_lbl = QLabel(f'Remove "{db_name}"?')
        main_lbl.setObjectName("confirmMainLbl")
        main_lbl.setWordWrap(True)

        info_lbl = QLabel(
            "This deletes the index file. Your original asset folder will not be touched."
        )
        info_lbl.setObjectName("confirmInfoLbl")
        info_lbl.setWordWrap(True)

        b_lay.addWidget(main_lbl)
        b_lay.addWidget(info_lbl)
        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer: Yes (left accent), Cancel (right ghost) ───────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        yes_btn = QPushButton("Yes, Remove")
        yes_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(yes_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        yes_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)


class OpenDatabaseDialog(_DraggableDialog):
    _PREFS_KEY = "open_db_pos"
    W, H = 280, 320

    def __init__(self, db_manager: DatabaseManager, current: str, parent=None):
        super().__init__(parent)
        self._db_manager = db_manager
        self.setWindowTitle("Change Database")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.chosen: str = current

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Accent header bar ─────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Change Database")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        list_wrap = QWidget()
        list_wrap.setObjectName("dbListWrap")
        lw_lay = QVBoxLayout(list_wrap)
        lw_lay.setContentsMargins(8, 6, 8, 4)
        lw_lay.setSpacing(0)

        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        for name in db_manager.names():
            self._list.addItem(name)
            if name == current:
                self._list.setCurrentRow(self._list.count() - 1)
        lw_lay.addWidget(self._list)
        lay.addWidget(list_wrap, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")
        ok_btn = QPushButton("Open")
        ok_btn.setObjectName("editSaveBtn")

        self._remove_db_btn = QPushButton("Remove")
        self._remove_db_btn.setObjectName("dbRemoveBtn")
        self._remove_db_btn.setEnabled(False)

        f_lay.addWidget(ok_btn)
        f_lay.addStretch()
        f_lay.addWidget(self._remove_db_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        ok_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._list.itemDoubleClicked.connect(self._accept)
        self._list.currentRowChanged.connect(self._on_selection_changed)
        self._remove_db_btn.clicked.connect(self._remove_db)

        self._restore_pos()

    def _on_selection_changed(self, row: int) -> None:
        self._remove_db_btn.setEnabled(row >= 0)

    def _remove_db(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        name = item.text()
        dlg = ConfirmRemoveDialog(name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._db_manager.remove(name)
        # Remove from list widget
        row = self._list.currentRow()
        self._list.takeItem(row)
        # If the removed db was the current/chosen one, clear chosen
        if self.chosen == name:
            self.chosen = ""
        self._remove_db_btn.setEnabled(False)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item:
            self.chosen = item.text()
        self.accept()


# ── Script Runner ──────────────────────────────────────────────────────────────


class ScriptRunner(QThread):
    """Runs startup scripts sequentially in a background thread."""

    progress = Signal(int, int, str)  # current, total, message
    finished = Signal()

    def __init__(self, scripts: list[dict]):
        super().__init__()
        self._scripts = scripts

    def run(self) -> None:
        total = len(self._scripts)
        for i, entry in enumerate(self._scripts, 1):
            self.progress.emit(i, total, f"Executing Startup Scripts ({i}/{total})")
            cmd = f'python "{entry["path"]}"'
            if entry.get("args", "").strip():
                cmd += f" {entry['args'].strip()}"
            try:
                subprocess.run(cmd, shell=True, check=False)
            except Exception:
                pass
        self.finished.emit()


# ── Add Script Dialog ──────────────────────────────────────────────────────────


class AddScriptDialog(_DraggableDialog):
    _PREFS_KEY = "add_script_pos"
    W, H = 340, 262

    def __init__(self, parent=None):
        super().__init__(parent)
        self.script_path: str = ""
        self.script_args: str = ""
        self.script_name: str = ""
        self.setWindowTitle("Add Script")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)
        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)
        title_lbl = QLabel("Add Script")
        title_lbl.setObjectName("editDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # Body
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(8)

        notice = QLabel("Note: the script relies on its path not changing.")
        notice.setObjectName("scriptNotice")
        notice.setWordWrap(True)
        b_lay.addWidget(notice)

        self._path_btn = QPushButton("Select Python Script...")
        self._path_btn.setObjectName("editSaveBtn")
        self._path_btn.clicked.connect(self._pick_script)
        b_lay.addWidget(self._path_btn)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("scriptArgsEdit")
        self._name_edit.setPlaceholderText("Script name  (auto-filled from filename)")
        self._name_edit.setFixedHeight(24)
        b_lay.addWidget(self._name_edit)

        self._args_edit = QLineEdit()
        self._args_edit.setObjectName("scriptArgsEdit")
        self._args_edit.setPlaceholderText(
            "Execute arguments  (e.g. -r C:/some/folder)"
        )
        self._args_edit.setFixedHeight(24)
        b_lay.addWidget(self._args_edit)

        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("editSaveBtn")
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")
        f_lay.addWidget(add_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        add_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._restore_pos()

    def _pick_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Python Script", "", "Python Scripts (*.py)"
        )
        if path:
            self.script_path = path
            self._path_btn.setText(Path(path).name)
            # Auto-fill name only if the user hasn't typed one yet
            if not self._name_edit.text().strip():
                self._name_edit.setText(Path(path).stem)

    def _accept(self) -> None:
        if not self.script_path:
            QMessageBox.warning(self, APP_NAME, "Please select a Python script first.")
            return
        self.script_name = self._name_edit.text().strip() or Path(self.script_path).stem
        self.script_args = self._args_edit.text().strip()
        self.accept()


# ── Startup Scripts Dialog ─────────────────────────────────────────────────────


class StartupScriptsDialog(_DraggableDialog):
    _PREFS_KEY = "startup_scripts_pos"
    W, H = 280, 340

    def __init__(self, parent=None, readonly: bool = False):
        super().__init__(parent)
        # ── DEV MODE: when readonly=True all _save() calls are no-ops.
        #    The dialog is fully interactive - add, remove, reorder, edit all
        #    work visually - but nothing is written to startup_scripts.json.
        #    Changes are lost when the dialog closes. The real scripts file on
        #    disk is left completely untouched.
        self._save_scripts = (lambda _scripts: None) if readonly else _save_scripts

        self.setWindowTitle(
            "Startup Scripts" + ("  [dev - changes not saved]" if readonly else "")
        )
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._scripts: list[dict] = _load_scripts()

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)
        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)
        title_lbl = QLabel("Startup Scripts")
        title_lbl.setObjectName("editDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # List
        list_wrap = QWidget()
        list_wrap.setObjectName("dbListWrap")
        lw_lay = QVBoxLayout(list_wrap)
        lw_lay.setContentsMargins(8, 6, 8, 4)
        lw_lay.setSpacing(0)
        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.currentRowChanged.connect(self._on_selection_changed)
        lw_lay.addWidget(self._list)
        lay.addWidget(list_wrap, 1)
        self._refresh_list()

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # Footer with +/- and ↑/↓ buttons centered
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 6, 10, 7)
        f_lay.setSpacing(6)

        self._up_btn = QToolButton()
        self._up_btn.setText("↑")
        self._up_btn.setObjectName("scriptOrderBtn")
        self._up_btn.setFixedSize(22, 22)
        self._up_btn.setToolTip("Move Up")
        self._up_btn.setEnabled(False)
        self._up_btn.clicked.connect(self._move_up)

        self._down_btn = QToolButton()
        self._down_btn.setText("↓")
        self._down_btn.setObjectName("scriptOrderBtn")
        self._down_btn.setFixedSize(22, 22)
        self._down_btn.setToolTip("Move Down")
        self._down_btn.setEnabled(False)
        self._down_btn.clicked.connect(self._move_down)

        self._edit_btn = QToolButton()
        self._edit_btn.setText("Edit")
        self._edit_btn.setObjectName("scriptEditBtn")
        self._edit_btn.setFixedSize(48, 20)
        self._edit_btn.setToolTip("Edit Script")
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._edit_script)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("scriptAddBtn")
        self._add_btn.setFixedSize(22, 22)
        self._add_btn.setToolTip("Add Script")
        self._add_btn.clicked.connect(self._add_script)

        self._remove_btn = QToolButton()
        self._remove_btn.setText("−")
        self._remove_btn.setObjectName("scriptRemoveBtn")
        self._remove_btn.setFixedSize(22, 22)
        self._remove_btn.setToolTip("Remove Script")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._remove_script)

        f_lay.addStretch()
        f_lay.addWidget(self._up_btn)
        f_lay.addWidget(self._down_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._edit_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._add_btn)
        f_lay.addWidget(self._remove_btn)
        f_lay.addStretch()
        lay.addWidget(footer)

        self._restore_pos()

    def _refresh_list(self) -> None:
        row = self._list.currentRow()
        self._list.clear()
        for s in self._scripts:
            self._list.addItem(s["name"])
        # Restore selection if possible
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def _on_selection_changed(self, row: int) -> None:
        has = row >= 0
        count = len(self._scripts)
        self._remove_btn.setEnabled(has)
        self._edit_btn.setEnabled(has)
        self._up_btn.setEnabled(has and row > 0)
        self._down_btn.setEnabled(has and row < count - 1)

    def _edit_script(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        entry = self._scripts[row]
        dlg = AddScriptDialog(self)
        # Pre-fill the dialog with the existing values
        dlg.setWindowTitle("Edit Script")
        dlg.script_path = entry["path"]
        dlg._path_btn.setText(
            Path(entry["path"]).name if entry["path"] else "Select Python Script..."
        )
        dlg._name_edit.setText(entry.get("name", ""))
        dlg._args_edit.setText(entry.get("args", ""))
        if dlg.exec():
            self._scripts[row] = {
                "name": dlg.script_name,
                "path": dlg.script_path,
                "args": dlg.script_args,
            }
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(row)

    def _add_script(self) -> None:
        dlg = AddScriptDialog(self)
        if dlg.exec():
            entry = {
                "name": dlg.script_name,
                "path": dlg.script_path,
                "args": dlg.script_args,
            }
            self._scripts.append(entry)
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(len(self._scripts) - 1)

    def _remove_script(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            del self._scripts[row]
            self._save_scripts(self._scripts)
            self._refresh_list()

    def _move_up(self) -> None:
        row = self._list.currentRow()
        if row > 0:
            self._scripts[row - 1], self._scripts[row] = (
                self._scripts[row],
                self._scripts[row - 1],
            )
            self._save_scripts(self._scripts)
            self._list.setCurrentRow(
                row - 1
            )  # triggers _on_selection_changed via signal
            self._refresh_list()
            self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._list.currentRow()
        if row < len(self._scripts) - 1:
            self._scripts[row], self._scripts[row + 1] = (
                self._scripts[row + 1],
                self._scripts[row],
            )
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(row + 1)


# ── Main Window ────────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__()
        self.db_manager = db_manager
        self._active_db = ""
        self._app_menu: Optional[QMenu] = None
        self._index_worker: Optional[IndexWorker] = None
        self._script_runner: Optional[ScriptRunner] = None
        self._note_window: Optional[NoteWindow] = None

        # ── DEV MODE: create the in-memory stub database once here.
        #    This reference is ONLY ever used when DEV_MODE is True.
        #    It is never stored to prefs, never written to disk.
        self._dev_db: Optional[DevDatabase] = DevDatabase() if DEV_MODE else None

        title = f"{APP_NAME}  [DEV MODE - fake data only]" if DEV_MODE else APP_NAME
        self.setWindowTitle(title)
        self.setMinimumSize(560, 460)
        self.resize(1000, 720)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        # ── Top row ───────────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("≡")
        self._menu_btn.setObjectName("menuBtn")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.clicked.connect(self._toggle_app_menu)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Press / to search...")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._on_search_text_changed)

        # Debounce timer: fires _do_search 1 s after the user stops typing
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(1000)
        self._search_timer.timeout.connect(self._do_search)

        top_row.addWidget(self._menu_btn)
        top_row.addWidget(self._search, 1)

        self._note_btn = QToolButton()
        self._note_btn.setText("\uf249")
        self._note_btn.setObjectName("noteBtn")
        self._note_btn.setFixedSize(26, 26)
        self._note_btn.setToolTip("Notes")
        self._note_btn.clicked.connect(self._open_note_window)
        top_row.addWidget(self._note_btn)

        outer.addLayout(top_row)

        # ── Status bar ────────────────────────────────────────────────────
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(22)
        sb_lay = QHBoxLayout(status_bar)
        sb_lay.setContentsMargins(8, 0, 8, 0)
        sb_lay.setSpacing(8)

        self._db_lbl = QLabel("-")
        self._db_lbl.setObjectName("dbLbl")

        dot = QLabel("·")
        dot.setObjectName("statusDot")

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setObjectName("statusLbl")

        sb_lay.addWidget(self._db_lbl)
        sb_lay.addWidget(dot)
        sb_lay.addWidget(self._status_lbl)
        sb_lay.addStretch()
        outer.addWidget(status_bar)

        # ── Results canvas ────────────────────────────────────────────────
        self._results = ResultsPanel()
        self._results.setMinimumHeight(340)
        outer.addWidget(self._results, 1)
        # Indexing is started by run_startup_scripts() called after show()

    # ── Startup script execution ──────────────────────────────────────────

    def run_startup_scripts(self) -> None:
        """Run saved startup scripts then begin indexing. Called after show()."""
        # ── DEV MODE BRANCH ───────────────────────────────────────────────────
        # When --dev is active we skip ALL startup scripts and ALL real database
        # loading.  We inject the in-memory DevDatabase directly into the results
        # panel and display a status message.  No prefs are read or written here.
        # ─────────────────────────────────────────────────────────────────────
        if DEV_MODE:
            self._db_lbl.setText("[DEV MODE]")
            self._set_status("Dev mode - fake data, no disk access")
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        # ── PRODUCTION PATH (unchanged) ───────────────────────────────────────
        scripts = _load_scripts()
        if not scripts:
            self._pick_initial_db()
            return

        self._results.show_loading(f"Executing Startup Scripts (1/{len(scripts)})")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        runner = ScriptRunner(scripts)
        runner.progress.connect(self._on_script_progress)
        runner.finished.connect(self._on_scripts_finished)
        self._script_runner = runner
        runner.start()

    def _on_script_progress(self, current: int, total: int, msg: str) -> None:
        self._results.update_loading(msg)
        self._set_status(msg)

    def _on_scripts_finished(self) -> None:
        self._script_runner = None
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        self._pick_initial_db()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _pick_initial_db(self) -> None:
        prefs = _load_prefs()
        last = prefs.get("last_db", "")
        names = self.db_manager.names()
        if not names:
            self._set_status("No folders yet - use ≡ → Add Folder")
            return
        target = last if last in names else names[0]
        self._start_load_db(target)

    def _set_active_db(self, name: str) -> None:
        self._active_db = name
        self._db_lbl.setText(name or "-")
        # ── DEV MODE: never overwrite last_db - user's real session must survive
        if not DEV_MODE:
            prefs = _load_prefs()
            prefs["last_db"] = name
            _save_prefs(prefs)
        db = self.db_manager.get(name) if name else None
        root_folder = self.db_manager.root_for(name) if name else None
        self._results.set_db(db, root_folder)
        self._do_search()

    def _start_load_db(self, name: str) -> None:
        """Begin background indexing for a database, showing the loading overlay."""
        if self._index_worker and self._index_worker.isRunning():
            return  # already busy

        self._active_db = name
        self._db_lbl.setText(name or "-")
        self._set_status("Starting...")
        self._results.show_loading("Starting...")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        db = self.db_manager.get(name)
        folder = self.db_manager.root_for(name)
        if db is None or folder is None or not folder.exists():
            self._results.hide_loading()
            self._search.setEnabled(True)
            self._menu_btn.setEnabled(True)
            self._set_active_db(name)
            return

        worker = IndexWorker(db.path, folder, full_rebuild=False)
        worker.progress.connect(self._on_index_progress)
        worker.finished.connect(lambda total: self._on_index_finished(name, total))
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, current: int, total: int, msg: str) -> None:
        self._results.update_loading(msg)
        self._set_status(msg)

    def _on_index_finished(self, name: str, total: int) -> None:
        # Don't hide the overlay yet - _set_active_db → refresh() will show
        # "Rendering..." and hide the overlay once _populate() completes.
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        self._set_active_db(name)
        self._set_status(f"{total} assets")
        self._index_worker = None

    def _open_note_window(self) -> None:
        if self._note_window is None:
            self._note_window = NoteWindow(self)
        self._note_window.show_and_reload()

    def _on_search_text_changed(self) -> None:
        self._search_timer.start()  # restart the 1-second debounce window

    def _do_search(self) -> None:
        self._search_timer.stop()
        self._results.refresh(self._search.text().strip())

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    # ── App menu ──────────────────────────────────────────────────────────

    def _toggle_app_menu(self) -> None:
        if self._app_menu is not None:
            self._app_menu.close()
            self._app_menu = None
            return

        menu = QMenu(self)
        # ── DEV MODE: annotate menu items that are suppressed or limited ──────
        if DEV_MODE:
            menu.addAction("⚠ Dev Mode Active - no real DB loaded").setEnabled(False)
            menu.addSeparator()
        reload_menu = menu.addMenu("Reload Database")
        reload_menu.addAction("without Scripts", self._action_reload)
        reload_menu.addAction("with Scripts", self._action_reload_with_scripts)
        menu.addAction("Change Database", self._action_open_db)
        menu.addAction("Add Folder", self._action_add_folder)
        menu.addAction("Startup Scripts", self._action_startup_scripts)
        menu.aboutToHide.connect(self._on_menu_hide)
        self._app_menu = menu
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _on_menu_hide(self) -> None:
        self._app_menu = None

    def _action_reload(self) -> None:
        # ── DEV MODE: reset the in-memory stub and re-render fake data ────────
        if DEV_MODE:
            self._dev_db = DevDatabase()
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        if self._active_db:
            self._start_load_db(self._active_db)

    def _action_reload_with_scripts(self) -> None:
        """Run startup scripts first, then reload the active database."""
        # ── DEV MODE: just reset the stub (no real scripts to run) ───────────
        if DEV_MODE:
            self._dev_db = DevDatabase()
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        scripts = _load_scripts()
        if not scripts:
            # No scripts configured - fall back to a plain reload
            if self._active_db:
                self._start_load_db(self._active_db)
            return

        self._results.show_loading(f"Executing Startup Scripts (1/{len(scripts)})")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        runner = ScriptRunner(scripts)
        runner.progress.connect(self._on_script_progress)
        runner.finished.connect(self._on_reload_scripts_finished)
        self._script_runner = runner
        runner.start()

    def _on_reload_scripts_finished(self) -> None:
        """Called when startup scripts finish during a 'Reload with Scripts'."""
        self._script_runner = None
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        if self._active_db:
            self._start_load_db(self._active_db)

    def _action_open_db(self) -> None:
        dlg = OpenDatabaseDialog(self.db_manager, self._active_db, self)
        if dlg.exec() and dlg.chosen:
            name = dlg.chosen
            if name != self._active_db:
                # Unload the current db from memory if it isn't the same
                if self._active_db:
                    self.db_manager.unload(self._active_db)
                self._start_load_db(name)

    def _action_add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to add")
        if not path:
            return
        folder = Path(path)
        name = self.db_manager.add_folder(folder)
        if self._active_db:
            self.db_manager.unload(self._active_db)
        self._start_load_db(name)

    def _action_startup_scripts(self) -> None:
        # ── DEV MODE: open dialog in readonly mode - fully interactive but
        #    nothing is written to disk. Changes are lost on close.
        dlg = StartupScriptsDialog(self, readonly=DEV_MODE)
        dlg.exec()

    # ── Keyboard shortcuts ────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Slash and not self._search.hasFocus():
            self._search.setFocus()
            self._search.clear()
            event.accept()
        else:
            super().keyPressEvent(event)

    # ── Window close ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        # Stop any running worker
        if self._index_worker and self._index_worker.isRunning():
            self._index_worker.quit()
            self._index_worker.wait(2000)
        # Close all open database connections
        self.db_manager.close_all()
        # Clear pixmap cache to release file handles
        _PIXMAP_CACHE.clear()
        super().closeEvent(event)


# ── Notes helpers ──────────────────────────────────────────────────────────────


def _load_notes() -> dict:
    try:
        if NOTES_FILE.exists():
            return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_notes(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _flatten_notes(data: dict, query: str = "", category_path: str = "") -> list[dict]:
    """Recursively flatten notes JSON into {name, value, category} dicts."""
    results: list[dict] = []
    for key, value in data.items():
        if isinstance(value, str):
            if not query or query.lower() in key.lower():
                results.append({"name": key, "value": value, "category": category_path})
        elif isinstance(value, dict):
            sub = f"{category_path}/{key}" if category_path else key
            results.extend(_flatten_notes(value, query, sub))
    return results


def _set_nested(data: dict, keys: list, name: str, value: str) -> None:
    if not keys:
        data[name] = value
        return
    k = keys[0]
    if k not in data or not isinstance(data[k], dict):
        data[k] = {}
    _set_nested(data[k], keys[1:], name, value)


def _add_note_entry(name: str, value: str, category: str) -> None:
    data = _load_notes()
    if category:
        parts = [p for p in category.split("/") if p]
        _set_nested(data, parts, name, value)
    else:
        data[name] = value
    _save_notes(data)


# ── Note Entry Card ────────────────────────────────────────────────────────────


class NoteEntryCard(QWidget):
    CARD_W = THUMB_W  # 106 px
    CARD_H = 80  # 4:3 ratio  (106 × 3/4 ≈ 80)

    def __init__(
        self,
        name: str,
        value: str,
        notes_file: Path,
        panel: "NotePanel",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._name = name
        self._value = value
        self._notes_file = notes_file
        self._panel = panel
        self._flashing = False
        self._flash_timer: Optional[QTimer] = None
        self._hovered = False

        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("noteEntry")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)

        # Equal stretch=1 gives each label exactly half the card height.
        # AlignBottom on Name keeps it visually near the centre-line from above;
        # AlignTop on Value keeps it near the centre-line from below.
        name_lbl = QLabel(name)
        name_lbl.setObjectName("noteEntryName")
        name_lbl.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        )
        lay.addWidget(name_lbl, 1)

        val_lbl = QLabel(value)
        val_lbl.setObjectName("noteEntryValue")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(val_lbl, 1)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._copy_value()
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def _copy_value(self) -> None:
        QApplication.clipboard().setText(self._value)
        self._flashing = True
        self.update()
        if self._flash_timer:
            self._flash_timer.stop()
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._flash_timer.start(350)

    def _end_flash(self) -> None:
        self._flashing = False
        self._flash_timer = None
        self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainterPath, QPen

        super().paintEvent(event)
        if self._flashing:
            # Clicked: subtle green fill + border
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(80, 200, 120, 28))
            pen = QPen(QColor(80, 200, 120, 90))
            pen.setWidth(2)
            p.setPen(pen)
            path = QPainterPath()
            path.addRoundedRect(1, 1, self.CARD_W - 2, self.CARD_H - 2, 7, 7)
            p.drawPath(path)
            p.end()
        elif self._hovered:
            # Hover: very faint white/neutral tint
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(255, 255, 255, 10))
            pen = QPen(QColor(255, 255, 255, 35))
            pen.setWidth(1)
            p.setPen(pen)
            path = QPainterPath()
            path.addRoundedRect(1, 1, self.CARD_W - 2, self.CARD_H - 2, 7, 7)
            p.drawPath(path)
            p.end()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setObjectName("cardMenu")
        copy_act = menu.addAction("Copy")
        menu.addSeparator()
        edit_act = menu.addAction("Edit Json")
        chosen = menu.exec(event.globalPos())
        if chosen is copy_act:
            QApplication.clipboard().setText(self._value)
        elif chosen is edit_act:
            self._open_edit_json()

    def _open_edit_json(self) -> None:
        try:
            raw = (
                self._notes_file.read_text(encoding="utf-8")
                if self._notes_file.exists()
                else "{}"
            )
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            raw = "{}"
        fake = {
            "image_path": "",
            "json_path": str(self._notes_file),
            "json_data": raw,
            "name": "notes",
        }
        dlg = EditJsonDialog(fake, None, self, focus_key=self._name)
        if dlg.exec():
            self._panel.reload()


# ── Note Section ───────────────────────────────────────────────────────────────


class NoteSection(QWidget):
    def __init__(
        self,
        title: str,
        depth: int = 0,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth
        self._child_sections: list["NoteSection"] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(4)

        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(24 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        # No context menu on note folder headers

        hc_lay.addWidget(self._header)
        hc_lay.addStretch()
        outer.addWidget(header_container)

        # ── Body ─────────────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        self._card_widget = QWidget()
        self._card_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

        self._cards: list[NoteEntryCard] = []
        self._current_cols: int = COLS

    def add_card(
        self, name: str, value: str, notes_file: Path, panel: "NotePanel"
    ) -> None:
        card = NoteEntryCard(name, value, notes_file, panel)
        i = len(self._cards)
        self._cards.append(card)
        row = i // COLS
        self._card_grid.addWidget(card, row, i % COLS)
        self._card_grid.setRowMinimumHeight(row, 0)
        self._card_grid.setRowStretch(row, 0)

    def add_child_section(self, sec: "NoteSection") -> None:
        self._child_sections.append(sec)
        self._body_lay.addWidget(sec)

    def _relayout_cards(self) -> None:
        avail_w = self._card_widget.width()
        if avail_w < NoteEntryCard.CARD_W:
            return
        cols = max(1, avail_w // (NoteEntryCard.CARD_W + 6))
        if cols == self._current_cols:
            return
        self._current_cols = cols
        while self._card_grid.count():
            self._card_grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._card_grid.addWidget(card, i // cols, i % cols)
        # Force each row to only be as tall as the card — no extra expansion
        num_rows = (len(self._cards) + cols - 1) // cols
        for r in range(num_rows):
            self._card_grid.setRowMinimumHeight(r, 0)
            self._card_grid.setRowStretch(r, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._expanded and self._cards:
            self._relayout_cards()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        if self._expanded and self._cards:
            self._current_cols = 0
            QTimer.singleShot(0, self._relayout_cards)


# ── Note Panel ─────────────────────────────────────────────────────────────────


class NotePanel(QScrollArea):
    def __init__(self, notes_file: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._notes_file = notes_file
        self.setObjectName("resultsPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(1)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)

        self._current_query: str = ""

    def _get_expanded_titles(self) -> set[str]:
        """Recursively collect the title text of every expanded NoteSection."""
        titles: set[str] = set()

        def _collect(layout) -> None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, NoteSection):
                    if w._expanded:
                        titles.add(w._header.text())
                    _collect(w._body_lay)

        _collect(self._layout)
        return titles

    def reload(self, query: str = "") -> None:
        self._current_query = query
        # Snapshot state before clearing so we can restore it after repopulating
        expanded_titles = self._get_expanded_titles()
        scroll_value = self.verticalScrollBar().value()
        try:
            data = (
                json.loads(self._notes_file.read_text(encoding="utf-8"))
                if self._notes_file.exists()
                else {}
            )
        except Exception:
            data = {}
        self._populate(data, query, expanded_titles, scroll_value)

    def _populate(
        self,
        data: dict,
        query: str = "",
        expanded_titles: set[str] | None = None,
        scroll_value: int = 0,
    ) -> None:
        # Clear layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (w := item.widget()):
                w.deleteLater()

        all_entries = _flatten_notes(data, query)

        root_entries = [e for e in all_entries if not e["category"]]
        other_entries = [e for e in all_entries if e["category"]]

        # ── Root entries (no category) shown at top as flat card grid ────
        if root_entries:
            root_widget = QWidget()
            root_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            root_grid = QGridLayout(root_widget)
            root_grid.setContentsMargins(0, 2, 0, 6)
            root_grid.setSpacing(6)
            root_grid.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            for i, entry in enumerate(root_entries):
                card = NoteEntryCard(
                    entry["name"], entry["value"], self._notes_file, self
                )
                root_grid.addWidget(card, i // COLS, i % COLS)
            num_rows = (len(root_entries) + COLS - 1) // COLS
            for r in range(num_rows):
                root_grid.setRowMinimumHeight(r, 0)
                root_grid.setRowStretch(r, 0)
            self._layout.addWidget(root_widget)

        # ── Folder sections for categorised entries ───────────────────────
        if other_entries:
            all_folder_paths: set[str] = set()
            for e in other_entries:
                parts = e["category"].split("/")
                for depth in range(1, len(parts) + 1):
                    all_folder_paths.add("/".join(parts[:depth]))

            folder_list = sorted(all_folder_paths)
            sections: dict[str, NoteSection] = {}

            for folder_path in folder_list:
                parts = folder_path.split("/")
                depth = len(parts)
                title = parts[-1]
                sec = NoteSection(title, depth=depth - 1)
                sections[folder_path] = sec

                if depth == 1:
                    self._layout.addWidget(sec)
                else:
                    parent_path = "/".join(parts[:-1])
                    if parent_path in sections:
                        sections[parent_path].add_child_section(sec)
                    else:
                        self._layout.addWidget(sec)

            for entry in other_entries:
                cat = entry["category"]
                if cat in sections:
                    sections[cat].add_card(
                        entry["name"], entry["value"], self._notes_file, self
                    )

        self._layout.addStretch()

        # Re-open any section that was expanded before the refresh
        if expanded_titles:

            def _restore(layout) -> None:
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item is None:
                        continue
                    w = item.widget()
                    if (
                        isinstance(w, NoteSection)
                        and w._header.text() in expanded_titles
                    ):
                        if not w._expanded:
                            w._toggle()
                        _restore(w._body_lay)

            _restore(self._layout)

        # Restore scroll position after layout settles
        if scroll_value:
            QTimer.singleShot(
                0, lambda: self.verticalScrollBar().setValue(scroll_value)
            )


# ── Create Note Dialog ─────────────────────────────────────────────────────────


class CreateNoteDialog(_DraggableDialog):
    _PREFS_KEY = "create_note_pos"
    W, H = 310, 230

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Create New...")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body: three fields ────────────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(8)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("scriptArgsEdit")
        self._name_edit.setPlaceholderText("Name  (required)")
        self._name_edit.setFixedHeight(24)
        b_lay.addWidget(self._name_edit)

        self._value_edit = QLineEdit()
        self._value_edit.setObjectName("scriptArgsEdit")
        self._value_edit.setPlaceholderText("Value  (required)")
        self._value_edit.setFixedHeight(24)
        b_lay.addWidget(self._value_edit)

        self._cat_edit = QLineEdit()
        self._cat_edit.setObjectName("scriptArgsEdit")
        self._cat_edit.setPlaceholderText("Category  (optional, e.g. Pet/Dog)")
        self._cat_edit.setFixedHeight(24)
        b_lay.addWidget(self._cat_edit)

        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer ────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        save_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._name_edit.returnPressed.connect(self._accept)
        self._value_edit.returnPressed.connect(self._accept)
        self._cat_edit.returnPressed.connect(self._accept)

        self._restore_pos()
        QTimer.singleShot(0, self._name_edit.setFocus)

    def _accept(self) -> None:
        name = self._name_edit.text().strip()
        value = self._value_edit.text().strip()
        if not name or not value:
            QMessageBox.warning(self, APP_NAME, "Name and Value are required.")
            return
        category = self._cat_edit.text().strip()
        _add_note_entry(name, value, category)
        self.accept()


# ── Note Window ────────────────────────────────────────────────────────────────


class NoteWindow(QDialog):
    """Floating, non-modal notes window with its own search + canvas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Notes")
        self.setModal(False)
        self.setMinimumSize(480, 360)
        self.resize(720, 540)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        # Make the dialog background match the main app window exactly
        self.setObjectName("noteWindowRoot")

        # Restore saved position/size
        prefs = _load_prefs()
        pos = prefs.get("note_window_pos")
        if pos and len(pos) == 2:
            self.move(pos[0], pos[1])

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(600)
        self._search_timer.timeout.connect(self._do_search)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        # ── Top row: "+" button + search bar ─────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("noteAddBtn")
        self._add_btn.setFixedSize(26, 26)
        # Force the text to center properly
        self._add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._add_btn.clicked.connect(self._open_create_dialog)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search notes by name...")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._on_search_changed)

        top_row.addWidget(self._add_btn)
        top_row.addWidget(self._search, 1)
        lay.addLayout(top_row)

        # ── Canvas ────────────────────────────────────────────────────────
        self._panel = NotePanel(NOTES_FILE)
        self._panel.setMinimumHeight(280)
        lay.addWidget(self._panel, 1)

    def closeEvent(self, event) -> None:
        prefs = _load_prefs()
        prefs["note_window_pos"] = [self.x(), self.y()]
        _save_prefs(prefs)
        super().closeEvent(event)

    def show_and_reload(self) -> None:
        """Show (or raise) the window and reload note data."""
        self._panel.reload(self._search.text().strip())
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _open_create_dialog(self) -> None:
        dlg = CreateNoteDialog(self)
        if dlg.exec():
            self._panel.reload(self._search.text().strip())

    def _on_search_changed(self) -> None:
        self._search_timer.start()

    def _do_search(self) -> None:
        self._search_timer.stop()
        self._panel.reload(self._search.text().strip())


# ── Styling ────────────────────────────────────────────────────────────────────


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")

    BG_BASE = "#0d1017"
    BG_SURFACE = "#131720"
    BG_RAISED = "#181e2e"
    BG_BORDER = "rgba(255,255,255,0.07)"
    ACCENT = "#7b8ee8"
    ACCENT_DIM = "rgba(123,142,232,0.18)"
    ACCENT_MID = "rgba(123,142,232,0.38)"
    TEXT_PRI = "#dde1f0"
    TEXT_SEC = "rgba(180,190,220,0.55)"
    TEXT_DIM = "rgba(180,190,220,0.30)"

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(13, 16, 23))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Base, QColor(10, 13, 20))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(19, 23, 32))
    pal.setColor(QPalette.ColorRole.Text, QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Button, QColor(24, 30, 46))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(123, 142, 232))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(24, 30, 46))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(210, 215, 235))
    app.setPalette(pal)

    app.setStyleSheet(f"""
        QMainWindow, QWidget {{
            background: {BG_BASE};
            color: {TEXT_PRI};
            font-size: 12px;
        }}

        /* ── status bar ──────────────────────────────────────────────── */
        #statusBar {{
            background: {BG_RAISED};
            border: 1px solid {BG_BORDER};
            border-radius: 5px;
        }}
        /* Labels inside the status bar must share its background */
        #statusBar QLabel {{
            background: transparent;
        }}
        #statusLbl {{ color: {TEXT_SEC}; font-size: 11px; }}
        #statusDot {{ color: {TEXT_DIM}; font-size: 11px; }}
        #dbLbl     {{ color: {ACCENT}; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }}

        /* ── hamburger ───────────────────────────────────────────────── */
        #menuBtn {{
            background: transparent; border: none;
            font-size: 18px; color: rgba(255,255,255,0.55);
            border-radius: 5px;
        }}
        #menuBtn:hover   {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        #menuBtn:pressed {{ background: rgba(255,255,255,0.03); }}

        /* ── notes button ────────────────────────────────────────────── */
        #noteBtn {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px;
            color: rgba(180,190,220,0.45);
            font-size: 14px;
            font-weight: 400;
        }}
        #noteBtn:hover   {{ background: {BG_RAISED}; border-color: rgba(255,255,255,0.13); color: rgba(210,215,240,0.80); }}
        #noteBtn:pressed {{ background: rgba(255,255,255,0.03); }}

        /* ── note window "+" button ──────────────────────────────────── */
        #noteAddBtn {{
            background: rgba(80,180,120,0.18);
            border: 1px solid rgba(80,180,120,0.35);
            border-radius: 4px;
            color: rgba(100,210,140,0.90);
            font-size: 14px;
            font-weight: 700;
            padding: 0px;
            text-align: center;
        }}
        #noteAddBtn:hover   {{ background: rgba(80,180,120,0.30); border-color: rgba(80,180,120,0.60); color: rgb(120,230,160); }}
        #noteAddBtn:pressed {{ background: rgba(80,180,120,0.10); }}

        /* ── note window background (match main app window) ─────────── */
        #noteWindowRoot {{
            background: {BG_BASE};
        }}
        #noteWindowRoot QScrollArea {{
            background: {BG_BASE};
        }}
        #noteWindowRoot QWidget {{
            background: {BG_BASE};
        }}

        /* ── note entry card ─────────────────────────────────────────── */
        #noteEntry {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 7px;
        }}
        #noteEntryName {{
            font-size: 11px;
            font-weight: 700;
            color: {TEXT_PRI};
            background: transparent;
        }}
        #noteEntryValue {{
            font-size: 9px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── search ──────────────────────────────────────────────────── */
        QLineEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        QLineEdit:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── folder section header ───────────────────────────────────── */
        #sectionHeaderWrap {{ background: transparent; }}
        #sectionBody       {{ background: transparent; }}

        #sectionHeader {{
            background: transparent;
            border: none;
            border-radius: 4px;
            text-align: left;
            padding-left: 2px;
            font-size: 11px;
            font-weight: 600;
            color: rgba(180,195,255,0.60);
            letter-spacing: 0.2px;
        }}
        #sectionHeader:hover {{
            background: {ACCENT_DIM};
            color: {ACCENT};
        }}

        /* ── folder copy button (!F-*.json) ─────────────────────────── */
        #folderCopyBtn {{
            background: transparent;
            border: 1px solid rgba(123,142,232,0.20);
            border-radius: 3px;
            color: rgba(123,142,232,0.45);
            font-size: 8px;
            font-weight: 500;
            letter-spacing: 0.2px;
            padding: 1px 4px 0px 4px;
        }}
        #folderCopyBtn:hover {{
            background: {ACCENT_DIM};
            border-color: {ACCENT_MID};
            color: {ACCENT};
        }}
        #folderCopyBtn:pressed {{
            background: rgba(123,142,232,0.08);
        }}

        /* accent stripe on sub-folder headers */
        #sectionAccent {{
            background: rgba(123,142,232,0.30);
            border-radius: 1px;
        }}

        /* ── thumbnail card ─────────────────────────────────────────── */
        #cardImage {{
            background: {BG_SURFACE};
            border-radius: 7px;
        }}
        #cardName {{
            font-size: 10px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── card context menu ───────────────────────────────────────── */
        QMenu#cardMenu {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 3px;
        }}
        QMenu#cardMenu::item {{
            padding: 4px 14px;
            font-size: 11px;
            border-radius: 3px;
            color: rgba(210,215,240,0.88);
        }}
        QMenu#cardMenu::item:selected {{
            background: {ACCENT_DIM};
            color: {ACCENT};
        }}
        QMenu#cardMenu::separator {{
            height: 1px;
            background: rgba(255,255,255,0.07);
            margin: 3px 4px;
        }}

        /* ── app menus ───────────────────────────────────────────────── */
        QMenu {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 3px;
        }}
        QMenu::item          {{ padding: 4px 14px; font-size: 11px; border-radius: 3px; color: rgba(210,215,240,0.88); }}
        QMenu::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        QMenu::separator     {{ height: 1px; background: rgba(255,255,255,0.07); margin: 3px 4px; }}

        /* ── shadow layer behind dialog frame ───────────────────────── */
        #dialogShadow {{
            background: rgba(0,0,0,0.55);
            border-radius: 10px;
        }}

        /* ── shared dialog frame ─────────────────────────────────────── */
        #editDialogFrame {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.13);
            border-radius: 8px;
        }}

        /* ── dialog accent header ────────────────────────────────────── */
        #editDialogHeader {{
            background: {BG_RAISED};
            border-bottom: 1px solid rgba(255,255,255,0.07);
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }}
        #editDialogHeader QLabel, #editDialogHeader QWidget {{
            background: transparent;
        }}
        #editDialogDot {{
            background: {ACCENT};
            border-radius: 3px;
        }}
        #editDialogTitle {{
            font-size: 11px; font-weight: 600;
            color: {ACCENT}; letter-spacing: 0.4px;
            background: transparent;
        }}
        #dbDialogClose {{
            background: transparent; border: none;
            color: {TEXT_DIM}; font-size: 10px; border-radius: 3px;
        }}
        #dbDialogClose:hover {{ background: rgba(255,60,60,0.18); color: rgba(255,100,100,0.9); }}

        /* ── separator ───────────────────────────────────────────────── */
        #dbDialogSep {{
            background: rgba(255,255,255,0.07);
            max-height: 1px; border: none;
        }}

        /* ── database list ───────────────────────────────────────────── */
        #dbDialogList {{
            background: transparent; border: none;
        }}
        #dbDialogList::item {{
            padding: 5px 12px; border-radius: 3px;
            font-size: 11px; color: rgba(210,215,240,0.85);
        }}
        #dbDialogList::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        #dbDialogList::item:hover    {{ background: rgba(255,255,255,0.025); }}

        /* ── dialog footer ───────────────────────────────────────────── */
        #editDialogFooter {{ background: transparent; }}

        /* Cancel - ghost */
        #editCancelBtn {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px; padding: 3px 12px;
            font-size: 11px; color: {TEXT_DIM};
        }}
        #editCancelBtn:hover {{ background: rgba(255,255,255,0.04); color: {TEXT_SEC}; }}

        /* Remove DB - red, disabled when nothing selected */
        #dbRemoveBtn {{
            background: rgba(180,60,60,0.18);
            border: 1px solid rgba(200,70,70,0.35);
            border-radius: 4px; padding: 3px 12px;
            font-size: 11px; font-weight: 600;
            color: rgba(220,90,90,0.90);
        }}
        #dbRemoveBtn:hover  {{ background: rgba(200,70,70,0.30); border-color: rgba(220,80,80,0.60); color: rgb(240,100,100); }}
        #dbRemoveBtn:pressed {{ background: rgba(180,60,60,0.10); }}
        #dbRemoveBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.18); }}

        /* Save / Open - accent */
        #editSaveBtn {{
            background: {ACCENT_DIM};
            border: 1px solid {ACCENT_MID};
            border-radius: 4px; padding: 3px 16px;
            font-size: 11px; font-weight: 600;
            color: {ACCENT};
        }}
        #editSaveBtn:hover  {{ background: rgba(123,142,232,0.26); }}
        #editSaveBtn:pressed {{ background: rgba(123,142,232,0.10); }}

        /* ── JSON editor ─────────────────────────────────────────────── */
        #editEditorWrap, #dbListWrap {{
            background: transparent;
        }}
        #jsonEditor {{
            background: {BG_SURFACE};
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 5px;
            color: {TEXT_PRI};
            font-family: "Consolas", "Courier New", monospace;
            font-size: 11px;
            selection-background-color: {ACCENT};
            padding: 4px;
        }}

        /* ── loading overlay ─────────────────────────────────────────── */
        #loadingOverlay {{
            background: rgba(13,16,23,0.88);
        }}
        #loadingDots {{
            font-size: 28px;
            color: {ACCENT};
        }}
        #loadingMsg {{
            font-size: 12px;
            color: {TEXT_SEC};
        }}

        /* ── startup scripts +/- buttons ────────────────────────────── */
        #scriptAddBtn {{
            background: rgba(80,180,120,0.18);
            border: 1px solid rgba(80,180,120,0.35);
            border-radius: 4px;
            color: rgba(100,210,140,0.90);
            font-size: 14px;
            font-weight: 600;
        }}
        #scriptAddBtn:hover  {{ background: rgba(80,180,120,0.30); border-color: rgba(80,180,120,0.60); color: rgb(120,230,160); }}
        #scriptAddBtn:pressed {{ background: rgba(80,180,120,0.10); }}

        #scriptRemoveBtn {{
            background: rgba(180,80,80,0.15);
            border: 1px solid rgba(180,80,80,0.28);
            border-radius: 4px;
            color: rgba(210,100,100,0.70);
            font-size: 14px;
            font-weight: 600;
        }}
        #scriptRemoveBtn:hover  {{ background: rgba(180,80,80,0.28); border-color: rgba(180,80,80,0.55); color: rgb(230,110,110); }}
        #scriptRemoveBtn:pressed {{ background: rgba(180,80,80,0.10); }}
        #scriptRemoveBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        #scriptEditBtn {{
            background: rgba(180,150,80,0.15);
            border: 1px solid rgba(180,150,80,0.28);
            border-radius: 4px;
            color: rgba(220,185,100,0.70);
            font-size: 13px;
            font-weight: 600;
        }}
        #scriptEditBtn:hover   {{ background: rgba(180,150,80,0.30); border-color: rgba(200,170,90,0.55); color: rgb(240,200,110); }}
        #scriptEditBtn:pressed {{ background: rgba(180,150,80,0.10); }}
        #scriptEditBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        #scriptOrderBtn {{
            background: rgba(123,142,232,0.10);
            border: 1px solid rgba(123,142,232,0.22);
            border-radius: 4px;
            color: rgba(123,142,232,0.55);
            font-size: 13px;
            font-weight: 600;
        }}
        #scriptOrderBtn:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; color: {ACCENT}; }}
        #scriptOrderBtn:pressed {{ background: rgba(123,142,232,0.08); }}
        #scriptOrderBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        /* ── add script dialog notice ────────────────────────────────── */
        #scriptNotice {{
            font-size: 10px;
            color: rgba(180,190,220,0.45);
            background: transparent;
        }}
        #scriptArgsEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 11px;
            color: {TEXT_PRI};
        }}
        #scriptArgsEdit:focus {{ border-color: {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── remove-confirm dialog labels ────────────────────────────── */
        #confirmMainLbl {{
            font-size: 12px; font-weight: 600;
            color: {TEXT_PRI};
            background: transparent;
        }}
        #confirmInfoLbl {{
            font-size: 11px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── generic fallback ────────────────────────────────────────── */
        QDialog {{ background: {BG_SURFACE}; }}
        QPushButton {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px; padding: 5px 12px;
            color: {TEXT_PRI};
        }}
        QPushButton:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; }}
        QPushButton:pressed {{ background: rgba(255,255,255,0.03); }}

        QListWidget {{
            background: transparent;
            border: 1px solid {BG_BORDER};
            border-radius: 6px;
        }}
        QListWidget::item          {{ padding: 5px 10px; border-radius: 3px; }}
        QListWidget::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}

        /* ── scrollbar ───────────────────────────────────────────────── */
        QScrollBar:vertical         {{ background: transparent; width: 4px; margin: 0; }}
        QScrollBar::handle:vertical {{
            background: rgba(255,255,255,0.12); border-radius: 2px; min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{ background: rgba(123,142,232,0.40); }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical     {{ height: 0; }}
    """)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> int:
    # ── Windows: tell the taskbar to group under our own AppUserModelID ──────
    # This must happen BEFORE QApplication is created.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_ORG}.{APP_NAME}"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    if ICON_PATH.exists():
        icon = QIcon(str(ICON_PATH))
        app.setWindowIcon(icon)

    apply_style(app)
    db_manager = DatabaseManager()
    win = MainWindow(db_manager)
    win.show()
    win.run_startup_scripts()
    code = app.exec()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
