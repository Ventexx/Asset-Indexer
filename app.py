from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
    QPlainTextEdit,
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
PREFS_FILE = APP_DIR / "prefs.json"

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
        conn.commit()

        db_total: int = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        return db_total, changes
    finally:
        conn.close()


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
        """Blocking index on the *calling* thread's connection. Returns (total, changes)."""
        return _run_index(self.path, folder, full_rebuild, progress_cb=None)

    def search(self, query: str, limit: int = 2000) -> list[dict]:
        q = f"%{query}%"
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE name LIKE ? OR json_data LIKE ?"
            " ORDER BY folder, name LIMIT ?",
            (q, q, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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

        self._msg_lbl = QLabel("Loading…")
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
        if self.parent():
            self.setGeometry(self.parent().rect())
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        if self.parent():
            self.setGeometry(self.parent().rect())
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


class EditJsonDialog(QDialog):
    """
    Small frameless dialog to edit the raw JSON file linked to a card's image.
    Follows the same design as OpenDatabaseDialog.
    """

    def __init__(self, asset: dict, db: Optional[Database], parent=None):
        super().__init__(parent)
        self._asset = asset
        self._db = db
        self.setWindowTitle("Edit JSON")
        self.setModal(True)
        self.setFixedSize(480, 420)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        frame = QFrame(self)
        frame.setObjectName("dbDialogFrame")
        frame.setGeometry(0, 0, 480, 420)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("dbDialogHeader")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(12, 10, 12, 10)
        title_lbl = QLabel("Edit JSON")
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

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # Load raw JSON from disk
        json_path = asset.get("json_path", "")
        raw = ""
        if json_path and Path(json_path).exists():
            try:
                raw = Path(json_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                raw = asset.get("json_data", "{}")
        else:
            raw = asset.get("json_data", "{}")

        # Try to pretty-print
        try:
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            pass

        self._editor = QPlainTextEdit()
        self._editor.setObjectName("jsonEditor")
        self._editor.setPlainText(raw)
        self._editor.setFont(self._editor.font())
        lay.addWidget(self._editor, 1)

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
        save_btn = QPushButton("Save")
        save_btn.setObjectName("dbOpenBtn")
        f_lay.addWidget(cancel_btn)
        f_lay.addWidget(save_btn)
        lay.addWidget(footer)

        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save)

    def _save(self) -> None:
        new_text = self._editor.toPlainText().strip()
        # Validate JSON
        try:
            json.loads(new_text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, APP_NAME, f"Invalid JSON:\n{e}")
            return

        # Write back to the .json file on disk
        json_path = self._asset.get("json_path", "")
        if json_path:
            try:
                Path(json_path).write_text(new_text, encoding="utf-8")
            except Exception as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
                return

        # Update the database entry
        if self._db:
            self._db.update_json(self._asset["image_path"], new_text)

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

        edit_act = menu.addAction("Edit JSON…")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is edit_act:
            dlg = EditJsonDialog(self.asset, self._db, self)
            if dlg.exec():
                # Refresh asset json_data from db so copies are updated
                self.edited.emit(self.asset["image_path"])
        elif chosen.data():
            QApplication.clipboard().setText(chosen.data())


# ── Folder Section ─────────────────────────────────────────────────────────────


class FolderSection(QWidget):
    card_deleted = Signal(str)
    card_edited = Signal(str)

    def __init__(self, title: str, depth: int = 0, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth
        self._child_sections: list[FolderSection] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        # We use a fixed-width container so the header button itself only takes
        # up as much space as its text + icon need (instead of stretching full width).
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(0)

        self._title = title
        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        # SizePolicy: preferred width (shrinks to content), fixed height
        self._header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(24 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)

        # (no accent stripe)

        hc_lay.addWidget(self._header)
        hc_lay.addStretch()  # push button to the left so only text-area gets hover bg
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
        self._body_lay.addWidget(sec)

    def has_cards(self) -> bool:
        return len(self._cards) > 0

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        if self._expanded and self._cards:
            self._current_cols = 0  # force reflow on next call
            QTimer.singleShot(0, self._relayout_cards)


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

        # Loading overlay (child of the viewport so it covers the scroll area)
        self._overlay = LoadingOverlay(self.viewport())

    def set_db(self, db: Optional[Database]) -> None:
        self._db = db
        self.refresh()

    def refresh(self, query: str = "") -> None:
        assets = self._db.search(query) if self._db else []
        self._populate(assets)

    def show_loading(self, msg: str = "Loading…") -> None:
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

    def _populate(self, assets: list[dict]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if w := item.widget():
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
            sec = FolderSection(title, depth=depth)
            sec.set_db(self._db)
            sec.card_deleted.connect(self._on_card_deleted)
            sec.card_edited.connect(self._on_card_edited)
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

        self._layout.addStretch()

    def _on_card_deleted(self, image_path: str) -> None:
        if self._db:
            self._db.delete(image_path)
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()

    def _on_card_edited(self, image_path: str) -> None:
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

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        frame = QFrame(self)
        frame.setObjectName("dbDialogFrame")
        frame.setGeometry(0, 0, 260, 300)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

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

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        for name in db_manager.names():
            self._list.addItem(name)
            if name == current:
                self._list.setCurrentRow(self._list.count() - 1)
        lay.addWidget(self._list, 1)

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
        self._app_menu: Optional[QMenu] = None
        self._index_worker: Optional[IndexWorker] = None

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

        # ── Top row ───────────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("≡")
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

        # ── Status bar ────────────────────────────────────────────────────
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

        # ── Results canvas ────────────────────────────────────────────────
        self._results = ResultsPanel()
        self._results.setMinimumHeight(340)
        outer.addWidget(self._results, 1)

        self._pick_initial_db()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _pick_initial_db(self) -> None:
        prefs = _load_prefs()
        last = prefs.get("last_db", "")
        names = self.db_manager.names()
        if not names:
            self._set_status("No folders yet — use ≡ → Add Folder")
            return
        target = last if last in names else names[0]
        self._start_load_db(target)

    def _set_active_db(self, name: str) -> None:
        self._active_db = name
        self._db_lbl.setText(name or "—")
        prefs = _load_prefs()
        prefs["last_db"] = name
        _save_prefs(prefs)
        db = self.db_manager.get(name) if name else None
        self._results.set_db(db)
        self._do_search()

    def _start_load_db(self, name: str) -> None:
        """Begin background indexing for a database, showing the loading overlay."""
        if self._index_worker and self._index_worker.isRunning():
            return  # already busy

        self._active_db = name
        self._db_lbl.setText(name or "—")
        self._set_status("Starting…")
        self._results.show_loading("Starting…")
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
        self._results.hide_loading()
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        self._set_active_db(name)
        self._set_status(f"{total} assets")
        self._index_worker = None

    def _do_search(self) -> None:
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
        menu.addAction("Reload", self._action_reload)
        menu.addAction("Open Database", self._action_open_db)
        menu.addAction("Add Folder", self._action_add_folder)
        menu.aboutToHide.connect(self._on_menu_hide)
        self._app_menu = menu
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _on_menu_hide(self) -> None:
        self._app_menu = None

    def _action_reload(self) -> None:
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

        /* ── frameless database / edit dialogs ───────────────────────── */
        #dbDialogFrame {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 8px;
        }}
        #dbDialogHeader {{ background: transparent; }}
        #dbDialogTitle {{
            font-size: 11px; font-weight: 600;
            color: {ACCENT}; letter-spacing: 0.3px;
        }}
        #dbDialogClose {{
            background: transparent; border: none;
            color: {TEXT_DIM}; font-size: 10px; border-radius: 3px;
        }}
        #dbDialogClose:hover {{ background: rgba(255,60,60,0.18); color: rgba(255,100,100,0.9); }}
        #dbDialogSep {{
            background: rgba(255,255,255,0.07);
            max-height: 1px; border: none;
        }}
        #dbDialogList {{
            background: transparent; border: none;
        }}
        #dbDialogList::item {{
            padding: 5px 12px; border-radius: 3px;
            font-size: 11px; color: rgba(210,215,240,0.85);
        }}
        #dbDialogList::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        #dbDialogList::item:hover    {{ background: rgba(255,255,255,0.05); }}
        #dbDialogFooter {{ background: transparent; }}
        #dbCancelBtn, #dbOpenBtn {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px; padding: 4px 10px;
            font-size: 11px; color: {TEXT_SEC};
        }}
        #dbCancelBtn:hover {{ background: rgba(255,255,255,0.05); }}
        #dbOpenBtn {{
            border-color: {ACCENT_MID}; color: {ACCENT};
        }}
        #dbOpenBtn:hover {{ background: {ACCENT_DIM}; }}

        /* ── JSON editor ─────────────────────────────────────────────── */
        #jsonEditor {{
            background: {BG_SURFACE};
            border: none;
            color: {TEXT_PRI};
            font-family: monospace;
            font-size: 11px;
            selection-background-color: {ACCENT};
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
    return code


if __name__ == "__main__":
    raise SystemExit(main())
