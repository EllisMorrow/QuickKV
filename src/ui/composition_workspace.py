
"""Composition page hosted in the existing QuickKV popup."""

from dataclasses import replace
import hashlib

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu,
    QFrame, QMessageBox, QPlainTextEdit, QTextBrowser, QVBoxLayout, QWidget,
)

from core.continuous_matcher import ContinuousMatcher
from core.composition.document import Choice, Document, Token, new_id, qt_offset
from core.composition.groups import Catalog, Option
from core.composition.store import CompositionStorageError, LibraryConflict, STORAGE_ERRORS
from ui.composition_editor import CompositionEditor, EditorState
from ui.composition_panel import CompositionPanel, button, fill_list
from ui.composition_workflows import Workflows


class MarkdownView(QTextBrowser):
    def loadResource(self, resource_type, name):
        # Rendering a draft must not fetch its linked or embedded content.
        return None


class CompositionWorkspace(QWidget, Workflows):
    escape_requested = Signal()
    submit_requested = Signal(str)
    copy_requested = Signal(str)
    external_reload_requested = Signal()
    entry_inserted = Signal(dict)
    library_changed = Signal()

    def __init__(self, manager, settings, parent=None):
        super().__init__(parent)
        self.manager, self.settings = manager, settings
        self.matcher = ContinuousMatcher(manager, settings)
        self.reference_blocks = None
        self.reference_index = {}
        self.automatic = False
        self.continuous_matches = ()
        self.continuous_range = None
        self.catalog = Catalog.defaults()
        self.factory = self.store = self.expand_entry = None
        self.initialized = self.dirty = self.loading = self.context_active = self.has_conflict = False
        self.session_id = self.catalog.selected_module
        self.revision = 0
        self.pending_insertion = None
        self.blocked_hover = ""
        self.hover_candidate = None
        self.group_history = []
        self.editor = CompositionEditor()
        self.editor.changed.connect(self.document_changed)
        self.editor.cursorPositionChanged.connect(self.cursor_changed)
        self.editor.updateRequest.connect(lambda *_: self.position_panel())
        self.editor.lookup_requested.connect(self.lookup)
        self.editor.submit_requested.connect(self.submit)
        self.editor.escape_requested.connect(self.escape)
        self.editor.context_requested.connect(self.context)
        self.editor.activate_requested.connect(self.activate_at)
        self.editor.hover_requested.connect(self.hover_at)
        self.editor.installEventFilter(self)
        self.editor.viewport().installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        tools = QHBoxLayout()
        self.quick_modules = button(tools, "模块 ▾", self.toggle_quick_modules)
        self.quick_modules.installEventFilter(self)
        self.quick_modules.setToolTip("快速切换模块")
        self.module_button = button(tools, "默认模块", self.choose_module)
        self.module_button.setMaximumWidth(175)
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setWindowOpacity(0.45)
        tools.addWidget(separator)
        self.template_button = button(tools, "片段", self.template_menu)
        self.reload_button = button(tools, "回载 ▾", self.reload_menu)
        tools.addStretch()
        self.hover = button(tools, "悬浮响应：关", self.save_hover)
        self.hover.setCheckable(True)
        self.more = button(tools, "···", self.more_menu)
        self.more.setAccessibleName("更多组合输入操作")
        layout.addLayout(tools)

        edits = QHBoxLayout()
        button(edits, "＋组合词", self.lookup).setToolTip("搜索组合词（Ctrl+J）")
        button(edits, "＋同级", lambda: self.editor.add_sibling())
        button(edits, "＋子级", lambda: self.editor.add_sibling(child=True))
        button(edits, "⇤", lambda: self.editor.indent_branch(False)).setToolTip("反缩进")
        button(edits, "⇥", lambda: self.editor.indent_branch(True)).setToolTip("缩进")
        button(edits, "↶", self.editor.undo).setToolTip("撤销")
        button(edits, "↷", self.editor.redo).setToolTip("重做")
        edits.addStretch()
        layout.addLayout(edits)
        layout.addWidget(self.editor, 1)

        self.preview_window = QDialog(self, Qt.Tool)
        self.preview_window.setWindowTitle("Markdown 预览（只读）")
        self.preview_window.resize(420, 345)
        self.preview_window.setAttribute(Qt.WA_ShowWithoutActivating)
        self.preview_window.setAttribute(Qt.WA_WindowPropagation)
        self.preview_window.finished.connect(lambda _: self.preview_button.setChecked(False))
        preview_layout = QVBoxLayout(self.preview_window)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Markdown 输出预览")
        preview_options = QHBoxLayout()
        self.preview_source = button(preview_options, "源码", self.select_preview_view)
        self.preview_source.setCheckable(True)
        self.preview_source.setChecked(True)
        preview_options.addStretch()
        preview_layout.addLayout(preview_options)
        self.markdown_view = MarkdownView()
        self.markdown_view.setOpenLinks(False)
        self.markdown_view.setOpenExternalLinks(False)
        preview_layout.addWidget(self.markdown_view)
        preview_layout.addWidget(self.preview)
        self.select_preview_view()
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.hide()
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.preview_button = button(footer, "Markdown 预览", self.toggle_preview)
        self.preview_button.setCheckable(True)
        footer.addStretch()
        button(footer, "复制整段", self.copy)
        self.submit_button = button(footer, "落字到…", self.submit)
        self.submit_button.setToolTip("落字（Ctrl+Enter）")
        self.submit_button.setObjectName("compositionSubmit")
        layout.addLayout(footer)

        self.panel = CompositionPanel(self)
        self.autosave = self.timer(350, self.flush)
        self.max_save_delay = self.timer(2000, self.flush)
        self.status_timer = self.timer(4500, self.status.hide)
        self.candidate_hover_timer = self.timer(150, self.open_candidate_hover)
        self.candidate_leave_timer = self.timer(320, self.close_candidate_hover)
        self.hover_timer = self.timer(90, self.open_hover_candidate)
        self.continuous_timer = self.timer(170, self.show_continuous)
        self.hover_leave = self.timer(330, self.close_hover)
        self.panel.installEventFilter(self)
        self.setup_workflows()

    def timer(self, interval, callback):
        timer = QTimer(self, interval=interval, singleShot=True)
        timer.timeout.connect(callback)
        return timer

    def initialize(self):
        if self.initialized:
            return
        self.initialized = True
        if self.factory is None:
            return
        try:
            self.store = self.factory()
            try:
                self.store.recover_library()
            except LibraryConflict:
                self.has_conflict = True
            self.catalog = Catalog.load(self.store.get("catalog"))
            self.hover.setChecked(bool(self.store.get("hover", False)))
            self.sync_hover()
            self.load_settings()
            self.load_session(self.store.get("active_session", self.catalog.selected_module))
            self.refresh_modules()
            if self.has_conflict:
                self.report("专用词库有未决冲突。草稿仍可编辑；请从更多菜单处理冲突。", persistent=True)
        except STORAGE_ERRORS:
            self.store = None
            self.report("无法打开组合输入数据，原数据已保留。当前编辑可复制。", persistent=True)

    def load_session(self, identity):
        self.loading = True
        try:
            if self.store:
                revision, model, anchor, cursor = self.store.load_draft(identity)
            else:
                revision, model, anchor, cursor = self.memory_sessions.get(identity, (0, Document(), 0, 0))
            self.session_id, self.revision = identity, revision
            self.editor.load_state(EditorState(model, anchor, cursor))
            self.dirty = False
            self.update_preview_output()
            self.submit_button.setText("替换或复制…" if identity.startswith("reload-") else "落字到…")
            self.refresh_annotations()
        finally:
            self.loading = False

    def focus_editor(self):
        self.initialize()
        self.editor.setFocus()

    def showEvent(self, event):
        super().showEvent(event)
        self.initialize()

    def hideEvent(self, event):
        if getattr(self, "candidate_dialog", None):
            self.candidate_dialog.close()
        if getattr(self, "quick_module_window", None):
            self.quick_module_window.hide()
        self.dismiss_panel()
        self.preview_window.hide()
        self.preview_button.setChecked(False)
        center = getattr(self, "template_center", None)
        if center is not None:
            center.hide()
        self.flush()
        super().hideEvent(event)

    def document_changed(self):
        if self.loading:
            return
        self.schedule_save()
        if self.preview_window.isVisible():
            self.update_preview_output()
        if self.panel.token and not self.pending_insertion:
            current = self.find_token(self.panel.token.id)
            if current:
                self.panel.update_token(current)
            elif not self.panel.mode.startswith("config"):
                self.dismiss_panel(block_hover=True)
        self.refresh_annotations()
        if self.automatic:
            self.continuous_timer.start()

    def cursor_changed(self):
        if self.loading or self.editor._rendering:
            return
        if self.panel.token and not self.pending_insertion and self.editor.hasFocus():
            _, caret = self.editor.offsets()
            if not self.panel.token.start <= caret <= self.panel.token.end:
                self.dismiss_panel()
        if self.store and self.initialized:
            self.schedule_save()
        if (not self.panel.isVisible() and self.editor.hasFocus() and not self.editor.composing and
                not self.editor.textCursor().hasSelection() and QApplication.mouseButtons() == Qt.NoButton):
            item = self.editor.model.token_at(self.editor.offsets()[1])
            if item and item.id != self.blocked_hover:
                self.panel.open_token(item)

    def schedule_save(self):
        self.dirty = True
        self.autosave.start()
        if not self.max_save_delay.isActive():
            self.max_save_delay.start()

    def flush(self):
        center = getattr(self, "template_center", None)
        if center and not center.flush_fragment():
            return False
        self.autosave.stop()
        self.max_save_delay.stop()
        if not self.dirty:
            return True
        state = self.editor.capture()
        try:
            revision = self.revision + 1
            if self.store:
                self.store.save_draft(self.session_id, revision, state.document, state.anchor, state.cursor)
            elif self.factory is None:
                self.memory_sessions[self.session_id] = (revision, state.document, state.anchor, state.cursor)
            else:
                raise CompositionStorageError("草稿存储不可用")
        except STORAGE_ERRORS:
            self.report("草稿保存失败，内容仍在编辑器。请重试或复制备份。", persistent=True)
            return False
        self.revision, self.dirty = revision, False
        return True

    def save_record(self, key, value):
        try:
            if self.store:
                self.store.set(key, value)
            elif self.factory is not None:
                raise CompositionStorageError("配置无法保存")
            return True
        except STORAGE_ERRORS:
            self.report("组合输入设置未保存", persistent=True)
            return False

    def save_catalog(self, catalog, *, word=None, words=(), active_session=None, new_draft=None, draft_update=None):
        try:
            catalog.validate()
            records = {"catalog": catalog.to_record()}
            existing = {group.id for group in catalog.groups}
            favorites = {k: [identity for identity in values if identity in existing]
                         for k, values in self.group_favorites.items()}
            if favorites != self.group_favorites:
                records["group_favorites"] = favorites
            if active_session is not None:
                records["active_session"] = active_session
            if draft_update is not None:
                document, cursor = draft_update
                document.validate()
                records["__draft__"] = (self.session_id, self.revision + 1, {
                    "document": document.to_record(), "anchor": cursor, "cursor": cursor,
                })
            if self.store:
                if new_draft is not None:
                    self.store.save_draft(active_session, 1, new_draft, 0, 0, records=records)
                elif words:
                    self.store.add_words(words, records)
                elif word is None:
                    self.store.set_records(records)
                else:
                    self.store.add_word(word, records)
            elif self.factory is not None:
                raise CompositionStorageError("配置无法保存")
        except LibraryConflict:
            self.has_conflict = True
            self.report("专用词库出现写入冲突，请在更多菜单检查。", persistent=True)
            return False
        except STORAGE_ERRORS:
            self.report("词组配置未保存。请检查必填项、词库格式或文件冲突。", persistent=True)
            return False
        if self.catalog != catalog:
            self.group_history.append(self.catalog)
            self.group_history[:] = self.group_history[-40:]
        self.catalog = catalog
        self.group_favorites = favorites
        if draft_update is not None:
            self.revision += 1
        self.refresh_modules()
        if word is not None or words:
            self.library_changed.emit()
        return True

    def undo_catalog(self):
        if self.group_history:
            previous = self.group_history[-1]
            previous = replace(previous, selected_module=self.catalog.selected_module)
            if not any(m.id == previous.selected_module for m in previous.modules):
                return
            count = len(self.group_history)
            if self.save_catalog(previous):
                del self.group_history[count - 1:]
                if self.panel.isVisible():
                    self.panel.rebuild_timer.start()

    def save_hover(self):
        checked = self.hover.isChecked()
        if not self.save_record("hover", checked):
            self.hover.setChecked(not checked)
        self.sync_hover()

    def sync_hover(self):
        self.hover.setText("悬浮响应：开" if self.hover.isChecked() else "悬浮响应：关")

    def hover_enabled(self):
        return self.hover.isChecked() and not self.editor.composing and not self.context_active and not (
            self.editor.textCursor().hasSelection() or QApplication.mouseButtons()
        )

    def report(self, text, *, persistent=False):
        self.status.setText(text)
        self.status.show()
        self.status_timer.stop()
        if not persistent:
            self.status_timer.start()

    def confirm(self, text):
        return QMessageBox.question(self, "组合输入", text,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def copy(self):
        if not self.editor.composing and self.output() != "":
            self.copy_requested.emit(self.output())

    def submit(self):
        if not self.editor.composing and self.output() != "":
            self.flush()
            self.submit_requested.emit(self.output())

    def select_preview_view(self):
        source = self.preview_source.isChecked()
        self.preview.setVisible(source)
        self.markdown_view.setVisible(not source)

    def update_preview_output(self):
        output = self.output()
        if output == getattr(self, "_preview_output", None):
            return
        self._preview_output = output
        self.preview.setPlainText(output)
        flags = QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML
        document = self.markdown_view.document()
        document.setMarkdown(output, flags)
        block = document.begin()
        while block.isValid():
            if block.textList():
                cursor = QTextCursor(block)
                formatting = block.blockFormat()
                formatting.setTopMargin(min(2, formatting.topMargin()))
                formatting.setBottomMargin(min(2, formatting.bottomMargin()))
                cursor.setBlockFormat(formatting)
            block = block.next()

    def toggle_preview(self):
        visible = self.preview_button.isChecked()
        if visible:
            self.update_preview_output()
        if visible:
            area = self.screen().availableGeometry()
            if not getattr(self, "_preview_positioned", False) or not area.contains(self.preview_window.geometry()):
                self.preview_window.resize(min(420, area.width()), min(345, area.height()))
                self.preview_window.move(min(self.window().geometry().right(),
                                             area.right() - self.preview_window.width() + 1),
                                         max(area.top(), min(self.window().y(),
                                             area.bottom() - self.preview_window.height() + 1)))
            self._preview_positioned = True
        self.preview_window.setVisible(visible)

    def escape(self):
        if self.panel.isVisible():
            self.dismiss_panel(block_hover=True)
        else:
            self.escape_requested.emit()

    def dismiss_panel(self, block_hover=False):
        if block_hover and self.panel.token:
            self.blocked_hover = self.panel.token.id
        self.hover_timer.stop()
        self.hover_leave.stop()
        self.continuous_timer.stop()
        self.continuous_range = None
        self.pending_insertion = None
        self.panel.reset()

    def position_panel(self):
        if not self.panel.isVisible() or not self.panel.token:
            return
        start = self.panel.token.start
        if start > len(self.editor.model.text):
            self.dismiss_panel()
            return
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(qt_offset(self.editor.model.text, start))
        rectangle = self.editor.cursorRect(cursor)
        viewport = self.editor.viewport()
        if not viewport.rect().intersects(rectangle):
            self.dismiss_panel()
            return
        panel = self.panel
        anchor = viewport.mapToGlobal(rectangle.bottomLeft())
        screen = QApplication.screenAt(anchor) or self.screen()
        area = screen.availableGeometry()
        if panel.mode.startswith("config-"):
            preferred = 445 if panel.compact_config else 570
        elif panel.mode == "main" and not panel.search_preview.isHidden():
            preferred = 570
        else:
            preferred = 335 if not panel.mode else 445
        width = min(area.width(), preferred)
        configuration = panel.mode.startswith("config-") and not panel.compact_config
        measure_width = max(1, width - 30)
        content = panel.body_layout.heightForWidth(measure_width)
        if content < 0:
            content = panel.body_layout.sizeHint().height()
        chrome = panel.header.sizeHint().height() + 16
        if not panel.preview.isHidden():
            chrome += min(48, panel.preview.heightForWidth(measure_width)) + 4
        height = chrome if not panel.mode else max(300 if configuration else 110, chrome + content + 12)
        height = min(height, area.height(), 560 if configuration else 430)
        if panel.anchor is None or panel.anchor[0] != anchor:
            y = anchor.y()
            if y + min(420, area.height()) > area.bottom() + 1:
                y = max(area.top(), area.bottom() - min(420, area.height()) + 1)
            panel.anchor = (anchor, y)
        x = max(area.left(), min(anchor.x(), area.right() - width + 1))
        y = min(panel.anchor[1], area.bottom() - height + 1)
        panel.setGeometry(x, max(area.top(), y), width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_panel()
        if hasattr(self, "indicator_timer"):
            self.indicator_timer.start()

    def eventFilter(self, watched, event):
        candidate = getattr(self, "candidate_dialog", None)
        if candidate is not None:
            if watched is candidate and event.type() == QEvent.Hide:
                self.candidate_dialog = None
                self.update_popup_event_filter(excluding=candidate)
            elif candidate.isVisible() and isinstance(watched, QWidget):
                inside = watched is candidate or candidate.isAncestorOf(watched)
                if event.type() == QEvent.MouseButtonPress:
                    if inside:
                        self._candidate_hover = False
                    else:
                        QTimer.singleShot(0, candidate, candidate.close)
                elif event.type() == QEvent.Enter and inside:
                    self.candidate_leave_timer.stop()
                elif event.type() == QEvent.Leave and watched is candidate and self._candidate_hover:
                    self.candidate_leave_timer.start()
        if watched is self.editor.viewport() and event.type() == QEvent.Leave:
            self.candidate_hover_timer.stop()
            self.candidate_leave_timer.start()
        panel = getattr(self, "quick_module_window", None)
        if panel:
            if watched is panel and event.type() == QEvent.Hide:
                self.update_popup_event_filter(excluding=panel)
            elif (panel.isVisible() and event.type() == QEvent.MouseButtonPress
                  and isinstance(watched, QWidget) and watched not in (panel, self.quick_modules)
                  and not panel.isAncestorOf(watched) and not getattr(self, "_quick_menu_action", False)):
                QTimer.singleShot(0, panel, panel.hide)
        if watched is self.quick_modules:
            if event.type() == QEvent.Enter:
                self._module_keyboard = False
                if not getattr(self, "_quick_hover_blocked", False):
                    self.open_quick_modules()
            elif event.type() == QEvent.Leave:
                self._quick_hover_blocked = False
                if getattr(self, "quick_module_window", None):
                    self.quick_module_close.start()
            elif event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Down):
                self._module_keyboard = True
                self.open_quick_modules()
                self.quick_module_window.activateWindow()
                item = self.quick_module_chips.itemAt(0)
                if item:
                    item.widget().setFocus()
                return True
            elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                if getattr(self, "quick_module_window", None) and self.quick_module_window.isVisible():
                    self.quick_module_window.hide()
                    return True
        if watched is self.editor.viewport() and event.type() in (QEvent.Resize, QEvent.FontChange):
            if hasattr(self, "indicator_timer"):
                self.indicator_timer.start()
        if (watched is self.editor and event.type() == QEvent.KeyPress and not self.editor.composing
                and self.panel.isVisible() and self.panel.mode == "suggestions"
                and event.modifiers() == Qt.NoModifier):
            listing = self.panel.listing
            if event.key() == Qt.Key_Tab and listing.currentItem():
                self.accept_continuous(listing.currentItem().data(Qt.UserRole))
                return True
            if event.key() in (Qt.Key_Up, Qt.Key_Down) and listing.count():
                amount = -1 if event.key() == Qt.Key_Up else 1
                listing.setCurrentRow((listing.currentRow() + amount) % listing.count())
                return True
        if event.type() == QEvent.Enter:
            if watched is self.panel:
                self.hover_leave.stop()
            elif watched.property("expandCompositionGroups"):
                self.panel.expand_groups()
        elif event.type() == QEvent.Leave and watched in (self.editor.viewport(), self.panel):
            if not self.panel.locked:
                self.hover_leave.start()
        return super().eventFilter(watched, event)

    def open_candidate_hover(self):
        pos = getattr(self, "_candidate_hover_position", None)
        if (pos is not None and self.hover_enabled() and not self.context_active
                and not self.editor.composing and not self.editor.textCursor().hasSelection()):
            self.activate_candidate(pos, hover=True)

    def hover_at(self, position):
        if not self.hover_enabled() or self.panel.locked or self.context_active or self.editor.composing:
            return
        candidate = self.candidate_at(position)
        if candidate and not self.editor.textCursor().hasSelection():
            self.candidate_leave_timer.stop()
            if getattr(self, "_shown_candidate", None) != candidate or not getattr(self, "candidate_dialog", None):
                self._candidate_hover_position = position
                if not self.candidate_hover_timer.isActive():
                    self.candidate_hover_timer.start()
        else:
            self.candidate_hover_timer.stop()
            if getattr(self, "_candidate_hover", False) and not self.candidate_leave_timer.isActive():
                self.candidate_leave_timer.start()
        if not self.hover_enabled() or self.panel.locked:
            return
        token = self.editor.model.token_at(position)
        if not token or token.id != self.blocked_hover:
            self.blocked_hover = ""
        if not token:
            self.hover_timer.stop()
            if not self.hover_leave.isActive():
                self.hover_leave.start()
        elif token.id != self.blocked_hover:
            self.hover_leave.stop()
            self.hover_candidate = token.id
            if not self.hover_timer.isActive():
                self.hover_timer.start()

    def open_hover_candidate(self):
        if self.hover_enabled() and not self.panel.locked:
            token = self.find_token(self.hover_candidate)
            if token:
                self.panel.open_token(token)

    def close_hover(self):
        if not self.panel.locked and not self.panel.underMouse():
            self.dismiss_panel()

    def find_token(self, identity):
        return next((t for t in self.editor.model.tokens if t.id == identity), None)

    def activate_at(self, position):
        self.blocked_hover = ""
        token = next((item for item in self.editor.model.tokens if item.start <= position < item.end), None)
        if token:
            self.pending_insertion = None
            self.panel.open_token(token, mode=token.direction_at(position), locked=True)
        else:
            self.dismiss_panel()
            self.activate_candidate(position)

    def matching_group_options(self, text):
        return tuple((group, option) for group in self.catalog.groups for option in group.values()
                     if not option.pattern and option.text == text)

    def lookup(self, position=None, *, attach=False):
        if self.editor.composing:
            return
        start, end = sorted(self.editor.offsets())
        if position is not None and start == end:
            start = end = position
        token = self.editor.model.token_at(start) if start == end else None
        if token:
            self.panel.open_token(token, mode="main", locked=True, focus=True)
            return
        literal = self.editor.model.text[start:end] if attach else ""
        if "\n" in literal:
            self.report("组合词须在同一行内")
            return
        self.pending_insertion = (self.editor.model, start, end, attach)
        matches = self.matching_group_options(literal) if literal else ()
        owners = {group.id: (group, option) for group, option in matches}
        initial = Choice(literal)
        if len(owners) == 1:
            group, option = next(iter(owners.values()))
            initial = group.choice(option)
        self.panel.open_token(Token(start, end, literal, initial), mode="main", locked=True, focus=True)
        if literal:
            self.panel.query.setText(literal)

    def apply_token(self, token, *, before_apply=None, catalog_update=None):
        try:
            if self.pending_insertion:
                model, start, end, attach = self.pending_insertion
                if model != self.editor.model:
                    return False
                if attach:
                    updated = model.attach(replace(token, start=start, end=end, literal=model.text[start:end]))
                    new_position = end
                else:
                    updated = model.replace_token(replace(token, start=start, end=end))
                    new_position = start + len(token.render())
            else:
                original = self.find_token(token.id)
                if original is None:
                    return False
                model = self.editor.model
                if token.render() == original.render():
                    updated = model.detach(original.id).attach(
                        replace(token, start=original.start, end=original.end, literal=original.literal))
                    new_position = original.end
                else:
                    updated = model.replace_token(replace(token, start=original.start, end=original.end))
                    new_position = original.start + len(token.render())
            if catalog_update is not None:
                catalog, words = catalog_update
                if not self.save_catalog(catalog, words=words, draft_update=(updated, new_position)):
                    return False
            if before_apply is not None and not before_apply():
                return False
            self.pending_insertion = None
            self.editor.apply(updated, new_position)
            current = self.find_token(token.id)
            if current and self.panel.isVisible():
                self.panel.update_token(current)
            return True
        except ValueError:
            self.report("组合词内容或选区已改变")
            return False

    def show_continuous(self):
        if (not self.automatic or not self.editor.hasFocus() or self.editor.composing
                or self.editor.textCursor().hasSelection() or self.panel.locked or self.context_active):
            return
        pos = self.editor.offsets()[1]
        document = self.editor.model
        if document.token_at(pos):
            return
        line_start = document.nodes[document.line_index(pos)].start
        fragment, matches = self.matcher.automatic_match(
            document.text[max(line_start, pos - 65):pos], document.text[pos:pos + 1]
        )
        self.continuous_matches = tuple(entry for entry in matches if not entry.get("is_clipboard"))[:12]
        if not fragment or not self.continuous_matches:
            if self.panel.mode == "suggestions":
                self.dismiss_panel()
            return
        start = pos - len(fragment)
        self.continuous_range = (document, start, pos)
        self.panel.open_token(Token(start, pos, fragment, Choice(fragment)), mode="suggestions")

    def accept_continuous(self, entry):
        if (not self.continuous_range or self.editor.composing
                or self.continuous_range[0] is not self.editor.model):
            return
        document, start, end = self.continuous_range
        if self.editor.offsets() != (end, end):
            self.dismiss_panel()
            return
        self.pending_insertion = (document, start, end, False)
        self.insert_entry(entry)

    def search_library(self, query):
        if not query:
            return []
        return [entry for entry in self.matcher.lookup(query) if not entry.get("is_clipboard") and self.entry_text(entry)][:80]

    @staticmethod
    def entry_text(entry):
        text = entry.get("parent") or entry.get("full_content", "").removeprefix("- ").split("\n", 1)[0]
        return text.strip()

    def matching_group_names(self, query, groups):
        direct = {group.id for group in groups if query.casefold() in group.name.casefold()}
        manager = self.manager
        if not hasattr(manager, "_preprocess_block"):
            return direct
        candidates = [manager._preprocess_block({"parent": group.name, "full_content": group.name,
                                                  "source_path": "", "group_identity": group.id})
                      for group in groups]
        matched = manager.find_matches(query, self.settings.multi_word_search,
                                       self.settings.pinyin_initial_search, candidates=candidates)
        return direct | {block["group_identity"] for block in matched}

    def group_matches(self, query, groups, *, highlights=None):
        manager = self.manager
        if not hasattr(manager, "_preprocess_block"):
            return [(group, option) for group in groups for option in group.values()
                    if query.casefold() in option.text.casefold()]
        if getattr(self, "_group_search_catalog", None) is not self.catalog.groups:
            self._group_search_blocks = {
                group.id: [manager._preprocess_block({
                    "parent": option.text, "full_content": option.text,
                    "source_path": option.source, "composition_option": (group, option),
                }) for option in group.values() if not option.pattern]
                for group in self.catalog.groups
            }
            self._group_search_catalog = self.catalog.groups
        candidates = [block for group in groups for block in self._group_search_blocks[group.id]]
        matches = manager.find_matches(
            query, self.settings.multi_word_search, self.settings.pinyin_initial_search, candidates=candidates)
        if highlights is not None:
            highlights.update((block["composition_option"][1].text, block.get("highlight_groups", {}))
                              for block in matches)
        return [block["composition_option"] for block in matches]

    def search_options(self, group, query, *, highlights=None):
        options = group.values()
        if not query:
            return options
        by_identity = {(o.source, o.text): o for o in options if o.source}
        linked = [by_identity[key] for entry in self.search_library(query)
                  if (key := (entry.get("source_path", ""), self.entry_text(entry))) in by_identity]
        ranked = [o for _, o in self.group_matches(query, (group,), highlights=highlights)]
        return tuple(dict.fromkeys((*linked, *ranked, *(o for o in options if query.casefold() in o.text.casefold()
                                               or query in o.value()))))

    def referenced_option(self, entry, parent):
        return Option(self.entry_text(entry), parent=parent, source=entry.get("source_path", ""),
                      fingerprint=hashlib.sha256(entry["full_content"].encode("utf8")).hexdigest())

    def reference_status(self, option):
        if not option.source:
            return ""
        blocks = getattr(self.manager, "word_blocks", ())
        if self.reference_blocks is not blocks:
            self.reference_blocks, self.reference_index = blocks, {}
            for entry in blocks:
                if entry.get("source_path"):
                    key = (entry["source_path"], self.entry_text(entry))
                    self.reference_index.setdefault(key, []).append(entry)
        found = self.reference_index.get((option.source, option.text), ())
        if len(found) == 1 and hashlib.sha256(found[0]["full_content"].encode("utf8")).hexdigest() == option.fingerprint:
            return ""
        return "来源暂不可匹配，沿用已保存的文字"

    def insert_entry(self, entry):
        text = self.expand_entry(entry) if self.expand_entry else entry.get("full_content", self.entry_text(entry))
        pending = self.pending_insertion
        if text is None or pending and pending[0] != self.editor.model:
            return
        if pending:
            self.editor.select_offsets(pending[1], pending[2])
        self.dismiss_panel(block_hover=True)
        self.editor.replace_selection(text)
        self.editor.setFocus()
        self.entry_inserted.emit(entry)

    def refresh_lookup(self):
        if self.panel.isVisible() and self.panel.mode == "main":
            self.panel.fill_main()

    def pick(self, title, options, query="", supply=None):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(350, 240)
        layout = QVBoxLayout(dialog)
        search = QLineEdit(query)
        search.setPlaceholderText("搜索…")
        results = QListWidget()
        layout.addWidget(search)
        layout.addWidget(results)
        values = {}

        def refresh():
            values.clear()
            typed = search.text().strip()
            displayed = supply(typed) if supply else (
                (identity, label, value) for identity, label, value in options
                if typed.casefold() in label.casefold()
            )
            rows = []
            for identity, label, value in displayed:
                values[identity] = value
                rows.append((identity, label))
            fill_list(results, rows)
            if rows:
                results.setCurrentRow(0)

        search.textChanged.connect(refresh)
        search.returnPressed.connect(dialog.accept)
        results.itemActivated.connect(dialog.accept)
        button(layout, "选择", dialog.accept)
        refresh()
        if dialog.exec() and results.currentItem():
            return values.get(results.currentItem().data(Qt.UserRole))
        return None

    def pick_group(self, title, groups):
        return self.pick(title, [(g.id, g.name, g) for g in groups])

    def pick_entry(self, query):
        return self.pick("引用词库中的词汇", (), query, lambda value: (
            (str(index), self.entry_text(entry), entry)
            for index, entry in enumerate(self.search_library(value))
        ))

    def more_menu(self):
        menu = QMenu(self)
        menu.addAction("管理词组", self.manage_groups)
        menu.addAction("撤销最近词组配置", self.undo_catalog)
        menu.addAction("立即保存草稿", self.flush)
        menu.addSeparator()
        self.add_workflow_actions(menu)
        menu.exec(self.more.mapToGlobal(QPoint(0, self.more.height())))

    def manage_groups(self):
        pos = self.editor.offsets()[1]
        token = self.editor.model.token_at(pos) or Token(pos, pos, "", Choice(""))
        self.panel.open_token(token, mode="config-main", locked=True)

    def context(self, global_position, position):
        self.context_active = True
        self.hover_timer.stop()
        try:
            menu = QMenu(self)
            token = self.editor.model.token_at(position)
            if token and not self.editor.textCursor().hasSelection():
                for side, label in (("prefixes", "编辑前缀"), ("main", "编辑主词"), ("suffixes", "编辑后缀")):
                    menu.addAction(label, lambda _checked=False, mode=side: self.panel.open_token(
                        token, mode=mode, locked=True, focus=mode == "main"))
                menu.addAction("转为普通文字", lambda: self.editor.apply(self.editor.model.detach(token.id)))
                menu.addAction("复制组合词", lambda: self.copy_requested.emit(token.literal))
                menu.addAction("删除组合词", lambda: self.editor.apply(
                    self.editor.model.splice(token.start, token.end, ""), token.start))
            else:
                menu.addAction("插入组合词", lambda: self.lookup(position))
                if self.editor.textCursor().hasSelection():
                    menu.addAction("将选区建立为组合词", lambda: self.lookup(attach=True))
            self.add_reload_actions(menu, position)
            menu.addSeparator()
            self.editor.editing_actions(menu)
            menu.addSeparator()
            index = self.editor.current_line(position)
            outline = menu.addMenu("大纲操作")
            for title, action in (
                ("新建同级", lambda: self.editor.add_sibling(index=index)),
                ("新建子级", lambda: self.editor.add_sibling(index=index, child=True)),
                ("新建父级", lambda: self.editor.add_parent(index)),
                ("缩进", lambda: self.editor.indent_branch(True, index)),
                ("反缩进", lambda: self.editor.indent_branch(False, index)),
                ("折叠 / 展开", lambda: self.editor.toggle_fold(index)),
                ("上移分支", lambda: self.editor.move_branch(index, -1)),
                ("下移分支", lambda: self.editor.move_branch(index, 1)),
                ("复制分支到剪贴板", lambda: self.copy_requested.emit(
                    self.editor.model.slice(*self.editor.model.branch_bounds(index)))),
                ("复制成新分支", lambda: self.editor.duplicate_branch(index)),
                ("删除分支", lambda: self.editor.delete_branch(index)),
            ):
                outline.addAction(title, action)
            self.add_outline_actions(outline, index)
            menu.exec(global_position)
        finally:
            self.context_active = False

    def apply_theme(self):
        from core.config import THEMES
        theme = THEMES[self.settings.theme]
        text, bg, border = theme["text_color"], theme["input_bg_color"], theme["border_color"]
        selected = theme["item_selected_bg"]
        self.setStyleSheet(f"""
            CompositionWorkspace {{ background: transparent; border: none; }}
            QLabel {{ color: {text}; background: transparent; border: none; }}
            QLabel[secondaryText="true"] {{ color: {theme["title_color"]}; font-size: {max(9, self.settings.font_size - 2)}px; }}
            QLabel[compositionContext="true"] {{ font-weight: bold; padding: 3px 0; }}
            QPushButton:focus, QToolButton:focus {{ border-color: {selected}; }}
            QPlainTextEdit, QTextBrowser, QLineEdit, QListWidget, QComboBox {{
                color: {text}; background: {bg}; border: 1px solid {border};
                font-size: {self.settings.font_size}px;
            }}
            QPushButton, QToolButton {{
                color: {text}; background: transparent; border: 1px solid {border};
                border-radius: 4px; padding: 3px 6px;
            }}
            QPushButton:hover, QPushButton:checked, QPushButton#compositionSubmit {{ background: {selected}; }}
            QListWidget::item {{ padding: 2px 5px; }}
            QListWidget::item:selected {{ background: {selected}; color: {text}; }}
            QPushButton[compositionPrimary="true"] {{ background: {selected}; font-weight: bold; }}
            QPushButton[observationState="recorded"] {{ background: {selected}; font-weight: bold; }}
            QPushButton[observationState="pending"] {{ border-style: dashed; }}
            QPushButton[observationState="unknown"] {{ border-width: 2px; }}
            QPushButton[observationState="na"] {{ background: {bg}; }}
            QFrame#compositionPanel QPushButton {{ border-color: transparent; }}
            QFrame#compositionPanel QPushButton[compositionChip="true"] {{ border-color: {border}; }}
            QFrame#compositionPanel QPushButton:hover {{ border-color: {border}; }}
            QFrame#compositionPanel QPushButton:focus {{ border-color: {selected}; }}
            QFrame#compositionPanel {{ background: {bg}; border: 1px solid {border}; border-radius: 6px; }}
            QLabel#compositionResult {{ color: {text}; border-top: 1px solid {border}; padding: 4px; }}
        """)
