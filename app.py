from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Prompt Indexer"
APP_ORG = "PromptIndexer"
APP_DIR = Path.home() / ".prompt_indexer"
ICON_PATH = Path(__file__).parent / "Icon.png"
THUMB = 156
COLS = 3


# ── Database ───────────────────────────────────────────────────────────────────


class Database:
    """One SQLite database per root folder."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem  # display name == folder name
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                image_path  TEXT NOT NULL UNIQUE,
                json_path   TEXT NOT NULL,
                json_data   TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.commit()

    def index(self, folder: Path, full_rebuild: bool = False) -> tuple[int, int]:
        """
        Scan *folder* recursively for PNG+JSON pairs.
        Returns (total assets, number of changes).
        """
        if full_rebuild:
            self._conn.execute("DELETE FROM assets")
            self._conn.commit()

        existing: set[str] = {
            r[0] for r in self._conn.execute("SELECT image_path FROM assets").fetchall()
        }
        found: set[str] = set()
        changes = 0

        for png in folder.rglob("*.png"):
            jpath = png.with_suffix(".json")
            if not jpath.exists():
                continue

            try:
                raw = jpath.read_text(encoding="utf-8", errors="ignore")
                json.loads(raw)  # validate; raises on bad JSON
            except Exception:
                raw = "{}"

            key = str(png)
            found.add(key)

            if key not in existing:
                self._conn.execute(
                    "INSERT INTO assets(name, image_path, json_path, json_data)"
                    " VALUES(?,?,?,?)",
                    (png.stem, key, str(jpath), raw),
                )
                changes += 1
            else:
                # Refresh JSON data in case the file was edited
                self._conn.execute(
                    "UPDATE assets SET json_data=? WHERE image_path=?",
                    (raw, key),
                )

        # Remove entries whose files no longer exist
        stale = existing - found
        for path in stale:
            self._conn.execute("DELETE FROM assets WHERE image_path=?", (path,))
        changes += len(stale)

        self._conn.commit()
        total: int = self._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        return total, changes

    def search(self, query: str, limit: int = 400) -> list[dict]:
        q = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM assets"
            " WHERE name LIKE ? OR json_data LIKE ?"
            " ORDER BY name LIMIT ?",
            (q, q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, image_path: str) -> None:
        self._conn.execute("DELETE FROM assets WHERE image_path=?", (image_path,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── Database Manager ───────────────────────────────────────────────────────────


class DatabaseManager:
    """
    Keeps track of all registered root folders and their Database objects.
    The folder→path mapping is saved to roots.json in APP_DIR.
    """

    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._roots_file = APP_DIR / "roots.json"
        self._roots: dict[str, Path] = {}  # db_name → folder path
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

    def get(self, name: str) -> Optional[Database]:
        if name not in self._roots:
            return None
        if name not in self._dbs:
            self._dbs[name] = Database(APP_DIR / f"{name}.db")
        return self._dbs[name]

    def add_folder(self, folder: Path) -> str:
        """Register a new root folder and return the db name assigned to it."""
        name = folder.name
        base, n = name, 1
        while name in self._roots and self._roots[name] != folder:
            name = f"{base}_{n}"
            n += 1
        self._roots[name] = folder
        self._save()
        return name

    def index_all(self, full_rebuild: bool = False) -> dict[str, tuple[int, int]]:
        out = {}
        for name, folder in self._roots.items():
            if folder.exists():
                db = self.get(name)
                if db:
                    out[name] = db.index(folder, full_rebuild)
        return out

    def close_all(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()


# ── Thumbnail Card ─────────────────────────────────────────────────────────────


class ThumbnailCard(QFrame):
    """Single image tile with a dynamic right-click context menu."""

    deleted = Signal(str)  # emits image_path

    def __init__(self, asset: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.asset = asset

        self.setObjectName("card")
        self.setFixedSize(THUMB + 8, THUMB + 8)
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        lbl = QLabel()
        lbl.setFixedSize(THUMB, THUMB)
        lbl.setAlignment(Qt.AlignCenter)

        pix = QPixmap(asset["image_path"])
        if not pix.isNull():
            lbl.setPixmap(
                pix.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            lbl.setText(asset["name"])

        lay.addWidget(lbl)
        self.setToolTip(asset["name"])

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)

        try:
            data: dict = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            data = {}

        has_actions = False
        for key, value in data.items():
            text = str(value).strip()
            if not text:
                continue
            # "positive_prompt" → "Copy Positive Prompt"
            label = "Copy " + " ".join(w.capitalize() for w in key.split("_"))
            act = menu.addAction(label)
            act.setData(text)
            has_actions = True

        if has_actions:
            menu.addSeparator()

        delete_act = menu.addAction("Delete")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is delete_act:
            self.deleted.emit(self.asset["image_path"])
        elif chosen.data():
            QApplication.clipboard().setText(chosen.data())


# ── Results Panel ──────────────────────────────────────────────────────────────


class ResultsPanel(QScrollArea):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("resultsPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(6, 6, 6, 6)
        self._grid.setSpacing(6)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setWidget(self._content)

        self._db: Optional[Database] = None

    def set_db(self, db: Optional[Database]) -> None:
        self._db = db
        self.refresh()

    def refresh(self, query: str = "") -> None:
        assets = self._db.search(query) if self._db else []
        self._populate(assets)

    def _populate(self, assets: list[dict]) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        for i, asset in enumerate(assets):
            card = ThumbnailCard(asset)
            card.deleted.connect(self._on_card_deleted)
            self._grid.addWidget(card, i // COLS, i % COLS)

    def _on_card_deleted(self, image_path: str) -> None:
        if self._db:
            self._db.delete(image_path)
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()


# ── Open Database Dialog ───────────────────────────────────────────────────────


class OpenDatabaseDialog(QDialog):
    def __init__(
        self,
        db_manager: DatabaseManager,
        current: str,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Open Database")
        self.setModal(True)
        self.setFixedSize(300, 340)
        self.chosen: str = current

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Select a database:"))

        self._list = QListWidget()
        for name in db_manager.names():
            self._list.addItem(name)
            if name == current:
                self._list.setCurrentRow(self._list.count() - 1)
        lay.addWidget(self._list)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Open")
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

        ok_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._list.itemDoubleClicked.connect(self._accept)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item:
            self.chosen = item.text()
        self.accept()


# ── Main Window ────────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__()
        self.db_manager = db_manager
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(560, 460)
        self.resize(580, 560)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        # ── Central layout
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        # ── Status bar
        status_bar = QFrame()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(32)
        sb_lay = QHBoxLayout(status_bar)
        sb_lay.setContentsMargins(4, 0, 4, 0)
        sb_lay.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("☰")
        self._menu_btn.setObjectName("menuBtn")
        self._menu_btn.setFixedSize(28, 28)
        self._menu_btn.clicked.connect(self._show_app_menu)

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setObjectName("statusLbl")

        sb_lay.addWidget(self._menu_btn)
        sb_lay.addWidget(self._status_lbl)
        sb_lay.addStretch()
        outer.addWidget(status_bar)

        # ── Search row
        search_row = QHBoxLayout()
        search_row.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.textChanged.connect(self._do_search)

        self._db_combo = QComboBox()
        self._db_combo.setFixedWidth(164)
        self._db_combo.setPlaceholderText("— pick a database —")
        self._db_combo.currentTextChanged.connect(self._on_db_changed)

        search_row.addWidget(self._search, 1)
        search_row.addWidget(self._db_combo)
        outer.addLayout(search_row)

        # ── Results canvas
        self._results = ResultsPanel()
        self._results.setMinimumHeight(340)
        outer.addWidget(self._results, 1)

        # Populate dropdown, then run startup indexing
        self._refresh_combo()
        self._startup_index()

    # ── Internal helpers ────────────────────────────────────────────────

    def _refresh_combo(self, select: str = "") -> None:
        self._db_combo.blockSignals(True)
        self._db_combo.clear()
        for name in self.db_manager.names():
            self._db_combo.addItem(name)
        if select:
            idx = self._db_combo.findText(select)
            self._db_combo.setCurrentIndex(max(idx, 0))
        elif self._db_combo.count():
            self._db_combo.setCurrentIndex(0)
        self._db_combo.blockSignals(False)
        self._on_db_changed(self._db_combo.currentText())

    def _on_db_changed(self, name: str) -> None:
        db = self.db_manager.get(name) if name else None
        self._results.set_db(db)
        self._do_search()

    def _do_search(self) -> None:
        self._results.refresh(self._search.text().strip())

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    # ── App menu actions ────────────────────────────────────────────────

    def _show_app_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Reload", self._action_reload)
        menu.addAction("Open Database", self._action_open_db)
        menu.addAction("Add Folder", self._action_add_folder)
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _action_reload(self) -> None:
        self._set_status("Rebuilding databases…")
        QApplication.processEvents()
        try:
            results = self.db_manager.index_all(full_rebuild=True)
            total = sum(t for t, _ in results.values())
            self._set_status(
                f"Reloaded — {total} assets across {len(results)} database(s)"
            )
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Reload failed:\n{exc}")
            self._set_status("Reload failed")
        self._do_search()

    def _action_open_db(self) -> None:
        current = self._db_combo.currentText()
        dlg = OpenDatabaseDialog(self.db_manager, current, self)
        if dlg.exec() and dlg.chosen:
            idx = self._db_combo.findText(dlg.chosen)
            if idx >= 0:
                self._db_combo.setCurrentIndex(idx)

    def _action_add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to add")
        if not path:
            return
        folder = Path(path)
        name = self.db_manager.add_folder(folder)
        self._set_status(f'Indexing "{name}"…')
        QApplication.processEvents()
        try:
            db = self.db_manager.get(name)
            if db:
                total, new = db.index(folder)
                self._set_status(f'"{name}" — {total} assets ({new} new)')
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Indexing failed:\n{exc}")
            self._set_status("Indexing failed")
        self._refresh_combo(select=name)

    # ── Startup ─────────────────────────────────────────────────────────

    def _startup_index(self) -> None:
        """
        On startup, scan all known folders for new / removed files only.
        Does not do a full rebuild — just syncs changes since last run.
        """
        if not self.db_manager.names():
            self._set_status("No folders yet — use ☰ → Add Folder")
            return
        self._set_status("Checking for changes…")
        QApplication.processEvents()
        try:
            results = self.db_manager.index_all(full_rebuild=False)
            total = sum(t for t, _ in results.values())
            changed = sum(c for _, c in results.values())
            self._set_status(
                f"{total} assets" + (f"  ·  {changed} updated" if changed else "")
            )
        except Exception as exc:
            self._set_status(f"Startup index error: {exc}")
        self._do_search()


# ── Styling ────────────────────────────────────────────────────────────────────


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(18, 22, 32))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(225, 228, 238))
    pal.setColor(QPalette.ColorRole.Base, QColor(13, 17, 27))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(24, 29, 44))
    pal.setColor(QPalette.ColorRole.Text, QColor(225, 228, 238))
    pal.setColor(QPalette.ColorRole.Button, QColor(28, 34, 50))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(225, 228, 238))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(88, 110, 200))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(30, 35, 52))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 223, 235))
    app.setPalette(pal)

    app.setStyleSheet("""
        QMainWindow, QWidget  { background: #12161e; color: #e1e4ee; font-size: 13px; }

        /* status bar */
        #statusBar  { background: rgba(255,255,255,0.03); border-radius: 8px; }
        #statusLbl  { color: rgba(255,255,255,0.42); font-size: 11px; }

        /* hamburger button */
        #menuBtn {
            background: transparent; border: none;
            font-size: 16px; color: rgba(255,255,255,0.68);
            border-radius: 6px;
        }
        #menuBtn:hover   { background: rgba(255,255,255,0.08); }
        #menuBtn:pressed { background: rgba(255,255,255,0.04); }

        /* search */
        QLineEdit {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 10px; padding: 7px 12px;
            selection-background-color: #586ec8;
        }
        QLineEdit:focus { border: 1px solid rgba(140,160,255,0.40); }

        /* database dropdown */
        QComboBox {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 10px; padding: 7px 10px;
        }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView {
            background: #1c2133; border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.10);
            selection-background-color: #586ec8;
        }

        /* thumbnail card */
        #card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 10px;
        }
        #card:hover {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(140,160,255,0.30);
        }

        /* context + app menus */
        QMenu {
            background: #1a1f30;
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 8px; padding: 4px;
        }
        QMenu::item          { padding: 6px 18px 6px 12px; border-radius: 5px; }
        QMenu::item:selected { background: rgba(88,110,200,0.55); }
        QMenu::separator     { height: 1px; background: rgba(255,255,255,0.08); margin: 3px 8px; }

        /* dialogs */
        QDialog { background: #151922; }
        QPushButton {
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 8px; padding: 6px 14px;
        }
        QPushButton:hover   { background: rgba(255,255,255,0.11); }
        QPushButton:pressed { background: rgba(255,255,255,0.04); }

        QListWidget {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 8px;
        }
        QListWidget::item          { padding: 6px 10px; border-radius: 5px; }
        QListWidget::item:selected { background: rgba(88,110,200,0.55); }

        /* scrollbar */
        QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
        QScrollBar::handle:vertical {
            background: rgba(255,255,255,0.15); border-radius: 3px; min-height: 30px;
        }
        QScrollBar::handle:vertical:hover  { background: rgba(255,255,255,0.28); }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical      { height: 0; }
    """)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    apply_style(app)

    db_manager = DatabaseManager()
    win = MainWindow(db_manager)
    win.show()

    code = app.exec()
    db_manager.close_all()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
