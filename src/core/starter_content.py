"""Generic starter content used only in an empty portable installation."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from core.composition.document import Document, Token
from core.composition.groups import Catalog, Group, Module, Option
from core.composition.store import CompositionStore, replace_library


STARTER_WORDS = """# QuickKV 常用词库

- 收到，谢谢！
- 好的，我确认后回复你。
- 我已收到，稍后处理。
- 谢谢提醒，我会跟进。
- 麻烦你再确认一下，谢谢。
- 方便时请发我一份。
- 请问目前进展如何？
- 有进展我会及时同步。
- 这件事我来跟进。
- 我先整理一下，稍后发给你。
- 没问题，辛苦了。
- 让你久等了，抱歉。
- 我现在不方便回复，晚点联系你。
- 谢谢你的帮助！
- 祝你今天顺利！

- 待办记录
  事项：
  下一步：
  截止日期：

- 会议记录
  议题：
  结论：
  待办：

- 进度更新
  已完成：
  进行中：
  需要协助：
"""


def starter_composition():
    defaults = Catalog.defaults()
    priority = Group("优先级", "starter-priority", options=tuple(Option(text) for text in ("优先", "常规", "稍后")))
    status = Group("进度", "starter-status", options=tuple(Option(text) for text in ("待办", "进行中", "完成")))
    tasks = Group("日常动作", "starter-actions",
                  options=tuple(Option(text) for text in ("整理", "核对", "跟进", "回复", "记录", "发送")),
                  prefixes=(priority.id,), suffixes=(status.id,))
    catalog = replace(defaults, groups=(*defaults.groups, tasks, priority, status), modules=(
        replace(defaults.module(), name="日常速记", groups=(tasks.id, defaults.groups[0].id)),
        Module("空白模块", "starter-empty", scope_limited=True),
    ))
    catalog.validate()
    model = Document.from_raw("- 今日待办\n  - 优先+核对+待办\n  - 常规+整理+进行中\n")
    for prefix, main, suffix in (
        (priority.options[0], tasks.options[1], status.options[0]),
        (priority.options[1], tasks.options[0], status.options[1]),
    ):
        roles = (priority.choice(prefix), tasks.choice(main), status.choice(suffix))
        literal = "+".join(choice.text for choice in roles)
        start = model.text.index(literal)
        model = model.attach(Token(start, start + len(literal), literal, roles[1], (roles[0],), (roles[2],)))
    templates = {
        "starter-checklist": {
            "name": "待办清单", "module": catalog.selected_module,
            "fragment": Document.from_raw("- 待办事项\n  - 下一步：\n  - 截止日期：\n").to_record(),
        },
        "starter-meeting": {
            "name": "会议记录", "module": catalog.selected_module,
            "fragment": Document.from_raw("- 会议记录\n  - 结论：\n  - 后续行动：\n").to_record(),
        },
    }
    return catalog, model, templates


def initialize_starter_content(config, word_file, database, composition_library):
    """Publish the starter catalog and draft together; never replace existing files."""
    config, word_file, database, composition_library = map(Path, (config, word_file, database, composition_library))
    if any(path.exists() for path in (config, word_file, database, composition_library)):
        return False
    if word_file.parent.is_dir() and any(path.suffix.lower() == ".md" for path in word_file.parent.iterdir()):
        return False

    database.parent.mkdir(parents=True, exist_ok=True)
    catalog, model, templates = starter_composition()
    with TemporaryDirectory(dir=database.parent, prefix=".starter-") as directory:
        staging = Path(directory) / "starter.sqlite3"
        store = CompositionStore(staging, Path(directory) / "unused.md")
        store.save_draft(catalog.selected_module, 1, model, 0, 0, records={
            "catalog": catalog.to_record(), "templates": templates, "active_session": catalog.selected_module,
        })
        replace_library(database, None, staging.read_bytes())
    replace_library(word_file, None, STARTER_WORDS.encode("utf-8"))
    return True
