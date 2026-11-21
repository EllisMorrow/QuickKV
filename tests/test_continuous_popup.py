import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from ui.search_popup import SearchPopup


class ContinuousPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_toggle_preserves_draft_without_changing_normal_selection(self):
        entry = {"full_content": "快捷搜索结果"}
        manager = SimpleNamespace(
            word_blocks=[entry], clipboard_history=[],
            find_matches=lambda query, *_: [entry] if query else [],
        )
        settings = SimpleNamespace(
            width=400, height=250, theme="light", font_size=14,
            word_wrap_enabled=True, multi_word_search=False, pinyin_initial_search=False,
            highlight_matches=False, show_source_enabled=False, save=lambda: True,
        )
        popup = SearchPopup(manager, settings)
        self.addCleanup(popup.close)
        selected = []
        popup.suggestion_selected.connect(selected.append)
        popup.reappear_in_place()
        self.app.processEvents()
        self.assertFalse(popup.is_continuous_mode())

        popup.search_box.setText("快捷搜索")
        popup.update_list(popup.search_box.text())
        QTest.keyClick(popup.search_box, Qt.Key.Key_Return)
        self.assertEqual(["快捷搜索结果"], selected)
        self.assertFalse(popup.isVisible())

        popup.reappear_in_place()
        popup.mode_button.click()
        editor = popup.continuous_editor.findChild(QPlainTextEdit)
        editor.setPlainText("第一行\n第二行")
        cursor = editor.textCursor()
        cursor.setPosition(2)
        editor.setTextCursor(cursor)
        self.assertTrue(popup.continuous_input_visible)
        self.assertFalse(popup.search_box.isVisible())

        popup.hide()
        self.assertFalse(popup.continuous_input_visible)
        popup.reappear_in_place()
        popup.mode_button.click()
        self.assertTrue(popup.search_box.isVisible())
        self.assertEqual((400, 250), (popup.width(), popup.height()))
        popup.mode_button.click()
        self.assertEqual("第一行\n第二行", editor.toPlainText())
        self.assertEqual(2, editor.textCursor().position())
        self.assertEqual(["快捷搜索结果"], selected)


if __name__ == "__main__":
    unittest.main()
