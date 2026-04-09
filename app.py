from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPalette, QPixmap
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
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Prompt Indexer"
APP_ORG = "PromptIndexer"
APP_DIR = Path.home() / ".prompt_indexer"
ICON_PATH = Path(__file__).parent / "Icon.png"

# 2:3 card dimensions
THUMB_W = 120
THUMB_H = 180
COLS = 6


# ── Pixmap Cache ───────────────────────────────────────────────────────────────

_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(path: str) -> QPixmap:
    if path not in _PIXMAP_CACHE:
        pix = QPixmap(path)
        if not pix.isNull():
            pix = pix.scaled(
                THUMB_W,
                THUMB_H,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        _PIXMAP_CACHE[path] = pix
    return _PIXMAP_CACHE[path]


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
        self._conn.commit()

    def index(self, folder: Path, full_rebuild: bool = False) -> tuple[int, int]:
        if full_rebuild:
            self._conn.execute("DELETE FROM assets")
            self._conn.commit()

        existing: set[str] = {
            r[0] for r in self._conn.execute("SELECT image_path FROM assets").fetchall()
        }
        found: set[str] = set()
        changes = 0

        for png in sorted(folder.rglob("*.png")):
            jpath = png.with_suffix(".json")
            if not jpath.exists():
                continue
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
                self._conn.execute(
                    "INSERT INTO assets(name, folder, image_path, json_path, json_data)"
                    " VALUES(?,?,?,?,?)",
                    (png.stem, rel_folder, key, str(jpath), raw),
                )
                changes += 1
            else:
                self._conn.execute(
                    "UPDATE assets SET json_data=?, folder=? WHERE image_path=?",
                    (raw, rel_folder, key),
                )

        stale = existing - found
        for p in stale:
            self._conn.execute("DELETE FROM assets WHERE image_path=?", (p,))
        changes += len(stale)

        self._conn.commit()
        total: int = self._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        return total, changes

    def search(self, query: str, limit: int = 2000) -> list[dict]:
        q = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE name LIKE ? OR json_data LIKE ?"
            " ORDER BY folder, name LIMIT ?",
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

    def get(self, name: str) -> Optional[Database]:
        if name not in self._roots:
            return None
        if name not in self._dbs:
            self._dbs[name] = Database(APP_DIR / f"{name}.db")
        return self._dbs[name]

    def add_folder(self, folder: Path) -> str:
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
    deleted = Signal(str)

    def __init__(self, asset: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.asset = asset
        self.setObjectName("card")
        self.setFixedSize(THUMB_W + 8, THUMB_H + 8)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)

        self._lbl = QLabel()
        self._lbl.setFixedSize(THUMB_W, THUMB_H)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setText("·")
        lay.addWidget(self._lbl)
        self.setToolTip(asset["name"])

        QTimer.singleShot(0, self._apply_pixmap)

    def _apply_pixmap(self) -> None:
        pix = _load_pixmap(self.asset["image_path"])
        if not pix.isNull():
            self._lbl.setPixmap(pix)
            self._lbl.setText("")
        else:
            self._lbl.setText(self.asset["name"])

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


# ── Folder Section ─────────────────────────────────────────────────────────────


class FolderSection(QWidget):
    card_deleted = Signal(str)

    def __init__(self, title: str, depth: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header button
        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setCheckable(True)
        self._header.setChecked(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(f"  {'  ' * depth}{title}")
        self._header.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(30)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        outer.addWidget(self._header)

        # Thin separator
        sep = QFrame()
        sep.setObjectName("sectionSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        # Card grid (hidden by default)
        self._container = QWidget()
        self._container.setObjectName("sectionContainer")
        indent = depth * 18
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(indent + 8, 8, 8, 12)
        self._grid.setSpacing(6)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._container.setVisible(False)
        outer.addWidget(self._container)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._container.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )

    def add_card(self, asset: dict) -> None:
        i = self._grid.count()
        card = ThumbnailCard(asset)
        card.deleted.connect(self.card_deleted)
        self._grid.addWidget(card, i // COLS, i % COLS)


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
        self._layout.setSpacing(2)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)

        self._db: Optional[Database] = None

    def set_db(self, db: Optional[Database]) -> None:
        self._db = db
        self.refresh()

    def refresh(self, query: str = "") -> None:
        assets = self._db.search(query) if self._db else []
        self._populate(assets)

    def _populate(self, assets: list[dict]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        sections: dict[str, FolderSection] = {}
        for asset in assets:
            folder_key = asset.get("folder", "") or ""
            if folder_key not in sections:
                depth = folder_key.replace("\\", "/").count("/") + (
                    1 if folder_key else 0
                )
                # show only the last path component as the header title
                title = Path(folder_key).name if folder_key else "(root)"
                sec = FolderSection(title, depth=max(0, depth - 1))
                sec.card_deleted.connect(self._on_card_deleted)
                sections[folder_key] = sec
                self._layout.addWidget(sec)
            sections[folder_key].add_card(asset)

        self._layout.addStretch()

    def _on_card_deleted(self, image_path: str) -> None:
        if self._db:
            self._db.delete(image_path)
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()


# ── Open Database Dialog ───────────────────────────────────────────────────────


class OpenDatabaseDialog(QDialog):
    def __init__(self, db_manager: DatabaseManager, current: str, parent=None):
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
        self._active_db = ""
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(560, 460)
        self.resize(1000, 720)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        # ── Top row: [☰]  [search…] ─────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("☰")
        self._menu_btn.setObjectName("menuBtn")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.clicked.connect(self._show_app_menu)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._do_search)

        top_row.addWidget(self._menu_btn)
        top_row.addWidget(self._search, 1)
        outer.addLayout(top_row)

        # ── Status bar ───────────────────────────────────────────────────
        status_bar = QFrame()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(22)
        sb_lay = QHBoxLayout(status_bar)
        sb_lay.setContentsMargins(8, 0, 8, 0)
        sb_lay.setSpacing(8)

        self._db_lbl = QLabel("—")
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

        # ── Results canvas ───────────────────────────────────────────────
        self._results = ResultsPanel()
        self._results.setMinimumHeight(340)
        outer.addWidget(self._results, 1)

        self._pick_initial_db()
        self._startup_index()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _pick_initial_db(self) -> None:
        names = self.db_manager.names()
        if names:
            self._set_active_db(names[0])

    def _set_active_db(self, name: str) -> None:
        self._active_db = name
        self._db_lbl.setText(name or "—")
        db = self.db_manager.get(name) if name else None
        self._results.set_db(db)
        self._do_search()

    def _do_search(self) -> None:
        self._results.refresh(self._search.text().strip())

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    # ── App menu ─────────────────────────────────────────────────────────

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
        dlg = OpenDatabaseDialog(self.db_manager, self._active_db, self)
        if dlg.exec() and dlg.chosen:
            self._set_active_db(dlg.chosen)

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
        self._set_active_db(name)

    # ── Startup indexing ─────────────────────────────────────────────────

    def _startup_index(self) -> None:
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
        QMainWindow, QWidget { background: #12161e; color: #e1e4ee; font-size: 13px; }

        /* status bar */
        #statusBar  { background: rgba(255,255,255,0.03); border-radius: 5px; }
        #statusLbl  { color: rgba(255,255,255,0.42); font-size: 11px; }
        #statusDot  { color: rgba(255,255,255,0.18); font-size: 11px; }
        #dbLbl      { color: rgba(140,160,255,0.80); font-size: 11px; font-weight: 600; }

        /* hamburger */
        #menuBtn {
            background: transparent; border: none;
            font-size: 15px; color: rgba(255,255,255,0.68);
            border-radius: 5px;
        }
        #menuBtn:hover   { background: rgba(255,255,255,0.08); }
        #menuBtn:pressed { background: rgba(255,255,255,0.04); }

        /* search */
        QLineEdit {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 7px; padding: 3px 10px; font-size: 12px;
            selection-background-color: #586ec8;
        }
        QLineEdit:focus { border: 1px solid rgba(140,160,255,0.40); }

        /* folder section header */
        #sectionHeader {
            background: rgba(255,255,255,0.035);
            border: none; border-radius: 0px;
            text-align: left; padding-left: 8px;
            font-size: 12px; font-weight: 600;
            color: rgba(200,210,255,0.75);
        }
        #sectionHeader:hover  { background: rgba(255,255,255,0.07); color: #e1e4ee; }
        #sectionSep           { color: rgba(255,255,255,0.05); max-height: 1px; }
        #sectionContainer     { background: transparent; }

        /* thumbnail card */
        #card {
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 8px;
        }
        #card:hover {
            background: rgba(140,160,255,0.13);
            border: 1px solid rgba(140,160,255,0.50);
        }

        /* menus */
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
        QScrollBar:vertical         { background: transparent; width: 5px; margin: 0; }
        QScrollBar::handle:vertical {
            background: rgba(255,255,255,0.14); border-radius: 2px; min-height: 24px;
        }
        QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.26); }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical     { height: 0; }
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
