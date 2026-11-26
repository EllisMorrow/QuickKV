
"""Anchored composition token choices and group configuration."""

from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QDrag, QFont, QTextLayout
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget, QTextBrowser, QStyledItemDelegate, QStyle, QStyleOptionViewItem,
)

from core.composition.document import Choice, new_id
from core.composition.groups import Group, Numeric, Option, choose_modifier, replace_main


SOURCE_ROLE = Qt.UserRole + 1


class CandidateDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        source = index.data(SOURCE_ROLE)
        if not source:
            return super().paint(painter, option, index)
        style = option.widget.style()
        row = QStyleOptionViewItem(option)
        self.initStyleOption(row, index)
        label = row.text
        row.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, row, painter, row.widget)
        area = row.rect.adjusted(7, 0, -7, 0)
        painter.save()
        painter.setClipRect(area)
        small = QFont(row.font)
        if row.font.pixelSize() > 0:
            small.setPixelSize(max(7, row.font.pixelSize() - 2))
        else:
            small.setPointSizeF(max(7, row.font.pointSizeF() - 1.5))
        painter.setFont(small)
        metrics = painter.fontMetrics()
        source = metrics.elidedText(source, Qt.ElideRight, area.width() * 2 // 5)
        selected = row.state & QStyle.State_Selected
        color = row.palette.highlightedText().color() if selected else row.palette.text().color()
        muted = type(color)(color)
        muted.setAlpha(190)
        painter.setPen(muted)
        painter.drawText(area, Qt.AlignVCenter, source)
        area.setLeft(area.left() + metrics.horizontalAdvance(source) + 6)
        painter.setFont(row.font)
        painter.setPen(color)
        shown = painter.fontMetrics().elidedText(label, Qt.ElideRight, area.width())
        only_hits = index.data(SOURCE_ROLE + 1)
        if only_hits and shown == label:
            layout = QTextLayout(label, row.font)
            formats = []
            for offset, count, shade in only_hits:
                run = QTextLayout.FormatRange()
                run.start, run.length = offset, count
                run.format.setForeground(QColor(shade))
                formats.append(run)
            layout.setFormats(formats)
            layout.beginLayout()
            line = layout.createLine()
            line.setLineWidth(area.width())
            layout.endLayout()
            layout.draw(painter, QPointF(area.left(), area.center().y() - line.height() / 2))
        else:
            painter.drawText(area, Qt.AlignVCenter, shown)
        painter.restore()


class ChipLayout(QLayout):
    def __init__(self):
        super().__init__()
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        if isinstance(item.widget(), QPushButton):
            item.widget().setProperty("compositionChip", True)
        self.items.append(item)

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QRect(0, 0, width, 0), False)

    def sizeHint(self):
        return QSize(320, self.heightForWidth(320))

    def minimumSize(self):
        return QSize(40, max((item.sizeHint().height() for item in self.items), default=0))

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect, True)

    def arrange(self, rect, apply):
        x = y = line = 0
        for item in self.items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            width = min(max(1, rect.width()), size.width())
            if x and x + width > rect.width():
                x, y, line = 0, y + line + 4, 0
            if apply:
                item.setGeometry(QRect(rect.topLeft() + QPoint(x, y), QSize(width, size.height())))
            x += width + 4
            line = max(line, size.height())
        return y + line


def button(layout, label, callback):
    widget = QPushButton(label)
    widget.clicked.connect(lambda: callback())
    layout.addWidget(widget)
    return widget


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def choice_label(choice, placeholder, separator="-"):
    return separator.join(c.text for c in choice) if choice else placeholder


def make_list():
    widget = QListWidget()
    widget.setDragDropMode(QListWidget.InternalMove)
    widget.setDefaultDropAction(Qt.MoveAction)
    widget.setUniformItemSizes(True)
    return widget


def fill_list(widget, values, selected=None):
    widget.clear()
    for identity, text in values:
        item = QListWidgetItem(text, widget)
        item.setData(Qt.UserRole, identity)
        item.setToolTip(text)
        item.setSizeHint(QSize(1, max(24, widget.fontMetrics().height() + 6)))
        if identity == selected:
            widget.setCurrentItem(item)


def list_ids(widget):
    return tuple(widget.item(i).data(Qt.UserRole) for i in range(widget.count()))


