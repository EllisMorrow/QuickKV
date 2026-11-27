
"""Native text input with a shared history for text and outline metadata."""

from collections import deque
from dataclasses import dataclass, replace
import time

from PySide6.QtCore import QEvent, QMimeData, Qt, Signal
from PySide6.QtGui import QAction, QFontMetricsF, QKeySequence, QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from core.composition.document import Document, project, python_offset, qt_offset


@dataclass(frozen=True)
class EditorState:
    document: Document
    anchor: int = 0
    cursor: int = 0


class CompositionEditor(QPlainTextEdit):
    changed = Signal()
    pasted = Signal(int, int, bool)
    lookup_requested = Signal()
    submit_requested = Signal()
    escape_requested = Signal()
    context_requested = Signal(object, int)
    activate_requested = Signal(int)
    hover_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUndoRedoEnabled(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAccessibleName("组合输入正文")
        self.setPlaceholderText("输入正文，或按 Ctrl+J 插入组合词")
        self.state = EditorState(Document())
        self.undo_stack = deque()
        self.redo_stack = deque()
        self._undo_characters = 0
        self._rendering = False
        self._cursor_before_input = None
        self._typing = None
        self._has_folding = False
        self.composing = False
        self.recognize_paste = True
        self.document().contentsChange.connect(self._accept_native_change)
        self.update_tab_distance()

    def update_tab_distance(self):
        self.setTabStopDistance(4 * QFontMetricsF(self.font()).horizontalAdvance(" "))

    def event(self, event):
        handled = super().event(event)
        if event.type() in (QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.DevicePixelRatioChange, QEvent.ScreenChangeInternal):
            self.update_tab_distance()
        return handled

    @property
    def model(self):
        return self.state.document

    def toPlainText(self):
        return self.model.raw

    def offsets(self, cursor=None):
        cursor = self.textCursor() if cursor is None else cursor
        return tuple(python_offset(self.model.text, offset)
                     for offset in (cursor.anchor(), cursor.position()))

    def capture(self, cursor=None):
        anchor, position = self.offsets(cursor)
        return EditorState(self.model, anchor, position)

    def select_offsets(self, anchor, position=None):
        cursor = QTextCursor(self.document())
        cursor.setPosition(qt_offset(self.model.text, anchor))
        cursor.setPosition(qt_offset(self.model.text, anchor if position is None else position),
                           QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def load_state(self, state):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._undo_characters = 0
        self._typing = None
        self._show_state(state)

    def _show_state(self, state):
        self._rendering = True
        try:
            self.state = state
            native = self.document().toRawText().replace("\u2029", "\n")
            expected = state.document.text
            if native != expected:
                start = 0
                stop = min(len(native), len(expected))
                while start < stop and native[start] == expected[start]:
                    start += 1
                suffix = 0
                while suffix < stop - start and native[-suffix - 1] == expected[-suffix - 1]:
                    suffix += 1
                cursor = QTextCursor(self.document())
                cursor.setPosition(qt_offset(native, start))
                cursor.setPosition(qt_offset(native, len(native) - suffix), QTextCursor.KeepAnchor)
                cursor.insertText(expected[start:len(expected) - suffix])
            self.select_offsets(state.anchor, state.cursor)
            self.refresh_folding()
        finally:
            self._rendering = False

    def _remember(self, before):
        self.undo_stack.append(before)
        self._undo_characters += len(before.document.text)
        while len(self.undo_stack) > 1 and (len(self.undo_stack) > 150 or self._undo_characters > 8_000_000):
            self._undo_characters -= len(self.undo_stack.popleft().document.text)

    def apply(self, updated, position=None, anchor=None):
        before = self.capture()
        if updated == before.document:
            return
        position = min(before.cursor, len(updated.text)) if position is None else position
        self._remember(before)
        self.redo_stack.clear()
        self._typing = None
        self._show_state(EditorState(updated, position if anchor is None else anchor, position))
        self.changed.emit()

    def _accept_native_change(self, position, removed, added):
        if self._rendering or not (removed or added):
            return
        before = self.model
        native = self.document().toRawText().replace("\u2029", "\n")
        if native == before.text:
            return
        start = python_offset(before.text, position)
        end = python_offset(before.text, min(qt_offset(before.text, len(before.text)), position + removed))
        inserted_end = python_offset(native, min(qt_offset(native, len(native)), position + added))
        inserted = native[start:inserted_end]
        if removed and added and before.text[:start] + inserted + before.text[end:] == native:
            # Qt may report unchanged paragraph context along with a replacement.
            # Retain unchanged combination boundaries inside that context.
            old = before.text[start:end]
            prefix = 0
            while prefix < min(len(old), len(inserted)) and old[prefix] == inserted[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < min(len(old), len(inserted)) - prefix
                   and old[-suffix - 1] == inserted[-suffix - 1]):
                suffix += 1
            start += prefix
            end -= suffix
            inserted = inserted[prefix:len(inserted) - suffix]
        after = before.splice(start, end, inserted)
        if after.text != native:
            # Qt can include the implicit final paragraph in its notifications.
            # Reject an unexpected edit instead of guessing which metadata moved.
            self._show_state(self._cursor_before_input or self.state)
            return
        previous = self._cursor_before_input or EditorState(before, start, end)
        timestamp = time.monotonic()
        simple_typing = start == end and len(inserted) == 1 and inserted != "\n" and after.tokens == before.tokens
        if not (simple_typing and self._typing and self._typing[0] == start and timestamp - self._typing[1] < 0.75):
            self._remember(previous)
        self._typing = (inserted_end, timestamp) if simple_typing else None
        self.redo_stack.clear()
        self.state = EditorState(after, inserted_end, inserted_end)
        self.refresh_folding()
        self.changed.emit()

    def undo(self):
        if self.undo_stack and not self.composing:
            self._typing = None
            self.redo_stack.append(self.capture())
            old = self.undo_stack.pop()
            self._undo_characters -= len(old.document.text)
            self._show_state(old)
            self.changed.emit()

    def redo(self):
        if self.redo_stack and not self.composing:
            self._typing = None
            self._remember(self.capture())
            self._show_state(self.redo_stack.pop())
            self.changed.emit()

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        self._typing = None
        self._cursor_before_input = self.capture()
        try:
            super().inputMethodEvent(event)
        finally:
            self._cursor_before_input = None

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.composing = False
        self._typing = None

    def keyPressEvent(self, event):
        if not self.composing:
            key, mods = event.key(), event.modifiers()
            if event.matches(QKeySequence.Undo):
                self.undo()
                return
            if event.matches(QKeySequence.Redo):
                self.redo()
                return
            if key == Qt.Key_Escape:
                self.escape_requested.emit()
                return
            if mods == Qt.ControlModifier and key == Qt.Key_J:
                self.lookup_requested.emit()
                return
            if mods == Qt.ControlModifier and key in (Qt.Key_Return, Qt.Key_Enter):
                self.submit_requested.emit()
                return
            if (key, mods) in ((Qt.Key_Tab, Qt.NoModifier), (Qt.Key_Backtab, Qt.ShiftModifier)):
                self.indent_branch(key == Qt.Key_Tab)
                return
            if mods == Qt.NoModifier and key in (Qt.Key_Return, Qt.Key_Enter) and not self.textCursor().hasSelection():
                if self.add_sibling(at_cursor=True):
                    return
        self._cursor_before_input = self.capture()
        try:
            super().keyPressEvent(event)
        finally:
            self._cursor_before_input = None

    def replace_selection(self, text):
        start, end = sorted(self.offsets())
        self.apply(self.model.splice(start, end, text), start + len(project(text)))

    def insertFromMimeData(self, mime):
        if mime.hasText():
            first = min(self.offsets())
            self.replace_selection(mime.text())
            self.pasted.emit(first, first + len(project(mime.text())), self.recognize_paste)

    def paste_with_recognition(self):
        previous = self.recognize_paste
        self.recognize_paste = True
        try:
            self.paste()
        finally:
            self.recognize_paste = previous

    def paste_as_text(self):
        previous = self.recognize_paste
        self.recognize_paste = False
        try:
            self.paste()
        finally:
            self.recognize_paste = previous

    def createMimeDataFromSelection(self):
        mime = QMimeData()
        mime.setText(self.model.slice(*sorted(self.offsets())))
        return mime

    def dropEvent(self, event):
        if (event.source() not in (self, self.viewport()) or not self.textCursor().hasSelection()
                or not event.possibleActions() & Qt.CopyAction):
            return super().dropEvent(event)
        source_start, source_end = sorted(self.offsets())
        destination = self.offsets(self.cursorForPosition(event.position().toPoint()))[1]
        if source_start <= destination <= source_end:
            event.ignore()
            return
        current = self.model
        text, breaks, tokens, nodes = current.copy_fragment(source_start, source_end)
        entire_lines = source_start in {node.start for node in current.nodes} and text.endswith("\n")
        clone = event.dropAction() != Qt.MoveAction
        if not clone:
            current = current.splice(source_start, source_end, "")
            if destination > source_end:
                destination -= source_end - source_start
        if not entire_lines or destination not in {node.start for node in current.nodes}:
            nodes = ()
        self.apply(current.insert_fragment(destination, (text, breaks, tokens, nodes), clone=clone),
                   destination + len(text))
        # The model already moved the selection. Returning Qt's copy action keeps the
        # source editor's native post-drop handler from deleting it a second time.
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def contextMenuEvent(self, event):
        position = self.offsets(self.cursorForPosition(event.pos()))[1]
        self.context_requested.emit(event.globalPos(), position)
        event.accept()

    def mousePressEvent(self, event):
        self._click_origin = event.position().toPoint() if event.button() == Qt.LeftButton else None
        self._click_dragged = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.LeftButton and not self.composing and not self.textCursor().hasSelection()
                and not getattr(self, "_click_dragged", False)):
            point = event.position().toPoint()
            cursor = self.cursorForPosition(point)
            rect = self.cursorRect(cursor)
            if point.x() < rect.x() and cursor.position() > cursor.block().position():
                cursor.movePosition(QTextCursor.PreviousCharacter)
            self.activate_requested.emit(self.offsets(cursor)[1])
        self._click_origin = None

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        origin = getattr(self, "_click_origin", None)
        if origin is not None and (event.position().toPoint() - origin).manhattanLength() >= QApplication.startDragDistance():
            self._click_dragged = True
        if not event.buttons() and not self.composing and not self.textCursor().hasSelection():
            self.hover_requested.emit(self.offsets(self.cursorForPosition(event.position().toPoint()))[1])

    def current_line(self, position=None):
        return self.model.line_index(self.offsets()[1] if position is None else position)

    def line_ending(self):
        return self.model.breaks[0] if self.model.breaks else "\n"

    def indentation_step(self, index, *, prefer_children=False):
        item = self.model.list_item(index)
        indent = item["indent"] if item else ""
        peers = [(i, match["indent"]) for i in range(len(self.model.nodes))
                 if i != index and (match := self.model.list_item(i))]
        level = len(indent.expandtabs(4))
        parent = next((prefix for i, prefix in reversed(peers)
                       if i < index and len(prefix.expandtabs(4)) < level), None)
        end = self.model.branch_bounds(index)[1]
        children = [prefix[len(indent):] for i, prefix in peers
                    if i > index and self.model.nodes[i].start < end
                    and prefix.startswith(indent) and len(prefix) > len(indent)]
        if prefer_children and children:
            return min(children, key=lambda suffix: len((indent + suffix).expandtabs(4)))
        if parent is not None and indent.startswith(parent):
            return indent[len(parent):]
        if "\t" in indent:
            return "\t"
        neighbors = sorted(peers, key=lambda peer: abs(peer[0] - index))
        nearest = next((prefix for _, prefix in neighbors if prefix), "")
        if nearest.startswith("\t"):
            return "\t"
        if nearest and all(prefix.startswith(" ") or not prefix for _, prefix in peers):
            width = min(len(prefix) for _, prefix in peers if prefix)
            return " " * min(width, len(indent) or width)
        return "  "

    def add_sibling(self, *, at_cursor=False, index=None, child=False):
        index = self.current_line() if index is None else index
        start, end = self.model.line_bounds(index)
        item = self.model.list_item(index)
        if at_cursor and not item:
            return False
        indentation = item["indent"] if item else ""
        marker = item["marker"] if item and item["marker"] in "-+*" else "-"
        if child:
            indentation += self.indentation_step(index, prefer_children=True)
        line = indentation + marker + " "
        if at_cursor:
            position = self.offsets()[1]
            if item and position < start + item.end():
                return False
            line_end = end - (self.model.text[end - 1:end] == "\n")
            if position == line_end and self.model.branch_bounds(index)[1] > end:
                return self.add_sibling(index=index)
            self.apply(self.model.splice(position, position, self.line_ending() + line),
                       position + 1 + len(line))
        else:
            _, boundary = self.model.branch_bounds(index)
            if not self.model.text or self.model.text[boundary - 1:boundary] == "\n":
                suffix = self.line_ending() if boundary < len(self.model.text) else ""
                self.apply(self.model.splice(boundary, boundary, line + suffix), boundary + len(line))
            else:
                self.apply(self.model.splice(boundary, boundary, self.line_ending() + line), boundary + 1 + len(line))
        return True

    def add_parent(self, index):
        item = self.model.list_item(index)
        indentation = item["indent"] if item else ""
        start, _ = self.model.line_bounds(index)
        updated = self._indent_model(index, True).splice(start, start, indentation + "- " + self.line_ending())
        self.apply(updated, start + len(indentation) + 2)

    def _indent_model(self, index, forward):
        start, end = self.model.branch_bounds(index)
        updated = self.model
        step = self.indentation_step(index, prefer_children=forward)
        item = self.model.list_item(index)
        prefix = item["indent"] if item else ""
        removed = prefix[:-len(step)] if prefix.endswith(step) else prefix
        offset = len(prefix) if forward else len(removed)
        for node in reversed(self.model.nodes):
            if not (start <= node.start < end or node.start == start == end):
                continue
            position = node.start + offset
            content = updated.text[node.start:]
            if forward and content.startswith(prefix):
                updated = updated.splice(position, position, step)
            elif not forward and removed != prefix and content.startswith(prefix):
                updated = updated.splice(position, position + len(step), "")
        return updated

    def indent_branch(self, forward, index=None):
        index = self.current_line() if index is None else index
        updated = self._indent_model(index, forward)
        start, _ = self.model.line_bounds(index)
        old_cursor = self.offsets()[1]
        old_item = self.model.list_item(index)
        new_item = updated.list_item(index)
        shift = new_item.end() - old_item.end() if old_item and new_item else (len(self.indentation_step(index)) if forward else 0)
        self.apply(updated, min(len(updated.text), max(start, old_cursor + shift)))

    def delete_branch(self, index):
        start, end = self.model.branch_bounds(index)
        self.apply(self.model.splice(start, end, ""), start)

    def duplicate_branch(self, index):
        start, end = self.model.branch_bounds(index)
        if start == end:
            return
        prefix = "" if self.model.text[end - 1:end] == "\n" else self.line_ending()
        self.apply(self.model.insert_fragment(end, self.model.copy_fragment(start, end), clone=True,
                                              prefix=prefix), end + bool(prefix))

    def move_branch(self, index, direction):
        start, end = self.model.branch_bounds(index)
        item = self.model.list_item(index)
        if not item or start == end:
            return
        indent = len(item["indent"].expandtabs(4))
        candidates = (range(index - 1, -1, -1) if direction < 0 else
                      range(self.model.line_index(end), len(self.model.nodes)))
        for other in candidates:
            peer = self.model.list_item(other)
            if not peer:
                continue
            level = len(peer["indent"].expandtabs(4))
            if level < indent:
                break
            if level != indent:
                continue
            first, last = self.model.branch_bounds(other)
            if direction < 0 and last > start or direction > 0 and first < end:
                continue
            self._move_to(start, end, first if direction < 0 else last)
            break

    def _move_to(self, start, end, destination):
        source = self.model
        text, breaks, tokens, nodes = source.copy_fragment(start, end)
        separator = ""
        removal_start = start
        if destination < start and end == len(source.text) and not text.endswith("\n") and start:
            separator = source.slice(start - 1, start)
            removal_start -= 1
        updated = source.splice(removal_start, end, "")
        if destination > start:
            destination -= end - start
        prefix = self.line_ending() if destination and updated.text[destination - 1] != "\n" else ""
        suffix = ""
        if not text.endswith("\n") and (separator or destination < len(updated.text)):
            suffix = separator or self.line_ending()
        # At EOF the separator belongs between the branches, not after the moved one.
        if prefix and text.endswith("\n"):
            prefix, text, breaks = breaks[-1], text[:-1], breaks[:-1]
        self.apply(updated.insert_fragment(destination, (text, breaks, tokens, nodes),
                                           prefix=prefix, suffix=suffix), destination + bool(prefix))

    def toggle_fold(self, index):
        model = self.model
        node = model.nodes[index]
        self.apply(replace(model, nodes=tuple(
            replace(item, collapsed=not node.collapsed) if item.id == node.id else item
            for item in model.nodes)), node.start)

    def refresh_folding(self):
        folded = any(node.collapsed for node in self.model.nodes)
        if not (folded or self._has_folding):
            return
        self._has_folding = folded
        block = self.document().firstBlock()
        hidden_until = 0
        for index, node in enumerate(self.model.nodes):
            if not block.isValid():
                break
            visible = node.start >= hidden_until
            if block.isVisible() != visible:
                block.setVisible(visible)
                block.setLineCount(1 if visible else 0)
                self.document().markContentsDirty(block.position(), block.length())
            if visible and node.collapsed:
                hidden_until = self.model.branch_bounds(index)[1]
            block = block.next()
        self.viewport().update()

    def editing_actions(self, menu):
        actions = (
            ("撤销", self.undo, bool(self.undo_stack)),
            ("重做", self.redo, bool(self.redo_stack)),
            ("剪切", self.cut, self.textCursor().hasSelection()),
            ("复制", self.copy, self.textCursor().hasSelection()),
            ("只粘贴文字", self.paste_as_text, bool(QApplication.clipboard().text())),
        )
        for label, callback, enabled in actions:
            action = QAction(label, menu)
            action.setEnabled(enabled)
            action.triggered.connect(callback)
            menu.addAction(action)
