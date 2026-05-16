"""
notes_module.py  –  Notes  |  Asset Indexer Module
===================================================
A fully self-contained module that replicates the NoteWindow feature that
used to live inside app.py.  Connect it via the ⊞ Modules button.

The module reads / writes the same notes.json that the main app uses, so
notes created here are immediately visible if you re-open the window.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

# ── Bootstrap: locate module_base.py ──────────────────────────────────────────
# Supports being placed anywhere — as long as module_base.py is in the same
# directory, or in a `modules/` sibling of the script.
_HERE = Path(__file__).parent
for _candidate in [_HERE, _HERE.parent / "modules"]:
    if (_candidate / "module_base.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from module_base import get_context  # noqa: E402  (path set above)

# ── PySide6 ───────────────────────────────────────────────────────────────────
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
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

# ── Constants (mirrored from app.py) ──────────────────────────────────────────
APP_NAME    = "Asset Indexer"
THUMB_W     = 106
THUMB_H     = 159
NAME_H      = 16
COLS        = 6
_AZ_SORT_KEY = "A-Z Sort in Folder"

# ── Resolve notes file path from context ──────────────────────────────────────
ctx = get_context()
APP_DIR    = Path(ctx.app_dir) if ctx.app_dir else Path.home() / ".asset_indexer"
NOTES_FILE = APP_DIR / "notes.json"


# ═══════════════════════════════════════════════════════════════════════════════
#  Notes helpers  (identical logic to app.py)
# ═══════════════════════════════════════════════════════════════════════════════


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


def _ensure_az_sort_flag(data: dict) -> dict:
    if _AZ_SORT_KEY not in data:
        updated = {_AZ_SORT_KEY: True}
        updated.update(data)
        _save_notes(updated)
        return updated
    return data


def _flatten_notes(data: dict, query: str = "", category_path: str = "") -> list[dict]:
    results: list[dict] = []
    for key, value in data.items():
        if key == _AZ_SORT_KEY:
            continue
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Draggable dialog base  (same pattern as app.py's _DraggableDialog)
# ═══════════════════════════════════════════════════════════════════════════════

_SESSION_POS: dict[str, list[int]] = {}


class _DraggableDialog(QDialog):
    _PREFS_KEY: str = ""
    _drag_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = None
            if self._PREFS_KEY:
                _SESSION_POS[self._PREFS_KEY] = [self.x(), self.y()]
        super().mouseReleaseEvent(event)

    def _restore_pos(self) -> None:
        if self._PREFS_KEY and self._PREFS_KEY in _SESSION_POS:
            pos = _SESSION_POS[self._PREFS_KEY]
            if len(pos) == 2:
                self.move(pos[0], pos[1])


# ═══════════════════════════════════════════════════════════════════════════════
#  EditJsonDialog  (needed by NoteEntryCard's "Edit Json" context menu action)
# ═══════════════════════════════════════════════════════════════════════════════


class EditJsonDialog(_DraggableDialog):
    _PREFS_KEY = "edit_json_pos"
    W, H = 480, 520

    def __init__(self, asset: dict, db, parent=None, focus_key: str = ""):
        super().__init__(parent)
        self._asset = asset
        self._db = db
        self._focus_key = focus_key

        self.setWindowTitle("Edit JSON")
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
        title_lbl = QLabel(f"Edit JSON — {asset.get('name', '')}")
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

        editor_wrap = QWidget()
        editor_wrap.setObjectName("editEditorWrap")
        ew_lay = QVBoxLayout(editor_wrap)
        ew_lay.setContentsMargins(10, 8, 10, 4)

        self._editor = QPlainTextEdit()
        self._editor.setObjectName("jsonEditor")

        json_path = asset.get("json_path", "")
        raw = ""
        if json_path and Path(json_path).exists():
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
        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")
        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save)
        self._restore_pos()

        if focus_key:
            QTimer.singleShot(0, self._apply_focus_highlight)

    def _apply_focus_highlight(self) -> None:
        from PySide6.QtGui import QTextCharFormat, QTextCursor

        doc = self._editor.document()
        search = f'"{self._focus_key}":'
        found = doc.find(search)
        if found.isNull():
            return
        line_end = QTextCursor(found)
        line_end.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )
        self._editor.setTextCursor(line_end)
        self._editor.centerCursor()
        plain_cursor = QTextCursor(found)
        plain_cursor.clearSelection()
        self._editor.setTextCursor(plain_cursor)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(200, 170, 80, 38))
        sel = QTextEdit.ExtraSelection()
        sel.cursor = line_end
        sel.format = fmt
        self._editor.setExtraSelections([sel])

        def _clear():
            self._editor.setExtraSelections([])
            try:
                self._editor.cursorPositionChanged.disconnect(_clear)
            except RuntimeError:
                pass

        self._editor.cursorPositionChanged.connect(_clear)

    def _save(self) -> None:
        new_text = self._editor.toPlainText().strip()
        try:
            json.loads(new_text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, APP_NAME, f"Invalid JSON:\n{e}")
            return
        json_path = self._asset.get("json_path", "")
        if json_path:
            try:
                Path(json_path).write_text(new_text, encoding="utf-8")
            except Exception as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
                return
        self.accept()


# ═══════════════════════════════════════════════════════════════════════════════
#  NoteEntryCard
# ═══════════════════════════════════════════════════════════════════════════════


class NoteEntryCard(QWidget):
    CARD_W = THUMB_W
    CARD_H = 80

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

        name_lbl = QLabel(name)
        name_lbl.setObjectName("noteEntryName")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
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
        super().paintEvent(event)
        if self._flashing:
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


# ═══════════════════════════════════════════════════════════════════════════════
#  NoteSection
# ═══════════════════════════════════════════════════════════════════════════════


class NoteSection(QWidget):
    def __init__(
        self,
        title: str,
        depth: int = 0,
        notes_file: Optional[Path] = None,
        category_path: str = "",
        global_az_sort: bool = True,
        panel: Optional["NotePanel"] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._expanded = False
        self._depth = depth
        self._child_sections: list["NoteSection"] = []
        self._notes_file = notes_file
        self._category_path = category_path
        self._global_az_sort = global_az_sort
        self._panel_ref: Optional["NotePanel"] = panel

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header
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
        self._header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._header.setFixedHeight(24 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(
            lambda pos: self._on_header_context_menu(self._header.mapToGlobal(pos))
        )

        hc_lay.addWidget(self._header)
        hc_lay.addStretch()
        outer.addWidget(header_container)

        # Body
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        self._card_widget = QWidget()
        self._card_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

        self._cards: list[NoteEntryCard] = []
        self._current_cols: int = COLS

    def _get_local_az_sort(self) -> Optional[bool]:
        if not self._notes_file or not self._notes_file.exists() or not self._category_path:
            return None
        try:
            data = json.loads(self._notes_file.read_text(encoding="utf-8", errors="ignore"))
            parts = self._category_path.split("/")
            node = data
            for part in parts:
                if not isinstance(node, dict) or part not in node:
                    return None
                node = node[part]
            if isinstance(node, dict) and _AZ_SORT_KEY in node:
                return bool(node[_AZ_SORT_KEY])
        except Exception:
            pass
        return None

    def _toggle_az_sort(self, current_effective: bool) -> None:
        if not self._notes_file or not self._category_path:
            return
        try:
            data = (
                json.loads(self._notes_file.read_text(encoding="utf-8", errors="ignore"))
                if self._notes_file.exists()
                else {}
            )
        except Exception:
            data = {}

        parts = self._category_path.split("/")
        node = data
        for part in parts:
            existing = node.get(part)
            if not isinstance(existing, dict):
                node[part] = {}
            node = node[part]

        node[_AZ_SORT_KEY] = not current_effective
        _save_notes(data)

        if self._panel_ref is not None:
            self._panel_ref.reload(self._panel_ref._current_query)

    def _on_header_context_menu(self, global_pos) -> None:
        if not self._expanded:
            return
        local_val = self._get_local_az_sort()
        effective_az = local_val if local_val is not None else self._global_az_sort
        menu = QMenu(self)
        menu.setObjectName("cardMenu")
        if effective_az:
            sort_act = menu.addAction("A-Z Sort")
        else:
            sort_act = menu.addAction("Standard Sort")
        chosen = menu.exec(global_pos)
        if chosen is sort_act:
            self._toggle_az_sort(effective_az)

    def add_card(self, name: str, value: str, notes_file: Path, panel: "NotePanel") -> None:
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


# ═══════════════════════════════════════════════════════════════════════════════
#  NotePanel
# ═══════════════════════════════════════════════════════════════════════════════


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
        data = _ensure_az_sort_flag(data)
        az_sort = bool(data.get(_AZ_SORT_KEY, True))
        self._populate(data, query, expanded_titles, scroll_value, az_sort)

    def _populate(
        self,
        data: dict,
        query: str = "",
        expanded_titles: set[str] | None = None,
        scroll_value: int = 0,
        az_sort: bool = True,
    ) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (w := item.widget()):
                w.deleteLater()

        all_entries = _flatten_notes(data, query)

        root_entries = [e for e in all_entries if not e["category"]]
        other_entries = [e for e in all_entries if e["category"]]

        if az_sort:
            root_entries.sort(key=lambda e: e["name"].lower())

        if root_entries:
            root_widget = QWidget()
            root_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            root_grid = QGridLayout(root_widget)
            root_grid.setContentsMargins(0, 2, 0, 6)
            root_grid.setSpacing(6)
            root_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            for i, entry in enumerate(root_entries):
                card = NoteEntryCard(entry["name"], entry["value"], self._notes_file, self)
                root_grid.addWidget(card, i // COLS, i % COLS)
            num_rows = (len(root_entries) + COLS - 1) // COLS
            for r in range(num_rows):
                root_grid.setRowMinimumHeight(r, 0)
                root_grid.setRowStretch(r, 0)
            self._layout.addWidget(root_widget)

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
                sec = NoteSection(
                    title,
                    depth=depth - 1,
                    notes_file=self._notes_file,
                    category_path=folder_path,
                    global_az_sort=az_sort,
                    panel=self,
                )
                sections[folder_path] = sec

                if depth == 1:
                    self._layout.addWidget(sec)
                else:
                    parent_path = "/".join(parts[:-1])
                    if parent_path in sections:
                        sections[parent_path].add_child_section(sec)
                    else:
                        self._layout.addWidget(sec)

            from collections import defaultdict
            by_cat: dict[str, list[dict]] = defaultdict(list)
            for entry in other_entries:
                by_cat[entry["category"]].append(entry)

            for folder_path, sec in sections.items():
                entries = by_cat.get(folder_path, [])
                if not entries:
                    continue
                local_val = sec._get_local_az_sort()
                effective = local_val if local_val is not None else az_sort
                if effective:
                    entries = sorted(entries, key=lambda e: e["name"].lower())
                for entry in entries:
                    sec.add_card(entry["name"], entry["value"], self._notes_file, self)

        self._layout.addStretch()

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

        if scroll_value:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(scroll_value))


# ═══════════════════════════════════════════════════════════════════════════════
#  CreateNoteDialog
# ═══════════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════════
#  NoteWindow  (the actual module window — a QMainWindow so it is fully independent)
# ═══════════════════════════════════════════════════════════════════════════════


class NoteWindow(QMainWindow):
    """Standalone Notes window — identical behaviour to the old in-app NoteWindow."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notes — Asset Indexer")
        self.setMinimumSize(480, 360)
        self.resize(720, 540)

        for _icon_path in [_HERE / "Icon.png", _HERE.parent / "Icon.png"]:
            if _icon_path.exists():
                self.setWindowIcon(QIcon(str(_icon_path)))
                break

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        # ── Top row: "+" + search ─────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("noteAddBtn")
        self._add_btn.setFixedSize(26, 26)
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

        # Search debounce
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(600)
        self._search_timer.timeout.connect(self._do_search)

        # Load notes on open
        self._panel.reload()

    def _open_create_dialog(self) -> None:
        dlg = CreateNoteDialog(self)
        if dlg.exec():
            self._panel.reload(self._search.text().strip())

    def _on_search_changed(self) -> None:
        self._search_timer.start()

    def _do_search(self) -> None:
        self._search_timer.stop()
        self._panel.reload(self._search.text().strip())


