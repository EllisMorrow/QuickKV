# -*- coding: utf-8 -*-
import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtGui import QAction


def _directory_display_labels(directories):
    """Return compact, unique suffix labels for a collection of directories."""
    parts_by_directory = {directory: Path(directory).parts for directory in directories}
    labels = {}

    for directory, parts in parts_by_directory.items():
        suffix_size = min(2, len(parts))
        while suffix_size < len(parts):
            suffix = os.path.join(*parts[-suffix_size:]).casefold()
            if all(
                suffix != os.path.join(*other_parts[-min(suffix_size, len(other_parts)):]).casefold()
                for other_directory, other_parts in parts_by_directory.items()
                if other_directory != directory
            ):
                break
            suffix_size += 1

        suffix = os.path.join(*parts[-suffix_size:]) if parts else directory
        labels[directory] = suffix if suffix_size == len(parts) else f"…{os.sep}{suffix}"

    return labels


def populate_library_target_menu(menu, targets, on_triggered):
    """Populate a menu with native actions grouped by their destination directory."""
    if not targets:
        no_library_action = QAction("无可用词库", menu)
        no_library_action.setEnabled(False)
        menu.addAction(no_library_action)
        return

    grouped_targets = OrderedDict()
    for target in sorted(
        targets,
        key=lambda item: (
            os.path.dirname(item['path']).casefold(),
            os.path.basename(item['path']).casefold(),
        ),
    ):
        directory = os.path.dirname(target['path'])
        grouped_targets.setdefault(directory, []).append(target)

    directory_labels = _directory_display_labels(grouped_targets)
    menu.setToolTipsVisible(True)

    for group_index, (directory, directory_targets) in enumerate(grouped_targets.items()):
        if group_index:
            menu.addSeparator()

        directory_action = QAction(f"目录  {directory_labels[directory]}", menu)
        directory_action.setEnabled(False)
        directory_action.setToolTip(directory)
        menu.addAction(directory_action)

        for target in directory_targets:
            path = target['path']
            action = QAction(os.path.basename(path), menu)
            action.setData(path)
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(
                lambda checked=False, target_path=path: on_triggered(target_path)
            )
            menu.addAction(action)
