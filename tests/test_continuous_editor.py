import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.continuous_editor import ContinuousEditor
from ui.candidate_text import highlighted_html, match_colors, match_excerpt
from core.config import THEMES
from core.word_manager import WordManager
from core.word_source import WordSource


class ContinuousEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.refund = {
            "parent": "退款说明", "full_content": "退款说明\n已提交退款。",
            "aliases": [], "shortcut_code": "tk",
        }
        contact = {
            "parent": "联系在线客服", "full_content": "联系在线客服",
            "aliases": ["lx"], "shortcut_code": None,
        }
        distractor = {
            "parent": "其他词条", "full_content": "其他词条",
            "aliases": ["tk"], "shortcut_code": None,
        }
        manager = WordManager.__new__(WordManager)
        manager.ranking_state = None
        manager.word_blocks = [
            manager._preprocess_block(block) for block in (self.refund, contact, distractor)
        ]
        find_matches = manager.find_matches
        manager.find_matches = lambda query, *args, **kwargs: (
            find_matches(query, *args, **kwargs) if kwargs else [distractor] if query == "tk" else []
        )
        settings = SimpleNamespace(
            theme="light", font_size=14, multi_word_search=False, pinyin_initial_search=True
        )
        self.widget = ContinuousEditor(manager, settings)
        self.widget.expand_entry = lambda block: block["parent"]
        self.widget.resize(360, 190)
        self.widget.show()
        self.widget.activateWindow()
        self.widget.focus_editor()
        self.app.processEvents()
        self.addCleanup(self.widget.close)
        self.editor = self.widget.editor

    def seed(self, text):
        self.editor.setPlainText(text)
        self.editor.moveCursor(QTextCursor.End)

    def type_and_wait(self, text):
        QTest.keyClicks(self.editor, text)
        QTest.qWait(self.widget._timer.interval() + 40)
        self.assertFalse(self.widget.candidates.isHidden())

    def test_multiple_insertions_tight_viewport_and_undo(self):
        inserted = []
        self.widget.entry_inserted.connect(lambda block: inserted.append(block["parent"]))
        self.seed("🙂 ")
        self.type_and_wait("tk")
        self.assertEqual(self.widget.results.count(), 1)
        self.assertEqual(self.widget.results.currentItem().text(), "退款说明")
        self.assertTrue(self.editor.viewport().geometry().contains(self.widget.candidates.geometry()))
        QTest.keyClick(self.editor, Qt.Key_Tab)
        QTest.keyClick(self.editor, Qt.Key_Return)
        self.type_and_wait("lx")
        QTest.keyClick(self.editor, Qt.Key_Tab)
        self.assertEqual(self.editor.toPlainText(), "🙂 退款说明\n联系在线客服")
        QTest.keyClick(self.editor, Qt.Key_Z, Qt.ControlModifier)
        self.assertEqual(self.editor.toPlainText(), "🙂 退款说明\nlx")
        self.assertEqual(inserted, ["退款说明", "联系在线客服"])

    def test_same_shortcut_uses_normal_tiebreakers_and_refresh_retains_selection(self):
        newer = {
            "entry_id": "progress", "parent": "退款进度", "full_content": "退款进度",
            "shortcut_code": "tk", "usage_meta": {"count": 1, "last_used_at": "2026-11-21T10:00:00"},
        }
        self.refund["entry_id"] = "explanation"
        self.widget.word_manager.word_blocks = [self.refund, newer]
        self.type_and_wait("tk")
        self.assertEqual(self.widget.results.currentItem().text(), "退款进度")

        self.refund["is_favorite"] = True
        self.widget.refresh_candidates()
        self.assertEqual(self.widget.results.item(0).text(), "退款说明")
        self.assertEqual(self.widget.results.currentItem().text(), "退款进度")

        self.widget.word_manager.word_blocks = [self.refund]
        self.widget.refresh_candidates()
        self.assertEqual(self.widget.results.currentItem().text(), "退款说明")

    def test_chinese_prefix_matches_follow_main_search_ranking(self):
        manager = WordManager.__new__(WordManager)
        manager.ranking_state = None
        titles = ("退款说明", "退款进度", "售后事项")
        manager.word_blocks = [
            manager._preprocess_block({
                "parent": title, "full_content": title,
                "aliases": ["退款"] if index == 2 else [],
            })
            for index, title in enumerate(titles)
        ]
        manager.word_blocks[1]["is_favorite"] = True
        self.widget.word_manager = manager
        self.widget._index_entries()
        matches = self.widget._prefix_matches("退款")
        expected = manager.find_matches("退款", False, True)
        self.assertEqual([b["parent"] for b in matches], [b["parent"] for b in expected])
        self.assertEqual([b["parent"] for b in matches], ["退款进度", "退款说明", "售后事项"])

    def test_manual_fuzzy_query_only_replaces_selected_text(self):
        self.seed("保留 🙂 关键词 保留")
        cursor = self.editor.textCursor()
        cursor.setPosition(6)
        cursor.setPosition(9, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        QTest.keyClick(self.editor, Qt.Key_J, Qt.ControlModifier)
        self.assertEqual(self.widget.lookup.text(), "关键词")
        QTest.keyClicks(self.widget.lookup, "tk")
        QTest.qWait(self.widget._timer.interval() + 40)
        QTest.keyClick(self.widget.lookup, Qt.Key_Tab)
        self.assertEqual(self.editor.toPlainText(), "保留 🙂 退款说明 保留")

    def test_automatic_letters_share_normal_search_and_load_real_shortcuts(self):
        with tempfile.TemporaryDirectory() as directory:
            dictionary = Path(directory) / "词库.md"
            dictionary.write_text(
                "- 退款说明 ``不出现`` ``k:tk``\n款项会原路退回。\n"
                "- 退款进度 ``不出现`` ``k:tk``\n请留意进度。\n"
                "- 退款流程\n",
                encoding="utf-8",
            )
            manager = WordManager.__new__(WordManager)
            manager.ranking_state = None
            manager.word_blocks = [
                manager._preprocess_block(block) for block in WordSource(str(dictionary)).word_blocks
            ]
            self.widget.word_manager = manager
            self.widget.settings.multi_word_search = True

            self.seed("保留，")
            self.type_and_wait("t")
            self.assertEqual(self.widget.results.count(), 3)
            self.type_and_wait("k")
            self.assertEqual(
                {self.widget.results.item(i).text() for i in range(self.widget.results.count())},
                {"退款说明", "退款进度"},
            )
            self.type_and_wait(" l")
            self.assertEqual(self.widget.results.currentItem().text(), "退款流程")
            QTest.keyClick(self.editor, Qt.Key_Tab)
            self.assertEqual(self.editor.toPlainText(), "保留，退款流程")

            self.widget.settings.pinyin_initial_search = False
            self.seed("")
            QTest.keyClicks(self.editor, "t")
            QTest.qWait(self.widget._timer.interval() + 40)
            self.assertEqual(self.widget.results.count(), 2)
            self.assertNotIn(
                "退款流程",
                [self.widget.results.item(i).text() for i in range(self.widget.results.count())],
            )

    def test_ime_commit_searches_only_the_chinese_suffix(self):
        self.seed("请退款")
        event = QInputMethodEvent("shuo", [])
        self.app.sendEvent(self.editor, event)
        self.assertTrue(self.widget.candidates.isHidden())
        commit = QInputMethodEvent()
        commit.setCommitString("说")
        self.app.sendEvent(self.editor, commit)
        QTest.qWait(self.widget._timer.interval() + 40)
        self.assertEqual(self.widget.results.currentItem().text(), "退款说明")
        QTest.keyClick(self.editor, Qt.Key_Tab)
        self.assertEqual(self.editor.toPlainText(), "请退款说明")

    def test_escape_preserves_draft_and_submission_keeps_a_recovery_copy(self):
        self.seed("整段草稿")
        close = []
        submitted = []
        self.widget.escape_requested.connect(lambda: close.append(True))
        self.widget.submit_requested.connect(submitted.append)
        QTest.keyClick(self.editor, Qt.Key_J, Qt.ControlModifier)
        QTest.keyClick(self.widget.lookup, Qt.Key_Escape)
        self.assertTrue(self.widget.candidates.isHidden())
        self.assertFalse(close)
        QTest.keyClick(self.editor, Qt.Key_Escape)
        self.assertEqual(close, [True])
        QTest.keyClick(self.editor, Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual(submitted, ["整段草稿"])
        self.widget.complete_submission(submitted[0])
        self.assertFalse(self.widget.has_draft())
        self.widget.recover_button.click()
        self.assertEqual(self.editor.toPlainText(), "整段草稿")

    def test_long_candidate_centers_match_and_previews_complete_entry(self):
        title = "前" * 85 + "阻力" + "后" * 85
        full_text = "- " + title + "\n末尾完整正文"
        groups = {0: {87}, 1: {88}}
        excerpt, offset = match_excerpt(title, [85, 86], 16, len)
        self.assertTrue(excerpt.startswith("…") and excerpt.endswith("…"))
        self.assertEqual(excerpt[85 - offset:87 - offset], "阻力")
        self.assertLessEqual(abs(excerpt.index("阻力") - len(excerpt) // 2), 2)
        colors = match_colors(groups, THEMES["light"])
        markup = highlighted_html(excerpt, colors, "#000000", offset=offset + 2)
        self.assertIn(f'color:{colors[87]};">阻', markup)
        self.assertIn(f'color:{colors[88]};">力', markup)

        self.widget.resize(555, 330)
        self.widget.word_manager.find_matches = lambda *_: [
            {"parent": title, "full_content": full_text, "highlight_groups": groups},
            *[{"parent": str(i), "full_content": str(i)} for i in range(7)],
        ]
        self.type_and_wait("zl")
        self.assertEqual(self.widget.results.count(), 8)
        self.assertEqual(self.widget.preview.toPlainText(), title + "\n末尾完整正文")
        self.assertGreater(self.widget.preview.x(), self.widget.candidates.geometry().right())
        self.assertTrue(self.editor.viewport().geometry().contains(self.widget.preview.geometry()))
        list_geometry = self.widget.candidates.geometry()
        QTest.keyClick(self.editor, Qt.Key_Down)
        self.assertTrue(self.widget.preview.isHidden())
        self.assertEqual(self.widget.candidates.geometry(), list_geometry)
        QTest.keyClick(self.editor, Qt.Key_Up)
        self.assertFalse(self.widget.preview.isHidden())
        QTest.keyClick(self.editor, Qt.Key_Escape)
        self.assertTrue(self.widget.preview.isHidden())
