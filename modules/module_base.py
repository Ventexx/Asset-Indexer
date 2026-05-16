"""
module_base.py  –  Asset Indexer Module SDK
============================================

Every module script imports this file to:
  1. Parse the --_ctx argument that the app injects at launch.
  2. Get a ModuleContext object with live app state (active DB, root folder, etc.).
  3. (Optional) subclass ModuleWindow for a ready-made PySide6 window that already
     matches the app's dark theme.

Quick-start template
--------------------

    from module_base import get_context, ModuleWindow
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    import sys

    def main():
        ctx = get_context()          # reads --_ctx from sys.argv
        app = QApplication(sys.argv)

        win = ModuleWindow(ctx, title="My Module")
        label = QLabel(f"Active DB: {ctx.active_db}")
        win.body_layout.addWidget(label)
        win.show()

        sys.exit(app.exec())

    if __name__ == "__main__":
        main()

Context fields
--------------
  ctx.active_db    str   Name of the currently loaded database (empty if none)
  ctx.db_path      str   Absolute path to the .db file (empty if none)
  ctx.root_folder  str   Absolute path to the indexed folder (empty if none)
  ctx.app_dir      str   ~/.asset_indexer  (where prefs/DBs/notes live)
  ctx.dev_mode     bool  True when the app was started with --dev

Connection points
-----------------
The context gives you read access to the SQLite database directly:

    import sqlite3
    if ctx.db_path:
        conn = sqlite3.connect(ctx.db_path)
        rows = conn.execute("SELECT name, json_data FROM assets").fetchall()
        conn.close()

Writing back (e.g. updating JSON) is safe as long as you commit and close
before the app does its next index pass.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Context ────────────────────────────────────────────────────────────────────


@dataclass
class ModuleContext:
    """Live snapshot of app state passed to every module at launch."""

    active_db: str = ""
    db_path: str = ""
    root_folder: str = ""
    app_dir: str = ""
    dev_mode: bool = False

    # Convenience helpers --------------------------------------------------

    @property
    def db_path_obj(self) -> Optional[Path]:
        return Path(self.db_path) if self.db_path else None

    @property
    def root_folder_obj(self) -> Optional[Path]:
        return Path(self.root_folder) if self.root_folder else None

    @property
    def app_dir_obj(self) -> Path:
        return Path(self.app_dir) if self.app_dir else Path.home() / ".asset_indexer"


def get_context() -> ModuleContext:
    """
    Parse --_ctx <json-string> from sys.argv and return a ModuleContext.

    The app always injects this argument when launching a module.  If the
    argument is absent (e.g. you run the module manually for testing) an
    empty context is returned so the module can still start up gracefully.
    """
    ctx_json: str = ""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--_ctx" and i + 1 < len(args):
            ctx_json = args[i + 1]
            break

    if not ctx_json:
        # No context injected – return a sensible empty default
        return ModuleContext()

    try:
        # The app double-serialises: the outer json.dumps wraps the inner json string
        raw = json.loads(ctx_json)          # outer unwrap  → str
        if isinstance(raw, str):
            data = json.loads(raw)          # inner unwrap  → dict
        else:
            data = raw                      # already a dict (future-proofing)
        return ModuleContext(**{k: v for k, v in data.items() if k in ModuleContext.__dataclass_fields__})
    except Exception:
        return ModuleContext()


# ── Optional base window ───────────────────────────────────────────────────────
# Only imported when PySide6 is available.  Modules that are pure CLI tools
# can use get_context() without ever touching ModuleWindow.

try:
    from PySide6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette

    # ── Colour tokens (match app's dark theme) ─────────────────────────────
    BG_BASE    = "#0d1017"
    BG_SURFACE = "#111520"
    BG_RAISED  = "#171c2b"
    BG_BORDER  = "rgba(255,255,255,0.08)"
    TEXT_PRI   = "rgba(210,215,240,0.92)"
    TEXT_SEC   = "rgba(180,190,220,0.55)"
    ACCENT     = "rgba(123,142,232,1.0)"

    _MODULE_STYLE = f"""
        QMainWindow, QWidget {{
            background: {BG_BASE};
            color: {TEXT_PRI};
            font-family: "Segoe UI", "Inter", sans-serif;
            font-size: 12px;
        }}
        QFrame#moduleHeader {{
            background: {BG_SURFACE};
            border-bottom: 1px solid {BG_BORDER};
        }}
        QLabel#moduleTitleLbl {{
            font-size: 12px;
            font-weight: 600;
            color: {TEXT_PRI};
            background: transparent;
        }}
        QLabel#moduleSubLbl {{
            font-size: 10px;
            color: {TEXT_SEC};
            background: transparent;
        }}
        QPushButton {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 5px 12px;
            color: {TEXT_PRI};
        }}
        QPushButton:hover   {{ background: rgba(123,142,232,0.18); border-color: rgba(123,142,232,0.40); }}
        QPushButton:pressed {{ background: rgba(255,255,255,0.03); }}
        QScrollBar:vertical         {{ background: transparent; width: 4px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.12); border-radius: 2px; min-height: 24px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """

    class ModuleWindow(QMainWindow):
        """
        Ready-made main window for modules.

        Usage:
            win = ModuleWindow(ctx, title="My Module", subtitle="optional")
            win.body_layout.addWidget(your_widget)
            win.show()

        The window has:
          • A thin header bar with the title, optional subtitle, and a close ×
          • A body area with a QVBoxLayout (`win.body_layout`) for your content
          • The app's dark theme pre-applied
        """

        def __init__(
            self,
            ctx: ModuleContext,
            title: str = "Module",
            subtitle: str = "",
            width: int = 700,
            height: int = 500,
            parent=None,
        ):
            super().__init__(parent)
            self.ctx = ctx
            self.setWindowTitle(title)
            self.resize(width, height)
            self.setMinimumSize(400, 300)
            self.setStyleSheet(_MODULE_STYLE)

            root = QWidget()
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            # ── Header ────────────────────────────────────────────────────
            header = QFrame()
            header.setObjectName("moduleHeader")
            header.setFixedHeight(36)
            h_lay = QHBoxLayout(header)
            h_lay.setContentsMargins(14, 0, 10, 0)
            h_lay.setSpacing(8)

            title_lbl = QLabel(title)
            title_lbl.setObjectName("moduleTitleLbl")
            h_lay.addWidget(title_lbl)

            if subtitle:
                sub_lbl = QLabel(subtitle)
                sub_lbl.setObjectName("moduleSubLbl")
                h_lay.addWidget(sub_lbl)

            h_lay.addStretch()

            if ctx.active_db:
                db_lbl = QLabel(ctx.active_db)
                db_lbl.setObjectName("moduleSubLbl")
                h_lay.addWidget(db_lbl)

            close_btn = QToolButton()
            close_btn.setText("✕")
            close_btn.setFixedSize(22, 22)
            close_btn.setStyleSheet("""
                QToolButton { background: transparent; border: none; color: rgba(180,190,220,0.45); font-size: 14px; }
                QToolButton:hover { color: rgba(230,90,90,0.9); }
            """)
            close_btn.clicked.connect(self.close)
            h_lay.addWidget(close_btn)

            outer.addWidget(header)

            # ── Body ──────────────────────────────────────────────────────
            body = QWidget()
            self.body_layout = QVBoxLayout(body)
            self.body_layout.setContentsMargins(12, 10, 12, 10)
            self.body_layout.setSpacing(8)
            outer.addWidget(body, 1)

    _PYSIDE6_AVAILABLE = True

except ImportError:
    _PYSIDE6_AVAILABLE = False
    ModuleWindow = None  # type: ignore[assignment,misc]
