"""Generic starter content used only in an empty portable installation."""

from dataclasses import replace
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from core.composition.document import Document, Token
from core.composition.groups import Catalog, Group, Module, Numeric, Option
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


# Shared roles are declared once. Each module opens with its primary group.
SHARED_GROUPS = {
    "priority": ("优先级", "紧急 重要 常规 稍后 可选 低优先级"),
    "status": ("完成状态", "待办 进行中 已完成 已暂停 待开始 已取消 待跟进 已归档"),
    "place": ("地点", "家中 办公室 会议室 线上 图书馆 超市 社区 医院 车站"),
    "deadline": ("期限", "今天 明天 本周 下周 本月底 下月底 本季度 待定 无期限"),
    "risk": ("风险程度", "低风险 中风险 高风险 已识别 可控制 需排查 需升级"),
    "owner": ("责任人", "本人 同事 负责人 项目组 协作方 供应商 客户 待分配"),
    "result": ("成果", "方案 清单 报告 记录 草案 原型 文档 验收材料 交付件"),
    "source": ("信息来源", "书籍 文章 网页 课程 正式文档 会议 访谈 现场观察 实验记录"),
    "mastery": ("掌握程度", "未学习 初步理解 基本掌握 熟练 可复述 待巩固"),
    "channel": ("沟通渠道", "当面 电话 邮件 即时消息 视频会议 工单 公告 文档评论"),
    "feedback": ("反馈类型", "建议 疑问 确认 异议 提醒 补充 变更请求 结果反馈"),
    "impact": ("影响程度", "轻微 局部 明显 严重 阻塞 影响进度 影响质量 影响体验"),
    "evidence": ("证据状态", "待验证 已验证 需复查 可复现 未复现 有记录 缺少证据"),
    "handling": ("处理方式", "独立处理 协作处理 委托处理 现场处理 远程处理 暂缓处理"),
    "items": ("物品类别", "食材 日用品 清洁用品 文具 药品 账单 衣物 工具 数码配件"),
    "time": ("时间安排", "早晨 上午 中午 下午 傍晚 晚上 稍后 工作日 周末"),
    "review": ("复核状态", "未复核 复核中 已复核 需补充 退回 待确认 已确认"),
}

MODULES = (
    ("daily", "日常速记", "日常行动", "记下 核对 整理 收集 查看 发送 跟进 提醒 补充 预约",
     ("priority", "time", "place"), ("status",),
     (("随手记", "事项 要点 下一步"), ("今日待办", "待办 优先项 进度 后续安排"))),
    ("task", "任务管理", "任务类型", "计划 准备 分解 检查 安排 执行 协调 跟踪 交接 复盘",
     ("priority", "handling"), ("status", "deadline", "progress", "duration"),
     (("任务卡", "任务 验收标准 截止日期 下一步"), ("周计划", "本周重点 待办 时间安排 回顾"))),
    ("project", "项目管理", "项目行动", "立项 调研 设计 开发 测试 审核 联调 演示 交付 验收",
     ("priority", "owner", "risk"), ("status", "result", "review", "progress"),
     (("项目记录", "目标 当前进度 问题 解决方案 下一步"), ("交付检查", "交付物 验收条件 负责人 风险"))),
    ("study", "阅读学习", "学习方式", "阅读 标注 摘录 练习 回顾 测验 复述 讨论 实践 总结",
     ("source", "time"), ("mastery", "score", "duration"),
     (("学习记录", "主题 核心内容 理解 疑问 下一步"), ("阅读卡片", "来源 摘录 理解 待验证"))),
    ("work", "工作沟通", "沟通行动", "询问 回复 说明 汇报 约谈 协商 反馈 确认 同步 通知",
     ("channel", "priority"), ("feedback", "status", "deadline"),
     (("会议纪要", "议题 要点 决议 负责人 待办"), ("反馈记录", "背景 反馈 建议 后续跟进"))),
    ("issues", "问题分析", "问题类型", "异常 缺陷 误差 延迟 中断 遗漏 歧义 冲突 性能问题 兼容问题",
     ("impact", "source"), ("evidence", "status", "handling"),
     (("问题分析", "现象 影响 原因假设 证据 处理方案"), ("验证记录", "假设 步骤 实际结果 复查"))),
    ("home", "生活管理", "生活行动", "采购 清洁 维修 缴费 取件 保养 归置 备餐 预约 检查库存",
     ("items", "place", "time"), ("status", "quantity", "deadline"),
     (("采购清单", "物品 数量 预算 备注"), ("生活安排", "事项 地点 时间 准备 完成情况"))),
)

