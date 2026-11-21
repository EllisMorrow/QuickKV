import bisect
import html
import re

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from core.config import THEMES
from ui.candidate_text import highlighted_html
from ui.continuous_candidates import ContinuousCandidateDelegate, presentation


_LATIN = re.compile(r"[A-Za-z0-9_]+$")
_LATIN_PHRASE = re.compile(r"[A-Za-z0-9_]+(?: +[A-Za-z0-9_]+)+$")
_LATIN_CHAR = re.compile(r"[A-Za-z0-9_]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]{2,8}$")
_VISIBLE_ROWS = 5


class ContinuousEditor(QWidget):
    submit_requested = Signal(str)
    copy_requested = Signal(str)
    escape_requested = Signal()
    entry_inserted = Signal(dict)

    def __init__(self, word_manager, settings, parent=None):
        super().__init__(parent)
        self.word_manager = word_manager
        self.settings = settings
        self.expand_entry = None
        self._source_blocks = None
        self._prefix_keys = []
        self._prefix_blocks = []
        self._shortcuts = {}
        self._last_draft = ""
        self._replacement = None
        self._manual = False
        self._ime_widget = None
        self._pending = False
        self._reserve_preview = False

        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName("草稿正文")
        self.editor.setPlaceholderText("在这里写正文")

        self.candidates = QFrame(self.editor)
        self.candidates.setObjectName("continuousCandidates")
        overlay = QVBoxLayout(self.candidates)
        overlay.setContentsMargins(4, 4, 4, 4)
        overlay.setSpacing(2)
        self.lookup = QLineEdit()
        self.lookup.setAccessibleName("找词")
        self.lookup.setPlaceholderText("搜索词库…")
        self.range_label = QLabel()
        self.range_label.setTextFormat(Qt.PlainText)
        self.results = QListWidget()
        self.results.setAccessibleName("可插入的词条")
        self.results.setFocusPolicy(Qt.NoFocus)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results.setUniformItemSizes(True)
        self.results.setMouseTracking(True)
        self.results.setItemDelegate(ContinuousCandidateDelegate(settings, self.results))
        overlay.addWidget(self.lookup)
        overlay.addWidget(self.range_label)
        overlay.addWidget(self.results)
        self.candidates.hide()

        self.preview = QTextBrowser(self.editor)
        self.preview.setObjectName("continuousPreview")
        self.preview.setAccessibleName("完整词条预览")
        self.preview.setFocusPolicy(Qt.NoFocus)
        self.preview.setOpenLinks(False)
        self.preview.document().setDocumentMargin(9)
        self.preview.hide()

        self.lookup_button = QPushButton("找词")
        self.lookup_button.setToolTip("找词（Ctrl+J）")
        self.copy_button = QPushButton("复制整段")
        self.recover_button = QPushButton("恢复上次草稿")
        self.send_button = QPushButton("落字")
        self.set_target_label("")
        footer = QHBoxLayout()
        footer.setSpacing(4)
        footer.addWidget(self.lookup_button)
        footer.addWidget(self.copy_button)
        footer.addWidget(self.recover_button)
        footer.addStretch()
        footer.addWidget(self.send_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.editor, 1)
        layout.addLayout(footer)

        self._timer = QTimer(self, singleShot=True, interval=160)
        self._timer.timeout.connect(self._refresh_results)
        self._after_input = QTimer(self, singleShot=True, interval=0)
        self._after_input.timeout.connect(self._refresh_after_input)
        self.editor.installEventFilter(self)
        self.lookup.installEventFilter(self)
        self.editor.textChanged.connect(self._draft_changed)
        self.editor.cursorPositionChanged.connect(self.dismiss_candidates)
        self.editor.selectionChanged.connect(self.dismiss_candidates)
        self.editor.updateRequest.connect(self._position_candidates)
        self.lookup.textChanged.connect(self._queue_lookup)
        self.results.itemClicked.connect(self._accept)
        self.results.itemEntered.connect(self.results.setCurrentItem)
        self.results.currentItemChanged.connect(self._update_preview)
        self.lookup_button.clicked.connect(self._open_lookup)
        self.send_button.clicked.connect(self._submit)
        self.copy_button.clicked.connect(self._copy)
        self.recover_button.clicked.connect(self._recover)
        self.apply_theme()
        self._update_footer()

    def apply_theme(self):
        theme = THEMES[self.settings.theme]
        self.setStyleSheet(f"""
            QPlainTextEdit, QLineEdit, QListWidget {{
                background: {theme['input_bg_color']};
                color: {theme['text_color']};
                border: 1px solid {theme['border_color']};
                font-size: {self.settings.font_size}px;
            }}
            QPlainTextEdit {{ padding: 5px; }}
            QFrame#continuousCandidates, QTextBrowser#continuousPreview {{
                background: {theme['bg_color']};
                border: 1px solid {theme['border_color']};
                border-radius: 7px;
                color: {theme['text_color']};
                font-size: {self.settings.font_size}px;
            }}
            QListWidget::item:selected {{
                background: {theme['item_selected_bg']};
                color: {theme['item_selected_text']};
            }}
            QLabel {{ background: transparent; border: none; color: {theme['text_color']}; }}
            QPushButton {{
                background: {theme['input_bg_color']};
                color: {theme['text_color']};
                border: 1px solid {theme['border_color']};
                border-radius: 3px;
                padding: 4px;
            }}
            QPushButton:hover {{ background: {theme['item_hover_bg']}; }}
            QPushButton:disabled {{ color: {theme['title_color']}; }}
        """)
        self._update_preview()
        self._position_candidates()

    def set_target_label(self, title):
        short_title = title if len(title) <= 12 else title[:11] + "…"
        self.send_button.setText(f"落字到 {short_title.replace('&', '&&')}" if title else "落字")
        target = f"落字到 {title}" if title else "落字（尚未确认目标窗口）"
        self.send_button.setToolTip(f"<qt>{html.escape(target)} · Ctrl+Enter</qt>")

    def focus_editor(self):
        self.editor.setFocus()

    def has_draft(self):
        return not self.editor.document().isEmpty()

    def set_submission_pending(self, pending):
        self._pending = pending
        self.editor.setReadOnly(pending)
        self.dismiss_candidates()
        self._update_footer()

    def complete_submission(self, snapshot):
        self._last_draft = snapshot
        if self.editor.toPlainText() == snapshot:
            self.editor.clear()
        self._update_footer()

    def _recover(self):
        if not self.has_draft() and self._last_draft:
            self.editor.setPlainText(self._last_draft)
            self.editor.moveCursor(QTextCursor.End)
            self.focus_editor()

    def _draft_changed(self):
        self.dismiss_candidates()
        self._update_footer()

    def _update_footer(self):
        enabled = not self._pending and self.has_draft()
        self.send_button.setEnabled(enabled)
        self.copy_button.setEnabled(enabled)
        self.lookup_button.setEnabled(not self._pending)
        self.recover_button.setVisible(bool(self._last_draft) and not self.has_draft())
        self.recover_button.setEnabled(not self._pending)

    def _submit(self):
        if not self._pending and not self._manual and self._ime_widget is None:
            text = self.editor.toPlainText()
            if text.strip():
                self.dismiss_candidates()
                self.submit_requested.emit(text)

    def _copy(self):
        if not self._pending and self._ime_widget is None and self.has_draft():
            self.dismiss_candidates()
            self.copy_requested.emit(self.editor.toPlainText())

    def dismiss_candidates(self):
        was_open = not self.candidates.isHidden()
        self._timer.stop()
        self._manual = False
        self._replacement = None
        self.candidates.hide()
        self.preview.hide()
        return was_open

    def hideEvent(self, event):
        self._after_input.stop()
        self.dismiss_candidates()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._ime_widget is not None:
            event.accept()
            return
        super().keyPressEvent(event)

    def _index_entries(self):
        blocks = self.word_manager.word_blocks
        if blocks is self._source_blocks:
            return
        prefixes = []
        shortcuts = {}
        for index, block in enumerate(blocks):
            code = (block.get("shortcut_code") or "").casefold()
            if code:
                shortcuts.setdefault(code, []).append(block)
            terms = {block["parent"], *block.get("aliases", []), code}
            prefixes.extend((key.casefold(), index) for key in terms if key)
        prefixes.sort()
        self._prefix_keys = [key for key, _ in prefixes]
        self._prefix_blocks = [blocks[index] for _, index in prefixes]
        self._shortcuts = shortcuts
        self._source_blocks = blocks

    def _prefix_matches(self, query):
        key = query.casefold()
        index = bisect.bisect_left(self._prefix_keys, key)
        matches = []
        seen = set()
        while index < len(self._prefix_keys) and self._prefix_keys[index].startswith(key):
            block = self._prefix_blocks[index]
            if id(block) not in seen:
                title = block["parent"]
                start = block["full_content"].find(title)
                hits = set(range(start, start + len(query))) if start >= 0 and title.casefold().startswith(key) else set()
                matches.append({**block, "highlight_groups": {0: hits}})
                seen.add(id(block))
            index += 1
        return matches

    def _automatic_match(self, cursor):
        # Qt offsets count UTF-16 units. Limit the examined context even on a long line.
        position = cursor.position()
        start = max(cursor.block().position(), position - 65)
        context = QTextCursor(cursor)
        context.setPosition(start)
        context.setPosition(position, QTextCursor.KeepAnchor)
        before = context.selectedText()
        context.setPosition(position)
        context.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        after = context.selectedText()
        following_latin = bool(_LATIN_CHAR.fullmatch(after))

        self._index_entries()
        if self._shortcuts:
            for length in range(min(64, len(before)), 0, -1):
                fragment = before[-length:]
                matches = self._shortcuts.get(fragment.casefold())
                inside_word_left = (
                    _LATIN_CHAR.fullmatch(fragment[0]) is not None and length < len(before)
                    and _LATIN_CHAR.fullmatch(before[-length - 1]) is not None
                )
                inside_word_right = following_latin and _LATIN_CHAR.fullmatch(fragment[-1]) is not None
                if matches and not (inside_word_left or inside_word_right):
                    return fragment, [self._highlight_shortcut(block, fragment) for block in matches]
        latin = _LATIN.search(before)
        if latin and not following_latin and len(latin[0]) <= 64:
            # Only search the unfinished text on this line, never the whole draft.
            phrase = _LATIN_PHRASE.search(before) if self.settings.multi_word_search else None
            if phrase and len(phrase[0]) <= 64:
                matches = self._find_matches(phrase[0])
                if matches:
                    return phrase[0], matches
            fragment = latin[0]
            matches = self._find_matches(fragment)
            return fragment, matches or self._prefix_matches(fragment)
        han = _HAN.search(before)
        if han:
            for index in range(len(han[0]) - 1):
                fragment = han[0][index:]
                matches = self._prefix_matches(fragment)
                if matches:
                    return fragment, matches
        return "", []

    def _find_matches(self, query):
        return self.word_manager.find_matches(
            query, self.settings.multi_word_search, self.settings.pinyin_initial_search
        )

    @staticmethod
    def _highlight_shortcut(block, fragment):
        title = block["full_content"].partition("\n")[0]
        annotation = re.search(r"``\s*k\s*[:：]\s*(" + re.escape(fragment) + r")\s*``", title, re.I)
        indices = set(range(*annotation.span(1))) if annotation else set()
        return {**block, "highlight_groups": {0: indices}}

    def _show_automatic(self):
        if self._manual or self._pending or self._ime_widget is not None or not self.editor.hasFocus():
            return
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            return
        fragment, matches = self._automatic_match(cursor)
        if not matches:
            self.dismiss_candidates()
            return
        end = cursor.position()
        start = end - len(fragment.encode("utf-16-le")) // 2
        self._replacement = (self.editor.document().revision(), start, end)
        self._render_results(fragment, matches)

    def _open_lookup(self):
        if self._pending or self._ime_widget is not None:
            return
        self._timer.stop()
        self._after_input.stop()
        cursor = self.editor.textCursor()
        self._replacement = (
            self.editor.document().revision(), cursor.selectionStart(), cursor.selectionEnd()
        )
        self._manual = True
        self.lookup.blockSignals(True)
        self.lookup.setText(cursor.selectedText().replace("\u2029", " "))
        self.lookup.blockSignals(False)
        self._update_lookup()
        self.lookup.setFocus()
        self.lookup.selectAll()

    def _update_lookup(self):
        if not self._manual or self._ime_widget is self.lookup:
            return
        query = self.lookup.text().strip()
        self._index_entries()
        matches = self._find_matches(query)
        exact = self._shortcuts.get(query.casefold(), [])
        if exact:
            seen = {id(block) for block in exact}
            matches = [self._highlight_shortcut(block, query) for block in exact] + [
                block for block in matches if id(block) not in seen
            ]
        _, start, end = self._replacement
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        self._render_results(cursor.selectedText(), matches)

    def _queue_lookup(self):
        if not self._manual:
            return
        self.results.clear()
        self.results.hide()
        self.range_label.setText("正在查找…" if self._ime_widget is None else "")
        self._position_candidates()
        self._timer.start()

    def _refresh_results(self):
        if self._manual:
            self._update_lookup()
        else:
            self._show_automatic()

    def refresh_candidates(self):
        if self._manual:
            self._update_lookup()
        elif not self.candidates.isHidden():
            if self.editor.hasFocus():
                self._show_automatic()
            else:
                self.dismiss_candidates()

    def _refresh_after_input(self):
        if self._ime_widget is not None:
            return
        if self._manual and self.lookup.hasFocus():
            self._update_lookup()
        elif self.editor.hasFocus():
            self._timer.start()

    def _render_results(self, fragment, matches):
        self.lookup.setVisible(self._manual)
        snippet = fragment.replace("\u2029", " ").replace("\t", " ")
        if len(snippet) > 18:
            snippet = snippet[:17] + "…"
        context = f"替换「{snippet}」" if snippet else "插入光标处"
        self.range_label.setText(f"{context} · Tab 插入" if matches else "没有匹配词条")
        self.results.clear()
        self._reserve_preview = any(
            self._needs_preview(block, min(360, self.editor.viewport().width())) for block in matches
        )
        for block in matches:
            item = QListWidgetItem(block["parent"])
            item.setData(Qt.UserRole, block)
            self.results.addItem(item)
        if matches:
            self.results.setCurrentRow(0)
        self.results.setVisible(bool(matches))
        self.candidates.show()
        self._update_preview()

    def _needs_preview(self, block, width):
        title, newline, _ = block["full_content"].partition("\n")
        if newline:
            return True
        padding = 20
        if getattr(self.settings, "show_source_enabled", False) and block.get("source_path"):
            padding += int(width * .34)
        return self.results.fontMetrics().horizontalAdvance(title.removeprefix("- ")) > width - padding

    def _update_preview(self, *_):
        item = self.results.currentItem()
        if self.candidates.isHidden() or item is None:
            self.preview.hide()
            return
        block = item.data(Qt.UserRole)
        bounds_width = self.editor.viewport().width()
        usable_width = bounds_width * 46 // 100 if self._reserve_preview and bounds_width >= 320 else bounds_width
        if self._needs_preview(block, min(360, usable_width)):
            text, leading, colors = presentation(block, self.settings)
            theme = THEMES[self.settings.theme]
            wrap = getattr(self.settings, "word_wrap_enabled", False)
            self.preview.setLineWrapMode(QTextBrowser.WidgetWidth if wrap else QTextBrowser.NoWrap)
            self.preview.setHtml(highlighted_html(text, colors, theme["text_color"], offset=leading, anchor=True))
            self.preview.show()
            self._position_candidates()
            if colors:
                self.preview.scrollToAnchor("match")
        else:
            self.preview.hide()
            self._position_candidates()

    def _position_candidates(self, *_):
        if self.candidates.isHidden() or self._replacement is None:
            return
        viewport = self.editor.viewport()
        bounds = viewport.geometry()
        has_preview = not self.preview.isHidden()
        side_by_side = self._reserve_preview and bounds.width() >= 320
        width = min(360, bounds.width() * 46 // 100 if side_by_side else bounds.width())
        if width <= 0:
            return
        if self.results.count():
            row = self.results.sizeHintForRow(0)
            height = min(_VISIBLE_ROWS, self.results.count()) * row + 2
            reserve = self.lookup.sizeHint().height() if self._manual else 0
            if has_preview and not side_by_side:
                reserve += min(90, bounds.height() // 2)
            self.results.setFixedHeight(min(height, max(row + 2, bounds.height() - reserve - 32)))
        height = self.candidates.sizeHint().height()
        caret = self.editor.cursorRect().translated(bounds.topLeft())
        gap = 6
        preview_width = min(420, bounds.width() - width - gap) if side_by_side else width
        total_width = width + gap + preview_width if side_by_side else width
        remaining = max(1, bounds.height() - height - gap)
        preview_height = min(320, bounds.height()) if side_by_side else min(240, remaining)
        total_height = height + gap + preview_height if has_preview and not side_by_side else height
        x = max(bounds.left(), min(caret.left(), bounds.right() - total_width + 1))
        below = caret.bottom() + 3
        y = below if below + total_height <= bounds.bottom() + 1 else caret.top() - total_height - 3
        y = max(bounds.top(), min(y, bounds.bottom() + 1 - total_height))
        self.candidates.setGeometry(x, y, width, height)
        self.candidates.raise_()
        if has_preview:
            if side_by_side:
                preview_x = x + width + gap
                preview_y = min(y, bounds.bottom() - preview_height + 1)
            else:
                preview_x, preview_y = x, y + height + gap
            self.preview.setGeometry(preview_x, preview_y, preview_width, preview_height)
            self.preview.raise_()

    def _accept(self, item):
        if self._ime_widget is not None or self._replacement is None or not self.expand_entry:
            return
        revision, start, end = self._replacement
        document = self.editor.document()
        if document.revision() != revision:
            self.dismiss_candidates()
            return
        block = item.data(Qt.UserRole)
        self.dismiss_candidates()
        self.focus_editor()
        expanded = self.expand_entry(block)
        if expanded is None or document.revision() != revision:
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(expanded)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.focus_editor()
        self.entry_inserted.emit(block)

    def eventFilter(self, watched, event):
        if watched not in (self.editor, self.lookup):
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind == QEvent.InputMethod:
            self._ime_widget = watched if event.preeditString() else None
            if watched is self.editor:
                self.dismiss_candidates()
                if event.commitString() and self._ime_widget is None:
                    self._after_input.start()
            elif self._ime_widget is self.lookup:
                self._queue_lookup()
            else:
                self._after_input.start()
        elif kind == QEvent.FocusOut and self._ime_widget is watched:
            self._ime_widget = None
        elif kind == QEvent.KeyPress and self._ime_widget is None:
            return self._handle_key(watched, event)
        return super().eventFilter(watched, event)

    def _handle_key(self, watched, event):
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key_Escape and modifiers == Qt.NoModifier:
            if self.dismiss_candidates():
                self.focus_editor()
            else:
                self.escape_requested.emit()
            return True
        if watched is self.editor and modifiers == Qt.ControlModifier:
            if key == Qt.Key_J:
                self._open_lookup()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._submit()
                return True
        if modifiers == Qt.NoModifier and not self.candidates.isHidden() and self.results.count():
            if key in (Qt.Key_Up, Qt.Key_Down):
                direction = -1 if key == Qt.Key_Up else 1
                self.results.setCurrentRow((self.results.currentRow() + direction) % self.results.count())
                return True
            if key == Qt.Key_Tab:
                self._accept(self.results.currentItem())
                return True
        if watched is self.editor and not event.matches(QKeySequence.Paste) and not modifiers & (
            Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier
        ):
            if event.text().isprintable() or key in (Qt.Key_Backspace, Qt.Key_Delete):
                # The editor updates its text and cursor synchronously after the filter.
                self._after_input.start()
        return False
