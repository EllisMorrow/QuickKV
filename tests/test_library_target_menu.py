import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ui.library_target_menu import populate_library_target_menu


class LibraryTargetMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_files_share_one_directory_heading_and_keep_native_actions(self):
        menu = QMenu()
        selected_paths = []
        directory = os.path.join("D:\\", "QuickKV", "libraries")
        paths = [
            os.path.join(directory, "beta.md"),
            os.path.join(directory, "alpha.md"),
        ]

        populate_library_target_menu(
            menu,
            [{"path": path, "label": path} for path in paths],
            selected_paths.append,
        )

        actions = menu.actions()
        self.assertEqual(3, len(actions))
        self.assertFalse(actions[0].isEnabled())
        self.assertTrue(actions[0].text().startswith("目录  "))
        self.assertEqual(["alpha.md", "beta.md"], [action.text() for action in actions[1:]])
        self.assertEqual(paths[1], actions[1].data())

        actions[1].trigger()
        self.assertEqual([paths[1]], selected_paths)

    def test_same_filenames_in_different_directories_remain_distinguishable(self):
        menu = QMenu()
        first_path = os.path.join("D:\\", "TeamA", "shared", "words.md")
        second_path = os.path.join("D:\\", "TeamB", "shared", "words.md")

        populate_library_target_menu(
            menu,
            [
                {"path": first_path, "label": first_path},
                {"path": second_path, "label": second_path},
            ],
            lambda path: None,
        )

        actions = menu.actions()
        headings = [action.text() for action in actions if not action.isEnabled() and not action.isSeparator()]
        files = [action for action in actions if action.isEnabled() and not action.isSeparator()]

        self.assertEqual(2, len(set(headings)))
        self.assertEqual(["words.md", "words.md"], [action.text() for action in files])
        self.assertEqual({first_path, second_path}, {action.data() for action in files})
        self.assertEqual({first_path, second_path}, {action.toolTip() for action in files})

    def test_keyboard_navigation_skips_directory_heading(self):
        menu = QMenu()
        selected_paths = []
        path = os.path.join("D:\\", "QuickKV", "libraries", "words.md")
        populate_library_target_menu(
            menu,
            [{"path": path, "label": path}],
            selected_paths.append,
        )

        menu.popup(QPoint(0, 0))
        self.app.processEvents()
        QTest.keyClick(menu, Qt.Key_Down)
        self.assertEqual("words.md", menu.activeAction().text())

        QTest.keyClick(menu, Qt.Key_Return)
        self.assertEqual([path], selected_paths)


if __name__ == "__main__":
    unittest.main()
