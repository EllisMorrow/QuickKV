
"""Create a new portable QuickKV experience directory from a clean local build."""

import argparse
from dataclasses import replace
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.composition.backups import Backups
from core.composition.document import Choice, Document, Token
from core.composition.groups import Catalog, Group, Module, Numeric, Option
from core.composition.store import CompositionStore
from core.composition.structure import template_fragment


MODIFIERS = (
    ("priority", "优先级", "优先 常规 延后 紧急 可选"),
    ("size", "规模", "微型 小型 中型 大型 批量"),
    ("channel", "渠道", "线下 远程 混合 邮件 公告"),
    ("team", "分工", "记录组 资料组 验证组 协调组 保障组"),
    ("window", "时段", "晨间 上午 午间 下午 晚间"),
    ("source", "来源", "手册 访谈 草图 白板 样张 纪要 提要"),
    ("risk", "风险", "低风险 待排查 可控 有阻塞 需协调"),
    ("status", "完成状态", "待开始 进行中 已完成 已暂停 已归档"),
    ("check", "复核", "待复核 已核对 需补证 准予归档 待确认"),
    ("place", "地点", "阅览室 资料室 共享桌 校对台 工具间"),
    ("material", "载体", "纸本 文档 音频 视频 图表"),
    ("audience", "受众", "组员 访客 志愿者 学员 伙伴"),
    ("delivery", "交付方式", "面交 上传 团队同步 站点登记 自行领取"),
    ("method", "方法", "精读 比对 速览 摘录 复述"),
    ("quality", "证据等级", "已核实 待查证 仅参考 已复验 原始记录"),
    ("owner", "责任人", "甲组 乙组 丙组 丁组 联合小组"),
    ("cycle", "周期", "当天 每周 每月 单次 定期"),
)
MAINS = (
    ("task", "项目行动", "设计 开发 测试 审核 立项 跟进 演练 交付 校验 整理"),
    ("review", "复核行动", "审核 核对 抽查 校对 复盘 对照 复验"),
    ("artifact", "产物", "原型 记录 清单 计划 纪要 样本 图稿 报告"),
    ("reading", "阅读行动", "阅读 标注 对比 回忆 摘要 速读 检索 温习 讲解"),
    ("learning", "学习产物", "练习 卡片 笔记 索引 主题 难点 例题"),
    ("errand", "日常行动", "采购 预约 清点 送达 取件 登记 配送 保养"),
    ("event", "活动", "讲座 展示 讨论 彩排 工作坊 交流 会议"),
    ("inventory", "物品", "海报 桌牌 手册 展板 胶带 书签 名册 工具箱 标识牌 收纳盒 量尺 标签纸"),
)
EXAMPLES = (
    ("project", "项目管理", ("task", "review", "artifact"), "导航样机", "开发", "测试"),
    ("study", "阅读学习", ("reading", "learning", "review"), "图书馆索引练习", "阅读", "标注"),
    ("daily", "日常事务", ("errand", "event", "inventory"), "社区共享桌", "采购", "清点"),
)