# ═══════════════════════════════════════════════════════════════════════════════
#  Stylesheet  (exact copy of the relevant sections from app.py's apply_style)
# ═══════════════════════════════════════════════════════════════════════════════


def _apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")

    BG_BASE    = "#0d1017"
    BG_SURFACE = "#131720"
    BG_RAISED  = "#181e2e"
    BG_BORDER  = "rgba(255,255,255,0.07)"
    ACCENT     = "#7b8ee8"
    ACCENT_DIM = "rgba(123,142,232,0.18)"
    ACCENT_MID = "rgba(123,142,232,0.38)"
    TEXT_PRI   = "#dde1f0"
    TEXT_SEC   = "rgba(180,190,220,0.55)"
    TEXT_DIM   = "rgba(180,190,220,0.30)"

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(13, 16, 23))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Base,            QColor(10, 13, 20))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(19, 23, 32))
    pal.setColor(QPalette.ColorRole.Text,            QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Button,          QColor(24, 30, 46))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(221, 225, 240))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(123, 142, 232))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(24, 30, 46))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(210, 215, 235))
    app.setPalette(pal)

    app.setStyleSheet(f"""
        QMainWindow, QWidget {{
            background: {BG_BASE};
            color: {TEXT_PRI};
            font-family: "Segoe UI", "Inter", sans-serif;
            font-size: 12px;
        }}

        QLineEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        QLineEdit:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_RAISED}; }}

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

        /* ── scroll bar ──────────────────────────────────────────────── */
        QScrollBar:vertical         {{ background: transparent; width: 4px; margin: 0; }}
        QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.12); border-radius: 2px; min-height: 24px; }}
        QScrollBar::handle:vertical:hover {{ background: rgba(123,142,232,0.40); }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{ height: 0; }}

        /* ── card / section context menu  (exact copy of app.py QMenu#cardMenu) ── */
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

        /* ── generic QMenu fallback  (exact copy of app.py QMenu) ─────── */
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

        /* Save - accent */
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

        /* ── create-note inputs ──────────────────────────────────────── */
        #scriptArgsEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 11px;
            color: {TEXT_PRI};
        }}
        #scriptArgsEdit:focus {{ border-color: {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── generic fallback ────────────────────────────────────────── */
        QDialog {{ background: {BG_BASE}; }}
        QPushButton {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px; padding: 5px 12px;
            color: {TEXT_PRI};
        }}
        QPushButton:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; }}
        QPushButton:pressed {{ background: rgba(255,255,255,0.03); }}
    """)


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Notes")

    # Icon lives in the project root; this module lives in ./modules/
    for _icon_path in [_HERE / "Icon.png", _HERE.parent / "Icon.png"]:
        if _icon_path.exists():
            app.setWindowIcon(QIcon(str(_icon_path)))
            break

    _apply_style(app)
    win = NoteWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
