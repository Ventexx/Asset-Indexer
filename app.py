from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
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

# 2:3 card image area
THUMB_W = 106
THUMB_H = 159
NAME_H = 16  # height of the name label below the image
COLS = 6


# ── Pixmap Cache ───────────────────────────────────────────────────────────────

_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(path: str) -> QPixmap:
    """
    Fill-crop to THUMB_W×THUMB_H (cover, no distortion).
    The image is scaled so its shorter side matches the target,
    then centre-cropped so it fills the frame completely.
    """
    if path not in _PIXMAP_CACHE:
        pix = QPixmap(path)
        if not pix.isNull():
            # Scale so the image *covers* the target rectangle
            scaled = pix.scaled(
                THUMB_W,
                THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Centre-crop to exact size
            x = (scaled.width() - THUMB_W) // 2
            y = (scaled.height() - THUMB_H) // 2
            pix = scaled.copy(x, y, THUMB_W, THUMB_H)
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


class ThumbnailCard(QWidget):
    deleted = Signal(str)

    CARD_W = THUMB_W
    CARD_H = THUMB_H + 4 + NAME_H  # image + gap + name label outside

    def __init__(self, asset: dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.asset = asset
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Image label — clips to exact 2:3 size, rounded via paintEvent on this widget
        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("cardImage")
        self._img_lbl.setFixedSize(THUMB_W, THUMB_H)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setScaledContents(False)
        lay.addWidget(self._img_lbl)

        # Name label — sits OUTSIDE and BELOW the image frame
        self._name_lbl = QLabel(asset["name"])
        self._name_lbl.setObjectName("cardName")
        self._name_lbl.setFixedHeight(NAME_H)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setMaximumWidth(THUMB_W)
        lay.addWidget(self._name_lbl)

        QTimer.singleShot(0, self._apply_pixmap)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._img_lbl.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._img_lbl.update()
        super().leaveEvent(event)

    def _apply_pixmap(self) -> None:
        from PySide6.QtGui import QPainterPath

        pix = _load_pixmap(self.asset["image_path"])
        if not pix.isNull():
            # Render pixmap with rounded corners into a new pixmap
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
        """Draw a subtle hover border around the image area only."""
        from PySide6.QtGui import QPainterPath, QPen

        super().paintEvent(event)
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
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
    """
    A collapsible section that can hold either cards or child FolderSections.
    depth=0 → top-level folder (e.g. Pokemon)
    depth=1 → sub-folder (e.g. Pokemon/Kanto)
    """

    card_deleted = Signal(str)

    def __init__(self, title: str, depth: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth
        self._child_sections: list[FolderSection] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        # Wrap with indent padding so the hover background only covers the
        # visible text+arrow area, not the entire width including indent space.
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(0)

        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(24 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)

        # Visual line accent for sub-folders
        if depth > 0:
            accent = QWidget()
            accent.setObjectName("sectionAccent")
            accent.setFixedWidth(2)
            accent.setFixedHeight(22)
            hc_lay.addWidget(accent)
            hc_lay.addSpacing(4)

        hc_lay.addWidget(self._header)
        outer.addWidget(header_container)

        # ── Body (cards + child sections) ───────────────────────────────
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        # Card grid lives inside body
        self._card_widget = QWidget()
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

    # ── Public API ───────────────────────────────────────────────────────

    def add_card(self, asset: dict) -> None:
        i = self._card_grid.count()
        card = ThumbnailCard(asset)
        card.deleted.connect(self.card_deleted)
        self._card_grid.addWidget(card, i // COLS, i % COLS)

    def add_child_section(self, sec: "FolderSection") -> None:
        self._child_sections.append(sec)
        sec.card_deleted.connect(self.card_deleted)
        self._body_lay.addWidget(sec)

    def has_cards(self) -> bool:
        return self._card_grid.count() > 0

    # ── Toggle ───────────────────────────────────────────────────────────

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )


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

    def set_db(self, db: Optional[Database]) -> None:
        self._db = db
        self.refresh()

    def refresh(self, query: str = "") -> None:
        assets = self._db.search(query) if self._db else []
        self._populate(assets)

    def _populate(self, assets: list[dict]) -> None:
        # Clear
        while self._layout.count():
            item = self._layout.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        # Build a tree of sections from the flat ordered asset list.
        # folder values are like "Pokemon", "Pokemon/Kanto", "Pokemon/Johto"
        # We need to create parent sections for intermediate folders even if
        # they contain no direct assets.

        # Step 1: collect all unique folder paths (normalised to forward-slash)
        all_folders: list[str] = []
        seen: set[str] = set()
        for asset in assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            if fk not in seen:
                seen.add(fk)
                all_folders.append(fk)
            # Also ensure all ancestor folders exist
            parts = fk.split("/")
            for depth in range(1, len(parts)):
                ancestor = "/".join(parts[:depth])
                if ancestor not in seen:
                    seen.add(ancestor)
                    # Insert ancestor before fk
                    idx = all_folders.index(fk)
                    all_folders.insert(idx, ancestor)

        # Sort so parents always precede children
        all_folders.sort()

        # Step 2: create FolderSection for every folder
        sections: dict[str, FolderSection] = {}
        for fk in all_folders:
            parts = fk.split("/") if fk else []
            depth = len(parts)
            title = parts[-1] if parts else "(root)"
            sec = FolderSection(title, depth=depth)
            sec.card_deleted.connect(self._on_card_deleted)
            sections[fk] = sec

            if depth <= 1:
                # Top-level → add directly to scroll content
                self._layout.addWidget(sec)
            else:
                parent_key = "/".join(parts[:-1])
                if parent_key in sections:
                    sections[parent_key].add_child_section(sec)
                else:
                    self._layout.addWidget(sec)  # fallback

        # Step 3: distribute assets into their sections
        for asset in assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            if fk in sections:
                sections[fk].add_card(asset)

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
        self.setFixedSize(260, 300)
        self.chosen: str = current

        # Remove title bar chrome for a cleaner popup feel
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Outer frame with rounded corners
        frame = QFrame(self)
        frame.setObjectName("dbDialogFrame")
        frame.setGeometry(0, 0, 260, 300)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        # Header row
        header = QWidget()
        header.setObjectName("dbDialogHeader")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 10, 12, 10)
        title_lbl = QLabel("Databases")
        title_lbl.setObjectName("dbDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        # Separator
        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # List
        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        for name in db_manager.names():
            self._list.addItem(name)
            if name == current:
                self._list.setCurrentRow(self._list.count() - 1)
        lay.addWidget(self._list, 1)

        # Footer buttons
        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        footer = QWidget()
        footer.setObjectName("dbDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(8, 6, 8, 8)
        f_lay.setSpacing(6)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("dbCancelBtn")
        ok_btn = QPushButton("Open")
        ok_btn.setObjectName("dbOpenBtn")
        f_lay.addWidget(cancel_btn)
        f_lay.addWidget(ok_btn)
        lay.addWidget(footer)

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
        self._app_menu: Optional[QMenu] = None  # track open menu for toggle
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

        # ── Top row: [☰]  [search…] ──────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("☰")
        self._menu_btn.setObjectName("menuBtn")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.clicked.connect(self._toggle_app_menu)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._do_search)

        top_row.addWidget(self._menu_btn)
        top_row.addWidget(self._search, 1)
        outer.addLayout(top_row)

        # ── Status bar ───────────────────────────────────────────────────
        # Use a plain QWidget (not QFrame) — avoids the background-gap issue
        # with QFrame's internal margin/border.
        status_bar = QWidget()
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

    # ── Helpers ──────────────────────────────────────────────────────────

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

    # ── App menu (toggle) ─────────────────────────────────────────────────

    def _toggle_app_menu(self) -> None:
        # If a menu is already open, close it
        if self._app_menu is not None:
            self._app_menu.close()
            self._app_menu = None
            return

        menu = QMenu(self)
        menu.addAction("Reload", self._action_reload)
        menu.addAction("Open Database", self._action_open_db)
        menu.addAction("Add Folder", self._action_add_folder)
        menu.aboutToHide.connect(self._on_menu_hide)
        self._app_menu = menu
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _on_menu_hide(self) -> None:
        self._app_menu = None

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

    # ── Startup indexing ──────────────────────────────────────────────────

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

    # Deep navy-to-charcoal base with indigo accent
    BG_BASE = "#0d1017"  # near-black, slight blue
    BG_SURFACE = "#131720"  # card/panel surface
    BG_RAISED = "#181e2e"  # slightly raised elements
    BG_BORDER = "rgba(255,255,255,0.07)"
    ACCENT = "#7b8ee8"  # muted periwinkle
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
        #statusLbl {{ color: {TEXT_SEC}; font-size: 11px; }}
        #statusDot {{ color: {TEXT_DIM}; font-size: 11px; }}
        #dbLbl     {{ color: {ACCENT}; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }}

        /* ── hamburger ───────────────────────────────────────────────── */
        #menuBtn {{
            background: transparent; border: none;
            font-size: 14px; color: rgba(255,255,255,0.55);
            border-radius: 5px;
        }}
        #menuBtn:hover   {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        #menuBtn:pressed {{ background: rgba(255,255,255,0.03); }}

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

        /* accent stripe on sub-folder headers */
        #sectionAccent {{
            background: rgba(123,142,232,0.30);
            border-radius: 1px;
        }}

        /* ── thumbnail card — NO QSS border/bg, all custom-painted ─── */
        #cardImage {{
            background: {BG_SURFACE};
            border-radius: 7px;
        }}
        #cardName {{
            font-size: 10px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── card context menu — compact & square ────────────────────── */
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

        /* ── app / open-db menus — match card menu style ─────────────── */
        QMenu {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 3px;
        }}
        QMenu::item          {{ padding: 4px 14px; font-size: 11px; border-radius: 3px; color: rgba(210,215,240,0.88); }}
        QMenu::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        QMenu::separator     {{ height: 1px; background: rgba(255,255,255,0.07); margin: 3px 4px; }}

        /* ── frameless database dialog ───────────────────────────────── */
        #dbDialogFrame {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 8px;
        }}
        #dbDialogHeader {{
            background: transparent;
        }}
        #dbDialogTitle {{
            font-size: 11px;
            font-weight: 600;
            color: {ACCENT};
            letter-spacing: 0.3px;
        }}
        #dbDialogClose {{
            background: transparent;
            border: none;
            color: {TEXT_DIM};
            font-size: 10px;
            border-radius: 3px;
        }}
        #dbDialogClose:hover {{ background: rgba(255,60,60,0.18); color: rgba(255,100,100,0.9); }}
        #dbDialogSep {{
            background: rgba(255,255,255,0.07);
            max-height: 1px;
            border: none;
        }}
        #dbDialogList {{
            background: transparent;
            border: none;
        }}
        #dbDialogList::item {{
            padding: 5px 12px;
            border-radius: 3px;
            font-size: 11px;
            color: rgba(210,215,240,0.85);
        }}
        #dbDialogList::item:selected {{
            background: {ACCENT_DIM};
            color: {ACCENT};
        }}
        #dbDialogList::item:hover {{
            background: rgba(255,255,255,0.05);
        }}
        #dbDialogFooter {{ background: transparent; }}
        #dbCancelBtn, #dbOpenBtn {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px;
            padding: 4px 10px;
            font-size: 11px;
            color: {TEXT_SEC};
        }}
        #dbCancelBtn:hover {{ background: rgba(255,255,255,0.05); }}
        #dbOpenBtn {{
            border-color: {ACCENT_MID};
            color: {ACCENT};
        }}
        #dbOpenBtn:hover {{ background: {ACCENT_DIM}; }}

        /* ── generic dialog fallback ─────────────────────────────────── */
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