def seed(root):
    data = root / "用户数据"
    library = root / "MD词库-需自动载入的请放入" / "组合输入.md"
    store = CompositionStore(data / "组合输入.sqlite3", library)
    groups = []
    for identity, title, words in (*MODIFIERS, *MAINS):
        options = tuple(Option(word, f"{identity}-{i}") for i, word in enumerate(words.split()))
        if identity == "task":
            options = (replace(options[0], definition="形成可讨论的方案", criteria="有目标和验收条件",
                               explanation="先写出关键假设", counterexample="仅复述原请求"), *options[1:])
        if identity == "source":
            categories = (Option("资料", "category-1", category=True),
                          Option("纸面", "category-2", "category-1", True),
                          Option("样册", "category-3", "category-2", True),
                          Option("装订样本", "category-4", "category-3", True))
            options = (*categories, *(replace(o, parent="category-4") for o in options))
        prefixes, suffixes = (), ()
        if (identity, title, words) in MAINS:
            prefixes = ("priority", "size", "channel", "team", "window", "source", "builtin-timestamp")
            suffixes = ("status", "check", "progress", "risk")
        if identity == "artifact":
            prefixes, suffixes = ("material", "source"), ("check",)
        elif identity == "review":
            prefixes, suffixes = ("priority", "method"), ("check", "quality")
        elif identity == "inventory":
            prefixes, suffixes = (), ()
        groups.append(Group(title, identity, options=options, prefixes=prefixes, suffixes=suffixes))
    groups.append(Group("完成比例", "progress", "number", tuple(
        Option(word, f"progress-{i}") for i, word in enumerate(("0%", "20%", "50%", "75%", "100%"))
    ), numeric=Numeric("0", "100", "5", "%")))
    defaults = Catalog.defaults()
    modules = tuple(Module(name, mid, tuple(ids)) for mid, name, ids, *_ in EXAMPLES)
    modules = (*modules, Module("空白模块", "blank"), Module("组合词验证", "round4", ("task", "review", "artifact")))
    groups.extend((Group("校准标签", "example-name-a", options=(Option("频率"),)),
                   Group("校准标签", "example-name-b", options=(Option("刻度"),))))
    catalog = Catalog((*groups, *defaults.groups), modules, "project")
    catalog.validate()
    templates, frames = {}, {}
    for mid, title, ids, subject, *words in EXAMPLES:
        frame = mid + "-frame"
        paths = ["目标", "进度", "阻塞", "审批"]
        frames[frame] = {"name": title + "观察", "module": mid, "paths": paths}
        group = catalog.group(ids[0])
        for i, word in enumerate(words):
            main = group.choice(next(o for o in group.options if o.text == word))
            pre = catalog.group("priority")
            post = catalog.group("status")
            prefix, suffix = pre.choice(pre.options[i]), post.choice(post.options[i + 1])
            literal = f"{prefix.text}+{word}+{suffix.text}"
            raw = (f"- {subject} · {i + 1}\r\n  - 目标：建立一份可交接的记录\r\n"
                   f"  - 行动：{literal}\r\n  - 进度：\r\n  - 阻塞：\r\n  - 审批：\r\n"
                   "  - 独立检查\r\n    - 目标：比对两份样张\r\n")
            model = Document.from_raw(raw)
            pos = model.text.index(literal)
            model = model.attach(Token(pos, pos + len(literal), literal, main, (prefix,), (suffix,)))
            model = replace(model, nodes=tuple(
                replace(node, observation=frame, statuses=(("阻塞", "unknown"), ("审批", "na"))) if index == 0
                else replace(node, observation=frame) if index == 6 else node
                for index, node in enumerate(model.nodes)
            ))
            store.save_draft(mid if i == 0 else f"reload-{mid}-example", 1, model, 0, 0)
            template = template_fragment(model, 0, (model.tokens[0].id,) if i else ())
            templates[f"{mid}-{i}"] = {"name": f"{title} · {'清空行动值' if i else '保留行动值'}",
                                      "module": mid, "fragment": template.to_record()}
    mixed = Document.from_raw("- 校验示例\r\n  - 行动：优先+开发+进行中、优先+开发+待复验、优先+审核+进行中\r\n"
                              "  - ![布局](images/plan+review.png)，行动：优先+测试+进行中\r\n"
                              "  - 代码：\x60优先+开发+进行中\x60\r\n")
    store.save_draft("reload-mixed", 1, mixed, 0, 0)
    exercise = Document.from_raw("- 未归组：校准标签\r\n- 待识别：晨检+扭矩核验+补签\r\n- 行动：优先+开发+进行中\r\n")
    start = exercise.text.index("校准标签")
    exercise = exercise.attach(Token(start, start + len("校准标签"), "校准标签", Choice("校准标签")))
    store.save_draft("round4", 1, exercise, start, start)
    store.save_draft("reload-round4-example", 1, Document.from_raw(exercise.raw), 0, 0)
    round5 = Document.from_raw(
        "- 晨检+标定，123\r\n- 行动：开发+待联审\r\n"
        "- 核对+审签\r\n- 附件：![样张](assets/demo+map.png)\r\n")
    store.save_draft("reload-round5-exercise", 1, round5, 0, 0)
    round6 = Document.from_raw(
        "- 晨检+校准、晨检+校准，备注：保留同名候选\r\n"
        "- 行动：预约+补签\r\n- 交接+封签+待复查\r\n")
    store.save_draft("reload-round6-exercise", 1, round6, 0, 0)
    round7 = Document.from_raw("- 晨检+校验、编审+归档\r\n- 外联+检查+待签\r\n")
    store.save_draft("reload-round7-exercise", 1, round7, 0, 0)
    terms = dict.fromkeys(o.text for g in groups if g.kind == "ordinary" for o in g.values())
    store.publish_library(None, "".join(f"- {term}\n" for term in terms).encode("utf8"), {
        "catalog": catalog.to_record(), "templates": templates, "frameworks": frames,
    })
    backups = Backups(store, data / "组合输入备份")
    backups.create("体验基线", include_drafts=True)
    invalid = root / "异常示例"
    invalid.mkdir()
    (invalid / "损坏的示例.quickkv-backup").write_bytes(b"Invalid QuickKV archive\n")
    return len(groups), len(terms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source, target = args.bundle.resolve(), args.destination.resolve()
    if not (source / "QuickKV.exe").is_file() or (source / "用户数据").exists() or (
            source / "MD词库-需自动载入的请放入").exists():
        parser.error("需要一份未运行过的独立测试包")
    if source == target or source in target.parents:
        parser.error("体验目录必须在测试包之外")
    target.mkdir()
    shutil.copytree(source, target, dirs_exist_ok=True)
    groups, terms = seed(target)
    print(f"Generated {groups} groups and {terms} distinct terms")


if __name__ == "__main__":
    main()
