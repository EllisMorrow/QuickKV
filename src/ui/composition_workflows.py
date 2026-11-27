
"""Module, recall, template, observation and backup actions for the composition page."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QKeySequence, QShortcut, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QTableWidget, QTableWidgetItem, QAbstractItemView, QPushButton, QScrollArea, QToolButton, QWidget, QTextEdit, QVBoxLayout,
)

from core.composition.backups import Backups
from core.composition.document import Choice, Document, Token, new_id, project, qt_offset
from core.composition.groups import Catalog, Group, Module, Option, replace_main
from core.composition.recognition import Recognizer
from core.composition.store import CompositionStorageError
from core.composition.structure import clean_template_output, dimension_status, field_spans, outline_tree, template_fragment
from ui.composition_editor import EditorState
from ui.composition_panel import ChipLayout, button, clear_layout, fill_list, list_ids, make_list


WORKFLOW_ERRORS = (CompositionStorageError, OSError, sqlite3.Error, KeyError, TypeError, ValueError)
STATUSES = {"recorded": "已记录", "pending": "需补充", "unknown": "无法判断", "na": "不适用"}


class AnalysisSignals(QObject):
    finished = Signal(object, object)


class AnalysisJob(QRunnable):
    def __init__(self, key, recognizer, model, bounds):
        super().__init__()
        self.key, self.recognizer, self.model, self.bounds = key, recognizer, model, bounds
        self.signals = AnalysisSignals()

    def run(self):
        self.signals.finished.emit(self.key, self.recognizer.scan(self.model, self.bounds))


class HoverScope(QPushButton):
    changed = Signal()

    def __init__(self, parent):
        super().__init__("当前模块 ▾", parent)
        self.index = 0
        self.hover_blocked = False
        self.popup = QDialog(parent, Qt.Tool | Qt.FramelessWindowHint)
        self.popup.setAttribute(Qt.WA_ShowWithoutActivating)
        self.popup.setAttribute(Qt.WA_WindowPropagation)
        panel = QVBoxLayout(self.popup)
        for index, title in enumerate(("当前模块", "全部模块")):
            button(panel, title, lambda value=index, label=title: self.choose(value, label))
        self.close_timer = QTimer(self, interval=180)
        self.close_timer.timeout.connect(self.close_outside)
        self.clicked.connect(self.toggle)
        QShortcut(QKeySequence(Qt.Key_Escape), self.popup, activated=self.popup.hide)

    def currentIndex(self):
        return self.index

    def choose(self, index, label):
        self.popup.hide()
        if self.index != index:
            self.index = index
            self.setText(label + " ▾")
            self.changed.emit()

    def enterEvent(self, event):
        super().enterEvent(event)
        if not self.hover_blocked:
            self.open_popup()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.hover_blocked = False

    def hideEvent(self, event):
        self.popup.hide()
        super().hideEvent(event)

    def toggle(self):
        if self.popup.isVisible():
            self.hover_blocked = True
            self.popup.hide()
        else:
            self.open_popup()

    def open_popup(self):
        if self.popup.isVisible():
            return
        self.popup.adjustSize()
        point = self.mapToGlobal(QPoint(0, self.height()))
        area = self.screen().availableGeometry()
        self.popup.move(min(point.x(), area.right() - self.popup.width() + 1),
                        min(point.y(), area.bottom() - self.popup.height() + 1))
        self.popup.show()
        self.close_timer.start()

    def close_outside(self):
        point = QCursor.pos()
        if not self.popup.isVisible():
            self.close_timer.stop()
        elif (not self.rect().contains(self.mapFromGlobal(point))
              and not self.popup.frameGeometry().adjusted(-4, -4, 4, 4).contains(point)):
            self.popup.hide()


class FragmentDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent, Qt.Tool)
        self.flush_fragment = lambda: True

    def reject(self):
        if self.flush_fragment():
            super().reject()

    def closeEvent(self, event):
        if self.flush_fragment():
            super().closeEvent(event)
        else:
            event.ignore()


class Workflows:
    def setup_workflows(self):
        self.memory_sessions = {}
        self.module_shortcuts = []
        self.templates = {}
        self.frameworks = {}
        self.candidates = ()
        self.ignored = set()
        self.group_favorites = {}
        self.detect = True
        self.show_observation = True
        self.keep_backups = 5
        self.template_expand = None
        self.recognizer = None
        self.recognizer_catalog = None
        self.analysis_timer = self.timer(170, self.analyze)
        self.analysis_pool = QThreadPool(self)
        self.analysis_pool.setMaxThreadCount(1)
        self.analysis_key = 0
        self.analysis_active = None
        self.analysis_pending = None
        self.backup_timer = QTimer(self, interval=900_000)
        self.backup_timer.timeout.connect(self.auto_backup)
        self.editor.pasted.connect(self.analyze_after_paste)
        self.editor.selectionChanged.connect(self.selection_updated)
        self.indicator_timer = self.timer(0, self.refresh_indicators)
        self.editor.verticalScrollBar().valueChanged.connect(lambda *_: self.indicator_timer.start())
        self.editor.viewport().installEventFilter(self)
        self.editor.updateRequest.connect(lambda *_: self.reposition_indicators())
        self.editor.verticalScrollBar().valueChanged.connect(lambda *_: self.paint_candidates())
        self.indicators = []

    def load_settings(self):
        self.group_favorites = self.store.get("group_favorites", {})
        ignored = self.store.get("ignored_candidates", [])
        self.ignored = {tuple(item) for item in ignored if isinstance(item, list) and len(item) == 4}
        self.templates = self.store.get("templates", {})
        self.frameworks = self.store.get("frameworks", {})
        self.detect = self.store.get("recognition", True)
        self.automatic = self.store.get("continuous_suggestions", False)
        self.show_observation = self.store.get("observations", True)
        self.keep_backups = self.store.get("backup_keep", 5)
        self.backup_timer.start()
        QTimer.singleShot(500, self.auto_backup)

    def output(self):
        return clean_template_output(self.editor.model)

    def selection_updated(self):
        if self.editor.textCursor().hasSelection() and not self.pending_insertion:
            self.dismiss_panel(block_hover=True)

    def refresh_modules(self):
        name = self.catalog.module().name
        self.module_button.setText(name)
        self.module_button.setToolTip(f"{name} · 打开模块中心")
        if getattr(self, "quick_module_window", None) and self.quick_module_window.isVisible():
            self.populate_quick_modules()
        for shortcut in self.module_shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.module_shortcuts.clear()
        for module in self.catalog.modules:
            if module.shortcut:
                shortcut = QShortcut(QKeySequence(module.shortcut), self)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(lambda identity=module.id: self.switch_module(identity))
                self.module_shortcuts.append(shortcut)

    def switch_module(self, identity):
        if not self.flush():
            self.report("请先保存或复制当前正文，再切换模块")
            return False
        updated = replace(self.catalog, selected_module=identity)
        old_id = self.session_id
        try:
            self.load_session(identity)
            if not self.save_catalog(updated, active_session=identity):
                self.load_session(old_id)
                return False
        except WORKFLOW_ERRORS:
            self.report("目标模块正文无法读取", persistent=True)
            return False
        self.dismiss_panel()
        center = getattr(self, "template_center", None)
        if center is not None:
            center.hide()
        self.editor.setFocus()
        return True

    def toggle_quick_modules(self):
        panel = getattr(self, "quick_module_window", None)
        if panel and panel.isVisible():
            self._quick_hover_blocked = True
            panel.hide()
            self.quick_module_close.stop()
        else:
            self.open_quick_modules()

    def open_quick_modules(self):
        if not self.isVisible():
            return
        panel = getattr(self, "quick_module_window", None)
        if panel is None:
            panel = self.quick_module_window = QDialog(self, Qt.Tool | Qt.FramelessWindowHint)
            panel.setAttribute(Qt.WA_ShowWithoutActivating)
            panel.setAttribute(Qt.WA_WindowPropagation)
            panel.setWindowTitle("快速切换模块")
            panel.installEventFilter(self)
            box = QVBoxLayout(panel)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            box.addWidget(scroll)
            content = QWidget()
            self.quick_module_chips = ChipLayout()
            content.setLayout(self.quick_module_chips)
            scroll.setWidget(content)
            self.quick_module_close = QTimer(panel, interval=180)
            self.quick_module_close.timeout.connect(self.check_quick_modules_pointer)
            QShortcut(QKeySequence(Qt.Key_Escape), panel, activated=panel.hide)
        if panel.isVisible():
            return
        button_rect = self.quick_modules
        self.populate_quick_modules()
        point = button_rect.mapToGlobal(QPoint(0, button_rect.height()))
        area = button_rect.screen().availableGeometry()
        panel.move(min(point.x(), area.right() - panel.width() + 1),
                   min(point.y(), area.bottom() - panel.height() + 1))
        panel.show()
        self.update_popup_event_filter()
        self.quick_module_close.start()

    def populate_quick_modules(self):
        flow = self.quick_module_chips
        panel = self.quick_module_window
        clear_layout(flow)
        for module in self.catalog.modules:
            current = module.id == self.catalog.selected_module
            chip = button(flow, ("✓ " if current else "") + module.name,
                          lambda ident=module.id: (panel.hide(), self.switch_module(ident)))
            chip.setToolTip(module.name)
            chip.setMaximumWidth(220)
            chip.setCheckable(True)
            chip.setChecked(current)
            chip.setContextMenuPolicy(Qt.CustomContextMenu)
            chip.customContextMenuRequested.connect(
                lambda pos, m=module, widget=chip: self.quick_module_menu(m, widget.mapToGlobal(pos)))
        add = button(flow, "＋空白模块", self.create_blank_module)
        add.setToolTip("建立并切换到独立空白模块")
        flow.invalidate()
        panel.resize(350, min(260, max(70, flow.heightForWidth(310) + 28)))

    def create_blank_module(self):
        panel = self.quick_module_window
        self.quick_module_close.stop()
        names = {module.name for module in self.catalog.modules}
        base = "空白模块"
        label = base
        serial = 2
        while label in names:
            label = f"{base} {serial}"
            serial += 1
        try:
            name, ok = QInputDialog.getText(panel, "新建空白模块", "名称", text=label)
            if not ok or not name.strip() or not self.flush():
                return
            if name.strip() in names:
                self.report("模块名称已存在")
                return
            module = Module(name.strip(), scope_limited=True)
            catalog = replace(self.catalog.replace_module(module), selected_module=module.id)
            if self.save_catalog(catalog, active_session=module.id):
                panel.hide()
                self.dismiss_panel()
                self.load_session(module.id)
                self.editor.setFocus()
        finally:
            if panel.isVisible():
                self.quick_module_close.start()

    def quick_module_menu(self, module, point):
        self.quick_module_close.stop()
        self._quick_menu_action = True
        menu = QMenu(self.quick_module_window)
        menu.addAction("重命名", lambda: self.edit_module_record(self.quick_module_window, module, "rename"))
        menu.addAction("复制", lambda: self.edit_module_record(self.quick_module_window, module, "duplicate"))
        menu.addAction("删除", lambda: self.delete_module_record(module))
        try:
            menu.exec(point)
        finally:
            self._quick_menu_action = False
            if self.quick_module_window.isVisible():
                self.quick_module_close.start()

    def check_quick_modules_pointer(self):
        panel = self.quick_module_window
        widget = self.quick_modules
        point = QCursor.pos()
        if not panel.isVisible():
            self.quick_module_close.stop()
        elif not (panel.frameGeometry().adjusted(-4, -5, 4, 5).contains(point)
                  or widget.rect().adjusted(-4, -5, 4, 5).contains(widget.mapFromGlobal(point))
                  or (panel.isActiveWindow() and getattr(self, "_module_keyboard", False))):
            panel.hide()
            self.quick_module_close.stop()

    def choose_module(self):
        self.manage_modules()

    def edit_module_record(self, dialog, current, kind):
        if not current and kind != "new":
            return False
        default = "" if kind == "new" else current.name + (" 副本" if kind == "duplicate" else "")
        name, ok = QInputDialog.getText(dialog, "输入模块", "名称", text=default)
        if not ok or not name.strip():
            return
        module = Module(name.strip()) if kind == "new" else replace(
            current, name=name.strip(), id=(("reload-" if current.id.startswith("reload-") else "") + new_id()) if kind == "duplicate" else current.id,
            shortcut="" if kind == "duplicate" else current.shortcut,
        )
        if kind == "duplicate" and self.confirm("同时复制当前正文中的值？"):
            if not self.flush():
                return
            try:
                old = (self.store.load_draft(current.id) if self.store else self.memory_sessions.get(current.id))
                if old and self.store:
                    self.store.save_draft(module.id, 1, old[1], old[2], old[3])
                elif old:
                    self.memory_sessions[module.id] = old
            except WORKFLOW_ERRORS:
                self.report("无法复制模块正文")
                return
        return self.save_catalog(self.catalog.replace_module(module))

    def delete_module_record(self, current):
        return bool(current and self.delete_module_records({current.id}))

    def delete_module_records(self, selected):
        removed = tuple(module for module in self.catalog.modules if module.id in selected)
        modules = tuple(module for module in self.catalog.modules if module.id not in selected)
        if not removed or not modules:
            self.report("至少保留一个模块")
            return False
        if not self.flush():
            return False
        try:
            drafts = set(self.store.draft_ids()) if self.store else set(self.memory_sessions)
        except WORKFLOW_ERRORS:
            self.report("无法确认模块正文，删除已取消")
            return False
        names = "、".join(module.name for module in removed[:8])
        if len(removed) > 8:
            names += " 等"
        count = sum(module.id in drafts for module in removed)
        if not self.confirm(f"移除 {len(removed)} 个模块？\n{names}\n其中 {count} 个有正文。将先建立保护快照。"):
            return False
        if not self.protect("删除模块"):
            return False
        active = self.catalog.selected_module in selected
        target = modules[0].id if active else self.catalog.selected_module
        if active and self.store:
            try:
                self.store.load_draft(target)
            except WORKFLOW_ERRORS:
                self.report("目标模块正文无法读取，删除已取消")
                return False
        catalog = replace(self.catalog, modules=modules, selected_module=target)
        if not self.save_catalog(catalog, active_session=target if active else None):
            return False
        if active:
            self.dismiss_panel()
            self.load_session(target)
        return True

    def manage_modules(self, *, reload_only=False):
        def visible_modules():
            return tuple(m for m in self.catalog.modules if not reload_only or m.id.startswith("reload-"))

        def merge_order(items):
            visible_ids = {m.id for m in visible_modules()}
            ordered = iter(items)
            return tuple(next(ordered) if m.id in visible_ids else m for m in self.catalog.modules)
        dialog = QDialog(self)
        self.dismiss_panel()
        dialog.setWindowTitle("回载建立的模块" if reload_only else "模块中心")
        dialog.resize(530, 365)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("输入模块保存独立正文、光标和词组范围。"))
        controls = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("搜索模块")
        controls.addWidget(search, 1)
        layout.addLayout(controls)
        entries = make_list()
        layout.addWidget(entries)
        empty = QLabel("还没有通过回载建立的模块。")
        layout.addWidget(empty)

        checked = set()
        batch_count = QLabel()
        batch = QWidget()
        batch_row = QHBoxLayout(batch)
        batch_row.setContentsMargins(0, 0, 0, 0)

        def sync_checks():
            batch_count.setText(f"已选 {len(checked)} 项" if checked else "")
            batch.setVisible(bool(checked))

        def change_check(item):
            key = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked:
                checked.add(key)
            else:
                checked.discard(key)
            sync_checks()

        def select_visible():
            for index in range(entries.count()):
                entries.item(index).setCheckState(Qt.Checked)

        def clear_checks():
            checked.clear()
            refresh()

        def move_checked(offset):
            items = list(visible_modules())
            indices = range(1, len(items)) if offset < 0 else range(len(items) - 2, -1, -1)
            for index in indices:
                other = index + offset
                if items[index].id in checked and items[other].id not in checked:
                    items[index], items[other] = items[other], items[index]
            if tuple(items) != visible_modules():
                self.save_catalog(replace(self.catalog, modules=merge_order(items)))
                refresh()

        def delete_checked():
            if self.delete_module_records(checked):
                checked.clear()
                refresh()

        button(controls, "全选结果", select_visible)
        batch_row.addWidget(batch_count)
        button(batch_row, "批量上移", lambda: move_checked(-1))
        button(batch_row, "批量下移", lambda: move_checked(1))
        button(batch_row, "批量删除", delete_checked)
        button(batch_row, "取消选择", clear_checks)
        layout.addWidget(batch)
        entries.itemChanged.connect(change_check)

        def selected():
            item = entries.currentItem()
            return next((module for module in self.catalog.modules
                         if item and module.id == item.data(Qt.UserRole)), None)

        def refresh():
            current = selected()
            query = search.text().strip().casefold()
            entries.blockSignals(True)
            fill_list(entries, ((m.id, m.name + ("  ✓ 当前" if m.id == self.catalog.selected_module else ""))
                                for m in visible_modules() if query in m.name.casefold()),
                      current.id if current else self.catalog.selected_module)
            if entries.currentRow() < 0 and entries.count():
                entries.setCurrentRow(0)
            checked.intersection_update(module.id for module in self.catalog.modules)
            for index in range(entries.count()):
                item = entries.item(index)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in checked else Qt.Unchecked)
            entries.blockSignals(False)
            empty.setVisible(reload_only and not visible_modules())
            entries.setDragEnabled(not query)
            sync_checks()
            place_row_tools()

        def edit(kind):
            if self.edit_module_record(dialog, selected(), kind):
                refresh()

        def remove():
            if self.delete_module_record(selected()):
                refresh()

        row_tools = QWidget(entries.viewport())
        row_actions = QHBoxLayout(row_tools)
        row_actions.setContentsMargins(0, 0, 0, 0)
        row_actions.setSpacing(2)
        row_target = None

        def run_row_action(callback):
            if row_target is not None and not row_target.isHidden():
                entries.setCurrentItem(row_target)
                callback()

        for caption, tooltip, callback in (("✎", "重命名模块", lambda: edit("rename")),
                                           ("×", "删除模块", remove)):
            tool = QToolButton(row_tools)
            tool.setText(caption)
            tool.setToolTip(tooltip)
            tool.setAccessibleName(tooltip)
            tool.clicked.connect(lambda _, action=callback: run_row_action(action))
            row_actions.addWidget(tool)
        row_tools.hide()

        def place_row_tools(item=None):
            nonlocal row_target
            item = item or entries.currentItem()
            row_target = item
            if item and not item.isHidden():
                geometry = entries.visualItemRect(item)
                if entries.viewport().rect().contains(geometry.center()):
                    row_tools.adjustSize()
                    row_tools.move(entries.viewport().width() - row_tools.width() - 3,
                                   geometry.top() + max(0, (geometry.height() - row_tools.height()) // 2))
                    row_tools.show()
                    row_tools.raise_()
                    return
            row_tools.hide()

        entries.setStyleSheet("QListWidget::item { padding-right: 58px; }")
        entries.setMouseTracking(True)
        entries.itemEntered.connect(place_row_tools)
        entries.currentItemChanged.connect(lambda *_: place_row_tools())
        entries.verticalScrollBar().valueChanged.connect(lambda *_: place_row_tools())
        QShortcut(QKeySequence(Qt.Key_F2), entries, activated=lambda: edit("rename"))
        QShortcut(QKeySequence(Qt.Key_Delete), entries, activated=remove)

        def shortcut():
            current = selected()
            if not current:
                return
            keys, ok = QInputDialog.getText(dialog, "模块快捷键", "局部快捷键，例如 Ctrl+Alt+1",
                                          text=current.shortcut)
            if not ok:
                return
            seq = QKeySequence(keys).toString(QKeySequence.PortableText)
            if keys.strip() and (not seq or "+" not in seq or seq.casefold() in {
                    "ctrl+space", getattr(self.settings, "hotkey", "ctrl+space").casefold(),
                    "ctrl+j", "ctrl+return", "ctrl+enter", "ctrl+z", "ctrl+y", "ctrl+c", "ctrl+v",
            } or any(m.id != current.id and m.shortcut == seq for m in self.catalog.modules)):
                self.report("快捷键无效或已使用")
                return
            self.save_catalog(self.catalog.replace_module(replace(current, shortcut=seq)))

        def scope():
            current = selected()
            if current is None:
                return
            groups = QDialog(dialog)
            groups.setWindowTitle("模块可用词组")
            settings = QVBoxLayout(groups)
            settings.addWidget(QLabel(current.name))
            query = QLineEdit()
            query.setPlaceholderText("搜索词组")
            query.setClearButtonEnabled(True)
            settings.addWidget(query)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            body = QWidget()
            flow = ChipLayout()
            body.setLayout(flow)
            scroll.setWidget(body)
            settings.addWidget(scroll)
            chips = {}
            counter = QLabel()
            settings.addWidget(counter)
            def update_count(*_):
                counter.setText(f"已选 {sum(chip.isChecked() for chip in chips.values())} / {len(chips)}")
            for group in self.catalog.groups:
                chip = QPushButton(group.name)
                chip.setCheckable(True)
                chip.setToolTip(group.name)
                chip.setChecked(group.id in current.groups or (not current.groups and not current.scope_limited))
                chip.toggled.connect(update_count)
                flow.addWidget(chip)
                chips[group.id] = chip
            def filter_groups(text):
                for chip in chips.values():
                    chip.setVisible(text.strip().casefold() in chip.text().casefold())
                flow.invalidate()
                body.updateGeometry()
            query.textChanged.connect(filter_groups)
            update_count()
            bar = QHBoxLayout()
            button(bar, "保存", groups.accept)
            button(bar, "取消", groups.reject)
            settings.addLayout(bar)
            groups.resize(465, 320)
            if groups.exec():
                enabled = tuple(identity for identity, chip in chips.items() if chip.isChecked())
                self.save_catalog(self.catalog.replace_module(replace(current, groups=enabled, scope_limited=not enabled)))

        def reorder(offset=0):
            if offset:
                module = selected()
                if not module:
                    return
                items = list(visible_modules())
                i = items.index(module)
                j = i + offset
                if not 0 <= j < len(items):
                    return
                items[i], items[j] = items[j], items[i]
            else:
                if search.text().strip():
                    return
                items = [self.catalog.module(i) for i in list_ids(entries)]
            if len(items) == len(visible_modules()) and self.save_catalog(replace(self.catalog, modules=merge_order(items))) and offset:
                refresh()

        def switch():
            module = selected()
            if module and self.switch_module(module.id):
                dialog.accept()

        actions = (("重命名", lambda: edit("edit")), ("复制", lambda: edit("duplicate")),
                   ("上移", lambda: reorder(-1)), ("下移", lambda: reorder(1)),
                   ("删除", remove), ("快捷键", shortcut), ("词组范围", scope))
        def manage(point):
            if selected():
                self.center_menu(dialog, point, actions)

        if not reload_only:
            button(controls, "＋ 新建模块", lambda: edit("new"))
        bar = QHBoxLayout()
        button(bar, "切换到所选模块", switch).setProperty("compositionPrimary", True)
        bar.addStretch()
        more = button(bar, "···", lambda: manage(more.mapToGlobal(QPoint(0, more.height()))))
        more.setAccessibleName("管理所选模块")
        button(bar, "关闭", dialog.reject)
        layout.addLayout(bar)
        entries.setContextMenuPolicy(Qt.CustomContextMenu)
        def context(pos):
            item = entries.itemAt(pos)
            if item:
                entries.setCurrentItem(item)
                manage(entries.viewport().mapToGlobal(pos))
        entries.customContextMenuRequested.connect(context)
        entries.itemActivated.connect(lambda _: switch())
        search.returnPressed.connect(switch)
        search.textChanged.connect(refresh)
        entries.model().rowsMoved.connect(lambda *_: reorder())
        refresh()
        search.setFocus()
        dialog.exec()
        self.editor.setFocus()

    @staticmethod
    def center_menu(parent, point, actions):
        menu = QMenu(parent)
        for title, callback in actions:
            menu.addAction(title, callback)
        menu.exec(point)

    def reload_menu(self):
        menu = QMenu(self)
        menu.addAction("从外部选区载入", self.external_reload_requested.emit)
        menu.addAction("从剪贴板回载并新建模块", lambda: self.open_reload(QApplication.clipboard().text()))
        menu.addAction("打开回载建立的模块", self.choose_reload)
        self.toggle_action(menu, "识别提示", "detect", "recognition", self.refresh_annotations)
        menu.exec(self.reload_button.mapToGlobal(QPoint(0, self.reload_button.height())))

    def toggle_action(self, menu, label, attribute, key, refresh=None):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(bool(getattr(self, attribute)))

        def toggle(checked):
            if self.save_record(key, checked):
                setattr(self, attribute, checked)
                if refresh:
                    refresh()

        action.toggled.connect(toggle)

    def return_to_module_draft(self):
        modules = [module for module in self.catalog.modules if not module.id.startswith("reload-")]
        target = next((module for module in modules if module.id == getattr(self, "previous_reload_module", None)),
                      modules[0] if modules else None)
        if target:
            self.switch_module(target.id)

    def open_reload(self, raw):
        if not raw or not self.flush():
            return False
        identity = "reload-" + new_id()
        try:
            model = Document.from_raw(raw)
            for candidate in self.get_recognizer().scan(model):
                if candidate.certain:
                    model = model.attach(candidate.interpretations[0].token(
                        candidate.start, candidate.end, candidate.literal))
            source = self.catalog.module()
            base = datetime.now().strftime("回载 · %Y%m%d")
            names = {module.name for module in self.catalog.modules}
            serial = 1
            while f"{base}-{serial}" in names:
                serial += 1
            module = replace(source, name=f"{base}-{serial}", id=identity, shortcut="")
            catalog = replace(self.catalog.replace_module(module), selected_module=identity)
            if not self.save_catalog(catalog, active_session=identity, new_draft=model):
                return False
            if not self.store:
                self.memory_sessions[identity] = (1, model, 0, 0)
            if not source.id.startswith("reload-"):
                self.previous_reload_module = source.id
            self.load_session(identity)
        except WORKFLOW_ERRORS:
            self.report("回载未完成，当前正文已保留", persistent=True)
            return False
        self.dismiss_panel()
        self.editor.setFocus()
        return True

    def choose_reload(self):
        self.manage_modules(reload_only=True)

    def refresh_annotations(self):
        previous = getattr(self, "_annotated_model", None)
        model = self.editor.model
        old_session = getattr(self, "_annotated_session", None)
        self._annotated_model, self._annotated_session = model, self.session_id
        lost = (previous is not None and previous is not model and old_session == self.session_id
                and bool({token.id for token in previous.tokens} - {token.id for token in model.tokens}))
        self.analysis_key += 1
        self.analysis_pending = None
        self.analysis_timer.stop()
        self.candidates = ()
        if getattr(self, "candidate_dialog", None):
            self.candidate_dialog.close()
        self.paint_candidates()
        if self.detect and (self.session_id.startswith("reload-") or lost):
            self.analysis_timer.start()
        if self.editor.model is not getattr(self, "indicator_document", None):
            self.indicator_document = self.editor.model
            self.refresh_indicators()

    def analyze_after_paste(self, start, stop, recognize):
        if self.detect and recognize:
            self.analyze((start, stop))
        elif not recognize and self.session_id.startswith("reload-"):
            self.ignored.update(self.candidate_key(item) for item in
                                self.get_recognizer().scan(self.editor.model, (start, stop)))
            self.persist_ignored_candidates()

    def get_recognizer(self):
        if self.recognizer_catalog != self.catalog:
            self.recognizer = Recognizer(self.catalog)
            self.recognizer_catalog = self.catalog
        return self.recognizer

    def analyze(self, bounds=None, *, explicit=False):
        if self.editor.composing:
            return
        model, recognizer = self.editor.model, self.get_recognizer()
        self.analysis_key += 1
        if bounds is not None:
            start, end = bounds
            span = (model.nodes[model.line_index(start)].start, model.line_bounds(model.line_index(end))[1])
            candidates = tuple(c for c in recognizer.scan(model, span) if c.start < end and c.end > start)
            if explicit:
                removed = self.ignored.intersection(self.candidate_key(c) for c in candidates)
                if removed:
                    self.ignored.difference_update(removed)
                    self.persist_ignored_candidates()
            unaffected = (c for c in self.candidates if c.end <= start or c.start >= end)
            self.accept_analysis(self.analysis_key, sorted((*unaffected, *candidates), key=lambda c: c.start))
            if explicit and not candidates:
                self.report("选区内没有可识别的组合词")
            return
        if len(model.text) < 30_000:
            self.analysis_pending = None
            self.accept_analysis(self.analysis_key, recognizer.scan(model, bounds))
            return
        job = AnalysisJob(self.analysis_key, recognizer, model, bounds)
        if self.analysis_active is None:
            self.submit_analysis(job)
        else:
            self.analysis_pending = job

    def submit_analysis(self, job):
        self.analysis_active = job
        job.signals.finished.connect(self.finish_analysis, Qt.QueuedConnection)
        self.analysis_pool.start(job)

    def finish_analysis(self, key, candidates):
        self.analysis_active = None
        self.accept_analysis(key, candidates)
        if self.analysis_pending:
            job = self.analysis_pending
            self.analysis_pending = None
            self.submit_analysis(job)

    def accept_analysis(self, key, candidates):
        if key == self.analysis_key:
            self.candidates = tuple(c for c in candidates if self.candidate_key(c) not in self.ignored)
            self.paint_candidates()

    def paint_candidates(self):
        if self.editor._rendering:
            return
        model = self.editor.model
        first = min(len(model.nodes) - 1, max(0, self.editor.firstVisibleBlock().blockNumber() - 1))
        last = min(len(model.nodes) - 1, first + self.editor.viewport().height() // 10 + 5)
        minimum, maximum = model.nodes[first].start, model.line_bounds(last)[1]
        selections = []
        for candidate in self.candidates:
            if candidate.end < minimum:
                continue
            if candidate.start > maximum:
                break
            if model.text[candidate.start:candidate.end] != candidate.literal:
                continue
            item = QTextEdit.ExtraSelection()
            item.cursor = QTextCursor(self.editor.document())
            item.cursor.setPosition(qt_offset(model.text, candidate.start))
            item.cursor.setPosition(qt_offset(model.text, candidate.end), QTextCursor.KeepAnchor)
            item.format.setUnderlineStyle(QTextCharFormat.DashUnderline)
            item.format.setUnderlineColor(QColor("#9bcfc5" if self.settings.theme == "dark" else "#24564f"))
            selections.append(item)
        self.editor.setExtraSelections(selections)

    def candidate_at(self, position):
        return next((c for c in self.candidates if c.start <= position < c.end), None)

    def activate_candidate(self, position, *, hover=False):
        candidate = self.candidate_at(position)
        if not candidate:
            return
        old = getattr(self, "candidate_dialog", None)
        if old and old.isVisible():
            if getattr(self, "_shown_candidate", None) == candidate:
                if not hover:
                    self._candidate_hover = False
                    old.activateWindow()
                return
            old.close()
        self._shown_candidate = candidate
        self._candidate_hover = hover
        dialog = QDialog(self, Qt.Tool)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setAttribute(Qt.WA_ShowWithoutActivating, hover)
        dialog.installEventFilter(self)
        dialog.setWindowTitle("组合词候选")
        self.candidate_dialog = dialog
        layout = QVBoxLayout(dialog)
        self.add_candidate_source(layout, candidate.literal)
        if len(candidate.interpretations) > 1:
            layout.addWidget(QLabel("请选择组合方式："))
        table = self.combination_table(dialog, ("前缀", "主词", "后缀"), [
            tuple("、".join(self.choice_caption(value) for value in values) or "无"
                  for values in (row.prefixes, (row.main,), row.suffixes))
            for row in candidate.interpretations
        ])
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.selectRow(0)
        layout.addWidget(table)
        bar = QHBoxLayout()
        def use():
            index = table.currentRow()
            if index >= 0:
                interpretation = candidate.interpretations[index]
                dialog.close()
                self.establish(candidate, interpretation)
        accept = button(bar, "建立组合词", use)
        accept.setDefault(True)
        button(bar, "忽略此候选", lambda: (dialog.close(), self.ignore_candidate(candidate)))
        button(bar, "关闭", dialog.close)
        layout.addLayout(bar)
        table.cellDoubleClicked.connect(lambda *_: use())
        QShortcut(QKeySequence(Qt.Key_Return), table, activated=use)
        area = self.screen().availableGeometry()
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(qt_offset(self.editor.model.text, candidate.start))
        point = self.editor.viewport().mapToGlobal(self.editor.cursorRect(cursor).bottomLeft())
        width = min(610, area.width())
        dialog.resize(width, 160)
        table.resizeRowsToContents()
        header = table.horizontalHeader().sizeHint().height()
        table.setFixedHeight(min(210, header + 6 + sum(table.rowHeight(row) for row in range(table.rowCount()))))
        dialog.resize(width, min(layout.sizeHint().height(), area.height()))
        dialog.move(max(area.left(), min(point.x(), area.right() - dialog.width() + 1)),
                    max(area.top(), min(point.y(), area.bottom() - dialog.height() + 1)))
        dialog.show()
        self.update_popup_event_filter()
        QTimer.singleShot(0, dialog, lambda: self.fit_combination_table(dialog, table, area.height()))
        if not hover:
            table.setFocus()

    @staticmethod
    def add_candidate_source(layout, literal):
        row = QHBoxLayout()
        heading = QLabel("源内容")
        heading.setProperty("secondaryText", True)
        source = QLabel(literal)
        source.setTextFormat(Qt.PlainText)
        source.setWordWrap(True)
        source.setToolTip(literal)
        row.addWidget(heading)
        row.addWidget(source, 1)
        layout.addLayout(row)

    def update_popup_event_filter(self, *, excluding=None):
        windows = (getattr(self, "candidate_dialog", None), getattr(self, "quick_module_window", None))
        app = QApplication.instance()
        if app is not None:
            if any(window is not None and window is not excluding and window.isVisible() for window in windows):
                app.installEventFilter(self)
            else:
                app.removeEventFilter(self)

    def close_candidate_hover(self):
        dialog = getattr(self, "candidate_dialog", None)
        if not dialog or not dialog.isVisible() or not self._candidate_hover or self.context_active:
            return
        pos = QCursor.pos()
        viewport = self.editor.viewport()
        local = viewport.mapFromGlobal(pos)
        if dialog.frameGeometry().adjusted(-6, -6, 6, 6).contains(pos):
            return
        if viewport.rect().contains(local):
            offset = self.editor.offsets(self.editor.cursorForPosition(local))[1]
            if self.candidate_at(offset) == self._shown_candidate:
                return
        dialog.close()

    @staticmethod
    def fit_combination_table(dialog, table, maximum):
        table.resizeRowsToContents()
        height = table.horizontalHeader().height() + 6 + sum(
            table.rowHeight(row) for row in range(table.rowCount()))
        table.setFixedHeight(min(220, height))
        dialog.layout().activate()
        dialog.resize(dialog.width(), min(maximum, dialog.layout().sizeHint().height()))

    @staticmethod
    def combination_table(dialog, headers, rows):
        table = QTableWidget(len(rows), len(headers), dialog)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(table.horizontalHeader().ResizeMode.Stretch)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(True)
        for index, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 1 and len(headers) == 3:
                    font = table.font()
                    font.setBold(True)
                    item.setFont(font)
                if " · " in value and len(headers) == 3:
                    from html import escape
                    pieces = [part.partition(" · ") for part in value.split("、")]
                    text, background = table.palette().text().color(), table.palette().base().color()
                    muted = QColor.fromRgb(*(round(a * .76 + b * .24)
                                            for a, b in zip(text.getRgb()[:3], background.getRgb()[:3]))).name()
                    item.setData(Qt.AccessibleTextRole, value)
                    item.setText("")
                    label = QLabel("<br>".join(
                        f"{escape(word)} <small style='color:{muted}'>· {escape(group)}</small>"
                        if group else escape(word) for word, _, group in pieces))
                    label.setWordWrap(True)
                    label.setToolTip(value)
                    label.setAttribute(Qt.WA_TransparentForMouseEvents)
                    if column == 1:
                        label.setStyleSheet("font-weight: 600")
                    table.setCellWidget(index, column, label)
                elif value == "无" or (len(headers) == 4 and (column == 0 or index == 1)):
                    muted = table.palette().text().color()
                    muted.setAlpha(165)
                    item.setForeground(muted)
                table.setItem(index, column, item)
        table.resizeRowsToContents()
        QTimer.singleShot(0, table.resizeRowsToContents)
        return table

    def choice_caption(self, value):
        group = self.catalog.group(value.group_id)
        return value.text + (" · " + group.name if group else " · 未归组")

    def candidate_key(self, candidate):
        model = self.editor.model
        node = model.nodes[model.line_index(candidate.start)]
        return self.session_id, node.id, candidate.start - node.start, candidate.literal

    def persist_ignored_candidates(self):
        if not self.save_record("ignored_candidates", sorted(self.ignored)):
            self.report("忽略状态保留在当前会话，暂未保存")

    def ignore_candidate(self, candidate):
        self.ignored.add(self.candidate_key(candidate))
        self.persist_ignored_candidates()
        self.candidates = tuple(c for c in self.candidates if c != candidate)
        self.paint_candidates()

    def establish(self, candidate, chosen=None):
        document = self.editor.model
        if document.text[candidate.start:candidate.end] != candidate.literal:
            return
        alternatives = candidate.interpretations
        chosen = chosen or (alternatives[0] if len(alternatives) == 1 else self.pick(
            f"确认组合词 · {candidate.literal}", [(str(i), self.interpretation_label(c), c) for i, c in enumerate(alternatives)]))
        if not chosen:
            return
        catalog, words = self.catalog, ()
        if not chosen.certain:
            plan = self.resolve_unknown(chosen, candidate.literal)
            if plan is None:
                return
            chosen, catalog, words = plan
        if document is self.editor.model:
            try:
                attached = document.attach(chosen.token(candidate.start, candidate.end, candidate.literal))
                if catalog != self.catalog or words:
                    if not self.save_catalog(catalog, words=words, draft_update=(attached, candidate.end)):
                        return
                self.editor.apply(attached, candidate.end)
            except ValueError:
                self.report("候选范围已经变化")

    def interpretation_label(self, interpretation):
        def choice(value):
            group = self.catalog.group(value.group_id)
            suggestion = self.catalog.group(interpretation.suggested_group) if value is interpretation.main else None
            label = group.name if group else ("建议 " + suggestion.name if suggestion else "未归组")
            return f"{value.text}（{label}）"
        return ("前缀 · " + ("、".join(map(choice, interpretation.prefixes)) or "无")
                + " ｜ 主词 · " + choice(interpretation.main)
                + " ｜ 后缀 · " + ("、".join(map(choice, interpretation.suffixes)) or "无"))

    def pick_group_chips(self, title, options, *, explanation=""):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        if explanation:
            notes = QLabel(explanation)
            notes.setWordWrap(True)
            layout.addWidget(notes)
        search = QLineEdit()
        search.setPlaceholderText("查找词组或组内词")
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        flow = ChipLayout()
        body.setLayout(flow)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        selection = []
        chips = []
        def select(widget, value):
            selection[:] = [value]
            for chip, _, _ in chips:
                chip.setChecked(chip is widget)
            rename = isinstance(value, Group) and (widget.text().startswith("建立「") or widget.text() == "为词条新建词组…")
            new_name.setVisible(rename)
            if rename:
                new_name.setText(value.name)
            choose.setEnabled(True)
        for key, label, value in options:
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setToolTip(label)
            chip.clicked.connect(lambda _, widget=chip, item=value: select(widget, item))
            flow.addWidget(chip)
            chips.append((chip, label, value))
        def filter_choices(text):
            query = text.strip().casefold()
            for chip, label, value in chips:
                chip.setVisible(query in label.casefold() or any(
                    query in option.text.casefold() for option in value.values() if not option.pattern)
                    if isinstance(value, Group) else query in label.casefold())
            flow.invalidate()
            body.updateGeometry()
        search.textChanged.connect(filter_choices)
        new_name = QLineEdit()
        new_name.setPlaceholderText("新词组名称")
        new_name.hide()
        layout.addWidget(new_name)
        bar = QHBoxLayout()
        choose = button(bar, "确定", dialog.accept)
        choose.setEnabled(False)
        button(bar, "取消", dialog.reject)
        layout.addLayout(bar)
        dialog.resize(500, 320)
        if chips:
            select(chips[0][0], chips[0][2])
        if not dialog.exec() or not selection:
            return None
        result = selection[0]
        if new_name.isVisibleTo(dialog) and isinstance(result, Group):
            title = new_name.text().strip()
            if not title:
                self.report("请填写词组名称")
                return None
            if any(group.name == title for group in self.catalog.groups):
                self.report("同名词组已存在，请明确选择已有词组")
                return None
            result = replace(result, name=title)
        return result

    def confirm_establishment(self, literal, structured, catalog, words, actions):
        dialog = QDialog(self)
        dialog.setWindowTitle("建立组合词")
        layout = QVBoxLayout(dialog)
        self.add_candidate_source(layout, literal)
        words_by_role, groups_by_role = [], []
        for values in (structured.prefixes, (structured.main,), structured.suffixes):
            words_by_role.append("\n".join(value.text for value in values) or "无")
            groups = []
            for value in values:
                group = catalog.group(value.group_id)
                existing = self.catalog.group(value.group_id)
                kind = "新建词组" if not existing else (
                    "新增组内词汇" if value.option_id and not any(o.id == value.option_id for o in existing.options)
                    else "复用")
                groups.append(f"{kind} · {group.name}" if group else "未归组")
            groups_by_role.append("\n".join(groups) or "无")
        table = self.combination_table(dialog, ("角色", "前缀", "主词", "后缀"), [
            ("词汇", *words_by_role), ("归属及处理", *groups_by_role)
        ])
        layout.addWidget(table)
        table.setMaximumHeight(min(240, 38 + 42 * table.rowCount()))
        new_groups = sum(self.catalog.group(group.id) is None for group in catalog.groups)
        host = catalog.group(structured.main.group_id)
        old = self.catalog.group(host.id)
        links = sum(len(set(getattr(host, role)) - set(getattr(old, role, ())))
                    for role in ("prefixes", "suffixes"))
        label = QLabel(f"新增词汇 {len(set(words))} 项 · 新建词组 {new_groups} 项 · 新增关联 {links} 项")
        layout.addWidget(label)
        if links:
            disclaimer = QLabel("新增关联作用于共享主词组。其他已写入的组合词保留。")
            disclaimer.setWordWrap(True)
            layout.addWidget(disclaimer)
        bar = QHBoxLayout()
        button(bar, "确认建立", dialog.accept).setDefault(True)
        button(bar, "取消", dialog.reject)
        layout.addLayout(bar)
        dialog.resize(615, 270)
        QTimer.singleShot(0, dialog, lambda: self.fit_combination_table(
            dialog, table, dialog.screen().availableGeometry().height()))
        return bool(dialog.exec())

    def resolve_unknown(self, interpretation, literal=""):
        catalog = self.catalog
        words = []
        actions = []

        def assign(choice, groups, default_name, role):
            nonlocal catalog
            if choice.group_id:
                return choice
            matches = tuple(g for g in groups if g.kind == "ordinary")
            same = tuple(g for g in matches if g.name == default_name)
            if len(same) == 1:
                group = same[0]
            elif matches:
                new = Group(default_name)
                picks = [(g.id, f"{g.name} · {g.id[:8]}", g) for g in matches]
                if not same:
                    picks.insert(0, (new.id, f"建立「{default_name}」", new))
                group = self.pick_group_chips(f"为{role}「{choice.text}」选择词组", picks)
            else:
                group = Group(default_name)
            if group is None:
                return None
            is_new = catalog.group(group.id) is None

            option = next((o for o in group.values() if o.text == choice.text), None)
            if option is None:
                option = Option(choice.text)
                group = replace(group, options=(*group.options, option))
                words.append(choice.text)
            catalog = catalog.replace_group(group)
            actions.append(f"{'新建' if is_new else '复用'} {group.name} → {choice.text}")
            return group.choice(option)

        main = assign(interpretation.main, catalog.groups, interpretation.main.text, "主词")
        if main is None:
            return None
        host = catalog.group(main.group_id)
        if host is None:
            return None
        sides = {}
        for side, label in (("prefixes", "前缀"), ("suffixes", "后缀")):
            groups = tuple(catalog.group(key) for key in getattr(host, side))
            values = []
            references = list(getattr(host, side))
            for choice in getattr(interpretation, side):
                viable = tuple(g for g in catalog.groups if g.id != host.id and g.kind == "ordinary")
                allowed = (*groups, *(g for g in viable if g not in groups))
                resolved = assign(choice, allowed, f"{host.name} · {label}", label)
                if resolved is None or resolved.group_id == host.id:
                    return None
                values.append(resolved)
                if resolved.group_id not in references:
                    references.append(resolved.group_id)
                    actions.append(f"{host.name} · {label} → {catalog.group(resolved.group_id).name}")
                groups = tuple(catalog.group(key) for key in references)
            host = replace(host, **{side: tuple(references)})
            sides[side] = tuple(values)
        catalog = catalog.replace_group(host)
        module = catalog.module()
        if (module.groups or module.scope_limited) and host.id not in module.groups:
            catalog = catalog.replace_module(replace(module, groups=(*module.groups, host.id)))
        structured = replace(interpretation, main=main, **sides)
        if not self.confirm_establishment(literal, structured, catalog, words, actions):
            return None
        return structured, catalog, tuple(words)

    def establish_all(self):
        result = self.editor.model
        for candidate in self.candidates:
            if candidate.certain and result.text[candidate.start:candidate.end] == candidate.literal:
                result = result.attach(candidate.interpretations[0].token(
                    candidate.start, candidate.end, candidate.literal))
        self.editor.apply(result)
        self.analyze()

    def add_reload_actions(self, menu, position):
        menu.addAction("粘贴并识别", lambda: self.editor.paste_with_recognition())
        if self.editor.textCursor().hasSelection():
            menu.addAction("识别选区", lambda: self.analyze(tuple(sorted(self.editor.offsets())), explicit=True))
        candidate = self.candidate_at(position)
        if candidate:
            menu.addAction("查看并建立候选组合词", lambda: self.establish(candidate))
            menu.addAction("忽略候选", lambda: self.ignore_candidate(candidate))
        if any(c.certain for c in self.candidates):
            menu.addAction("批量建立确定组合词", self.establish_all)



    def template_menu(self):
        self.dismiss_panel()
        existing = getattr(self, "template_center", None)
        if existing is not None:
            self.refresh_template_center()
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
        dialog = self.template_center = FragmentDialog(self)
        dialog.setAttribute(Qt.WA_WindowPropagation)
        dialog.setWindowTitle("片段中心")
        dialog.resize(760, 450)
        layout = QVBoxLayout(dialog)
        tools = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("搜索分支片段")
        search.setClearButtonEnabled(True)
        tools.addWidget(search, 1)
        scope = HoverScope(dialog)
        tools.addWidget(scope)
        layout.addLayout(tools)
        content = QHBoxLayout()
        entries = make_list()
        content.addWidget(entries, 2)
        from ui.composition_editor import CompositionEditor
        details = QVBoxLayout()
        name = QLineEdit()
        name.setPlaceholderText("片段名称")
        details.addWidget(name)
        editor = CompositionEditor()
        editor.setAccessibleName("片段正文编辑")
        details.addWidget(editor, 1)
        content.addLayout(details, 3)
        layout.addLayout(content, 1)
        status = QLabel()
        count = QLabel()
        checked = []
        updating_checks = False

        def sync_checks():
            nonlocal updating_checks
            updating_checks = True
            count.setText(f"已选 {len(checked)} 项" if checked else "")
            for index in range(entries.count()):
                item = entries.item(index)
                key = item.data(Qt.UserRole)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if key in checked else Qt.Unchecked)
                title = self.templates[key]["name"]
                item.setText(f"{checked.index(key) + 1} · {title}" if key in checked else title)
                item.setToolTip(title)
            updating_checks = False

        def check_changed(item):
            if updating_checks:
                return
            key = item.data(Qt.UserRole)
            if item.checkState() == Qt.Checked and key not in checked:
                checked.append(key)
            elif item.checkState() != Qt.Checked and key in checked:
                checked.remove(key)
            sync_checks()

        def checked_or_current():
            return tuple(checked) if checked else ((pending["key"],) if pending["key"] else ())

        pending = {"key": None, "dirty": False, "loading": False}
        save_timer = QTimer(dialog, interval=350, singleShot=True)

        def flush_fragment():
            if not pending["dirty"]:
                return True
            save_timer.stop()
            key = pending["key"]
            record = self.templates.get(key)
            title = name.text().strip()
            if not record or not title:
                status.setText("保存失败：请输入片段名称")
                return False
            updated = {**record, "name": title, "fragment": editor.model.to_record()}
            if not self.update_templates({**self.templates, key: updated}):
                status.setText("保存失败，修改仍在编辑区")
                return False
            pending["dirty"] = False
            item = entries.currentItem()
            if item and item.data(Qt.UserRole) == key:
                sync_checks()
            status.setText("已保存")
            return True

        dialog.flush_fragment = flush_fragment
        save_timer.timeout.connect(flush_fragment)

        def changed(*_):
            if pending["key"] and not pending["loading"]:
                pending["dirty"] = True
                status.setText("待保存")
                save_timer.start()

        def load(key):
            record = self.templates.get(key)
            try:
                model = Document.load(record["fragment"]) if record else Document()
            except WORKFLOW_ERRORS:
                status.setText("片段数据无法读取")
                return
            pending.update(key=key, loading=True, dirty=False)
            name.setText(record["name"] if record else "")
            editor.load_state(EditorState(model))
            name.setEnabled(bool(record))
            editor.setReadOnly(not record)
            status.setText("已保存" if record else "")
            pending["loading"] = False

        def select(item, _previous):
            if pending["loading"]:
                return
            key = item.data(Qt.UserRole) if item else None
            if key == pending["key"]:
                return
            if not flush_fragment():
                entries.blockSignals(True)
                for index in range(entries.count()):
                    if entries.item(index).data(Qt.UserRole) == pending["key"]:
                        entries.setCurrentRow(index)
                        break
                entries.blockSignals(False)
                return
            load(key)

        def all_records():
            return sorted(self.templates.items(), key=lambda item: item[1].get("order", 0))

        def refresh(preferred=None):
            if not flush_fragment():
                return
            query = search.text().strip().casefold()
            records = all_records() if scope.currentIndex() else self.module_templates()
            entries.blockSignals(True)
            fill_list(entries, ((key, item["name"]) for key, item in records if query in item["name"].casefold()),
                      preferred or pending["key"])
            if entries.currentRow() < 0 and entries.count():
                entries.setCurrentRow(0)
            entries.blockSignals(False)
            checked[:] = [key for key in checked if key in self.templates]
            sync_checks()
            item = entries.currentItem()
            select(item, None)
            entries.setDragEnabled(not query and not scope.currentIndex())

        def create(model):
            if not flush_fragment():
                return
            key = new_id()
            names = {item["name"] for item in self.templates.values()}
            title, number = "新建片段", 1
            while title in names:
                number += 1
                title = f"新建片段 {number}"
            record = {"name": title, "module": self.catalog.selected_module, "fragment": model.to_record()}
            if self.update_templates({**self.templates, key: record}):
                search.blockSignals(True)
                search.clear()
                search.blockSignals(False)
                refresh(key)
                name.setFocus()
                name.selectAll()

        def from_outline():
            start, end = sorted(self.editor.offsets())
            index = self.editor.model.line_index(start)
            first, stop = self.editor.model.branch_bounds(index)
            if not self.editor.model.text[first:stop].strip() or end > stop:
                self.report("请选择一个大纲节点及其子内容")
                return
            fragment = self.template_from_branch(index)
            if fragment is not None:
                create(fragment)

        def insert():
            if not self.isVisible() or not flush_fragment():
                return
            records = tuple(self.templates[key] for key in checked_or_current() if key in self.templates)
            if records and self.insert_templates(records, self.editor.offsets()[1]):
                self.window().activateWindow()
                self.editor.setFocus()

        def order(visible):
            if not flush_fragment():
                return
            iterator = iter(visible)
            shown = set(visible)
            ordered = [next(iterator) if key in shown else key for key, _ in all_records()]
            self.update_templates({key: {**self.templates[key], "order": i} for i, key in enumerate(ordered)})

        def move(offset):
            if search.text().strip() or scope.currentIndex():
                self.report("请在当前模块的未筛选列表中排序")
                return
            visible = list(list_ids(entries))
            selected = set(checked_or_current())
            if selected.difference(visible):
                self.report("请先清除其他模块的勾选")
                return
            indices = range(1, len(visible)) if offset < 0 else range(len(visible) - 2, -1, -1)
            for index in indices:
                target = index + offset
                if visible[index] in selected and visible[target] not in selected:
                    visible[index], visible[target] = visible[target], visible[index]
            order(visible)
            refresh()

        def manage(action):
            keys = checked_or_current()
            if not keys or not flush_fragment():
                return
            updated = dict(self.templates)
            if action == "删除":
                if not self.confirm(f"删除 {len(keys)} 个片段？从片段插入的现有正文保留。") or not self.protect("删除片段"):
                    return
                for key in keys:
                    updated.pop(key)
            else:
                next_order = max((record.get("order", 0) for record in updated.values()), default=0)
                for index, key in enumerate(keys, 1):
                    record = updated[key]
                    updated[new_id()] = {**record, "name": record["name"] + " 副本", "order": next_order + index}
            if self.update_templates(updated):
                refresh()

        def move_module():
            keys = checked_or_current()
            if not keys or not flush_fragment():
                return
            target = self.pick_group_chips("移动片段到模块", [
                (module.id, module.name, module.id) for module in self.catalog.modules],
                explanation=f"确认后修改 {len(keys)} 个片段的模块归属，现有正文保留。")
            if not target or not self.protect("移动片段"):
                return
            updated = {key: {**record, "module": target} if key in keys else record
                       for key, record in self.templates.items()}
            if self.update_templates(updated):
                refresh()

        def clear_checks():
            checked.clear()
            sync_checks()

        actions = (("插入所选片段", insert), ("复制所选片段", lambda: manage("复制")),
                   ("所选片段上移", lambda: move(-1)), ("所选片段下移", lambda: move(1)),
                   ("移动所选片段到模块…", move_module), ("删除所选片段", lambda: manage("删除")),
                   ("清除勾选", clear_checks))
        button(tools, "＋ 新建片段", lambda: create(Document()))
        branch = button(tools, "从所选大纲新建（含子内容）", from_outline)
        branch.setToolTip("将主编辑器中选定的大纲节点及其下属全部子内容保存为新片段。")
        bar = QHBoxLayout()
        button(bar, "插入到当前光标", insert).setProperty("compositionPrimary", True)
        bar.addWidget(status)
        bar.addWidget(count)
        bar.addStretch()
        more = button(bar, "···", lambda: self.center_menu(dialog, more.mapToGlobal(
            QPoint(0, more.height())), actions))
        button(bar, "关闭", dialog.reject)
        layout.addLayout(bar)
        entries.itemChanged.connect(check_changed)
        entries.currentItemChanged.connect(select)
        entries.itemActivated.connect(lambda _: insert())
        entries.setContextMenuPolicy(Qt.CustomContextMenu)
        def context(point):
            item = entries.itemAt(point)
            if item:
                entries.setCurrentItem(item)
                self.center_menu(dialog, entries.viewport().mapToGlobal(point), actions)
        entries.customContextMenuRequested.connect(context)
        entries.model().rowsMoved.connect(lambda *_: order(list_ids(entries)) if not search.text().strip() and not scope.currentIndex() else None)
        search.textChanged.connect(lambda *_: refresh())
        search.returnPressed.connect(insert)
        scope.changed.connect(refresh)
        editor.lookup_requested.connect(lambda: self.fragment_lookup(editor))
        editor.pasted.connect(lambda start, end, recognize: self.fragment_recognize(
            editor, start, end) if recognize else None)
        name.textChanged.connect(changed)
        editor.changed.connect(changed)
        self.refresh_template_center = refresh
        refresh()
        search.setFocus()
        dialog.show()
        dialog.activateWindow()

    def fragment_lookup(self, editor):
        if editor.isReadOnly() or editor.composing:
            return
        original = editor.model
        start, end = sorted(editor.offsets())
        token = original.token_at(start) if start == end else None
        if token:
            start, end = token.start, token.end
        groups = self.catalog.groups

        def choices(query):
            matches = self.group_matches(query, groups) if query else (
                (group, option) for group in groups for option in group.values())
            return [(option.id, f"{option.value()} · {group.name}", (group, option))
                    for group, option in matches][:150]

        selected = self.pick("插入片段组合词", [], supply=choices)
        if not selected or original is not editor.model:
            return
        group, option = selected
        choice = group.choice(option)
        value = replace_main(token, choice, group) if token else Token(start, end, choice.text, choice)
        new = original.replace_token(value)
        editor.apply(new, start + len(value.render()))

    def fragment_recognize(self, editor, start, end):
        model = editor.model
        for candidate in self.get_recognizer().scan(model, (start, end)):
            if candidate.certain:
                model = model.attach(candidate.interpretations[0].token(candidate.start, candidate.end, candidate.literal))
        if model is not editor.model:
            editor.apply(model, end)

    def save_template(self, index):
        name, ok = QInputDialog.getText(self, "保存分支片段", "名称")
        if not ok or not name.strip():
            return
        template = self.template_from_branch(index)
        if template is not None:
            self.update_templates({**self.templates, new_id(): {
                "name": name.strip(), "module": self.catalog.selected_module, "fragment": template.to_record()
            }})

    def template_from_branch(self, index):
        model = self.editor.model
        original = template_fragment(model, index)
        observed = []
        for line in range(len(original.nodes)):
            start, stop = original.line_bounds(line)
            source = original.text[start:stop].rstrip("\n")
            for name, first, last in field_spans(source):
                if last > first:
                    observed.append((name, (start + first, start + last), source[first:last]))
        outside = [(t.literal, t.id, t.literal) for t in original.tokens
                   if not any(first <= t.start and t.end <= last for _, (first, last), _ in observed)]
        observable = (*observed, *outside)
        choices = ["保留全部", "清空键值和组合词值", "逐项选择清空"]
        policy, accepted = QInputDialog.getItem(self, "片段内容", "观察值处理", choices, editable=False)
        if not accepted:
            return
        cleared = ()
        if policy == choices[1]:
            cleared = tuple(identity for _, identity, _ in observable)
        elif policy == choices[2]:
            dialog = QDialog(self)
            dialog.setWindowTitle("选择需要重新观察的值")
            layout = QVBoxLayout(dialog)
            listing = QListWidget()
            layout.addWidget(listing)
            for title, identity, literal in observable:
                item = QListWidgetItem(title + " · " + literal, listing)
                item.setData(Qt.UserRole, identity)
                item.setCheckState(Qt.Unchecked)
            button(layout, "清空勾选项", dialog.accept)
            if not dialog.exec():
                return
            cleared = tuple(tuple(value) if isinstance(value, list) else value
                            for i in range(listing.count()) if listing.item(i).checkState() == Qt.Checked
                            for value in (listing.item(i).data(Qt.UserRole),))
        if self.editor.model is model:
            return template_fragment(model, index, cleared)
        self.report("正文已变化，请重新选择分支")
        return None

    def update_templates(self, updated):
        if self.save_record("templates", updated):
            self.templates = updated
            return True
        return False

    def module_templates(self):
        return tuple(sorted(((i, data) for i, data in self.templates.items()
                             if data["module"] == self.catalog.selected_module),
                            key=lambda item: item[1].get("order", 0)))

    def insert_template(self, record, boundary):
        return self.insert_templates((record,), boundary)

    def prepare_template(self, record):
        try:
            fragment = Document.load(record["fragment"])
        except WORKFLOW_ERRORS:
            self.report("片段数据无法读取")
            return None
        if self.template_expand and "{{" in fragment.text:
            from difflib import SequenceMatcher
            expanded = self.template_expand(fragment.raw)
            if expanded is None:
                return None
            projected = project(expanded)
            if projected != fragment.text:
                offsets = Document.from_raw(expanded)
                matcher = SequenceMatcher(None, fragment.text, projected, autojunk=False)
                for operation, start, end, first, last in reversed(matcher.get_opcodes()):
                    if operation != "equal":
                        fragment = fragment.splice(start, end, offsets.slice(first, last))
        return fragment

    def insert_templates(self, records, boundary):
        original = self.editor.model
        original_selection = self.editor.offsets()
        fragments = []
        for record in records:
            fragment = self.prepare_template(record)
            if fragment is None:
                return False
            if fragment.text:
                fragments.append(fragment)
        if (self.editor.model is not original or self.editor.offsets() != original_selection
                or not 0 <= boundary <= len(original.text)):
            self.report("正文或插入光标已改变")
            return False
        if not fragments:
            return False
        target = original
        position = boundary
        ending = self.editor.line_ending()
        caret = boundary
        for index, fragment in enumerate(fragments):
            prefix = ending if position and target.text[position - 1] != "\n" else ""
            suffix = (ending if index == len(fragments) - 1 and position < len(target.text)
                      and not fragment.text.endswith("\n") and target.text[position] != "\n" else "")
            content = fragment.copy_fragment(0, len(fragment.text))
            before = len(target.text)
            target = target.insert_fragment(position, content, clone=True, prefix=prefix, suffix=suffix)
            if index == 0:
                caret = position + bool(prefix)
            position += len(target.text) - before
        self.editor.apply(target, caret)
        return True

    def manage_templates(self, identity, action):
        record = self.templates[identity]
        updated = dict(self.templates)
        if action == "删除":
            if not self.confirm("删除选定片段？此前创建的正文保留。"):
                return
            updated.pop(identity)
        elif action == "重命名":
            name, confirmed = QInputDialog.getText(self, "重命名片段", "名称", text=record["name"])
            if not confirmed or not name.strip():
                return
            updated[identity] = {**record, "name": name.strip()}
        elif action == "复制":
            updated[new_id()] = {**record, "name": record["name"] + " 副本"}
        self.update_templates(updated)

    def add_outline_actions(self, menu, index):
        menu.addAction("保存分支为片段", lambda: self.save_template(index))
        menu.addAction("设置此节点观察维度", lambda: self.configure_frame(index))
        frames = self.available_frameworks()
        if frames:
            menu.addAction("引用已有观察框架", lambda: self.bind_frame(index, frames))
        node = self.editor.model.nodes[index]
        if node.observation:
            menu.addAction("查看观察维度", lambda: self.show_frame(index))
            title = "关闭此节点观察提示" if node.show_observations else "开启此节点观察提示"
            menu.addAction(title, lambda: self.update_node(index, show_observations=not node.show_observations))
        if node.slots:
            slots = menu.addMenu("填写片段输入位")
            for number, slot in enumerate(node.slots, 1):
                group = self.catalog.group(slot[1])
                label = f"{number} · {group.name if group else '直接输入'}"
                slots.addAction(label, lambda _checked=False, value=slot: self.fill_slot(node, value))

    def fill_slot(self, node, slot):
        position = node.start + slot[0]
        self.editor.select_offsets(position)
        self.editor.setFocus()
        if slot[1]:
            self.lookup()
            if self.panel.token:
                self.panel.browse_group(slot[1])

    def update_node(self, index, **settings):
        doc = self.editor.model
        nodes = list(doc.nodes)
        nodes[index] = replace(nodes[index], **settings)
        self.editor.apply(replace(doc, nodes=tuple(nodes)))

    def refresh_indicators(self):
        if not hasattr(self, "indicators") or not self.isVisible() or self.editor._rendering:
            return
        for widget in self.indicators:
            widget.hide()
            widget.deleteLater()
        self.indicators.clear()
        if not self.show_observation or not self.catalog.module().show_observations:
            return
        block = self.editor.firstVisibleBlock()
        doc = self.editor.model
        viewport = self.editor.viewport()
        while block.isValid():
            index = block.blockNumber()
            rectangle = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
            if rectangle.top() >= viewport.height() or index >= len(doc.nodes):
                break
            node = doc.nodes[index]
            config = self.frameworks.get(node.observation)
            if config and node.show_observations and block.isVisible() and rectangle.bottom() >= 0:
                states = tuple(dimension_status(doc, index, config["paths"]))
                recorded = sum(state == "recorded" for _, state, _ in states)
                applicable = sum(state != "na" for _, state, _ in states)
                label = f"◉ {recorded}/{applicable}" if applicable else "◉ 不适用"
                from PySide6.QtWidgets import QPushButton
                indicator = QPushButton(label, viewport)
                indicator.setAccessibleName("查看观察维度")
                indicator.setProperty("observationBlock", index)
                indicator.setStyleSheet("border: none; color: #739b90; padding: 1px 4px;")
                indicator.clicked.connect(lambda _=False, i=index: self.show_frame(i))
                indicator.adjustSize()
                indicator.move(max(0, viewport.width() - indicator.width() - 4), max(0, int(rectangle.top())))
                indicator.show()
                self.indicators.append(indicator)
            block = block.next()

    def reposition_indicators(self):
        editor = self.editor
        viewport = editor.viewport()
        for indicator in getattr(self, "indicators", ()):
            index = indicator.property("observationBlock")
            block = editor.document().findBlockByNumber(index)
            if not block.isValid() or not block.isVisible():
                indicator.hide()
                continue
            rect = editor.blockBoundingGeometry(block).translated(editor.contentOffset())
            indicator.move(max(0, viewport.width() - indicator.width() - 4), max(0, int(rect.top())))
            indicator.setVisible(rect.bottom() > 0 and rect.top() < viewport.height())

    def available_frameworks(self):
        identity = self.catalog.selected_module
        return [(i, data["name"], i) for i, data in self.frameworks.items()
                if data.get("module", identity) == identity]

    def bind_frame(self, index, frames):
        selected = self.pick("引用观察框架", frames)
        if selected:
            self.update_node(index, observation=selected, statuses=())

    def configure_frame(self, index):
        node = self.editor.model.nodes[index]
        original = self.frameworks.get(node.observation, {})
        inferred = []
        for _, parents, values in outline_tree(self.editor.model, index):
            inferred.extend(" → ".join((*parents, label)) for label, _ in values)
        name, ok = QInputDialog.getText(self, "观察框架", "名称", text=original.get("name", "观察维度"))
        if not ok or not name.strip():
            return
        choices = original.get("paths", tuple(dict.fromkeys(inferred)))
        raw, accepted = QInputDialog.getMultiLineText(self, "观察维度", "每行一个，可用 → 指定父子路径",
                                                      "\n".join(choices))
        if not accepted:
            return
        paths = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
        identity = node.observation or new_id()
        updated = {**self.frameworks, identity: {"name": name.strip(), "paths": paths,
                                                              "module": self.catalog.selected_module}}
        if self.save_record("frameworks", updated):
            self.frameworks = updated
            self.update_node(index, observation=identity)

    def show_frame(self, index):
        config = self.frameworks.get(self.editor.model.nodes[index].observation)
        if not config:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(config["name"])
        layout = QVBoxLayout(dialog)
        dialog.resize(360, 240)
        states = tuple(dimension_status(self.editor.model, index, config["paths"]))
        for dimension, state, matches in states:
            row = QHBoxLayout()
            label = QLabel(dimension)
            label.setTextFormat(Qt.PlainText)
            label.setToolTip(dimension)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            badge = button(row, STATUSES[state],
                           lambda d=dimension, m=matches: (dialog.accept(), self.focus_dimension(index, d, m)))
            badge.setMinimumWidth(84)
            badge.setProperty("observationState", state)
            badge.setToolTip(STATUSES[state])
            if state != "recorded":
                action = button(row, "状态", lambda d=dimension: (dialog.accept(), self.set_dimension_status(index, d)))
                action.setMinimumWidth(55)
            layout.addLayout(row)
        button(layout, "配置观察维度", lambda: (dialog.accept(), self.configure_frame(index)))
        dialog.exec()

    def set_dimension_status(self, index, dimension):
        states = ("pending", "unknown", "na")
        title, ok = QInputDialog.getItem(self, "观察状态", dimension, [STATUSES[s] for s in states], editable=False)
        if ok:
            status = {v: k for k, v in STATUSES.items()}[title]
            existing = dict(self.editor.model.nodes[index].statuses)
            existing[dimension] = status
            self.update_node(index, statuses=tuple(existing.items()))

    def focus_dimension(self, index, path, matches):
        if len(matches) > 1:
            document = self.editor.model
            chosen = self.pick("选择记录位置", [(str(i), document.text[s:e].strip(), i)
                                                for i in matches for s, e in (document.line_bounds(i),)])
            if chosen is None:
                return
            matches = (chosen,)
        if matches:
            _, position = self.editor.model.line_bounds(matches[0])
            if self.editor.model.text[position - 1:position] == "\n":
                position -= 1
            self.editor.select_offsets(position)
        else:
            segments = tuple(p.strip() for p in path.split("→") if p.strip())
            parent = index
            for depth, label in enumerate(segments):
                candidates = tuple(dimension_status(self.editor.model, index, (" → ".join(segments[:depth + 1]),)))
                if candidates and len(candidates[0][2]) == 1:
                    parent = candidates[0][2][0]
                else:
                    self.editor.add_sibling(index=parent, child=True)
                    self.editor.replace_selection(label + "：")
                    parent = self.editor.current_line()
        self.editor.setFocus()
        self.editor.ensureCursorVisible()

    def backup_service(self):
        return Backups(self.store, self.store.path.parent / "组合输入备份") if self.store else None

    def protect(self, label):
        service = self.backup_service()
        if not service:
            return self.factory is None
        try:
            service.create(label, "protected", include_drafts=True)
            return True
        except WORKFLOW_ERRORS:
            self.report("未能创建保护快照，操作已暂停", persistent=True)
            return False

    def auto_backup(self):
        service = self.backup_service()
        if service and self.flush() and self.reconcile_library():
            try:
                service.create("自动备份", "automatic", include_drafts=True, keep=self.keep_backups)
            except WORKFLOW_ERRORS:
                self.report("自动备份未完成，已有备份已保留")

    def manual_backup(self):
        service = self.backup_service()
        if not service:
            return
        name, ok = QInputDialog.getText(self, "手动备份", "备份名称")
        if ok and self.flush() and self.reconcile_library():
            try:
                service.create(name.strip() or "手动备份", include_drafts=True)
                self.report("已建立并校验备份")
            except WORKFLOW_ERRORS:
                self.report("备份创建失败")

    def backup_settings(self):
        count, ok = QInputDialog.getInt(self, "自动备份", "保留的有效自动备份数量", self.keep_backups, 1, 100)
        if ok and self.save_record("backup_keep", count):
            self.keep_backups = count

    def choose_backup(self):
        service = self.backup_service()
        if not service:
            return
        entries = service.entries()
        selected = self.pick("查看组合输入备份", [(str(p), f"{m['name']} · {m['created'][:16]}", p)
                                                 for p, m in entries])
        if selected is not None:
            self.backup_actions(selected)

    def backup_actions(self, path):
        service = self.backup_service()
        try:
            meta, config, library = service.read(path)
            catalog = Catalog.load(config["records"].get("catalog"))
            sources = {o.source for g in catalog.groups for o in g.options if o.source}
            missing = sum(not Path(source).is_file() for source in sources)
            summary = (f"{meta['name']} · 词组 {len(catalog.groups)} · 模块 {len(catalog.modules)} · "
                       f"模块正文 {len(config['drafts'])} · 词库 {len(library)} 字节 · 缺少外部来源 {missing}")
            actions = ["恢复配置及专用词库", "恢复配置、专用词库及模块正文", "固定保留", "导出备份", "删除备份"]
            if not config["drafts"]:
                actions.remove("恢复配置、专用词库及模块正文")
            action, ok = QInputDialog.getItem(self, "备份内容", summary, actions, editable=False)
            if not ok:
                return
            local = path.parent == service.directory
            if action.startswith("恢复") and self.flush() and self.reconcile_library():
                full = action == "恢复配置、专用词库及模块正文"
                if full and not self.confirm("恢复归档模块的正文？当前数据将存入恢复前保护快照。"):
                    return
                service.restore(path, include_drafts=full)
                self.catalog = Catalog.load(self.store.get("catalog"))
                self.load_settings()
                self.refresh_modules()
                if full:
                    self.load_session(self.catalog.selected_module)
                else:
                    self.refresh_annotations()
                self.library_changed.emit()
                self.report("备份已恢复，恢复前数据已存入保护快照")
            elif action == "固定保留" and local:
                name, accepted = QInputDialog.getText(self, "固定备份", "备份名称", text=meta["name"])
                if accepted:
                    service.pin(path, name.strip())
            elif action == "导出备份":
                file, accepted = QFileDialog.getSaveFileName(self, "导出备份", path.name,
                                                            "QuickKV 备份 (*.quickkv-backup)")
                if accepted and file:
                    shutil.copyfile(path, file)
            elif action == "删除备份" and local and self.confirm("删除这一份本机备份？"):
                path.unlink()
        except WORKFLOW_ERRORS:
            self.report("无法验证或处理该备份，当前正文已保留", persistent=True)

    def open_backup(self):
        file, _ = QFileDialog.getOpenFileName(self, "打开备份", "", "QuickKV 备份 (*.quickkv-backup)")
        if file and self.backup_service():
            self.backup_actions(Path(file))

    def reconcile_library(self):
        if not self.store:
            return True
        try:
            result = self.store.recover_library()
            if result == "completed":
                self.catalog = Catalog.load(self.store.get("catalog"))
                self.refresh_modules()
                if not self.dirty:
                    self.load_session(self.session_id)
        except WORKFLOW_ERRORS:
            if not self.has_conflict:
                self.report("专用词库有未决冲突，当前文件和恢复材料已保留。", persistent=True)
            self.has_conflict = True
            return False
        if self.has_conflict and "专用词库" in self.status.text():
            self.status.hide()
        self.has_conflict = False
        return True

    def add_workflow_actions(self, menu):
        self.toggle_action(menu, "正文连续联想", "automatic", "continuous_suggestions", self.dismiss_panel)
        transcription = menu.addAction("程序输出时镜像使用Q-KV「转写功能」")
        transcription.setCheckable(True)
        transcription.setChecked(getattr(self.settings, "transcription_enabled", True))
        if self.set_output_transcription:
            transcription.toggled.connect(self.set_output_transcription)
        else:
            transcription.setEnabled(False)
        menu.addSeparator()
        self.toggle_action(menu, "观察维度提示", "show_observation", "observations", self.refresh_indicators)
        module = self.catalog.module()
        label = "关闭本模块观察提示" if module.show_observations else "开启本模块观察提示"
        menu.addAction(label, lambda: (self.save_catalog(self.catalog.replace_module(replace(
            module, show_observations=not module.show_observations))), self.refresh_indicators()))
        menu.addSeparator()
        backups = menu.addMenu("备份与恢复")
        backups.addAction("创建手动备份", self.manual_backup)
        backups.addAction("查看本机备份", self.choose_backup)
        backups.addAction("导入并预览备份", self.open_backup)
        backups.addAction("打开备份目录", self.open_backup_directory)
        backups.addAction("设置自动保留份数", self.backup_settings)

    def open_backup_directory(self):
        service = self.backup_service()
        if service:
            try:
                service.directory.mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(service.directory)))
            except OSError:
                self.report("无法打开备份目录")
