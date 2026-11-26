
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.composition.document import Choice, Document, Token
from core.composition.store import CompositionStore, LibraryConflict, displaced_path
from tests.composition_samples import MARKDOWN_SELECTION, PROJECT_OUTLINE
from ui.composition_editor import CompositionEditor, EditorState


class CompositionFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.editor = CompositionEditor()
        self.addCleanup(self.editor.close)

    def load(self, text):
        self.editor.load_state(EditorState(Document.from_raw(text)))

    def test_unchanged_unicode_mixed_newlines_round_trip_via_editor_and_model(self):
        for raw in (PROJECT_OUTLINE, MARKDOWN_SELECTION, "\n\r\n\r\t\u2029\u202f\x00\ufeff😃\n\n"):
            with self.subTest(length=len(raw)):
                self.load(raw)
                self.assertEqual(self.editor.toPlainText(), raw)
                self.assertEqual(Document.load(self.editor.model.to_record()).raw, raw)

    def test_native_cursor_insertion_after_surrogate_pair_preserves_neighboring_literal(self):
        self.load("😃 review")
        doc = self.editor.model
        token = Token(2, 8, "review", Choice("review", "task"))
        self.editor.apply(doc.attach(token), 8)
        QTest.keyClicks(self.editor, "!")
        self.assertEqual(self.editor.model.raw, "😃 review!")
        self.assertEqual(self.editor.model.tokens, (token,))
        self.editor.undo()
        self.assertEqual(self.editor.offsets(), (8, 8))
        self.editor.redo()
        self.assertEqual(self.editor.model.raw, "😃 review!")

    def test_native_ime_commit_detaches_edited_token_and_undo_restores_it(self):
        self.load("review")
        original = self.editor.model.attach(Token(0, 6, "review", Choice("review", "task")))
        self.editor.apply(original, 3)
        QApplication.sendEvent(self.editor, QInputMethodEvent("zi", []))
        self.assertTrue(self.editor.composing)
        self.assertEqual(self.editor.model, original)
        commit = QInputMethodEvent()
        commit.setCommitString("字")
        QApplication.sendEvent(self.editor, commit)
        self.assertEqual(self.editor.model.raw, "rev字iew")
        self.assertFalse(self.editor.model.tokens)
        self.editor.undo()
        self.assertEqual(self.editor.model, original)

    def test_mixed_newline_mime_and_original_selection(self):
        self.load("start\r\nend")
        self.editor.select_offsets(6)
        mime = QMimeData()
        mime.setText(MARKDOWN_SELECTION)
        self.editor.insertFromMimeData(mime)
        self.assertEqual(self.editor.toPlainText(), "start\r\n" + MARKDOWN_SELECTION + "end")
        self.editor.select_offsets(6, 6 + len(Document.from_raw(MARKDOWN_SELECTION).text))
        self.assertEqual(self.editor.createMimeDataFromSelection().text(), MARKDOWN_SELECTION)
        self.editor.undo()
        self.assertEqual(self.editor.toPlainText(), "start\r\nend")

    def test_selection_deletion_preserves_outside_text_and_restores_token(self):
        self.load("first\r\n- review\r- publish")
        start = self.editor.model.text.index("review")
        token = Token(start, start + 6, "review", Choice("review"))
        self.editor.apply(self.editor.model.attach(token))
        self.editor.select_offsets(start + 2, start + 9)
        self.editor.replace_selection("X")
        self.assertEqual(self.editor.model.raw, "first\r\n- reXpublish")
        self.assertEqual(self.editor.model.tokens, ())
        self.editor.undo()
        self.assertEqual(self.editor.model.tokens, (token,))

    def test_split_merge_move_copy_and_history_keep_node_identity(self):
        self.load("- Review\r\n  - Read\n- Publish")
        original_ids = [node.id for node in self.editor.model.nodes]
        self.editor.move_branch(0, 1)
        self.assertEqual(self.editor.toPlainText(), "- Publish\n- Review\r\n  - Read")
        self.assertEqual([node.id for node in self.editor.model.nodes],
                         [original_ids[2], original_ids[0], original_ids[1]])
        self.editor.duplicate_branch(1)
        self.editor.model.validate()
        ids = [node.id for node in self.editor.model.nodes]
        self.assertEqual(ids[:3], [original_ids[2], original_ids[0], original_ids[1]])
        self.assertFalse(set(ids[3:]) & set(original_ids))
        self.editor.undo()
        self.editor.undo()
        self.assertEqual([node.id for node in self.editor.model.nodes], original_ids)
        self.editor.select_offsets(4)
        self.editor.insertPlainText("\n")
        self.assertEqual(self.editor.model.nodes[0].id, original_ids[0])
        self.assertNotIn(self.editor.model.nodes[1].id, original_ids)
        self.editor.select_offsets(5)
        QTest.keyClick(self.editor, Qt.Key_Backspace)
        self.assertEqual([node.id for node in self.editor.model.nodes], original_ids)

    def test_enter_after_parent_title_keeps_children_with_parent(self):
        self.load("- Project\r\n  - Design\r\n- Publish")
        self.editor.select_offsets(len("- Project"))
        QTest.keyClick(self.editor, Qt.Key_Return)
        self.assertEqual(self.editor.model.raw, "- Project\r\n  - Design\r\n- \r\n- Publish")
        QTest.keyClick(self.editor, Qt.Key_Z, Qt.ControlModifier)
        self.assertEqual(self.editor.model.raw, "- Project\r\n  - Design\r\n- Publish")

    def test_adjacent_branch_swaps_preserve_separator_inventory_and_identities(self):
        import random
        rng = random.Random(401)
        names = [f"- Item {i}" for i in range(8)]
        for _ in range(24):
            separators = [rng.choice(["\r\n", "\n", "\r", "\u2029"]) for _ in names[:-1]]
            raw = "".join(line + ending for line, ending in zip(names, (*separators, "")))
            self.load(raw)
            original = self.editor.model
            self.editor.move_branch(len(names) - 1, -1)
            updated = self.editor.model
            updated.validate()
            self.assertCountEqual(updated.breaks, original.breaks)
            self.assertEqual(updated.text.split("\n"), names[:-2] + names[-2:][::-1])
            self.assertEqual([n.id for n in updated.nodes], [n.id for n in (*original.nodes[:-2], *original.nodes[-2:][::-1])])
            self.editor.undo()
            self.assertEqual(self.editor.model.raw, raw)

    def test_keyboard_cut_paste_and_history_preserve_mixed_line_endings(self):
        raw = "- List\r\n  - Item\r- Archive"
        self.load(raw)
        self.editor.select_offsets(7, 16)
        self.addCleanup(self.app.clipboard().clear)
        selected = self.editor.model.slice(7, 16)
        QTest.keyClick(self.editor, Qt.Key_X, Qt.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), selected)
        QTest.keyClick(self.editor, Qt.Key_V, Qt.ControlModifier)
        self.assertEqual(self.editor.model.raw, raw)
        QTest.keyClick(self.editor, Qt.Key_Z, Qt.ControlModifier)
        self.assertNotEqual(self.editor.model.raw, raw)
        self.editor.redo()
        self.assertEqual(self.editor.model.raw, raw)

    def test_fold_hides_child_blocks_without_editing_its_text(self):
        self.load("- Review\n  - Tasks\n- Publish")
        raw = self.editor.model.raw
        self.editor.toggle_fold(0)
        self.assertFalse(self.editor.document().findBlockByNumber(1).isVisible())
        self.assertEqual(self.editor.model.raw, raw)
        self.editor.undo()
        self.assertTrue(self.editor.document().findBlockByNumber(1).isVisible())


class CompositionStorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "composition.sqlite3"
        self.library = Path(folder.name) / "composition.md"

    def test_restart_restores_structured_draft_and_rejects_stale_revision(self):
        store = CompositionStore(self.database, self.library)
        document = Document.from_raw(PROJECT_OUTLINE)
        store.save_draft("example", 3, document, 1, 2)
        store.save_draft("example", 2, Document(), 0, 0)
        self.assertEqual(CompositionStore(self.database, self.library).load_draft("example"),
                         (3, document, 1, 2))

    def test_abrupt_process_death_before_and_after_markdown_publication(self):
        code = """
import os
from pathlib import Path
import sys
from core.composition.store import CompositionStore
def publish(path, before, after):
    if sys.argv[3] == 'after':
        path.write_bytes(after)
    os._exit(42)
db, library = map(Path, sys.argv[1:3])
store = CompositionStore(db, library, publish=publish)
store.publish_library(b'- review\\n', b'- review\\n- archive\\n', {'groups': ['archive']})
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self.library.write_bytes(b"- review\n")
                run = subprocess.run(
                    [sys.executable, "-c", code, str(self.database), str(self.library), mode],
                    env=env, capture_output=True, timeout=10,
                )
                self.assertEqual(run.returncode, 42, run.stderr.decode(errors="replace"))
                store = CompositionStore(self.database, self.library)
                self.assertEqual(store.recover_library(), "completed" if mode == "after" else "rolled_back")
                self.assertEqual(store.get("groups"), ["archive"] if mode == "after" else None)
                self.assertEqual(store.recover_library(), "clean")

    def test_unidentified_external_version_is_preserved_with_recovery_intent(self):
        self.library.write_bytes(b"- review\n")

        def external_writer(path, before, after):
            path.write_bytes(b"- edited elsewhere\n")

        store = CompositionStore(self.database, self.library, publish=external_writer)
        with self.assertRaises(LibraryConflict):
            store.publish_library(b"- review\n", b"- review\n- archive\n", {"groups": ["archive"]})
        with self.assertRaises(LibraryConflict):
            CompositionStore(self.database, self.library).recover_library()
        self.assertIsNone(store.get("groups"))
        self.assertEqual(self.library.read_bytes(), b"- edited elsewhere\n")

    @unittest.skipUnless(sys.platform == "win32", "Uses the Windows atomic replacement API")
    def test_crash_after_windows_replacement_keeps_displaced_concurrent_bytes(self):
        self.library.write_bytes(b"- review\n")

        def cut_over_before_verification(path, expected, proposed):
            from core.word_source import WordSource
            path.write_bytes(b"- edited elsewhere\n")
            replacement = path.with_name("proposal")
            replacement.write_bytes(proposed)
            WordSource._replace_file_windows(str(path), str(replacement), str(displaced_path(path)))
            raise RuntimeError("simulated interruption before validating displaced bytes")

        store = CompositionStore(self.database, self.library, publish=cut_over_before_verification)
        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            store.publish_library(b"- review\n", b"- review\n- archive\n", {"groups": ["archive"]})
        store = CompositionStore(self.database, self.library)
        with self.assertRaises(LibraryConflict):
            store.recover_library()
        self.assertEqual(self.library.read_bytes(), b"- edited elsewhere\n")
        self.assertIsNone(store.get("groups"))
        self.assertTrue(store.retain_external_library())
        self.assertEqual(store.recover_library(), "clean")

    def test_oddball_input_cannot_overwrite_existing_library_or_duplicate_word(self):
        self.library.write_bytes(b"# Maintenance\r\n- review\r\n")
        store = CompositionStore(self.database, self.library)
        with self.assertRaises(LibraryConflict):
            store.publish_library(None, b"- archive\n", {})
        with self.assertRaises(ValueError):
            store.add_word("review\n- accident", {})
        store.add_word("review", {"groups": ["review"]})
        self.assertEqual(self.library.read_bytes(), b"# Maintenance\r\n- review\r\n")
        self.assertEqual(store.get("groups"), ["review"])


if __name__ == "__main__":
    unittest.main()