class SortButton(QPushButton):
    mime_type = "application/x-quickkv-composition-order"

    def __init__(self, text, panel, scope, identity, move):
        super().__init__(text)
        self.panel, self.scope, self.identity, self.move_item = panel, scope, identity, move
        self.origin = None
        self.setAcceptDrops(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.origin = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.origin is None or not event.buttons() & Qt.LeftButton or
                (event.position().toPoint() - self.origin).manhattanLength() < QApplication.startDragDistance()):
            return super().mouseMoveEvent(event)
        self.origin = None
        payload = QMimeData()
        payload.setData(self.mime_type, self.identity.encode("utf8"))
        drag = QDrag(self)
        drag.setMimeData(payload)
        self.panel.locked = True
        self.setDown(False)
        drag.exec(Qt.MoveAction)

    def accepts(self, event):
        source = event.source()
        return (isinstance(source, SortButton) and source.panel is self.panel
                and source.scope == self.scope and source.identity != self.identity)

    def dragEnterEvent(self, event):
        if self.accepts(event):
            self.setDown(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.setDown(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setDown(False)
        if self.accepts(event):
            self.move_item(event.source().identity, self.identity)
            event.acceptProposedAction()


def move_before(sequence, source, target, key=lambda value: value):
    ordered = list(sequence)
    value = next(value for value in ordered if key(value) == source)
    ordered.remove(value)
    ordered.insert(next(i for i, value in enumerate(ordered) if key(value) == target), value)
    return tuple(ordered)


class CompositionPanel(QFrame):
    def __init__(self, workspace):
        super().__init__(workspace, Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_WindowPropagation)
        self.anchor = None
        self.positions = {}
        self.workspace = workspace
        self.setObjectName("compositionPanel")
        self.setFrameShape(QFrame.StyledPanel)
        self.setFocusPolicy(Qt.ClickFocus)
        self.token = None
        self.mode = ""
        self.locked = False
        self.expanded = False
        self.main_group = ""
        self.selected_group = ""
        self.parent_option = ""
        self.time_widgets = []
        self.rebuild_timer = QTimer(self, interval=0, singleShot=True)
        self.rebuild_timer.timeout.connect(self.rebuild)
        self.measure_timer = QTimer(self, interval=0, singleShot=True)
        self.measure_timer.timeout.connect(self.workspace.position_panel)
        self.timer = QTimer(self, interval=250)
        self.timer.timeout.connect(self.refresh_time)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.header = QHBoxLayout()
        self.prefix = button(self.header, "前+", lambda: self.set_mode("prefixes"))
        self.main = button(self.header, "主词", lambda: self.set_mode("main", focus=True))
        self.suffix = button(self.header, "后+", lambda: self.set_mode("suffixes"))
        self.config = button(self.header, "配置", self.toggle_config)
        self.groups_button = button(self.header, "词组", self.open_group_management)
        self.groups_button.setToolTip("管理全局共享词组")
        self.close_button = button(self.header, "×", self.close_panel)
        self.close_button.setFixedWidth(25)
        for widget in (self.prefix, self.main, self.suffix, self.config):
            widget.setFocusPolicy(Qt.NoFocus)
            widget.installEventFilter(self)
        for widget in (self.prefix, self.main, self.suffix):
            widget.setFixedWidth(68)
            widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.header.insertStretch(self.header.count() - 1, 1)
        self.hover_modes = {self.prefix: "prefixes", self.main: "main", self.suffix: "suffixes"}
        layout.addLayout(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 2, 2, 2)
        self.body_layout.setSpacing(5)
        self.body_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll)
        self.preview = QLabel()
        self.preview.setTextFormat(Qt.PlainText)
        self.preview.setWordWrap(True)
        self.preview.setMaximumHeight(48)
        self.preview.setObjectName("compositionResult")
        layout.addWidget(self.preview)
        self.hide()

    @property
    def catalog(self):
        return self.workspace.catalog

    def open_token(self, token, *, mode="", locked=False, focus=False):
        if self.token and self.token.id == token.id and self.isVisible():
            if locked:
                self.locked = True
            if mode:
                self.set_mode(mode, focus=focus)
            return
        self.token = token
        self.mode = mode
        self.expanded = False
        self.anchor = None
        self.positions.clear()
        self.main_group = token.main.group_id
        self.selected_group = ""
        self.parent_option = ""
        self.locked = locked
        self.rebuild()
        self.show()
        self.raise_()
        self.timer.start()
        self.workspace.position_panel()
        if focus and hasattr(self, "query"):
            self.activateWindow()
            self.query.setFocus()

    def close_panel(self):
        self.workspace.dismiss_panel(block_hover=True)
        self.workspace.window().activateWindow()
        self.workspace.editor.setFocus()

    def reset(self):
        self.timer.stop()
        self.rebuild_timer.stop()
        self.measure_timer.stop()
        self.token = None
        self.anchor = None
        self.locked = False
        self.hide()

    def update_token(self, token):
        self.token = token
        self.update_summary()

    def update_summary(self):
        token = self.token
        for widget, label in (
            (self.prefix, choice_label(token.prefixes, "前+") + ("+" if token.prefixes else "")),
            (self.main, token.main.text or "主词"),
            (self.suffix, ("+" if token.suffixes else "") + choice_label(token.suffixes, "后+")),
        ):
            widget.setToolTip(label)
            widget.setAccessibleName(label)
            widget.setText(widget.fontMetrics().elidedText(label, Qt.ElideMiddle, max(15, widget.width() - 14)))
        self.preview.setText(token.literal or token.render())
        self.preview.setToolTip(self.preview.text())
        self.preview.setVisible(bool(self.mode and (token.literal or token.main.text)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.token:
            self.update_summary()

    def set_mode(self, mode, focus=False):
        if mode in ("main", "prefixes", "suffixes"):
            self.config_role = mode
        if self.mode:
            self.positions[self.mode] = self.scroll.verticalScrollBar().value()
        self.mode = mode
        self.rebuild()
        if focus and mode == "main":
            self.activateWindow()
            self.query.setFocus()

    def open_group_management(self):
        self.locked = True
        self.selected_group = self.token.main.group_id or self.main_group
        self.parent_option = ""
        self.set_mode("config-global")

    def toggle_config(self):
        self.locked = True
        if self.mode.startswith("config-global"):
            self.set_mode("config-" + getattr(self, "config_role", "main"))
        elif self.mode.startswith("config-"):
            self.set_mode(self.mode.removeprefix("config-"))
        else:
            self.set_mode("config-" + (self.mode or "main"))

    def mousePressEvent(self, event):
        self.locked = True
        super().mousePressEvent(event)

    def eventFilter(self, watched, event):
        if (getattr(self, "all_groups_open", False) and event.type() == QEvent.MouseButtonPress
                and isinstance(watched, QWidget) and watched is not self and not self.isAncestorOf(watched)):
            QTimer.singleShot(0, self.close_all_groups)
        if event.type() == QEvent.Enter and watched in self.hover_modes:
            if self.workspace.hover_enabled() and not self.locked:
                mode = self.hover_modes[watched]
                if self.mode != mode:
                    self.set_mode(mode)
        if watched is getattr(self, "query", None) and self.mode in ("main", "prefixes", "suffixes"):
            if event.type() == QEvent.MouseButtonPress and not self.query.text():
                if self.all_groups_open:
                    self.close_all_groups()
                    return True
                self.show_all_groups()
                return True
            elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_Down and not self.query.text() and (not self.main_group or event.modifiers() & Qt.AltModifier):
                if not self.all_groups_open:
                    self.show_all_groups()
                    return True
            elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape and self.all_groups_open and getattr(self, "preedit_widget", None) is not watched:
                self.close_all_groups()
                return True
        if event.type() == QEvent.InputMethod:
            self.preedit_widget = watched if event.preeditString() else None
        if event.type() == QEvent.FocusOut and getattr(self, "preedit_widget", None) is watched:
            self.preedit_widget = None
        if event.type() == QEvent.KeyPress and getattr(self, "preedit_widget", None) is not watched:
            if event.key() == Qt.Key_Escape:
                if watched.property("temporaryCompositionValue"):
                    watched.clear()
                    watched.hide()
                    self.remeasure()
                else:
                    self.close_panel()
                return True
            if (self.mode in ("main", "prefixes", "suffixes") and watched is getattr(self, "query", None)
                    and event.key() in (Qt.Key_Up, Qt.Key_Down) and self.results.count()):
                step = 1 if event.key() == Qt.Key_Down else -1
                self.results.setCurrentRow((self.results.currentRow() + step) % self.results.count())
                return True
        return super().eventFilter(watched, event)

    def rebuild(self):
        self.close_all_groups(refresh=False)
        if self.token is None:
            return
        self.time_widgets.clear()
        self.lookup_button = None
        self.attach_library = None
        self.compact_config = False
        clear_layout(self.body_layout)
        self.update_summary()
        for widget in (self.prefix, self.main, self.suffix, self.config):
            widget.setVisible(self.mode != "suggestions")
        self.scroll.setVisible(bool(self.mode))
        for mode, widget in (("prefixes", self.prefix), ("main", self.main), ("suffixes", self.suffix),
                             ("config", self.config)):
            widget.setCheckable(True)
            widget.setChecked(self.mode.startswith(mode))
        self.groups_button.setVisible(self.mode != "suggestions")
        self.config.setText("完成" if self.mode.startswith("config-") and not self.mode.startswith("config-global") else "配置")
        if self.mode.startswith("config-"):
            scope = self.mode.removeprefix("config-")
            if scope.startswith("global-"):
                host = self.catalog.group(self.managed_host)
                if host:
                    self.build_config(scope.removeprefix("global-"), host)
            else:
                self.build_config(scope)
        elif self.mode in ("main", "prefixes", "suffixes"):
            self.build_main()
        elif self.mode == "suggestions":
            self.build_suggestions()
        elif self.mode:
            self.build_modifiers()
        self.scroll.verticalScrollBar().setValue(self.positions.get(self.mode, 0))
        self.remeasure()

    def remeasure(self):
        self.body_layout.invalidate()
        self.body_layout.activate()
        self.body.updateGeometry()
        self.workspace.position_panel()
        self.measure_timer.start()

    @staticmethod
    def size_list(widget, rows):
        row = max(widget.fontMetrics().height() + 8, widget.sizeHintForRow(0))
        widget.setMinimumHeight(row + 6)
        widget.setMaximumHeight(min(rows, max(1, widget.count())) * row + 6)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def text_label(self, text, layout=None):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        (layout or self.body_layout).addWidget(label)
        return label

    def refresh_time(self):
        if self.time_widgets:
            instant = datetime.now()
            for widget, option in self.time_widgets:
                widget.setText(option.value(instant))

    def selectable_groups(self):
        module = self.catalog.module()
        if module.groups or module.scope_limited:
            return tuple(self.catalog.group(identity) for identity in module.groups)
        return self.catalog.groups

    def build_suggestions(self):
        self.listing = QListWidget()
        self.listing.setFocusPolicy(Qt.NoFocus)
        for entry in self.workspace.continuous_matches:
            item = QListWidgetItem(self.workspace.entry_text(entry), self.listing)
            item.setData(Qt.UserRole, entry)
            item.setToolTip(item.text())
        self.body_layout.addWidget(self.listing)
        self.size_list(self.listing, 5)
        self.listing.setCurrentRow(0)
        self.listing.itemClicked.connect(lambda item: self.workspace.accept_continuous(item.data(Qt.UserRole)))

    def build_main(self):
        role = {"main": "主词", "prefixes": "前缀", "suffixes": "后缀"}[self.mode]
        group = self.catalog.group(self.main_group if self.mode == "main" else self.token.main.group_id)
        if group:
            caption = self.text_label("当前词组 · " + group.name)
            caption.setToolTip(group.name)
            if self.mode == "main":
                button(self.body_layout, "返回默认展示", lambda: self.browse_group(""))
        self.query = QLineEdit()
        self.query.setPlaceholderText(f"查找{role}、词组或词库")
        self.query.setClearButtonEnabled(True)
        self.query.installEventFilter(self)
        self.body_layout.addWidget(self.query)
        self.results = QListWidget()
        self.results.setItemDelegate(CandidateDelegate(self.results))
        self.empty_results = self.text_label("输入关键词，或选择词组")
        self.results.installEventFilter(self)
        self.results.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self.main_result_menu)
        listing = QHBoxLayout()
        listing.addWidget(self.results, 1, Qt.AlignTop)
        self.search_preview = QTextBrowser()
        self.search_preview.setOpenLinks(False)
        self.search_preview.setMaximumHeight(115)
        self.search_preview.setAccessibleName("检索词条全文")
        self.search_preview.hide()
        listing.addWidget(self.search_preview, 1, Qt.AlignTop)
        self.body_layout.addLayout(listing)
        self.results.currentItemChanged.connect(lambda item, _: self.preview_candidate(item))
        self.result_limit = 120
        self.query.textChanged.connect(self.reset_query_limit)
        self.query.returnPressed.connect(self.accept_main_query)
        self.results.itemClicked.connect(self.accept_main_item)
        self.results.itemActivated.connect(self.accept_main_item)
        self.main_chips = ChipLayout()
        self.body_layout.addLayout(self.main_chips)
        self.main_rows = QVBoxLayout()
        self.body_layout.addLayout(self.main_rows)
        self.results.currentRowChanged.connect(self.update_chip_navigation)
        self.lookup_button = button(self.body_layout, "按原词条插入", self.insert_library_entry)
        self.lookup_button.setToolTip("保留现有搜索词条的多行内容及片段处理")
        self.attach_library = button(self.body_layout, "关联到主词组…", self.attach_library_entry)
        self.fill_main()

    def main_result_menu(self, point):
        item = self.results.itemAt(point)
        if item is None:
            return
        action, value = item.data(Qt.UserRole)
        if action == "option":
            group = self.catalog.group(self.main_group)
            identity = value
        elif action == "grouped" and len(value) == 1:
            group = self.catalog.group(value[0][0])
            identity = value[0][1]
        else:
            return
        option = next((o for o in group.options if o.id == identity), None)
        if option:
            self.option_menu(self.results.viewport().mapToGlobal(point), group, option, self.results)

    def show_all_groups(self):
        if not getattr(self, "all_groups_open", False):
            self.all_groups_open = True
            self.group_scroll_value = 0
            QApplication.instance().installEventFilter(self)
            self.fill_main()

    def close_all_groups(self, *, refresh=True):
        if getattr(self, "all_groups_open", False):
            self.all_groups_open = False
            QApplication.instance().removeEventFilter(self)
            if refresh and self.isVisible():
                self.fill_main()
        else:
            self.all_groups_open = False

    def hideEvent(self, event):
        self.close_all_groups(refresh=False)
        super().hideEvent(event)

    def linked_groups(self):
        if self.mode == "main":
            return tuple(group.id for group in self.selectable_groups())
        host = self.catalog.group(self.token.main.group_id)
        return getattr(host, self.mode, ())

    def build_global_groups(self):
        header = QHBoxLayout()
        role = {"main": "主词", "prefixes": "前缀", "suffixes": "后缀"}[self.mode]
        self.text_label(f"请选择需加入{role}的词组", header)
        header.addStretch()
        close = button(header, "×", self.close_all_groups)
        close.setToolTip("关闭全部词组")
        self.main_rows.addLayout(header)
        if self.mode != "main":
            self.text_label("关联作用于共享主词组，保留已写入的值。", self.main_rows)
        picker = QScrollArea()
        picker.setWidgetResizable(True)
        picker.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        picker.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = ChipLayout()
        body.setLayout(layout)
        picker.setWidget(body)
        linked = set(self.linked_groups())
        for group in self.catalog.groups:
            active = group.id in linked
            choose = button(layout, ("✓ " if active else "") + group.name,
                            lambda g=group: self.toggle_global_group(g))
            choose.setCheckable(True)
            choose.setChecked(active)
            choose.setMaximumWidth(190)
            choose.setToolTip(group.name + (" · 已关联，点击解除" if active else " · 点击关联"))
            self.enable_group_menu(choose, group)
        picker.setFixedHeight(max(40, min(230, layout.heightForWidth(max(210, self.width() - 44)) + 6)))
        self.main_rows.addWidget(picker)
        self.group_scroll = picker
        position = getattr(self, "group_scroll_value", 0)
        QTimer.singleShot(0, picker, lambda: picker.verticalScrollBar().setValue(position))

    def toggle_global_group(self, group):
        self.group_scroll_value = self.group_scroll.verticalScrollBar().value()
        side = self.mode
        links = self.linked_groups()
        next_links = tuple(key for key in links if key != group.id) if group.id in links else (*links, group.id)
        if side == "main":
            module = replace(self.catalog.module(), groups=next_links, scope_limited=True)
            self.workspace.save_catalog(self.catalog.replace_module(module))
        else:
            token = self.token
            host = self.catalog.group(token.main.group_id)
            if host is None:
                if not token.main.text:
                    self.workspace.report("请先选择主词")
                    self.fill_main()
                    return
                selected = self.choose_host(token.main.text)
                if selected is None:
                    self.fill_main()
                    return
                host, main = selected
                if group.id == host.id:
                    self.workspace.report("主词组不能关联自身")
                    self.fill_main()
                    return
                catalog = self.catalog.replace_group(replace(host, **{side: next_links}))
                self.workspace.apply_token(replace(token, main=main), catalog_update=(catalog, (main.text,)))
            elif group.id != host.id:
                changed = self.workspace.save_catalog(self.catalog.replace_group(replace(host, **{side: next_links})))
                if changed and group.id in links and any(c.group_id == group.id for c in getattr(token, side)):
                    self.workspace.report("已解除关联，当前已选值及正文保留")
        self.fill_main()

    def choose_global_group(self, group):
        if self.mode != "main":
            self.apply_modifier_result(group)
            return
        module = self.catalog.module()
        if group.id not in self.linked_groups():
            updated = replace(module, groups=(*module.groups, group.id))
            if not self.workspace.save_catalog(self.catalog.replace_module(updated)):
                return
        self.browse_group(group.id)

    def browse_group(self, identity):
        self.parent_option = ""
        self.main_group = identity
        self.rebuild()
        self.query.setFocus()

    def choose_group_chips(self, title, choices, selected=None, *, removable=False):
        from PySide6.QtWidgets import QDialog
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("查找词组")
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        flow = ChipLayout()
        body.setLayout(flow)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        buttons = {}
        for identity, name in choices:
            chip = QPushButton(name)
            chip.setCheckable(True)
            chip.setChecked(identity == selected)
            chip.setToolTip(name)
            flow.addWidget(chip)
            buttons[identity] = chip
            if selected is not None:
                chip.clicked.connect(lambda checked, key=identity: (
                    result.append(key), dialog.accept()))
            if removable and identity:
                chip.setContextMenuPolicy(Qt.CustomContextMenu)
                def remove(point, key=identity, widget=chip):
                    menu = QMenu(widget)
                    menu.addAction("从本模块移除", lambda: (dialog.reject(), self.remove_scope_group(key)))
                    menu.exec(widget.mapToGlobal(point))
                chip.customContextMenuRequested.connect(remove)
        def filter_groups(text):
            for chip in buttons.values():
                chip.setVisible(text.strip().casefold() in chip.text().casefold())
            flow.invalidate()
            body.updateGeometry()
        search.textChanged.connect(filter_groups)
        result = []
        actions = QHBoxLayout()
        if selected is None:
            button(actions, "关联所选词组", dialog.accept)
        button(actions, "取消", dialog.reject)
        layout.addLayout(actions)
        dialog.resize(420, 280)
        if not dialog.exec():
            return ()
        return tuple(result) if selected is not None else tuple(key for key, chip in buttons.items() if chip.isChecked())

    def reset_query_limit(self, text=""):
        if text:
            self.close_all_groups(refresh=False)
        self.result_limit = 120
        self.fill_main()

    def fill_main(self):
        group = self.catalog.group(self.main_group) if self.mode == "main" else None
        query = self.query.text().strip()
        highlights = {} if self.workspace.settings.highlight_matches else None
        self.results.clear()
        self.time_widgets.clear()
        if group and not query:
            options = self.workspace.search_options(group, query, highlights=highlights)
            if not query and any(option.category for option in group.options):
                options = tuple(option for option in options if option.parent == self.parent_option)
            for option in options[:self.result_limit]:
                value = option.value()
                caption = self.option_breadcrumb(group, option) if option.parent and not option.pattern else value
                item = QListWidgetItem(caption, self.results)
                item.setData(Qt.UserRole, ("option", option.id))
                if query and not option.pattern:
                    item.setText(value)
                    item.setData(SOURCE_ROLE, f"词 · {group.name} ·")
                item.setToolTip("\n".join(filter(None, (
                    caption, option.description(), self.workspace.reference_status(option)
                ))))
                if option.pattern:
                    self.time_widgets.append((item, option))
            if len(options) > self.result_limit:
                item = QListWidgetItem(f"显示更多（共 {len(options)} 项）", self.results)
                item.setData(Qt.UserRole, ("more", self.result_limit))
            if query and group.kind in ("ordinary", "number"):
                item = QListWidgetItem(f"＋ 新建词组「{query}」", self.results)
                item.setData(Qt.UserRole, ("new-group", query))
                item.setToolTip(query)
                item = QListWidgetItem(f"确认使用「{query}」", self.results)
                item.setData(Qt.UserRole, ("temporary", query))
                item.setToolTip(query)
        else:
            groups = self.catalog.groups if query else self.selectable_groups()
            named = self.workspace.matching_group_names(query, groups) if query else ()
            for candidate in groups:
                if not query or candidate.id in named:
                    item = QListWidgetItem(candidate.name, self.results)
                    if query:
                        item.setData(SOURCE_ROLE, "词组 ·")
                    item.setData(Qt.UserRole, ("group", candidate.id))
                    item.setToolTip(candidate.name)
            if query:
                words = {}
                for owner, option in self.workspace.group_matches(query, groups, highlights=highlights):
                    words.setdefault(option.text, []).append((owner, option))
                for owner in groups:
                    if owner.kind == "timestamp":
                        for option in owner.values():
                            if option.pattern and query in option.value():
                                words.setdefault(option.value(), []).append((owner, option))
                for label, choices in list(words.items())[:80]:
                    names = " / ".join(sorted({g.name for g, _ in choices}))
                    item = QListWidgetItem(label, self.results)
                    item.setData(SOURCE_ROLE, f"词 · {names} ·")
                    item.setData(Qt.UserRole, ("grouped", tuple((g.id, o.id) for g, o in choices)))
                    item.setToolTip(f"组内词汇 · {label}\n{names}")
                for entry in self.workspace.search_library(query):
                    label = self.workspace.entry_text(entry)
                    if label in words and not self.complex_library_entry(entry):
                        continue
                    source = entry.get("source_path", "").replace("\\", "/").rsplit("/", 1)[-1]
                    item = QListWidgetItem(label, self.results)
                    item.setData(SOURCE_ROLE, f"{'原词条' if self.complex_library_entry(entry) else '词'} · {source or '普通词库'} ·")
                    item.setData(Qt.UserRole, ("entry", entry))
                    item.setToolTip(entry.get("full_content", ""))
                item = QListWidgetItem(f"＋ 新建词组「{query}」", self.results)
                item.setData(Qt.UserRole, ("new-group", query))
                item.setToolTip(query)
                item = QListWidgetItem(f"确认使用「{query}」", self.results)
                item.setData(Qt.UserRole, ("temporary", query))
                item.setToolTip(query)
        if query and highlights is not None:
            from ui.candidate_text import match_colors
            from core.config import THEMES
            theme = THEMES[self.workspace.settings.theme]
            for row in range(self.results.count()):
                item = self.results.item(row)
                if item.data(SOURCE_ROLE):
                    action, data = item.data(Qt.UserRole)
                    groups = data.get("highlight_groups", {}) if action == "entry" else highlights.get(item.text(), {})
                    label = item.text()
                    colors = match_colors(groups, theme)
                    item.setData(SOURCE_ROLE + 1, [
                        (len(label[:offset].encode("utf-16-le")) // 2,
                         len(label[offset].encode("utf-16-le")) // 2, shade)
                        for offset, shade in colors.items() if 0 <= offset < len(label)
                    ])
        if self.results.count():
            default_row = 0
            if query:
                exact = next((index for index in range(self.results.count())
                              if self.results.item(index).text().casefold() == query.casefold()
                              and self.results.item(index).data(Qt.UserRole)[0] != "group"), None)
                if exact is None:
                    exact = next((i for i in range(self.results.count())
                                  if self.results.item(i).text().casefold() == query.casefold()), None)
                last = self.results.count() - 1
                if exact is not None:
                    default_row = exact
                elif self.results.item(last).data(Qt.UserRole)[0] == "temporary":
                    default_row = last
            self.results.setCurrentRow(default_row)
        clear_layout(self.main_chips)
        clear_layout(self.main_rows)
        if not query and self.all_groups_open:
            self.build_global_groups()
        elif not query and self.mode != "main":
            self.build_modifiers(self.main_rows)
        elif not query and not group:
            self.build_main_rows()
        elif not query and group:
            self.add_breadcrumbs(self.main_rows, group)
            categories = ChipLayout()
            for option in group.options:
                if option.category and option.parent == self.parent_option:
                    chip = button(categories, "▸ " + option.text, lambda key=option.id: self.open_category(key))
                    chip.setMaximumWidth(205)
                    chip.setToolTip(option.text)
            self.main_rows.addLayout(categories)
        compact = not query
        self.results.setVisible(bool(self.results.count()) and not compact)
        if compact and self.mode == "main" and not self.all_groups_open:
            for row in range(self.results.count()):
                item = self.results.item(row)
                kind, identity = item.data(Qt.UserRole)
                if kind == "group":
                    continue
                if kind != "option":
                    continue
                option = next(option for option in group.options if option.id == identity)
                chip = SortButton(item.text(), self, ("option", group.id, option.parent),
                                  identity, lambda src, dst: self.move_visible_option(group, src, dst))
                chip.setCheckable(True)
                chip.setChecked(self.token.main.group_id == group.id and self.token.main.option_id == identity)
                chip.setToolTip(item.toolTip() if option.pattern else "\n".join(filter(None, (item.text(), item.toolTip()))))
                chip.setMaximumWidth(220)
                self.enable_option_menu(chip, group, option)
                chip.setProperty("candidateRow", row)
                chip.clicked.connect(lambda _, index=row: self.accept_main_item(self.results.item(index)))
                self.main_chips.addWidget(chip)
                if option.pattern:
                    self.time_widgets.append((chip, option))
        if not query and self.mode == "main" and group and not self.all_groups_open and group.kind in ("ordinary", "number"):
            self.add_main_button(self.main_chips, group)
        self.update_chip_navigation(self.results.currentRow())
        self.empty_results.setVisible(not self.results.count() and self.mode == "main" and not self.all_groups_open)
        self.lookup_button.setVisible(any(self.results.item(i).data(Qt.UserRole)[0] == "entry"
                                          for i in range(self.results.count())))
        self.size_list(self.results, 6)
        self.preview_candidate(self.results.currentItem())
        self.remeasure()

    def build_main_rows(self):
        groups = self.selectable_groups()
        for group in groups if self.expanded else groups[:5]:
            row = QHBoxLayout()
            title = SortButton("⠿ " + group.name, self, ("module-main", self.catalog.selected_module),
                               group.id, self.move_module_group)
            title.setToolTip(group.name + " · 拖动仅调整当前模块顺序")
            title.setMaximumWidth(160)
            title.clicked.connect(lambda _, key=group.id: self.browse_group(key))
            self.enable_group_menu(title, group)
            row.addWidget(title)
            chips = ChipLayout()
            for option in tuple(o for o in group.values() if not o.parent)[:6]:
                chip = SortButton(option.value(), self, ("option", group.id, option.parent), option.id,
                                  lambda source, target, g=group: self.move_visible_option(g, source, target))
                chip.setMaximumWidth(190)
                chip.setToolTip(option.description() or option.value())
                chip.setCheckable(True)
                chip.setChecked(self.token.main.group_id == group.id and self.token.main.option_id == option.id)
                chip.clicked.connect(lambda _, g=group, o=option: self.apply_main_choice(
                    g.choice(o, datetime.now()), g))
                self.enable_option_menu(chip, group, option)
                if option.pattern:
                    self.time_widgets.append((chip, option))
                chips.addWidget(chip)
            if group.kind in ("ordinary", "number"):
                self.add_main_button(chips, group)
            row.addLayout(chips, 1)
            self.main_rows.addLayout(row)
        if len(groups) > 5 and not self.expanded:
            more = button(self.main_rows, f"其他 {len(groups) - 5} 组 ▾", self.expand_groups)
            more.installEventFilter(self.workspace)
            more.setProperty("expandCompositionGroups", True)

    def enable_group_menu(self, widget, group):
        widget.setContextMenuPolicy(Qt.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda point: self.group_menu(widget.mapToGlobal(point), group, widget))

    def group_menu(self, point, group, widget):
        menu = QMenu(widget)
        menu.addAction("加入当前范围", lambda: self.choose_global_group(group))
        menu.addAction("管理词组", lambda: self.manage_search_group(group))
        if self.mode == "main" and group.id in self.linked_groups():
            menu.addAction("重命名", lambda: self.rename_search_group(group))
            menu.addAction("上移", lambda: self.step_module_group(group.id, -1))
            menu.addAction("下移", lambda: self.step_module_group(group.id, 1))
            menu.addAction("移出当前模块", lambda: self.remove_scope_group(group.id))
        elif self.mode in ("prefixes", "suffixes") and group.id in self.linked_groups():
            menu.addAction("解除当前方向关联", lambda: self.unlink_search_group(group))
        menu.exec(point)

    def unlink_search_group(self, group):
        host = self.catalog.group(self.token.main.group_id)
        side = self.mode
        if host and self.workspace.confirm("解除共享主词组的关联？已写入的组合词将保留。"):
            references = tuple(key for key in getattr(host, side) if key != group.id)
            self.save_catalog(self.catalog.replace_group(replace(host, **{side: references})))

    def rename_search_group(self, group):
        name, ok = QInputDialog.getText(self, "重命名共享词组", "名称", text=group.name)
        if ok and name.strip():
            self.save_catalog(self.catalog.replace_group(replace(group, name=name.strip())))

    def manage_search_group(self, group):
        self.main_group = group.id
        self.selected_group = group.id
        self.parent_option = ""
        self.set_mode("config-global")
        self.rebuild()

    def step_module_group(self, identity, delta):
        order = list(self.linked_groups())
        index = order.index(identity)
        target = index + delta
        if 0 <= target < len(order):
            order[index], order[target] = order[target], order[index]
            module = replace(self.catalog.module(), groups=tuple(order))
            self.save_catalog(self.catalog.replace_module(module))

    def create_search_group(self, value):
        name, ok = QInputDialog.getText(self, "新建词组", "名称", text=value)
        if not ok or not name.strip():
            return
        if any(group.name == name.strip() for group in self.catalog.groups):
            self.workspace.report("同名词组已存在，请从检索结果选择")
            return
        group = Group(name.strip())
        if self.mode != "main":
            self.apply_modifier_result(group)
            return
        catalog = self.catalog.replace_group(group)
        module = catalog.module()
        if module.groups or module.scope_limited:
            catalog = catalog.replace_module(replace(module, groups=(*module.groups, group.id)))
        if self.save_catalog(catalog):
            self.browse_group(group.id)

    def apply_modifier_result(self, group, option=None, *, create=False):
        side = self.mode
        host = self.catalog.group(self.token.main.group_id)
        already_linked = bool(host and group and group.id in getattr(host, side))
        if already_linked and option is not None and not create:
            self.select_modifier(group, option, getattr(host, side))
            return
        if already_linked and option is None:
            self.close_all_groups(refresh=False)
            self.query.clear()
            self.fill_main()
            return
        catalog = self.catalog
        main = self.token.main
        if host is None:
            selected = self.choose_host(main.text)
            if selected is None:
                return
            host, main = selected
        explicit_assignment = create
        role = "前缀" if side == "prefixes" else "后缀"
        if create:
            linked = getattr(host, side)
            candidates = sorted((g for g in catalog.groups if g.kind == "ordinary" and g.id != host.id),
                                key=lambda g: g.id not in linked)
            new = Group(f"{host.name} · {role}")
            options = [(g.id, ("✓ " if g.id in linked else "") + g.name, g) for g in candidates]
            options.append((new.id, "为词条新建词组…", new))
            group = self.workspace.pick_group_chips(f"将新词「{option.text}」加入词组", options,
                explanation=f"确认后将新词加入所选词组，并在需要时引用到当前{role}。共享规则适用于引用它的模块，已有正文保留。")
            if group is None:
                return
            existing = next((value for value in group.values() if value.text == option.text), None)
            if existing:
                option = existing
                create = False
            else:
                group = replace(group, options=(*group.options, option))
        if group.id == host.id:
            self.workspace.report("主词组不能关联自身")
            return
        role = "前缀" if side == "prefixes" else "后缀"
        references = getattr(host, side)
        if group.id not in references:
            references = (*references, group.id)
        if not explicit_assignment and not self.workspace.confirm(f"将「{group.name}」用于「{host.name}」的{role}？\n引用此主词组的模块会共享新规则，已有正文保留。"):
            return
        host = replace(host, **{side: references})
        catalog = catalog.replace_group(group).replace_group(host)
        module = catalog.module()
        if (module.groups or module.scope_limited) and host.id not in module.groups:
            catalog = catalog.replace_module(replace(module, groups=(*module.groups, host.id)))
        token = replace(self.token, main=main)
        if option is not None:
            token = choose_modifier(token, side, group.choice(option, datetime.now()), references)
        words = tuple(dict.fromkeys(
            ([main.text] if not self.token.main.group_id else []) + ([option.text] if create else [])))
        if self.workspace.apply_token(token, catalog_update=(catalog, words)):
            self.rebuild_timer.start()

    def choose_host(self, literal):
        exact = {group.id: (group, option) for group, option in self.workspace.matching_group_options(literal)}
        if len(exact) == 1:
            group, option = next(iter(exact.values()))
            return group, group.choice(option)
        if exact:
            group = self.workspace.pick("选择已有主词归属", [
                (key, group.name + " · " + key[:8], group) for key, (group, _) in exact.items()])
            return (group, group.choice(exact[group.id][1])) if group else None
        same_name = Group(literal)
        matches = [g for g in self.catalog.groups if g.kind == "ordinary"]
        same = [g for g in matches if g.name == literal]
        if not same and self.catalog.group(self.token.main.group_id) is None:
            choices = [(same_name.id, f"建立同名词组「{literal}」", same_name)]
        else:
            choices = []
        choices += [(g.id, g.name + " · " + g.id[:8], g) for g in matches]
        group = self.workspace.pick("选择主词归属并继承组规则", choices)
        if group is None:
            return None
        option = next((o for o in group.values() if o.text == literal), None)
        if option is None:
            option = Option(literal)
            group = replace(group, options=(*group.options, option))
        return group, group.choice(option)

    def move_module_group(self, source, target):
        module = self.catalog.module()
        order = tuple(group.id for group in self.selectable_groups())
        self.save_catalog(self.catalog.replace_module(replace(module, groups=move_before(order, source, target))))

    def add_main_button(self, chips, group):
        plus = button(chips, "＋", lambda: self.add_main_value(group))
        plus.setToolTip("输入新值并加入共享词组及专用词库")

    def add_main_value(self, group):
        value, ok = QInputDialog.getText(self, group.name, "输入新值，按 Enter 使用并会被加入组")
        if not ok or not value.strip():
            return
        try:
            value = group.numeric.format_value(value) if group.kind == "number" else value.strip()
        except ValueError as error:
            self.workspace.report(str(error))
            return
        parent = self.parent_option if self.main_group == group.id else ""
        existing = tuple(o for o in group.values() if o.text == value)
        option = next((o for o in existing if o.parent == parent), existing[0] if existing else None)
        if option is None:
            option = Option(value, parent=parent)
            group = replace(group, options=(*group.options, option))
        self.apply_main_choice(group.choice(option), group, word=value, catalog=self.catalog.replace_group(group))

    def update_chip_navigation(self, row):
        for index in range(self.main_chips.count()):
            widget = self.main_chips.itemAt(index).widget()
            label = "› " if row == widget.property("candidateRow") and self.query.hasFocus() else ""
            index = widget.property("candidateRow")
            item = self.results.item(index) if isinstance(index, int) else None
            if item:
                widget.setText(label + item.text())

    @staticmethod
    def option_breadcrumb(group, option):
        parts = [option.text]
        ancestry = {value.id: value for value in group.options if value.category}
        parent = option.parent
        while parent in ancestry:
            category = ancestry[parent]
            parts.append(category.text)
            parent = category.parent
        return " › ".join(reversed(parts))

    @staticmethod
    def complex_library_entry(entry):
        text = entry.get("full_content", "").strip("\r\n")
        return "\n" in text or "\r" in text or text.lstrip().startswith((chr(96) * 3, "~~~", "![", "# "))

    def preview_candidate(self, item):
        if not hasattr(self, "search_preview"):
            return
        data = item.data(Qt.UserRole) if item else None
        enabled = bool(data and data[0] in ("entry", "grouped"))
        self.search_preview.setVisible(enabled)
        if enabled and data[0] == "entry":
            self.search_preview.setMaximumHeight(175 if self.complex_library_entry(data[1]) else 100)
            from core.config import THEMES
            from ui.candidate_text import match_colors, highlighted_html
            theme = THEMES[self.workspace.settings.theme]
            entry = data[1]
            self.search_preview.setHtml(highlighted_html(entry.get("full_content", ""),
                match_colors(entry.get("highlight_groups", {}), theme), theme["text_color"]))
        elif enabled:
            from html import escape
            self.search_preview.setMaximumHeight(82)
            owners = " / ".join(dict.fromkeys(self.catalog.group(g).name for g, _ in data[1]))
            self.search_preview.setHtml(
                f'<p><small>组名 · {escape(owners)}</small></p><p>{escape(item.text())}</p>')
        if getattr(self, "lookup_button", None) is not None:
            self.lookup_button.setVisible(bool(data and data[0] == "entry" and self.mode == "main"))
        if self.attach_library is not None:
            self.attach_library.setVisible(bool(self.mode == "main" and data and data[0] == "entry" and not self.complex_library_entry(data[1])))
        self.remeasure()

    def remove_scope_group(self, identity):
        module = self.catalog.module()
        allowed = module.groups if (module.groups or module.scope_limited) else tuple(g.id for g in self.catalog.groups)
        if self.save_catalog(self.catalog.replace_module(replace(module, scope_limited=len(allowed) == 1,
                                                                groups=tuple(g for g in allowed if g != identity)))):
            if self.main_group == identity:
                self.main_group = ""
            self.rebuild()

    def accept_main_query(self):
        item = self.results.currentItem()
        if item:
            self.accept_main_item(item)

    def accept_main_item(self, item):
        action, value = item.data(Qt.UserRole)
        group = self.catalog.group(self.main_group)
        if action == "more":
            self.result_limit += 120
            self.fill_main()
            self.results.setCurrentRow(value)
            self.results.scrollToItem(self.results.item(value))
            return
        if action == "new-group":
            self.create_search_group(value)
            return
        if action == "group":
            self.activateWindow()
            if self.mode == "main":
                self.browse_group(value)
            else:
                self.choose_global_group(self.catalog.group(value))
            return
        if self.mode != "main":
            if action == "grouped":
                choices = [(g, self.catalog.group(g).name, (g, option)) for g, option in value]
                chosen = choices[0][2] if len(choices) == 1 else self.workspace.pick("选择所属修饰词组", choices)
                if chosen:
                    group = self.catalog.group(chosen[0])
                    option = next(o for o in group.values() if o.id == chosen[1])
                    self.apply_modifier_result(group, option)
            elif action == "temporary":
                self.apply_modifier_result(None, Option(value), create=True)
            elif action == "entry" and not self.complex_library_entry(value):
                self.apply_modifier_result(None, Option(self.workspace.entry_text(value)), create=True)
            return
        if action == "grouped":
            options = [(self.catalog.group(g), o) for g, o in value]
            labels = list({g.id: (g.id, g.name, g) for g, _ in options}.values())
            group = options[0][0] if len(labels) == 1 else self.workspace.pick("选择所属主词组", labels)
            if not group:
                return
            option = next(o for g, oid in options if g == group for o in g.options if o.id == oid)
            choice = group.choice(option, datetime.now())
        elif action == "option":
            option = next(item for item in group.options if item.id == value)
            choice = group.choice(option, datetime.now())
        elif action == "entry":
            if self.complex_library_entry(value):
                self.workspace.insert_entry(value)
                return
            label = self.workspace.entry_text(value)
            matches = self.workspace.matching_group_options(label)
            owners = {g.id: (g.id, g.name, g) for g, _ in matches}
            if owners:
                group = matches[0][0] if len(owners) == 1 else self.workspace.pick(
                    "选择所属主词组", list(owners.values()))
                if not group:
                    return
                choice = group.choice(next(o for g, o in matches if g == group))
            else:
                choice = Choice(label)
        else:
            try:
                text = group.numeric.format_value(value) if group and group.kind == "number" else value
            except ValueError as error:
                self.workspace.report(str(error))
                return
            choice = Choice(text, group.id if group else "")
        self.apply_main_choice(choice, group, word=choice.text if action == "temporary" else None)

    def apply_main_choice(self, choice, group, *, word=None, catalog=None):
        updated = replace_main(self.token, choice, group)
        discarded = len(self.token.prefixes + self.token.suffixes) - len(updated.prefixes + updated.suffixes)
        transaction = (catalog or self.catalog, (word,)) if word is not None else None
        if discarded and not self.workspace.confirm(f"切换主词组将移除 {discarded} 个不兼容的修饰值，可撤销。"):
            return
        if self.workspace.apply_token(updated, catalog_update=transaction):
            self.main_group = choice.group_id
            self.selected_group = ""
            self.parent_option = ""
            self.expanded = False
            self.positions.clear()
            self.set_mode("")
            self.workspace.window().activateWindow()
            self.workspace.editor.setFocus()

    def attach_library_entry(self):
        item = self.results.currentItem()
        if not item:
            return
        action, entry = item.data(Qt.UserRole)
        if action != "entry" or self.complex_library_entry(entry):
            return
        sentinel = "new"
        owners = [(g.id, g.name, g.id) for g in self.catalog.groups if g.kind == "ordinary"]
        identity = self.workspace.pick("关联到主词组", [*owners, (sentinel, "＋ 新建主词组", sentinel)])
        if not identity:
            return
        catalog = self.catalog
        if identity == sentinel:
            name, ok = QInputDialog.getText(self, "新建主词组", "组名")
            if not ok or not name.strip():
                return
            group = Group(name.strip())
            module = catalog.module()
            if module.groups or module.scope_limited:
                catalog = catalog.replace_module(replace(module, groups=(*module.groups, group.id),
                                                         scope_limited=False))
        else:
            group = catalog.group(identity)
        option = next((o for o in group.values() if o.text == self.workspace.entry_text(entry)), None)
        if option is None:
            option = self.workspace.referenced_option(entry, "")
            group = replace(group, options=(*group.options, option))
            if not self.workspace.save_catalog(catalog.replace_group(group)):
                return
        candidate = QListWidgetItem()
        candidate.setData(Qt.UserRole, ("grouped", ((group.id, option.id),)))
        self.accept_main_item(candidate)

    def insert_library_entry(self):
        current = self.results.currentItem()
        if current:
            action, entry = current.data(Qt.UserRole)
            if action == "entry":
                self.workspace.insert_entry(entry)

    def build_modifiers(self, layout):
        group = self.catalog.group(self.token.main.group_id)
        references = getattr(group, self.mode) if group else ()
        if not references:
            button(layout, "配置关联词组", self.toggle_config)
        for index, identity in enumerate(references):
            if index == 5:
                extender = button(layout, f"其他 {len(references) - 5} 组 ▾", self.expand_groups)
                extender.installEventFilter(self.workspace)
                extender.setProperty("expandCompositionGroups", True)
                if not self.expanded:
                    break
            modifier = self.catalog.group(identity)
            if modifier:
                self.build_modifier_row(modifier, references, layout)
        for choice in getattr(self.token, self.mode):
            if choice.group_id not in references:
                row = QHBoxLayout()
                self.text_label(f"当前保留 · {choice.text}", row)
                button(row, "移除", lambda _=False, value=choice: self.remove_unlinked(value))
                layout.addLayout(row)

    def remove_unlinked(self, value):
        side = self.mode
        if self.workspace.apply_token(replace(self.token, **{side: tuple(
                c for c in getattr(self.token, side) if c != value)})):
            self.rebuild_timer.start()

    def expand_groups(self):
        if not self.expanded:
            self.expanded = True
            self.rebuild_timer.start()

    def build_modifier_row(self, group, references, layout):
        row = QHBoxLayout()
        title = SortButton("⠿ " + group.name, self, ("group", self.mode), group.id, self.move_visible_group)
        title.setToolTip("点击配置，拖动调整组顺序")
        title.clicked.connect(lambda: self.edit_modifier_group(group.id))
        row.addWidget(title)
        self.enable_group_menu(title, group)
        chips = ChipLayout()
        selected = next((choice for choice in getattr(self.token, self.mode) if choice.group_id == group.id), None)
        options = group.values()
        direct = options if group.kind == "timestamp" else tuple(option for option in options if not option.parent)
        for index, option in enumerate(direct[:12]):
            value = SortButton(option.value(), self, ("option", group.id, option.parent), option.id,
                               lambda source, target: self.move_visible_option(group, source, target))
            value.setMaximumWidth(220)
            value.setCheckable(True)
            value.setChecked(bool(selected and selected.option_id == option.id))
            value.setToolTip("\n".join(filter(None, (
                option.text, option.description(), self.workspace.reference_status(option)
            ))))
            value.clicked.connect(lambda _=False, opt=option: self.select_modifier(group, opt, references))
            self.enable_option_menu(value, group, option)
            if option.pattern:
                self.time_widgets.append((value, option))
            chips.addWidget(value)
        row.addLayout(chips, 1)
        extras = tuple(o for o in options if o.parent or o not in direct[:12])
        if extras:
            picker = QToolButton()
            picker.setText("更多选项…")
            picker.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(picker)
            children = {}
            for option in group.options:
                if option.category or option in extras:
                    children.setdefault(option.parent, []).append(option)

            def add_choices(parent_menu, identity):
                for option in children.get(identity, ()):
                    if option.category:
                        add_choices(parent_menu.addMenu(option.text), option.id)
                    else:
                        parent_menu.addAction(option.value(), lambda _checked=False, selected=option:
                                              self.select_modifier(group, selected, references))
            add_choices(menu, "")
            picker.setMenu(menu)
            chips.addWidget(picker)
        if group.kind in ("number", "ordinary"):
            plus = QPushButton("＋")
            plus.setToolTip("添加到当前词组及组合输入专用词库")
            chips.addWidget(plus)
            temporary = QLineEdit()
            temporary.hide()
            temporary.setProperty("temporaryCompositionValue", True)
            temporary.setPlaceholderText("输入新值，按 Enter 使用并会被加入组")
            temporary.installEventFilter(self)
            temporary.returnPressed.connect(lambda: self.use_temporary_modifier(group, temporary.text(), references))
            plus.clicked.connect(lambda: (temporary.show(), self.activateWindow(), temporary.setFocus(),
                                          self.remeasure()))
            layout.addLayout(row)
            layout.addWidget(temporary)
        else:
            layout.addLayout(row)

    def move_visible_group(self, source, target):
        host = self.catalog.group(self.token.main.group_id)
        side = self.mode
        if not host or side not in ("prefixes", "suffixes"):
            return
        new_order = move_before(getattr(host, side), source, target)
        if self.save_catalog(self.catalog.replace_group(replace(host, **{side: new_order}))):
            current = getattr(self.token, side)
            ranked = iter(sorted((value for value in current if value.group_id in new_order),
                                 key=lambda value: new_order.index(value.group_id)))
            reordered = tuple(next(ranked) if value.group_id in new_order else value for value in current)
            if current != reordered:
                self.workspace.apply_token(replace(self.token, **{side: reordered}))

    def move_visible_option(self, group, source, target):
        options = move_before(group.options, source, target, key=lambda option: option.id)
        self.save_catalog(self.catalog.replace_group(replace(group, options=options)))

    def use_temporary_modifier(self, group, text, references):
        try:
            value = group.numeric.format_value(text) if group.kind == "number" else text.strip()
            if not value:
                return
            existing = next((option for option in group.options
                             if not option.parent and not option.category and option.text == value), None)
            option = existing or Option(value)
            updated = group if existing else replace(group, options=(*group.options, option))
            self.select_modifier(updated, option, references, before_apply=lambda: self.save_catalog(
                self.catalog.replace_group(updated), word=value))
        except ValueError as error:
            self.workspace.report(str(error))

    def select_modifier(self, group, option, references, *, before_apply=None):
        current = next((choice for choice in getattr(self.token, self.mode)
                        if choice.group_id == group.id and choice.option_id and choice.option_id == option.id), None)
        choice = current or group.choice(option, datetime.now())
        if self.workspace.apply_token(choose_modifier(self.token, self.mode, choice, references),
                                      before_apply=before_apply):
            self.locked = True
            self.rebuild_timer.start()

    def edit_modifier_group(self, identity):
        self.selected_group = identity
        self.parent_option = ""
        self.toggle_config()

    def build_config(self, side, host=None):
        host = host or self.catalog.group(self.token.main.group_id)
        names = {"main": "主词组", "prefixes": "前缀引用", "suffixes": "后缀引用", "global": "全局词组管理"}
        title = self.text_label(("配置 · " if side != "global" else "") + names[side]
                                + (f" · 当前绑定 {host.name}" if host and side != "global" else ""))
        title.setProperty("compositionContext", True)
        title.setWordWrap(True)
        title.setToolTip(title.text())
        if side == "main" and host and getattr(self, "_config_host", None) != host.id:
            self.selected_group = host.id
            self._config_host = host.id
        if side == "global":
            references = tuple(group.id for group in self.catalog.groups)
        elif side == "main":
            references = tuple(group.id for group in self.selectable_groups())
            if not host and self.main_group in references and not self.selected_group:
                self.selected_group = self.main_group
            if self.token.main.text and not host:
                button(self.body_layout, "为当前主词选择词组…", lambda: self.bind_main("main", False))
            elif host and host.id not in references:
                button(self.body_layout, "查看当前绑定组", lambda: self.inspect_bound_group(host))
        else:
            references = getattr(host, side) if host else ()
        if side in ("prefixes", "suffixes") and not references:
            self.compact_config = True
            if host is None:
                main = self.token.main.text
                details = self.text_label(f"「{main}」尚未归入词组。")
                details.setWordWrap(True)
                details.setToolTip(details.text())
                if main:
                    create = button(self.body_layout, f"建立同名词组「{main}」", lambda: self.bind_main(side, True))
                    create.setToolTip(create.text())
                    create.setProperty("compositionPrimary", True)
                    button(self.body_layout, "加入已有词组…", lambda: self.bind_main(side, False))
            else:
                self.text_label("当前方向尚未关联词组")
                self.add_reference_control(self.body_layout, side, host)
                button(self.body_layout, "新建词组", lambda: self.new_group(side, host))
            return
        columns = QHBoxLayout()
        left = QVBoxLayout()
        right = QVBoxLayout()
        left.setAlignment(Qt.AlignTop)
        right.setAlignment(Qt.AlignTop)
        columns.addLayout(left, 1)
        columns.addLayout(right, 2)
        self.body_layout.addLayout(columns)
        navigation = make_list()
        navigation.setAccessibleName("词组导航")
        if self.selected_group not in references:
            self.selected_group = references[0] if references else ""
            self.parent_option = ""
        fill_list(navigation, ((identity, self.catalog.group(identity).name) for identity in references),
                  self.selected_group)

        navigation.model().rowsMoved.connect(lambda *_: self.reorder_groups(side, host, list_ids(navigation)))
        self.size_list(navigation, 10)
        search = QLineEdit()
        search.setPlaceholderText("查找词组")
        search.setClearButtonEnabled(True)
        hits = {}
        def filter_groups(query):
            query = query.strip()
            hits.clear()
            groups = tuple(self.catalog.group(identity) for identity in references)
            matching_names = self.workspace.matching_group_names(query, groups) if query else set(references)
            if query:
                for group, option in self.workspace.group_matches(query, groups):
                    hits.setdefault(group.id, option)
            for index in range(navigation.count()):
                item = navigation.item(index)
                identity = item.data(Qt.UserRole)
                group = self.catalog.group(identity)
                hit = hits.get(identity)
                item.setText(group.name + (f" · {hit.text}" if hit else ""))
                item.setToolTip(self.option_breadcrumb(group, hit) if hit else group.name)
                item.setHidden(identity not in matching_names and hit is None)
            navigation.setDragEnabled(not query)
        def select_hit(item):
            identity = item.data(Qt.UserRole)
            self.selected_group = identity
            self.parent_option = hits[identity].parent if identity in hits else ""
            self.highlight_option = hits[identity].id if identity in hits else ""
            self.rebuild_timer.start()
        search.textChanged.connect(filter_groups)
        navigation.itemClicked.connect(select_hit)
        left.addWidget(search)
        left.addWidget(navigation)
        button(left, "新建组", lambda: self.new_group(side, host))
        if side != "global":
            self.add_reference_control(left, side, host)
        group = self.catalog.group(self.selected_group)
        if group:
            self.edit_group(right, group, side, host)

    def inspect_bound_group(self, group):
        self.selected_group = group.id
        self.parent_option = ""
        self.set_mode("config-global")

    def add_reference_control(self, layout, side, host):
        button(layout, "引用已有词组", lambda: self.reference_group(side, host)).setProperty("compositionPrimary", True)

    def bind_main(self, side, same_name):
        literal = self.token.main.text
        groups = tuple(g for g in self.catalog.groups if not same_name or g.name == literal)
        choices = tuple((g.id, g.name + " · " + g.id[:8]) for g in groups if g.kind == "ordinary")
        exact = {g.id: (g, o) for g, o in self.workspace.matching_group_options(literal)}
        if len(exact) == 1:
            group, _ = next(iter(exact.values()))
        elif len(exact) > 1:
            selection = self.workspace.pick("选择已有主词归属", [
                (identity, g.name + " · " + identity[:8], g) for identity, (g, _) in exact.items()
            ])
            if selection is None:
                return
            group = selection
        elif same_name and not groups:
            group = Group(literal)
        elif choices:
            picked = self.choose_group_chips("加入词组并继承其前后缀规则", choices, "")
            if not picked:
                return
            group = self.catalog.group(picked[0])
        else:
            self.workspace.report("没有可加入的同名普通词组" if same_name else "没有可加入的普通词组")
            return
        option = next((o for o in group.values() if o.text == literal), None)
        if option is None:
            option = Option(literal)
            group = replace(group, options=(*group.options, option))
        catalog = self.catalog.replace_group(group)
        module = catalog.module()
        if (module.groups or module.scope_limited) and group.id not in module.groups:
            catalog = catalog.replace_module(replace(module, groups=(*module.groups, group.id)))
        token = replace(self.token, main=group.choice(option))
        if self.workspace.apply_token(token, before_apply=lambda: self.save_catalog(catalog, word=literal)):
            self.main_group = group.id
            self.set_mode("config-" + side)
            self.rebuild()

    def select_config_group(self, identity):
        self.selected_group = identity
        self.parent_option = ""
        self.rebuild_timer.start()

    def save_catalog(self, catalog, *, word=None):
        if self.workspace.save_catalog(catalog, word=word):
            self.rebuild_timer.start()
            return True
        return False

    def new_group(self, side, host):
        name, ok = QInputDialog.getText(self, "新建词组", "名称")
        if not ok or not name.strip():
            return
        kind, ok = QInputDialog.getItem(self, "词组类型", "类型", ["普通", "数值", "时间戳"], editable=False)
        if not ok:
            return
        group = Group(name.strip(), kind={"普通": "ordinary", "数值": "number", "时间戳": "timestamp"}[kind])
        catalog = self.catalog.replace_group(group)
        if side == "main":
            module = catalog.module()
            identities = tuple(g.id for g in self.selectable_groups())
            catalog = catalog.replace_module(replace(module, groups=(*identities, group.id), scope_limited=True))
        elif side != "global":
            catalog = catalog.replace_group(replace(host, **{side: (*getattr(host, side), group.id)}))
        if self.save_catalog(catalog):
            self.selected_group = group.id
            self.parent_option = ""
            if side == "main":
                self.main_group = group.id

    def reference_group(self, side, host):
        linked = (tuple(g.id for g in self.selectable_groups()) if side == "main" else getattr(host, side))
        unused = tuple(g for g in self.catalog.groups
                       if g.id not in linked and (side == "main" or g.id != host.id))
        identities = self.choose_group_chips("引用已有词组", tuple((g.id, g.name) for g in unused))
        if not identities:
            return
        order = (*linked, *identities)
        if self.update_group_references(side, host, order):
            self.selected_group = identities[0]
            self.parent_option = ""

    def update_group_references(self, side, host, order):
        if side == "main":
            return self.save_catalog(self.catalog.replace_module(
                replace(self.catalog.module(), groups=order, scope_limited=True)))
        return self.save_catalog(self.catalog.replace_group(replace(host, **{side: order})))

    def reorder_groups(self, side, host, order):
        if side == "global":
            self.save_catalog(replace(self.catalog, groups=tuple(self.catalog.group(i) for i in order)))
        else:
            self.update_group_references(side, host, order)

    def edit_group(self, layout, group, side, host):
        name = QLineEdit(group.name)
        name.setAccessibleName("当前共享词组名称")
        name.setToolTip(group.name)
        name.textChanged.connect(name.setToolTip)
        name.returnPressed.connect(lambda: self.save_catalog(self.catalog.replace_group(
            replace(group, name=name.text().strip()))))
        layout.addWidget(name)
        self.add_breadcrumbs(layout, group)
        options = ChipLayout()
        for option in group.options:
            if option.parent != self.parent_option:
                continue
            chip = SortButton(("▸ " if option.category else "") + option.text, self,
                              ("option", group.id, option.parent), option.id,
                              lambda source, target: self.move_visible_option(group, source, target))
            chip.setMaximumWidth(245)
            chip.setToolTip(option.text)
            if option.id == getattr(self, "highlight_option", ""):
                chip.setCheckable(True)
                chip.setChecked(True)
            chip.clicked.connect(lambda _, value=option: self.open_option(group, value.id))
            chip.setContextMenuPolicy(Qt.CustomContextMenu)
            chip.customContextMenuRequested.connect(
                lambda point, value=option, widget=chip: self.option_menu(widget.mapToGlobal(point), group, value, widget))
            options.addWidget(chip)
        layout.addLayout(options)
        add = QLineEdit()
        add.setPlaceholderText("输入新词、已有词或分类名")
        add.setClearButtonEnabled(True)
        layout.addWidget(add)
        actions = QHBoxLayout()
        button(actions, "＋词汇", lambda: self.add_option(group, add.text()))
        button(actions, "＋分类", lambda: self.add_option(group, add.text(), category=True))
        if group.kind == "ordinary":
            button(actions, "引用词库词汇", lambda: self.add_reference(group, add.text()))
        layout.addLayout(actions)
        if group.kind == "number":
            button(layout, "范围、步长和单位", lambda: self.configure_numeric(group))
        tools = QHBoxLayout()
        button(tools, "复制组", lambda: self.duplicate_group(group))
        if side == "global":
            button(tools, "删除组", lambda: self.remove_group(group))
            self.add_group_rule_controls(layout, group)
        elif side == "main":
            button(tools, "解除引用", lambda: self.remove_scope_group(group.id))
        else:
            button(tools, "解除引用", lambda: self.update_group_references(
                side, host, tuple(i for i in getattr(host, side) if i != group.id)))
        layout.addLayout(tools)

    def add_group_rule_controls(self, layout, group):
        links = QHBoxLayout()

        def open_rules(side):
            self.managed_host = group.id
            self.selected_group = ""
            self.parent_option = ""
            self.set_mode("config-global-" + side)

        button(links, "前缀引用", lambda: open_rules("prefixes"))
        button(links, "后缀引用", lambda: open_rules("suffixes"))
        layout.addLayout(links)

    def add_breadcrumbs(self, layout, group):
        categories = {option.id: option for option in group.options if option.category}
        if self.parent_option and self.parent_option not in categories:
            self.parent_option = ""
        if self.parent_option:
            category = categories[self.parent_option]
            path = [(category.id, category.text)]
            while category.parent in categories:
                category = categories[category.parent]
                path.append((category.id, category.text))
            crumbs = ChipLayout()
            for identity, label in [("", group.name), *reversed(path)]:
                crumb = button(crumbs, "▸ " + label, lambda key=identity: self.open_category(key))
                crumb.setToolTip(label)
                crumb.setMaximumWidth(205)
                crumb.setEnabled(identity != self.parent_option)
            layout.addLayout(crumbs)

    def open_category(self, identity):
        self.parent_option = identity
        self.rebuild_timer.start()

    def open_option(self, group, identity):
        option = next(o for o in group.options if o.id == identity)
        if option.category:
            self.open_category(identity)
        else:
            self.edit_option(group, option)

    def add_option(self, group, text, category=False):
        text = text.strip()
        if not text:
            return
        pattern = ""
        if not category and group.kind == "timestamp":
            pattern, ok = QInputDialog.getText(self, "时间格式", "格式，例如 %Y-%m-%d")
            if not ok or not pattern:
                return
        if not category and group.kind == "number":
            try:
                text = group.numeric.format_value(text)
            except ValueError as error:
                self.workspace.report(str(error))
                return
        if any(o.parent == self.parent_option and o.category == category and o.text == text
               and o.pattern == pattern for o in group.options):
            self.workspace.report("该项已存在于当前分类")
            return
        option = Option(text, parent=self.parent_option, category=category, pattern=pattern)
        self.save_catalog(self.catalog.replace_group(replace(group, options=(*group.options, option))),
                          word=text if not category and group.kind == "ordinary" else None)

    def add_reference(self, group, query):
        entry = self.workspace.pick_entry(query)
        if not entry:
            return
        option = self.workspace.referenced_option(entry, self.parent_option)
        if any(o.parent == option.parent and o.text == option.text and o.source == option.source
               for o in group.options):
            self.workspace.report("该词汇已存在于当前分类")
            return
        self.save_catalog(self.catalog.replace_group(replace(group, options=(*group.options, option))))

    def reorder_options(self, group, order):
        options = {option.id: option for option in group.options}
        selected = iter(order)
        updated = tuple(options[next(selected)] if o.id in order else o for o in group.options)
        self.save_catalog(self.catalog.replace_group(replace(group, options=updated)))

    def edit_option(self, group, option):
        text, ok = QInputDialog.getText(self, "编辑共享词组成员",
                                       "显示文字（影响引用此组的选择，保留已有正文）", text=option.text)
        if not ok or not text.strip():
            return
        if option.pattern:
            pattern, ok = QInputDialog.getText(self, "编辑时间格式", "格式", text=option.pattern)
            if not ok or not pattern:
                return
        else:
            pattern = ""
        edited = replace(option, text=text.strip(), pattern=pattern)
        self.save_catalog(self.catalog.replace_group(replace(group, options=tuple(
            edited if current.id == option.id else current for current in group.options))))

    def enable_option_menu(self, widget, group, option):
        widget.setContextMenuPolicy(Qt.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda point: self.option_menu(widget.mapToGlobal(point), group, option, widget))

    def option_menu(self, pos, group, option, widget):
        menu = QMenu(widget)
        menu.addAction("重命名及编辑", lambda: self.edit_option(group, option))
        if not option.category:
            menu.addAction("复制词汇", lambda: QApplication.clipboard().setText(option.text))
        menu.addAction("说明和判断标准", lambda: self.edit_description(group, option))
        if not option.category and group.kind == "ordinary":
            menu.addAction("重新关联词库", lambda: self.relink(group, option))
        if any(item.category for item in group.options):
            menu.addAction("移动到分类…", lambda: self.move_to_category(group, option))
        menu.addAction("移出组", lambda: self.remove_option(group, option))
        menu.exec(pos)

    def move_to_category(self, group, option):
        categories = {item.id: item for item in group.options if item.category}
        choices = [("", "词组根目录")]
        for key, item in categories.items():
            ancestor = key
            while ancestor in categories and ancestor != option.id:
                ancestor = categories[ancestor].parent
            if ancestor != option.id:
                choices.append((key, self.option_breadcrumb(group, item)))
        selected = self.choose_group_chips("移动到分类", choices, option.parent)
        if selected and selected[0] != option.parent:
            updated = replace(option, parent=selected[0])
            self.save_catalog(self.catalog.replace_group(replace(group, options=tuple(
                updated if cur.id == option.id else cur for cur in group.options))))

    def relink(self, group, option):
        entry = self.workspace.pick_entry(option.text)
        if entry:
            ref = self.workspace.referenced_option(entry, option.parent)
            linked = replace(option, text=ref.text, source=ref.source, fingerprint=ref.fingerprint)
            self.save_catalog(self.catalog.replace_group(replace(group, options=tuple(
                linked if item.id == option.id else item for item in group.options))))

    def edit_description(self, group, option):
        changes = {}
        for field, label in (("definition", "定义"), ("criteria", "判断标准"),
                             ("explanation", "说明"), ("counterexample", "反例")):
            value, ok = QInputDialog.getMultiLineText(self, "词汇说明", label, getattr(option, field))
            if not ok:
                return
            changes[field] = value
        edited = replace(option, **changes)
        self.save_catalog(self.catalog.replace_group(replace(group, options=tuple(
            edited if o.id == option.id else o for o in group.options))))

    def remove_option(self, group, option):
        removed = {option.id}
        while True:
            descendants = {o.id for o in group.options if o.parent in removed}
            if descendants <= removed:
                break
            removed |= descendants
        if not self.workspace.confirm("从共享词组移除此项及子项？已有正文和 Markdown 词条将保留。"):
            return
        self.save_catalog(self.catalog.replace_group(replace(group, options=tuple(
            o for o in group.options if o.id not in removed))))

    def configure_numeric(self, group):
        settings = {}
        for field, label in (("minimum", "最小值"), ("maximum", "最大值"), ("step", "步长"), ("unit", "单位")):
            value, ok = QInputDialog.getText(self, "数值组", label, text=getattr(group.numeric, field))
            if not ok:
                return
            settings[field] = value.strip()
        numeric = Numeric(**settings)
        self.save_catalog(self.catalog.replace_group(replace(group, numeric=numeric)))

    def duplicate_group(self, group):
        identities = {o.id: new_id() for o in group.options}
        copy = replace(group, id=new_id(), name=group.name + " 副本", options=tuple(
            replace(o, id=identities[o.id], parent=identities.get(o.parent, "")) for o in group.options))
        if self.save_catalog(self.catalog.replace_group(copy)):
            self.selected_group = copy.id

    def remove_group(self, group):
        if self.workspace.confirm(f"删除词组「{group.name}」并解除它在各处的引用？历史正文会保留。"):
            self.save_catalog(self.catalog.remove_group(group.id))