NUMBERS = {
    "progress": ("完成比例", "0 25 50 75 100", Numeric("0", "100", "1", "%")),
    "score": ("评分", "1 2 3 4 5", Numeric("1", "5", "1")),
    "quantity": ("数量", "1 2 3 5 10", Numeric("0", "", "1", "件")),
    "duration": ("耗时", "15 30 45 60 90", Numeric("0", "", "1", "分钟")),
}


def make_group(key, name, words, **settings):
    return Group(name, "starter-" + key, options=tuple(
        Option(text, f"starter-{key}-{index}") for index, text in enumerate(words.split())
    ), **settings)


def starter_composition():
    defaults = Catalog.defaults()
    timestamp = defaults.groups[0]
    groups = {key: make_group(key, name, words) for key, (name, words) in SHARED_GROUPS.items()}
    for key, (name, words, numeric) in NUMBERS.items():
        values = " ".join(numeric.format_value(word) for word in words.split())
        groups[key] = make_group(key, name, values, kind="number", numeric=numeric)
    modules = []
    templates = {}
    for key, name, label, words, prefixes, suffixes, fragments in MODULES:
        group = make_group(key, label, words, prefixes=tuple(groups[g].id for g in prefixes),
                           suffixes=tuple(groups[g].id for g in suffixes))
        groups[key] = group
        module_id = defaults.selected_module if not modules else "starter-" + key
        visible = tuple(groups[g].id for g in (key, *prefixes, *suffixes)[:4])
        modules.append(Module(name, module_id, (*visible, timestamp.id)))
        for index, (title, fields) in enumerate(fragments):
            text = f"- {title}\n" + "".join(f"  - {field}：\n" for field in fields.split())
            templates[f"starter-{key}-{index}"] = {
                "name": title, "module": module_id, "fragment": Document.from_raw(text).to_record(),
            }
    modules.append(Module("空白模块", "starter-empty", scope_limited=True))
    catalog = replace(defaults, groups=(timestamp, *groups.values()), modules=tuple(modules))
    catalog.validate()
    priority, status, main = (groups[g] for g in ("priority", "status", "daily"))
    roles = [
        (priority.choice(priority.options[1]), main.choice(main.options[1]), status.choice(status.options[0])),
        (priority.choice(priority.options[2]), main.choice(main.options[2]), status.choice(status.options[1])),
    ]
    literals = ["+".join(choice.text for choice in row) for row in roles]
    model = Document.from_raw("- 使用示例\n" + "".join(f"  - {text}\n" for text in literals))
    for text, (prefix, main_word, suffix) in zip(literals, roles):
        start = model.text.index(text)
        model = model.attach(Token(start, start + len(text), text, main_word, (prefix,), (suffix,)))
    return catalog, model, templates


def initialize_starter_content(config, word_file, database, composition_library):
    """Resume only a marked first-run transaction. Never replace an existing library."""
    config, word_file, database, composition_library = map(Path, (config, word_file, database, composition_library))
    if config.exists() or word_file.exists():
        return False
    other_markdown = composition_library.parent.glob("*.md")
    if any(path != composition_library for path in other_markdown):
        return False

    if database.exists():
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                marker = connection.execute("SELECT data FROM records WHERE key = 'starter_state'").fetchone()
        except sqlite3.Error:
            return False
        if marker != ('"pending"',):
            return False
    else:
        if composition_library.exists():
            return False
        database.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=database.parent, prefix=".starter-") as directory:
            staged = Path(directory) / "starter.sqlite3"
            store = CompositionStore(staged, Path(directory) / "unused.md")
            store.set("starter_state", "pending")
            replace_library(database, None, staged.read_bytes())

    store = CompositionStore(database, composition_library)
    if store.get("starter_state") != "pending":
        return False
    if store.recover_library() == "completed":
        return store.get("starter_state") == "complete"
    if composition_library.exists() or store.get("catalog") or store.draft_ids():
        return False
    catalog, model, templates = starter_composition()
    terms = dict.fromkeys(option.text for group in catalog.groups if group.kind == "ordinary"
                          for option in group.values())
    library = STARTER_WORDS + "\n## 组合输入常用词\n\n" + "".join(f"- {word}\n" for word in terms)
    store.publish_library(None, library.encode("utf-8"), {
        "catalog": catalog.to_record(), "templates": templates,
        "active_session": catalog.selected_module, "starter_state": "complete",
        "__draft__": (catalog.selected_module, 1, {"document": model.to_record(), "anchor": 0, "cursor": 0}),
    })
    return True
