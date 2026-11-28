
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from core.composition.backups import Backups
from core.composition.document import Choice, Document, Token
from core.composition.groups import Catalog, Group, Module, Numeric, Option, TIMESTAMP_ID, choose_modifier, replace_main
from core.composition.recognition import Recognizer, position_parts
from core.composition.store import CompositionStorageError, CompositionStore
from core.composition.structure import clean_template_output, dimension_status, template_fragment
from ui.search_popup import InputMode, SearchPopup


def example_catalog():
    catalog = Catalog.defaults()
    priority = Group("Priority", "priority", options=(Option("urgent", "urgent"), Option("routine", "routine")))
    status = Group("Status", "status", options=(Option("queued", "queued"), Option("done", "done")))
    tasks = Group("Task type", "task", options=(Option("review", "review"), Option("archive", "archive")),
                  prefixes=("priority",), suffixes=("status",))
    for group in (priority, status, tasks):
        catalog = catalog.replace_group(group)
    return catalog


class CompositionPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = CompositionStore(self.root / "conf.sqlite3", self.root / "library.md")
        self.store.set("catalog", example_catalog().to_record())
        self.settings = SimpleNamespace(
            width=430, height=250, theme="light", font_size=14, word_wrap_enabled=True,
            multi_word_search=False, pinyin_initial_search=False, highlight_matches=False,
            show_source_enabled=False, save=lambda: True,
        )
        entries = [{"full_content": "prototype", "parent": "prototype"}]
        self.manager = SimpleNamespace(word_blocks=entries, clipboard_history=[],
                                       find_matches=lambda query, *_: entries if query else [])

    def open(self):
        popup = SearchPopup(self.manager, self.settings)
        if os.environ.get("QUICKKV_TEST_HIDDEN"):
            popup.setAttribute(Qt.WA_DontShowOnScreen)
        popup.composition.factory = lambda: self.store
        self.addCleanup(popup.close)
        popup.reappear_in_place()
        self.app.processEvents()
        return popup

    def test_menu_text_and_background_follow_theme_across_submenu_ownership(self):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QMenu
        from core.config import THEMES

        popup = self.open()
        popup.composition_button.click()
        workspace_menu = QMenu(popup.composition)
        workspace_child = workspace_menu.addMenu("Outline actions")
        tray_menu = QMenu()
        tray_file_menu = QMenu("Library", tray_menu)
        tray_child = QMenu("Clipboard")
        tray_menu.addMenu(tray_child)
        tray_menu.addMenu(tray_file_menu)
        menus = (workspace_menu, workspace_child, tray_menu, tray_child, tray_file_menu)
        actions = [menu.addAction("Theme preview text") for menu in menus]
        for name in ("dark", "light", "dark"):
            self.settings.theme = name
            popup.apply_theme()
            theme = THEMES[name]
            fg = QColor(theme["text_color"]).rgb()
            bg = QColor(theme["bg_color"]).rgb()
            for menu, action in zip(menus, actions):
                with self.subTest(theme=name, title=menu.title()):
                    menu.ensurePolished()
                    menu.resize(menu.sizeHint())
                    snapshot = menu.grab().toImage()
                    scale = snapshot.devicePixelRatio()
                    row = menu.actionGeometry(action)
                    pixels = {
                        snapshot.pixel(x, y)
                        for x in range(round((row.left() + 8) * scale), round((row.right() - 8) * scale))
                        for y in range(round((row.top() + 2) * scale), round((row.bottom() - 2) * scale))
                    }
                    self.assertIn(bg, pixels)
                    self.assertIn(fg, pixels)

    def test_more_menu_opens_global_group_management_without_editing_draft(self):
        from PySide6.QtCore import QTimer
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertFalse(ui.editor.model.tokens)
        original = ui.editor.model

        def trigger():
            menu = self.app.activePopupWidget()
            actions = {action.text(): action for action in menu.actions()}
            menu.close()
            self.assertNotIn("立即保存草稿", actions)
            actions["管理词组"].trigger()

        QTimer.singleShot(30, trigger)
        QTest.mouseClick(ui.more, Qt.LeftButton)
        self.assertEqual(ui.panel.mode, "config-global")
        self.assertTrue(ui.panel.groups_button.isChecked())
        self.assertFalse(ui.panel.config.isChecked())
        close = next(b for b in ui.panel.findChildren(QPushButton)
                     if b.accessibleName() == "关闭全局词组管理")
        close.click()
        self.assertTrue(ui.panel.isVisible())
        self.assertFalse(ui.panel.groups_button.isChecked())
        self.assertEqual(ui.panel.mode, "")
        self.assertEqual(ui.editor.model, original)

    def test_same_group_changes_main_five_times_without_collapsing(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- urgent+review+queued"))
        token, = ui.editor.model.tokens
        ui.panel.open_token(token, mode="main", locked=True)
        group = ui.catalog.group("task")
        for word in ("archive", "review", "archive", "review", "archive"):
            button = next(b for b in ui.panel.findChildren(QPushButton)
                          if b.isVisible() and b.text() == word)
            button.click()
            self.app.processEvents()
            self.assertEqual(ui.editor.model.tokens[0].main.text, word)
            self.assertEqual(ui.editor.model.tokens[0].prefixes, token.prefixes)
            self.assertEqual(ui.editor.model.tokens[0].suffixes, token.suffixes)
            self.assertTrue(ui.panel.main.isChecked())
            self.assertTrue(ui.panel.isVisible())

    def test_draft_failure_blocks_exit_paths_and_retry_preserves_caret(self):
        from unittest.mock import patch
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClicks(ui.editor, "prepare")
        ui.editor.select_offsets(2, 5)
        with patch.object(self.store, "save_draft", side_effect=OSError("isolated failure")):
            popup.set_input_mode(InputMode.SEARCH)
            self.assertEqual(popup.input_mode, InputMode.COMPOSITION)
            self.assertFalse(popup.close())
            self.assertTrue(ui.dirty)
            self.assertTrue(ui.autosave.isActive())
            self.assertIn("保存失败", ui.status.text())
        ui.autosave.timeout.emit()
        self.assertFalse(ui.dirty)
        _, model, anchor, cursor = self.store.load_draft(ui.session_id)
        self.assertEqual((model.raw, anchor, cursor), ("prepare", 2, 5))

    def test_tab_draft_uses_font_metrics_and_keeps_original_line_endings(self):
        from PySide6.QtGui import QFontMetricsF
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        raw = "- Root\r\n\t- review\r\n\t\t- nested\n- Outside"
        self.assertTrue(ui.open_reload(raw))
        editor = ui.editor
        self.assertEqual(editor.model.raw, raw)
        before = editor.tabStopDistance()
        ui.settings.font_size = 29
        ui.apply_theme()
        self.app.processEvents()
        self.assertGreater(editor.tabStopDistance(), before)
        self.assertAlmostEqual(editor.tabStopDistance(), 4 * QFontMetricsF(editor.font()).horizontalAdvance(" "))
        editor.add_sibling(index=2, child=True)
        self.assertIn("\t\t\t- ", editor.model.raw)
        editor.undo()
        self.assertEqual(editor.model.raw, raw)
        editor.indent_branch(False, 1)
        self.assertIn("\r\n- review\r\n\t- nested", editor.model.raw)
        editor.undo()
        self.assertEqual(editor.model.raw, raw)

    def test_assignment_dialog_searches_deep_terms_outside_module_scope(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QListWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        outside = Group("Outside", "outside", options=(
            Option("Folder", "folder", category=True),
            Option("Subfolder", "sub", parent="folder", category=True),
            Option("Deep term", "deep", parent="sub"),
        ))
        catalog = ui.catalog.replace_group(outside)
        catalog = catalog.replace_module(replace(catalog.module(), groups=("task",)))
        self.assertTrue(ui.save_catalog(catalog))
        observed = {}

        def choose():
            dialog = self.app.activeModalWidget()
            if not isinstance(dialog, QDialog):
                return
            entries = dialog.findChild(QListWidget)
            dialog.findChild(QLineEdit).setText("Deep term")
            observed["rows"] = [entries.item(i).data(Qt.UserRole) for i in range(entries.count())]
            entries.setCurrentRow(1)
            observed["path"] = [b.toolTip() for b in dialog.findChildren(QPushButton) if b.isChecked()]
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()

        QTimer.singleShot(30, choose)
        self.assertEqual(ui.panel.choose_word_group("new term", ui.catalog.group("task"), "prefixes"), outside)
        self.assertEqual(observed["rows"], ["", "outside"])
        self.assertTrue(any("Folder" in path and "Deep term" in path for path in observed["path"]))
        self.assertEqual(ui.catalog.group("outside"), outside)

    def test_reload_center_uses_catalog_id_and_ignores_orphan_drafts(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QListWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.store.save_draft("reload-orphan", 1, Document.from_raw("orphan"), 0, 0)
        self.assertTrue(ui.open_reload("- sample body"))
        identity = ui.session_id
        ordinary = Module("回载只是名称", "plain")
        renamed = replace(ui.catalog.module(identity), name="记录一")
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(ordinary).replace_module(renamed)))
        visible = []

        def inspect():
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, QDialog):
                entries = dialog.findChild(QListWidget)
                visible.extend((entries.item(i).data(Qt.UserRole), entries.item(i).text())
                               for i in range(entries.count()))
                dialog.reject()

        QTimer.singleShot(30, inspect)
        ui.choose_reload()
        self.assertEqual(visible, [(identity, "记录一  ✓ 当前")])

    def test_automatic_backup_includes_module_after_only_body_changes(self):
        from unittest.mock import patch
        from ui.composition_workflows import Workflows
        popup = self.open()
        self.store.set("backup_drafts", False)
        popup.composition_button.click()
        ui = popup.composition
        backup = Backups(self.store, self.root / "automatic")
        create = backup.create

        def without_wait(*args, **kwargs):
            return create(*args, **kwargs, minimum_interval=0)

        with patch.object(ui, "backup_service", return_value=backup), patch.object(
            backup, "create", side_effect=without_wait,
        ):
            QTest.keyClicks(ui.editor, "first")
            Workflows.auto_backup(ui)
            QTest.keyClicks(ui.editor, " update")
            Workflows.auto_backup(ui)
        entries = backup.entries()
        self.assertEqual(len(entries), 2)
        snapshot = [backup.read(path)[1]["drafts"][0][2]["document"] for path, meta in entries
                    if meta["includes_drafts"]]
        self.assertCountEqual([Document.load(record).raw for record in snapshot], ["first", "first update"])

    def test_three_modes_retain_separate_drafts_and_carets(self):
        popup = self.open()
        popup.mode_button.click()
        QTest.keyClicks(popup.continuous_editor.editor, "continuous draft")
        popup.composition_button.click()
        self.assertIs(popup.input_mode, InputMode.COMPOSITION)
        self.assertTrue(popup.draft_input_visible)
        self.assertFalse(popup.continuous_input_visible)
        editor = popup.composition.editor
        QTest.keyClicks(editor, "composition draft")
        editor.select_offsets(3)
        popup.mode_button.click()
        self.assertEqual(popup.continuous_editor.editor.toPlainText(), "continuous draft")
        self.assertEqual(self.store.load_draft("default-module")[1].raw, "composition draft")
        popup.mode_button.click()
        self.assertIs(popup.input_mode, InputMode.SEARCH)
        popup.composition_button.click()
        self.app.processEvents()
        self.assertEqual(editor.model.raw, "composition draft")
        self.assertEqual(editor.offsets(), (3, 3))

    def test_typing_panel_modifier_and_module_reattach(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClicks(ui.editor, "- Task: ")
        ui.lookup()
        ui.panel.browse_group("task")
        ui.panel.query.setText("review")
        ui.panel.accept_main_query()
        self.assertEqual(ui.editor.model.raw, "- Task: review")
        token = ui.editor.model.tokens[0]
        ui.panel.open_token(token, mode="prefixes", locked=True)
        group = ui.catalog.group("priority")
        ui.panel.select_modifier(group, group.options[0], ("priority",))
        ui.panel.open_token(ui.editor.model.tokens[0], mode="suffixes", locked=True)
        status = ui.catalog.group("status")
        ui.panel.select_modifier(status, status.options[0], ("status",))
        self.assertEqual(ui.editor.model.raw, "- Task: urgent+review+queued")
        ui.editor.undo()
        self.assertEqual(ui.editor.model.raw, "- Task: urgent+review")
        ui.editor.redo()
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(Module("Reading", "reading"))))
        self.assertTrue(ui.switch_module("reading"))
        self.assertEqual(ui.editor.model.raw, "")
        QTest.keyClicks(ui.editor, "notes")
        self.assertTrue(ui.switch_module("default-module"))
        self.assertEqual(ui.editor.model.raw, "- Task: urgent+review+queued")

    def test_repeated_mode_module_and_panel_lifecycle_persists_distinct_sessions(self):
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QLineEdit
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClicks(ui.editor, "Museum catalogue")
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(Module("Conservation", "museum"))))
        for _ in range(20):
            self.assertTrue(ui.switch_module("museum"))
            QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
            self.assertTrue(ui.panel.isVisible())
            QTest.keyClick(ui.panel.query, Qt.Key_Escape)
            popup.mode_button.click()
            popup.hide()
            popup.reappear_in_place()
            popup.composition_button.click()
            self.assertTrue(ui.switch_module("default-module"))
            self.assertEqual(ui.editor.model.raw, "Museum catalogue")
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertLessEqual(len(ui.panel.findChildren(QLineEdit)), 1)
            self.assertFalse(ui.panel.timer.isActive())
        self.assertTrue(ui.flush())
        self.assertEqual(self.store.load_draft("default-module")[1].raw, "Museum catalogue")
        self.assertEqual(self.store.load_draft("museum")[1].raw, "")

    def test_recall_attachment_then_shared_config_change_and_backup_keep_literal(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        raw = "- Object: glazed+inspect+sealed\r\n"
        coating = Group("Finish", "finish", options=(Option("glazed"),))
        handling = Group("Handling", "handling", options=(Option("sealed"),))
        action = Group("Conservation action", "conservation", options=(Option("inspect"),),
                       prefixes=("finish",), suffixes=("handling",))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(coating).replace_group(handling).replace_group(action)))
        self.assertTrue(ui.open_reload(raw))
        ui.analyze()
        self.assertFalse(ui.candidates)
        self.assertEqual(len(ui.editor.model.tokens), 1)
        frozen = ui.editor.model.tokens
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(replace(coating, name="Surface"))))
        self.assertTrue(ui.flush())
        backup = ui.backup_service().create("Museum", include_drafts=True)
        self.assertTrue(ui.save_catalog(ui.catalog.remove_group("finish")))
        self.store.save_draft(ui.session_id, 9, Document.from_raw("newer draft"), 0, 0)
        ui.backup_service().restore(backup, include_drafts=True)
        _, restored, _, _ = self.store.load_draft(ui.session_id)
        self.assertEqual(restored.raw, raw)
        self.assertEqual(restored.tokens, frozen)
        self.assertEqual(self.store.get("active_session"), ui.session_id)

    def test_popup_remeasures_search_and_configuration_without_clipping(self):
        from PySide6.QtWidgets import QLineEdit
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        ui.manager.find_matches = lambda query, *_: [
            {"full_content": f"record {i}", "parent": f"record {i}"}
            for i in range(40 if query == "many" else 1 if query == "one" else 0)
        ]
        ui.manager.word_blocks = []
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)

        def settle():
            QTest.qWait(20)
            self.assertTrue(ui.panel.screen().availableGeometry().contains(ui.panel.geometry()))
            for widget in (ui.panel.main, ui.panel.config, ui.panel.close_button):
                visible = widget.visibleRegion().boundingRect()
                self.assertGreaterEqual(visible.height(), widget.height() - 2)

        settle()
        empty = ui.panel.height()
        self.assertFalse(ui.panel.results.isVisible())
        QTest.keyClicks(ui.panel.query, "one")
        settle()
        few = ui.panel.height()
        self.assertEqual(ui.panel.results.verticalScrollBar().maximum(), 0)
        ui.panel.query.clear()
        QTest.keyClicks(ui.panel.query, "many")
        settle()
        self.assertGreater(ui.panel.height(), few)
        self.assertGreater(ui.panel.results.verticalScrollBar().maximum(), 0)
        self.assertEqual(ui.panel.scroll.verticalScrollBar().maximum(), 0)
        ui.panel.query.clear()
        settle()
        self.assertEqual(ui.panel.height(), empty)
        for button in (ui.panel.prefix, ui.panel.main, ui.panel.suffix, ui.panel.config):
            QTest.mouseClick(button, Qt.LeftButton)
            settle()
        QTest.mouseClick(ui.panel.config, Qt.LeftButton)
        QTest.mouseClick(ui.panel.main, Qt.LeftButton)
        QTest.mouseClick(ui.panel.config, Qt.LeftButton)
        for width, height in ((465, 275), (780, 545)):
            popup.resize(width, height)
            screen = popup.screen().availableGeometry()
            popup.move(screen.right() - popup.width() + 1, screen.bottom() - popup.height() + 1)
            settle()
            fields = [field for field in ui.panel.body.findChildren(QLineEdit) if field.isVisible()]
            self.assertTrue(fields)
            self.assertEqual(ui.panel.scroll.verticalScrollBar().maximum(), 0)
            for field in fields:
                self.assertGreaterEqual(field.visibleRegion().boundingRect().height(), field.height() - 2)

    def test_unified_centers_find_switch_preview_and_insert_at_saved_caret(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QLineEdit, QPlainTextEdit
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(Module("Museum", "museum"))))
        observed = []

        def interact(text, title):
            dialog = (ui.template_center if title == "片段中心" else QApplication.activeModalWidget())
            if not dialog:
                return
            observed.append(dialog.windowTitle())
            if dialog.windowTitle() != title:
                dialog.reject()
                return
            query = dialog.findChild(QLineEdit)
            query.setText(text)
            preview = dialog.findChild(QPlainTextEdit)
            if preview:
                observed.append(preview.toPlainText())
            render = os.environ.get("QUICKKV_TEST_RENDER")
            if render:
                dialog.grab().save(str(Path(render) / (("template" if preview else "module") + ".png")))
            QTest.keyClick(query, Qt.Key_Return)
            if dialog.isVisible():
                dialog.reject()

        QTimer.singleShot(30, lambda: interact("Museum", "模块中心"))
        QTest.mouseClick(ui.module_button, Qt.LeftButton)
        self.assertEqual(ui.session_id, "museum")
        model = Document.from_raw("- Exhibit")
        ui.editor.apply(Document.from_raw("- First\n- Last"), 8)
        ui.editor.select_offsets(8)
        self.assertTrue(ui.update_templates({"example": {"module": "museum", "name": "Exhibit",
                                                        "fragment": model.to_record()}}))
        QTest.mouseClick(ui.template_button, Qt.LeftButton)
        self.assertFalse(ui.template_center.isModal())
        interact("Exhibit", "片段中心")
        self.assertEqual(observed, ["模块中心", "片段中心", "- Exhibit"])
        self.assertEqual(ui.editor.model.raw, "- First\n- Exhibit\n- Last")
        ui.editor.undo()
        self.assertEqual(ui.editor.model.raw, "- First\n- Last")
        self.assertEqual(ui.editor.offsets(), (8, 8))

    def test_independent_panel_keeps_editor_layout_and_anchor_stable(self):
        from PySide6.QtGui import QTextCursor
        popup = self.open()
        popup.composition_button.click()
        popup.resize(465, 275)
        ui = popup.composition
        model = Document.from_raw("- Task: review")
        token = Token(8, 14, "review", Choice("review", "task"))
        ui.editor.apply(model.attach(token), 9)
        cursor = QTextCursor(ui.editor.document())
        cursor.setPosition(8)
        original = ui.editor.cursorRect(cursor)
        self.assertFalse(popup.popup_shadow.isEnabled())
        ui.activate_at(9)
        self.assertTrue(ui.panel.isWindow())
        self.assertLess(ui.panel.width(), popup.width())
        top = ui.panel.geometry().topLeft()
        for widget in (ui.panel.prefix, ui.panel.suffix, ui.panel.main, ui.panel.config):
            QTest.mouseClick(widget, Qt.LeftButton)
            QTest.qWait(15)
            self.assertEqual(ui.panel.geometry().topLeft(), top)
            self.assertEqual(ui.editor.cursorRect(cursor), original)
            self.assertEqual(ui.editor.model.raw, model.raw)
        popup.hide()
        self.assertFalse(ui.panel.isVisible())

    def test_temporary_modifier_uses_click_to_open_and_escape_to_cancel(self):
        from PySide6.QtWidgets import QLineEdit
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        doc = Document.from_raw("review")
        token = Token(0, 6, "review", Choice("review", "task"))
        ui.editor.apply(doc.attach(token))
        ui.panel.open_token(token, mode="prefixes", locked=True)
        field, = [w for w in ui.panel.body.findChildren(QLineEdit)
                  if w.property("temporaryCompositionValue")]
        self.assertTrue(field.isHidden())
        plus = next(w for w in ui.panel.body.findChildren(QPushButton)
                    if w.toolTip() == "添加到当前词组及组合输入专用词库")
        QTest.mouseClick(plus, Qt.LeftButton)
        self.assertFalse(field.isHidden())
        QTest.keyClicks(field, "draft")
        QTest.keyClick(field, Qt.Key_Escape)
        self.assertTrue(field.isHidden())
        self.assertTrue(ui.panel.isVisible())
        self.assertEqual(ui.editor.model.raw, "review")
        self.assertFalse(self.store.library_path.exists())
        QTest.mouseClick(plus, Qt.LeftButton)
        QTest.keyClicks(field, "draft")
        QTest.keyClick(field, Qt.Key_Return)
        self.assertEqual(ui.editor.model.raw, "draft+review")
        self.assertIn("draft", [o.text for o in ui.catalog.group("priority").options])
        self.assertEqual(self.store.library_path.read_text(encoding="utf8"), "- draft\n")

    def test_click_each_structured_part_and_copy_without_moving_editor(self):
        from PySide6.QtGui import QTextCursor
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        literal = "on-site+review+queued"
        doc = Document.from_raw(literal)
        token = Token(0, len(literal), literal, Choice("review", "task"),
                      (Choice("on-site", "priority"),), (Choice("queued", "status"),))
        ui.editor.apply(doc.attach(token), 0)
        before = ui.editor.geometry()
        copied = []
        ui.copy_requested.connect(copied.append)
        ui.copy()
        ui.report("复制成功")
        self.assertEqual(copied, [literal])
        self.assertEqual(ui.editor.geometry(), before)
        for pos, expected in ((3, "prefixes"), (9, "main"), (16, "suffixes")):
            cursor = QTextCursor(ui.editor.document())
            cursor.setPosition(pos)
            point = ui.editor.cursorRect(cursor).center()
            QTest.mouseClick(ui.editor.viewport(), Qt.LeftButton, pos=point)
            self.assertEqual(ui.panel.mode, expected)
            self.assertTrue(ui.panel.isVisible())
        ui.dismiss_panel()
        ui.editor.select_offsets(len(literal))
        QTest.keyClicks(ui.editor, "!")
        self.assertEqual(ui.output(), literal + "!")
        QTest.mouseClick(popup.mode_button, Qt.LeftButton)
        self.assertFalse(ui.panel.isVisible())

    def test_last_group_removal_does_not_reopen_unrestricted_search(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        module = ui.catalog.module()
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(replace(module, groups=("task",)))))
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        self.assertEqual(ui.panel.results.count(), 1)
        ui.panel.remove_scope_group("task")
        self.assertEqual(ui.panel.results.count(), 0)
        self.assertTrue(ui.catalog.module().scope_limited)
        self.assertIsNotNone(ui.catalog.group("task"))
        self.assertTrue(Catalog.load(self.store.get("catalog")).module().scope_limited)

    def test_nonmodal_template_can_create_and_insert_after_main_editor_moves(self):
        from PySide6.QtWidgets import QLineEdit
        from ui.composition_editor import CompositionEditor
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.mouseClick(ui.template_button, Qt.LeftButton)
        center = ui.template_center
        def click(text):
            QTest.mouseClick(next(w for w in center.findChildren(QPushButton)
                                 if w.text() == text), Qt.LeftButton)
        click("＋ 新建片段")
        name = next(w for w in center.findChildren(QLineEdit) if w.placeholderText() == "片段名称")
        name.selectAll()
        QTest.keyClicks(name, "Checklist")
        draft = center.findChild(CompositionEditor)
        QTest.keyClicks(draft, "- Checklist")
        QTest.keyClicks(ui.editor, "- First")
        QTest.keyClick(ui.editor, Qt.Key_Return)
        QTest.keyClicks(ui.editor, "Last")
        self.assertEqual(ui.editor.model.raw, "- First\n- Last")
        self.assertTrue(center.flush_fragment())
        ui.editor.select_offsets(8)
        click("插入到当前光标")
        self.assertEqual(ui.editor.model.raw, "- First\n- Checklist\n- Last")
        self.assertTrue(center.isVisible())
        self.assertEqual(draft.model.raw, "- Checklist")

    def test_indicator_tracks_viewport_right_edge_and_resize_cursor_restores(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QCursor, QMouseEvent
        from dataclasses import replace
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        ui.frameworks = {"review": {"name": "Review", "paths": ["Task"]}}
        model = Document.from_raw("- Review\n  - Task: Draft")
        root = replace(model.nodes[0], observation="review")
        ui.editor.apply(replace(model, nodes=(root, *model.nodes[1:])))
        for width in (540, 880, 465):
            popup.resize(width, 330)
            QTest.qWait(20)
            indicator, = ui.indicators
            self.assertEqual(indicator.x() + indicator.width(), ui.editor.viewport().width() - 4)
        watched = popup.container
        watched.setCursor(QCursor(Qt.PointingHandCursor))
        point = QPoint(1, watched.height() // 2)
        event = QMouseEvent(QEvent.MouseMove, QPointF(point), QPointF(watched.mapToGlobal(point)),
                            Qt.NoButton, Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(watched, event)
        self.assertEqual(watched.cursor().shape(), Qt.SizeHorCursor)
        QApplication.sendEvent(watched, QEvent(QEvent.Leave))
        self.assertEqual(watched.cursor().shape(), Qt.PointingHandCursor)

    def test_unified_results_choose_group_and_preserve_complex_entry(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QListWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        other = Group("Conservation", "conservation", options=(Option("review", "recheck"),))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(other)))
        raw = "- review\r\n  - Original body"
        ui.manager.find_matches = lambda query, *_: [
            {"parent": "review", "full_content": raw, "source_path": "fictional.md"}
        ] if query else []
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        QTest.keyClicks(ui.panel.query, "review")
        self.assertEqual(ui.panel.results.currentItem().data(Qt.UserRole)[0], "grouped")
        def choose_group():
            dialog = QApplication.activeModalWidget()
            if dialog:
                listing = dialog.findChild(QListWidget)
                listing.setCurrentRow(listing.count() - 1)
                dialog.accept()
        QTimer.singleShot(10, choose_group)
        QTest.keyClick(ui.panel.query, Qt.Key_Return)
        self.assertEqual(ui.editor.model.tokens[0].main.group_id, "conservation")
        ui.dismiss_panel()
        ui.editor.select_offsets(0)
        ui.editor.apply(Document(), 0)
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        QTest.keyClicks(ui.panel.query, "review")
        QTest.keyClick(ui.panel.query, Qt.Key_Down)
        self.assertIn("Original body", ui.panel.search_preview.toPlainText())
        QTest.keyClick(ui.panel.query, Qt.Key_Return)
        self.assertEqual(ui.editor.model.raw, raw)
        self.assertFalse(ui.editor.model.tokens)

    def test_main_lookup_keyboard_selection_leaves_inline_roles_available(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        ui.panel.browse_group("task")
        QTest.keyClick(ui.panel.query, Qt.Key_Down)
        QTest.keyClick(ui.panel.query, Qt.Key_Return)
        self.assertEqual(ui.editor.model.raw, "archive")
        self.assertTrue(ui.panel.isVisible())
        self.assertEqual(ui.panel.mode, "main")
        self.assertTrue(ui.panel.main.isChecked())
        QTest.mouseClick(ui.panel.prefix, Qt.LeftButton)
        self.assertEqual(ui.panel.mode, "prefixes")

    def test_toolbar_inserts_at_current_caret(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClicks(ui.editor, "- Task: ")
        insertion = next(b for b in ui.findChildren(QPushButton) if b.text().endswith("组合词"))
        insertion.click()
        self.assertEqual(ui.pending_insertion[1:3], (8, 8))

    def test_sixth_modifier_expands_on_hover_and_changes_current_token_only(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        catalog = ui.catalog
        additions = tuple(Group(f"Attribute {index}", f"attribute-{index}",
                                options=(Option(f"choice-{index}", f"choice-{index}"),))
                          for index in range(5))
        for group in additions:
            catalog = catalog.replace_group(group)
        task = catalog.group("task")
        ui.save_catalog(catalog.replace_group(replace(task, prefixes=("priority", *(g.id for g in additions)))))
        model = Document.from_raw("review and review")
        first = Token(0, 6, "review", Choice("review", "task"))
        other = Token(11, 17, "review", Choice("review", "task"))
        ui.editor.apply(model.attach(first).attach(other), 1)
        ui.panel.open_token(first, mode="prefixes", locked=True)
        more = next(button for button in ui.panel.findChildren(QPushButton)
                    if button.property("expandCompositionGroups"))
        self.assertFalse(ui.panel.expanded)
        QApplication.sendEvent(more, QEvent(QEvent.Enter))
        self.app.processEvents()
        self.assertTrue(ui.panel.expanded)
        group = additions[-1]
        ui.panel.select_modifier(group, group.options[0], ui.catalog.group("task").prefixes)
        self.assertEqual(ui.editor.model.raw, "choice-4+review and review")
        self.assertEqual(ui.editor.model.tokens[-1].id, other.id)
        self.assertEqual(ui.editor.model.tokens[-1].literal, "review")

    def test_confirmed_direct_word_saves_once_and_mode_buttons_work_in_one_click(self):
        popup = self.open()
        QTest.mouseClick(popup.composition_button, Qt.LeftButton)
        ui = popup.composition
        self.assertIs(popup.input_mode, InputMode.COMPOSITION)
        QTest.mouseClick(popup.mode_button, Qt.LeftButton)
        self.assertIs(popup.input_mode, InputMode.CONTINUOUS)
        QTest.mouseClick(popup.mode_button, Qt.LeftButton)
        self.assertIs(popup.input_mode, InputMode.SEARCH)
        QTest.mouseClick(popup.composition_button, Qt.LeftButton)
        for confirm in (False, True):
            QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
            QTest.keyClicks(ui.panel.query, "museum-label")
            self.assertFalse(self.store.library_path.exists())
            QTest.keyClick(ui.panel.query, Qt.Key_Return if confirm else Qt.Key_Escape)
        self.assertEqual(ui.editor.model.raw, "museum-label")
        self.assertEqual(self.store.library_path.read_bytes(), b"- museum-label\n")
        self.assertTrue(ui.save_catalog(ui.catalog, word="museum-label"))
        self.assertEqual(self.store.library_path.read_bytes(), b"- museum-label\n")

    def test_reload_only_auto_attaches_unambiguous_terms(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        duplicates = Group("Conservation", "conservation", options=(Option("review"),))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(duplicates)))
        literal = "- Entry: review, urgent+review+queued, unknown+review\r\n"
        self.assertTrue(ui.open_reload(literal))
        ui.analyze()
        self.assertEqual(ui.editor.model.raw, literal)
        self.assertEqual([token.literal for token in ui.editor.model.tokens], ["urgent+review+queued"])
        self.assertTrue(any(not item.certain and item.literal == "review" for item in ui.candidates))
        self.assertFalse(self.store.library_path.exists())

    def test_reload_uses_same_widget_with_identical_unmodified_and_attached_text(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClicks(ui.editor, "unsent")
        raw = "- Files\r\n  - ![map](images/draft+review.png), Task: urgent+review+queued\r\n\n"
        self.assertTrue(ui.open_reload(raw))
        ui.analyze()
        self.assertFalse(ui.candidates)
        self.assertEqual(ui.catalog.selected_module, ui.session_id)
        self.assertTrue(ui.catalog.module().name.startswith("回载 · "))
        self.assertFalse(self.store.library_path.exists())
        self.assertEqual(ui.output(), raw)
        self.assertEqual(len(ui.editor.model.tokens), 1)
        self.assertTrue(ui.switch_module("default-module"))
        self.assertEqual(ui.editor.model.raw, "unsent")

    def test_first_configuration_creates_group_and_keeps_visible_word(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        QTest.keyClicks(ui.panel.query, "calibration")
        QTest.keyClick(ui.panel.query, Qt.Key_Return)
        literal = ui.editor.model.raw
        self.assertFalse(ui.editor.model.tokens[0].main.group_id)
        QTest.mouseClick(ui.panel.prefix, Qt.LeftButton)
        QTest.mouseClick(ui.panel.config, Qt.LeftButton)
        action = next(w for w in ui.panel.body.findChildren(QPushButton)
                      if w.text().startswith("建立同名词组"))
        QTest.mouseClick(action, Qt.LeftButton)
        group_id = ui.editor.model.tokens[0].main.group_id
        self.assertEqual(ui.catalog.group(group_id).name, "calibration")
        self.assertEqual(ui.panel.mode, "config-prefixes")
        self.assertEqual(ui.editor.model.raw, literal)
        self.assertEqual(self.store.library_path.read_bytes(), b"- calibration\n")

    def test_unrecognized_recall_persists_only_after_confirmation(self):
        from unittest.mock import patch
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        raw = "- Entry: morning+calibration+signed\r\n"
        self.assertTrue(ui.open_reload(raw))
        ui.analyze()
        candidate, = ui.candidates
        self.assertFalse(candidate.certain)
        with patch.object(ui, "pick_group_chips", side_effect=lambda _title, choices: choices[0][2]):
            with patch.object(ui, "confirm_establishment", return_value=False):
                ui.establish(candidate)
            self.assertFalse(self.store.library_path.exists())
            self.assertFalse(ui.editor.model.tokens)
            with patch.object(ui, "confirm_establishment", return_value=True):
                ui.establish(candidate)
        token, = ui.editor.model.tokens
        host = ui.catalog.group(token.main.group_id)
        self.assertEqual(host.name, "calibration")
        self.assertIn(token.prefixes[0].group_id, host.prefixes)
        self.assertIn(token.suffixes[0].group_id, host.suffixes)
        self.assertEqual(ui.editor.model.raw, raw)
        self.assertEqual(self.store.load_draft(ui.session_id)[1].tokens, (token,))
        self.assertEqual(set(self.store.library_path.read_text().splitlines()),
                         {"- morning", "- calibration", "- signed"})

    def test_four_level_breadcrumb_jumps_to_ancestor(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        group = ui.catalog.group("task")
        categories = tuple(Option(f"Level {i}", f"level-{i}", f"level-{i-1}" if i else "",
                                  category=True) for i in range(4))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(replace(
            group, options=(*group.options, *categories)))))
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        ui.panel.browse_group("task")
        QTest.mouseClick(ui.panel.config, Qt.LeftButton)
        ui.panel.open_category("level-3")
        from time import monotonic

        def visible_crumbs():
            return {w.text(): w for w in ui.panel.body.findChildren(QPushButton) if w.isVisible()}

        deadline = monotonic() + 2
        crumbs = visible_crumbs()
        while not {"▸ Level 1", "▸ Level 3"} <= crumbs.keys() and monotonic() < deadline:
            QTest.qWait(5)
            crumbs = visible_crumbs()
        self.assertIn("▸ Level 3", crumbs)
        self.assertFalse(crumbs["▸ Level 3"].isEnabled())
        QTest.mouseClick(crumbs["▸ Level 1"], Qt.LeftButton)
        self.assertEqual(ui.panel.parent_option, "level-1")

    def test_direct_group_chip_and_quick_module_switch(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(Module("Workshop", "workshop"))))
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        chip = next(widget for widget in ui.panel.body.findChildren(QPushButton)
                    if widget.isVisible() and widget.text() == "review")
        QTest.mouseClick(chip, Qt.LeftButton)
        self.assertEqual(ui.editor.model.tokens[0].main.group_id, "task")
        QTest.mouseClick(ui.quick_modules, Qt.LeftButton)
        self.assertTrue(ui.quick_module_window.isVisible())
        switch = next(w for w in ui.quick_module_window.findChildren(QPushButton)
                      if w.text() == "Workshop")
        QTest.mouseClick(switch, Qt.LeftButton)
        self.assertFalse(ui.quick_module_window.isVisible())
        self.assertEqual(ui.session_id, "workshop")
        self.assertEqual(self.store.load_draft("default-module")[1].raw, "review")

    def test_delete_selected_module_via_inline_button_refreshes_open_center(self):
        from unittest.mock import patch
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QListWidget, QToolButton
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(Module("Workshop", "workshop"))))
        self.assertTrue(ui.switch_module("workshop"))
        observed = []

        def remove():
            dialog = QApplication.activeModalWidget()
            try:
                listing = dialog.findChild(QListWidget)
                delete = next(w for w in dialog.findChildren(QToolButton) if w.toolTip() == "删除模块")
                QTest.mouseClick(delete, Qt.LeftButton)
                observed.extend(listing.item(i).data(Qt.UserRole) for i in range(listing.count()))
            finally:
                dialog.reject()
        with patch.object(ui, "confirm", return_value=True):
            QTimer.singleShot(20, remove)
            QTest.mouseClick(ui.module_button, Qt.LeftButton)
        self.assertEqual(observed, ["default-module"])
        self.assertEqual(ui.session_id, "default-module")
        self.assertEqual(self.store.get("active_session"), "default-module")

    def test_clean_clipboard_menu_discovers_unknown_unlabeled_outline_and_clicks_it(self):
        from unittest.mock import patch
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QTextCursor
        from PySide6.QtWidgets import QMenu
        if self.app.platformName() != "offscreen":
            self.skipTest("保留真实系统剪贴板，仅使用隔离 Qt 剪贴板")
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        raw = "- 晨检+校准，123\r\n  - ![sample](file+name.png)\r\n"
        self.app.clipboard().setText(raw)
        self.assertFalse(self.store.library_path.exists())

        selected_actions = []
        def activate_action(title):
            menu = self.app.activePopupWidget()
            if isinstance(menu, QMenu):
                action = next((action for action in menu.actions() if action.text() == title), None)
                menu.close()
                if action:
                    selected_actions.append(title)
                    action.trigger()
        QTimer.singleShot(25, lambda: activate_action("从剪贴板回载并新建模块"))
        QTest.mouseClick(ui.reload_button, Qt.LeftButton)
        self.assertIn("从剪贴板回载并新建模块", selected_actions)
        QTest.qWait(210)
        candidate, = ui.candidates
        self.assertEqual(candidate.literal, "晨检+校准")
        self.assertEqual(len(candidate.interpretations), 2)
        self.assertEqual(ui.editor.model.raw, raw)
        self.assertTrue(ui.editor.extraSelections())
        cursor = QTextCursor(ui.editor.document())
        cursor.setPosition(candidate.start + 1)
        with patch.object(ui, "pick_group_chips", side_effect=lambda _title, choices: choices[0][2]), patch.object(
                ui, "confirm_establishment", return_value=True):
            QTest.mouseClick(ui.editor.viewport(), Qt.LeftButton, pos=ui.editor.cursorRect(cursor).center())
            panel = ui.candidate_dialog
            self.assertIsNotNone(panel)
            establish = next(widget for widget in panel.findChildren(QPushButton)
                             if widget.text() == "建立组合词")
            QTest.mouseClick(establish, Qt.LeftButton)
        token, = ui.editor.model.tokens
        self.assertEqual(ui.editor.model.raw, raw)
        self.assertEqual(self.store.load_draft(ui.session_id)[1].tokens, (token,))

    def test_adjacent_edit_and_undo_preserve_bound_combination_and_click_role(self):
        from PySide6.QtGui import QTextCursor
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- Entry: review+queued\r\n"))
        token, = ui.editor.model.tokens
        ui.editor.select_offsets(token.end)
        ui.editor.replace_selection("，123")
        self.assertEqual(ui.editor.model.tokens, (token,))
        for offset, role in ((token.start + 1, "main"), (token.end - 2, "suffixes")):
            cursor = QTextCursor(ui.editor.document())
            cursor.setPosition(offset)
            QTest.mouseClick(ui.editor.viewport(), Qt.LeftButton, pos=ui.editor.cursorRect(cursor).center())
            self.assertEqual(ui.panel.mode, role)
            self.assertTrue(ui.panel.isVisible())
        ui.dismiss_panel()
        ui.editor.select_offsets(token.end, token.end + 4)
        QTest.keyClick(ui.editor, Qt.Key_Backspace)
        self.assertEqual(ui.editor.model.tokens, (token,))
        ui.editor.undo()
        ui.editor.redo()
        self.assertTrue(ui.flush())
        ui.load_session(ui.session_id)
        self.assertEqual(ui.editor.model.tokens, (token,))


    def test_qt_replacement_including_unchanged_combination_keeps_metadata(self):
        from PySide6.QtGui import QTextCursor
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- Entry: review+queued"))
        token, = ui.editor.model.tokens
        cursor = QTextCursor(ui.editor.document())
        cursor.setPosition(token.start)
        cursor.setPosition(token.end, QTextCursor.KeepAnchor)
        cursor.insertText(token.literal + "，123")
        self.assertEqual(ui.editor.model.tokens, (token,))
        self.assertEqual(ui.editor.model.raw, "- Entry: review+queued，123")

    def test_global_modifier_choice_confirms_and_saves_association_with_document(self):
        from unittest.mock import patch
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        group = Group("Venue", "venue", options=(Option("studio", "studio"),))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(group)))
        self.assertTrue(ui.open_reload("- Entry: review"))
        token, = ui.editor.model.tokens
        ui.panel.open_token(token, mode="prefixes", locked=True)
        QTest.keyClicks(ui.panel.query, "studio")
        self.assertEqual(ui.panel.results.currentItem().data(Qt.UserRole)[0], "grouped")
        with patch.object(ui, "confirm", return_value=True):
            QTest.keyClick(ui.panel.query, Qt.Key_Return)
        self.assertIn("venue", ui.catalog.group("task").prefixes)
        self.assertEqual(ui.editor.model.raw, "- Entry: studio+review")
        self.assertEqual(self.store.load_draft(ui.session_id)[1].tokens, ui.editor.model.tokens)

    def test_global_query_and_assoc_changes_do_not_import_legacy_favorites(self):
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(replace(
            ui.catalog.module(), groups=("task",), scope_limited=True))))
        QTest.keyClick(ui.editor, Qt.Key_J, Qt.ControlModifier)
        QTest.keyClicks(ui.panel.query, "urgent")
        result = ui.panel.results.currentItem()
        self.assertEqual(result.data(Qt.UserRole)[0], "grouped")
        self.assertEqual(ui.catalog.module().groups, ("task",))
        ui.panel.query.clear()
        QTest.mouseClick(ui.panel.query, Qt.LeftButton)
        QTest.qWait(25)
        chip = next(w for w in ui.panel.body.findChildren(QPushButton)
                    if w.isVisible() and w.text() == "Priority")
        self.assertTrue(self.store.get("group_favorites", {}) == {})
        self.assertTrue(ui.save_record("group_favorites", {"main": ["priority"]}))
        ui.load_settings()
        self.assertEqual(ui.catalog.module().groups, ("task",))
        QTest.mouseClick(chip, Qt.LeftButton)
        self.assertTrue(ui.panel.all_groups_open)
        self.assertEqual(ui.catalog.module().groups, ("task", "priority"))
        self.assertEqual(self.store.get("group_favorites"), {"main": ["priority"]})

    def test_ignore_one_duplicate_and_reidentify_partial_selection_via_editor_menu(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMenu
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        raw = "- 晨检+校准、晨检+校准，补充记录\r\n- 预约+补签\r\n"
        self.assertTrue(ui.open_reload(raw))
        ui.analyze()
        first, second, third = ui.candidates
        ui.ignore_candidate(first)
        ui.analyze()
        self.assertEqual([item.start for item in ui.candidates], [second.start, third.start])
        ui.editor.select_offsets(first.start + 1, first.start + 3)
        invoked = []
        def trigger():
            menu = QApplication.activePopupWidget()
            if isinstance(menu, QMenu):
                action = next((action for action in menu.actions() if action.text() == "识别选区"), None)
                menu.close()
                if action:
                    action.trigger()
                    invoked.append(True)
        QTimer.singleShot(25, trigger)
        ui.context(ui.editor.viewport().mapToGlobal(ui.editor.cursorRect().bottomLeft()), first.start + 1)
        self.assertEqual(invoked, [True])
        self.assertEqual([item.start for item in ui.candidates], [first.start, second.start, third.start])
        self.assertEqual(ui.editor.model.raw, raw)
        self.assertFalse(self.store.library_path.exists())
        self.assertEqual(self.store.get("ignored_candidates"), [])

    def test_cross_group_selection_refreshes_real_roles_and_undo(self):
        from unittest.mock import patch
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        channel = Group("Channel", "channel", options=(Option("onsite", "onsite"),))
        next_group = Group("Workshop", "workshop", options=(Option("assemble", "assemble"),),
                           prefixes=("channel",), suffixes=("status",))
        self.assertTrue(ui.save_catalog(ui.catalog.replace_group(channel).replace_group(next_group)))
        self.assertTrue(ui.open_reload("- Work: urgent+review+queued\r\n"))
        original, = ui.editor.model.tokens
        ui.panel.open_token(original, mode="main", locked=True)
        ui.panel.browse_group("workshop")
        self.assertEqual(ui.editor.model.tokens, (original,))
        QTest.qWait(20)
        choice = next(item for item in ui.panel.body.findChildren(QPushButton)
                      if item.isVisible() and item.text().lstrip("› ") == "assemble")
        with patch.object(ui, "confirm", return_value=True):
            QTest.mouseClick(choice, Qt.LeftButton)
        updated, = ui.editor.model.tokens
        self.assertEqual(updated.id, original.id)
        self.assertEqual(updated.main.group_id, "workshop")
        self.assertFalse(updated.prefixes)
        self.assertEqual(updated.suffixes, original.suffixes)
        QTest.mouseClick(ui.panel.prefix, Qt.LeftButton)
        QTest.qWait(10)
        self.assertTrue(any(w.isVisible() and w.text() == "onsite"
                            for w in ui.panel.body.findChildren(QPushButton)))
        self.assertTrue(ui.flush())
        self.assertEqual(self.store.load_draft(ui.session_id)[1].tokens, (updated,))
        ui.editor.undo()
        self.assertEqual(ui.editor.model.tokens, (original,))

    def test_quick_menu_click_toggle_and_first_mode_press_with_auxiliary_windows(self):
        popup = self.open()
        QTest.mouseClick(popup.composition_button, Qt.LeftButton)
        ui = popup.composition
        QTest.mouseClick(ui.template_button, Qt.LeftButton)
        ui.open_quick_modules()
        panel = ui.quick_module_window
        self.assertTrue(panel.isVisible())
        QTest.mouseClick(ui.quick_modules, Qt.LeftButton)
        self.assertFalse(panel.isVisible())
        QTest.mouseClick(ui.quick_modules, Qt.LeftButton)
        self.assertTrue(panel.isVisible())
        QTest.mousePress(popup.mode_button, Qt.LeftButton)
        QTest.mouseRelease(popup.mode_button, Qt.LeftButton)
        self.assertIs(popup.input_mode, InputMode.CONTINUOUS)
        self.assertFalse(panel.isVisible())
        self.assertFalse(ui.template_center.isVisible())

    def test_fragment_switch_and_close_flush_dirty_body_and_failed_save_keeps_it(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QLineEdit, QListWidget
        from ui.composition_editor import CompositionEditor
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        QTest.mouseClick(ui.template_button, Qt.LeftButton)
        center = ui.template_center
        create = next(w for w in center.findChildren(QPushButton) if w.text() == "＋ 新建片段")
        QTest.mouseClick(create, Qt.LeftButton)
        editor = center.findChild(CompositionEditor)
        title = next(w for w in center.findChildren(QLineEdit) if w.placeholderText() == "片段名称")
        title.selectAll()
        QTest.keyClicks(title, "Archive checklist")
        QTest.keyClicks(editor, "- Draft")
        listing = center.findChild(QListWidget)
        first = listing.currentItem().data(Qt.UserRole)
        with patch.object(ui, "update_templates", return_value=False):
            self.assertFalse(center.close())
            self.assertTrue(center.isVisible())
            self.assertEqual(editor.model.raw, "- Draft")
        QTest.mouseClick(create, Qt.LeftButton)
        self.assertEqual(len(ui.templates), 2)
        saved = self.store.get("templates")[first]
        self.assertEqual(saved["name"], "Archive checklist")
        self.assertEqual(Document.load(saved["fragment"]).raw, "- Draft")
        self.assertEqual(editor.model.raw, "")

    def test_fragment_batch_order_survives_filter_and_inserts_one_undo(self):
        from PySide6.QtWidgets import QLineEdit, QListWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        for key, name in (("first", "Review notes"), ("second", "Archive notes")):
            doc = Document.from_raw("- " + name)
            token = Token(2, len(doc.text), name, Choice(name, "task"))
            self.assertTrue(ui.update_templates({**ui.templates, key: {
                "name": name, "module": ui.catalog.selected_module, "fragment": doc.attach(token).to_record()
            }}))
        QTest.keyClicks(ui.editor, "- Existing")
        QTest.mouseClick(ui.template_button, Qt.LeftButton)
        center = ui.template_center
        listing = center.findChild(QListWidget)
        listing.item(1).setCheckState(Qt.Checked)
        listing.item(0).setCheckState(Qt.Checked)
        self.assertTrue(listing.item(1).text().startswith("1 ·"))
        search = next(w for w in center.findChildren(QLineEdit) if w.placeholderText() == "搜索分支片段")
        search.setText("Review")
        self.assertTrue(listing.item(0).text().startswith("2 ·"))
        action = next(w for w in center.findChildren(QPushButton) if w.text() == "插入到当前光标")
        QTest.mouseClick(action, Qt.LeftButton)
        self.assertEqual(ui.editor.model.raw, "- Existing\n- Archive notes\n- Review notes")
        inserted = ui.editor.model.tokens
        self.assertEqual([token.literal for token in inserted], ["Archive notes", "Review notes"])
        self.assertNotIn(inserted[0].id, [Document.load(item["fragment"]).tokens[0].id for item in ui.templates.values()])
        ui.editor.undo()
        self.assertEqual(ui.editor.model.raw, "- Existing")
        self.assertFalse(ui.editor.model.tokens)

    def test_current_main_group_add_and_config_scope_keep_stable_binding(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QInputDialog, QListWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        module = replace(ui.catalog.module(), groups=("task",), scope_limited=True)
        self.assertTrue(ui.save_catalog(ui.catalog.replace_module(module)))
        self.assertTrue(ui.open_reload("- Work: urgent+review+queued"))
        token, = ui.editor.model.tokens
        ui.panel.open_token(token, mode="main", locked=True)
        QTest.qWait(10)
        plus = next(w for w in ui.panel.body.findChildren(QPushButton) if w.isVisible() and w.text() == "＋")
        with patch.object(QInputDialog, "getText", return_value=("classify", True)):
            QTest.mouseClick(plus, Qt.LeftButton)
        updated, = ui.editor.model.tokens
        self.assertEqual(updated.main.group_id, token.main.group_id)
        self.assertEqual(updated.main.text, "classify")
        self.assertEqual(updated.prefixes + updated.suffixes, token.prefixes + token.suffixes)
        self.assertEqual(self.store.library_path.read_text().splitlines(), ["- classify"])
        QTest.mouseClick(ui.panel.main, Qt.LeftButton)
        QTest.mouseClick(ui.panel.config, Qt.LeftButton)
        QTest.qWait(20)
        lists = [w for w in ui.panel.body.findChildren(QListWidget) if w.isVisible() and w.accessibleName() == "词组导航"]
        self.assertEqual([lists[0].item(i).data(Qt.UserRole) for i in range(lists[0].count())], ["task"])
        QTest.mouseClick(ui.panel.groups_button, Qt.LeftButton)
        QTest.qWait(10)
        listing = next(w for w in ui.panel.body.findChildren(QListWidget) if w.isVisible() and w.accessibleName() == "词组导航")
        self.assertGreater(listing.count(), 1)
        self.assertEqual(ui.editor.model.tokens, (updated,))

    def test_candidate_preview_closes_on_outside_press_without_ignoring(self):
        from PySide6.QtGui import QTextCursor
        from PySide6.QtWidgets import QTableWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- 晨检+校准\n- Plain text"))
        ui.analyze()
        original = ui.candidates
        cursor = QTextCursor(ui.editor.document())
        cursor.setPosition(original[0].start + 1)
        QTest.mouseClick(ui.editor.viewport(), Qt.LeftButton, pos=ui.editor.cursorRect(cursor).center())
        panel = ui.candidate_dialog
        self.assertTrue(panel.isVisible())
        self.assertEqual(panel.findChild(QTableWidget).columnCount(), 3)
        cursor.setPosition(len(ui.editor.model.text) - 1)
        QTest.mouseClick(ui.editor.viewport(), Qt.LeftButton, pos=ui.editor.cursorRect(cursor).center())
        QTest.qWait(25)
        self.assertIsNone(ui.candidate_dialog)
        self.assertEqual(ui.candidates, original)
        self.assertFalse(ui.ignored)

    def test_new_modifier_uses_one_assignment_and_persists_the_same_snapshot(self):
        from unittest.mock import patch
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- Work: review"))
        token, = ui.editor.model.tokens
        ui.panel.open_token(token, mode="prefixes", locked=True)
        QTest.keyClicks(ui.panel.query, "careful")
        with patch.object(ui.panel, "choose_word_group", return_value=ui.catalog.group("status")) as pick, patch.object(ui, "confirm") as extra:
            QTest.keyClick(ui.panel.query, Qt.Key_Return)
        pick.assert_called_once()
        self.assertEqual(pick.call_args.args[0], "careful")
        extra.assert_not_called()
        updated, = ui.editor.model.tokens
        self.assertEqual(updated.prefixes[0].text, "careful")
        self.assertIn("status", ui.catalog.group("task").prefixes)
        self.assertEqual(self.store.load_draft(ui.session_id)[1].tokens, (updated,))
        self.assertEqual(self.store.library_path.read_text().splitlines(), ["- careful"])

    def test_confirmation_uses_horizontal_roles_and_cancel_keeps_catalog(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QTableWidget
        popup = self.open()
        popup.composition_button.click()
        ui = popup.composition
        self.assertTrue(ui.open_reload("- Work: urgent+review+queued"))
        token, = ui.editor.model.tokens
        from core.composition.recognition import Interpretation
        observed = []
        def reject():
            dialog = QApplication.activeModalWidget()
            table = dialog.findChild(QTableWidget)
            observed.extend((table.columnCount(), table.rowCount(), table.item(0, 2).text()))
            next(button for button in dialog.findChildren(QPushButton) if button.text() == "取消").click()
        QTimer.singleShot(20, reject)
        self.assertFalse(ui.confirm_establishment(token.literal,
            Interpretation(token.main, token.prefixes, token.suffixes), ui.catalog, (), []))
        self.assertEqual(observed, [4, 2, "review"])
        self.assertFalse(self.store.library_path.exists())


class CompositionRulesTests(unittest.TestCase):
    def test_multiple_outline_fields_without_commas_keep_separate_source_spans(self):
        raw = "- Action: 晨检+标定 Status: 审签+归档\r\n"
        model = Document.from_raw(raw)
        candidates = Recognizer(Catalog.defaults()).scan(model)
        self.assertEqual([candidate.literal for candidate in candidates], ["晨检+标定", "审签+归档"])
        self.assertEqual([model.text[c.start:c.end] for c in candidates], [c.literal for c in candidates])

    def test_unknown_main_next_to_registered_timestamp_keeps_timestamp_literal(self):
        model = Document.from_raw("- 晨检+2026-11-26\r\n- 12+30\r\n")
        candidate, = Recognizer(Catalog.defaults()).scan(model)
        self.assertEqual(candidate.literal, "晨检+2026-11-26")
        self.assertFalse(candidate.certain)
        explanation = next(rule for rule in candidate.interpretations if rule.main.text == "晨检")
        stamp, = explanation.suffixes
        self.assertEqual(stamp.text, "2026-11-26")
        self.assertEqual(stamp.group_id, TIMESTAMP_ID)


    def test_time_freezes_and_group_choice_preserves_shared_rules(self):
        catalog = example_catalog()
        task = catalog.group("task")
        stamp = catalog.group(TIMESTAMP_ID)
        token = Token(0, 6, "review", task.choice(task.options[0]))
        pre = catalog.group("priority")
        first = choose_modifier(token, "prefixes", pre.choice(pre.options[0]), ("priority",))
        same = replace_main(first, task.choice(task.options[1]), task)
        self.assertEqual(same.render(), "urgent+archive")
        changed = replace_main(same, stamp.choice(stamp.options[0], datetime(2031, 4, 8, 9, 17, 53)), stamp)
        self.assertEqual(changed.render(), "20310408091753")
        self.assertEqual(stamp.options[0].value(datetime(2031, 4, 8, 9, 17, 54)), "20310408091754")
        self.assertEqual(changed.render(), "20310408091753")
        self.assertEqual(Numeric("0", "100", "0.5", "%").format_value("72.5"), "72.5%")
        with self.assertRaises(ValueError):
            Numeric("0", "100", "0.5", "%").format_value("73.3")

    def test_markdown_range_protection_and_ambiguous_composition(self):
        catalog = example_catalog()
        secondary = Group("Hand-offs", "handoff", options=(Option("review", "review2"),),
                          prefixes=("priority",), suffixes=("status",))
        recognizer = Recognizer(catalog.replace_group(secondary))
        text = (
            "- Tour\r\n"
            "  - ![task](images/urgent+review.png), Task: urgent+review+queued\r"
            "  - Quote: \x60urgent+review+queued\x60\r\n"
            "\x60\x60\x60md\n- Task: urgent+review+queued\n\x60\x60\x60\n"
            "- Unknown: urgent+review+pending\n"
        )
        model = Document.from_raw(text)
        found = recognizer.scan(model)
        self.assertEqual([c.literal for c in found], ["urgent+review+queued", "urgent+review+pending"])
        self.assertFalse(found[0].certain)
        self.assertEqual(len(found[0].interpretations), 2)
        self.assertFalse(found[1].interpretations[0].suffixes[0].group_id)
        self.assertEqual(model.raw, text)

    def test_dynamic_time_is_identified_in_tight_key_value(self):
        candidate, = Recognizer(example_catalog()).scan(Document.from_raw("- Time:09:17"))
        self.assertTrue(candidate.certain)
        self.assertEqual(candidate.interpretations[0].main.text, "09:17")
        self.assertEqual(candidate.interpretations[0].main.group_id, TIMESTAMP_ID)

    def test_multiline_code_is_not_recognized_with_text_after_it(self):
        recognizer = Recognizer(example_catalog())
        model = Document.from_raw("Code: `line\n  Task: urgent+review+queued`, Task: review")
        self.assertEqual([candidate.literal for candidate in recognizer.scan(model)], ["review"])

    def test_slots_and_observations_keep_scope(self):
        raw = "- Plan\n  - Task: review\n  - Logistics: arranged\n    - Owner: group\n  - Subplan\n    - Task: other"
        model = Document.from_raw(raw)
        model = replace(model, nodes=tuple(replace(n, observation="other") if i == 4 else n
                                           for i, n in enumerate(model.nodes)))
        start = model.text.index("review")
        token = Token(start, start + 6, "review", Choice("review", "task"))
        model = model.attach(token)
        rows = list(dimension_status(model, 0, ("Task", "Logistics → Owner", "Missing")))
        self.assertEqual(rows, [("Task", "recorded", (1,)), ("Logistics → Owner", "recorded", (3,)),
                                ("Missing", "pending", ())])
        fragment = template_fragment(model, 0, (token.id,))
        self.assertEqual(fragment.nodes[1].slots[0][1], "task")
        self.assertNotIn("Task: \n", clean_template_output(fragment))
        self.assertIn("Task: other", clean_template_output(fragment))


class CompositionBackupTests(unittest.TestCase):
    def test_valid_hash_with_invalid_domain_data_cannot_restore(self):
        import json
        from zipfile import ZipFile
        from core.composition.backups import MEMBERS
        from core.composition.store import digest
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            store = CompositionStore(root / "db.sqlite3", root / "library.md")
            store.add_word("catalogue", {})
            service = Backups(store, root / "backups")
            candidate = service.create("Museum")
            metadata, configuration, library = service.read(candidate)
            for key, value in (("backup_keep", "five"), ("templates", {"broken": {}})):
                data = json.dumps({**configuration, "records": {key: value}}).encode()
                metadata["sha256"][MEMBERS[1]] = digest(data)
                bad = root / "bad.quickkv-backup"
                with ZipFile(bad, "w") as archive:
                    archive.writestr(MEMBERS[0], json.dumps(metadata))
                    archive.writestr(MEMBERS[1], data)
                    archive.writestr(MEMBERS[2], library)
                with self.assertRaises(CompositionStorageError):
                    service.restore(bad)
                self.assertEqual(store.library_path.read_bytes(), library)
                self.assertEqual(len(service.entries()), 1)

    def test_valid_snapshot_rotation_and_restore_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            store = CompositionStore(folder / "conf.sqlite3", folder / "library.md")
            service = Backups(store, folder / "backups")
            store.save_draft("current", 1, Document.from_raw("private fixture text"), 0, 3)
            store.add_word("review", {"catalog": example_catalog().to_record()})
            first = service.create("Manual", include_drafts=True)
            for i in range(7):
                store.set("variant", i)
                service.create("Auto", "automatic", keep=5, minimum_interval=0)
            self.assertEqual(sum(m["kind"] == "automatic" for _, m in service.entries()), 5)
            self.assertIsNone(service.create("Auto", "automatic", keep=5, minimum_interval=0))
            store.add_word("archive", {})
            store.save_draft("current", 2, Document.from_raw("current text"), 0, 3)
            service.restore(first, include_drafts=True)
            self.assertEqual(store.library_path.read_text(), "- review\n")
            self.assertEqual(store.load_draft("current")[1].raw, "current text")
            self.assertIsNone(store.get("variant"))
            self.assertTrue(any(m["kind"] == "protected" for _, m in service.entries()))
            recalled = [i for i in store.draft_ids() if i.startswith("reload-")]
            self.assertEqual(store.load_draft(recalled[0])[1].raw, "private fixture text")
            corrupt = folder / "corrupt.quickkv-backup"
            corrupt.write_bytes(b"bad zip")
            with self.assertRaises(CompositionStorageError):
                service.restore(corrupt)
            self.assertEqual(store.library_path.read_text(), "- review\n")
