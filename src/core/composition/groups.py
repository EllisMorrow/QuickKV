
"""Reusable word groups and the choices frozen in individual tokens."""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from decimal import Decimal, DecimalException

from core.composition.document import Choice, Token, new_id


TIMESTAMP_ID = "builtin-timestamp"
DEFAULT_MODULE_ID = "default-module"
TIME_FORMATS = (
    ("日期和时间戳", "%Y%m%d%H%M%S"), ("四位时间", "%H%M"), ("八位日期", "%Y%m%d"),
    ("六位时间", "%H%M%S"), ("标准日期", "%Y-%m-%d"), ("标准时间", "%H:%M"),
    ("带秒时间", "%H:%M:%S"), ("完整日期时间", "%Y-%m-%d %H:%M:%S"),
    ("星期", "%A"), ("年月", "%Y%m"),
)


@dataclass(frozen=True)
class Option:
    text: str
    id: str = field(default_factory=new_id)
    parent: str = ""
    category: bool = False
    source: str = ""
    fingerprint: str = ""
    definition: str = ""
    criteria: str = ""
    explanation: str = ""
    counterexample: str = ""
    pattern: str = ""

    def value(self, instant=None):
        now = instant or datetime.now()
        if self.id == "time-8" and self.text == "星期" and self.pattern == "%A":
            return "星期" + "一二三四五六日"[now.weekday()]
        return self.text if not self.pattern else now.strftime(self.pattern)

    def description(self):
        return "\n".join(filter(None, (self.definition, self.criteria, self.explanation, self.counterexample)))


@dataclass(frozen=True)
class Numeric:
    minimum: str = ""
    maximum: str = ""
    step: str = ""
    unit: str = ""

    def format_value(self, text):
        try:
            if len(text) > 64:
                raise ValueError("数值太长")
            number = Decimal(text)
            if not number.is_finite() or abs(number.adjusted()) > 50:
                raise ValueError("请填写合理的有限数值")
            minimum = Decimal(self.minimum) if self.minimum else None
            maximum = Decimal(self.maximum) if self.maximum else None
            step = Decimal(self.step) if self.step else None
            if minimum is not None and number < minimum or maximum is not None and number > maximum:
                raise ValueError("数值不在指定范围内")
            if step is not None and (step <= 0 or (number - (minimum or Decimal(0))) % step):
                raise ValueError("数值不符合设置的步长")
        except DecimalException as error:
            raise ValueError("数值或词组配置无效") from error
        return format(number, "f") + self.unit


@dataclass(frozen=True)
class Group:
    name: str
    id: str = field(default_factory=new_id)
    kind: str = "ordinary"
    options: tuple[Option, ...] = ()
    prefixes: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    numeric: Numeric = field(default_factory=Numeric)

    def choice(self, option, instant=None):
        return Choice(option.value(instant), self.id, option.id)

    def values(self):
        return tuple(option for option in self.options if not option.category)


@dataclass(frozen=True)
class Module:
    name: str
    id: str = field(default_factory=new_id)
    groups: tuple[str, ...] = ()
    shortcut: str = ""
    show_observations: bool = True
    scope_limited: bool = False


@dataclass(frozen=True)
class Catalog:
    groups: tuple[Group, ...] = ()
    modules: tuple[Module, ...] = ()
    selected_module: str = DEFAULT_MODULE_ID

    @classmethod
    def defaults(cls):
        timestamp = Group("时间戳", TIMESTAMP_ID, "timestamp", tuple(
            Option(label, f"time-{index}", pattern=pattern) for index, (label, pattern) in enumerate(TIME_FORMATS)
        ))
        return cls((timestamp,), (Module("默认模块", DEFAULT_MODULE_ID),))

    def group(self, identity):
        return next((group for group in self.groups if group.id == identity), None)

    def module(self, identity=None):
        return next(module for module in self.modules if module.id == (identity or self.selected_module))

    def replace_group(self, value):
        return replace(self, groups=(*tuple(g for g in self.groups if g.id != value.id), value)
                       if not self.group(value.id) else tuple(value if g.id == value.id else g for g in self.groups))

    def remove_group(self, identity):
        return replace(
            self,
            groups=tuple(replace(
                group, prefixes=tuple(g for g in group.prefixes if g != identity),
                suffixes=tuple(g for g in group.suffixes if g != identity)
            ) for group in self.groups if group.id != identity),
            modules=tuple(replace(module, groups=tuple(g for g in module.groups if g != identity))
                          for module in self.modules),
        )

    def replace_module(self, value):
        existing = any(module.id == value.id for module in self.modules)
        return replace(self, modules=tuple(value if m.id == value.id else m for m in self.modules)
                       if existing else (*self.modules, value))

    def to_record(self):
        record = asdict(self)
        for module in record["modules"]:
            if not module["scope_limited"]:
                del module["scope_limited"]
        return record

    @classmethod
    def load(cls, record):
        if record is None:
            return cls.defaults()
        groups = tuple(Group(
            **{**group, "options": tuple(Option(**o) for o in group["options"]),
               "prefixes": tuple(group["prefixes"]), "suffixes": tuple(group["suffixes"]),
               "numeric": Numeric(**group["numeric"])}
        ) for group in record["groups"])
        modules = tuple(Module(**{**module, "groups": tuple(module["groups"])}) for module in record["modules"])
        result = cls(groups, modules, record["selected_module"])
        result.validate()
        return result

    def validate(self):
        identities = {group.id for group in self.groups}
        module_ids = {module.id for module in self.modules}
        if (len(identities) != len(self.groups) or len(module_ids) != len(self.modules)
                or self.selected_module not in module_ids):
            raise ValueError("词组或输入模块身份无效")
        for module in self.modules:
            if not module.name.strip() or not set(module.groups) <= identities:
                raise ValueError("输入模块的词组引用无效")
        for group in self.groups:
            if not group.name.strip() or group.kind not in ("ordinary", "timestamp", "number"):
                raise ValueError("词组配置无效")
            if group.kind == "number":
                rules = group.numeric
                try:
                    minimum, maximum, step = (Decimal(value) if value else None
                                               for value in (rules.minimum, rules.maximum, rules.step))
                    if (any(v is not None and (not v.is_finite() or abs(v.adjusted()) > 50)
                            for v in (minimum, maximum, step))
                            or minimum is not None and maximum is not None and minimum > maximum
                            or step is not None and step <= 0):
                        raise ValueError("数值组范围或步长无效")
                except DecimalException as error:
                    raise ValueError("数值组范围或步长无效") from error
            for side in (group.prefixes, group.suffixes):
                if group.id in side or len(set(side)) != len(side) or not set(side) <= identities:
                    raise ValueError("前后缀引用无效")
            options = {option.id: option for option in group.options}
            if len(options) != len(group.options):
                raise ValueError("词汇身份重复")
            for option in group.options:
                seen = {option.id}
                parent = option.parent
                if not option.text.strip() or any(c in option.text for c in "\r\n\u2029"):
                    raise ValueError("词汇和分类须为单行")
                if group.kind == "timestamp" and not option.category:
                    if not option.pattern or not option.value(datetime(2031, 4, 8, 9, 17)):
                        raise ValueError("时间戳格式无效")
                while parent:
                    if parent in seen or parent not in options or not options[parent].category:
                        raise ValueError("分类关系无效")
                    seen.add(parent)
                    parent = options[parent].parent


def replace_main(token, choice, group):
    if token.main.group_id == choice.group_id:
        return replace(token, main=choice)
    prefixes = group.prefixes if group else ()
    suffixes = group.suffixes if group else ()
    return replace(token, main=choice,
                   prefixes=tuple(c for c in token.prefixes if not c.group_id or c.group_id in prefixes),
                   suffixes=tuple(c for c in token.suffixes if not c.group_id or c.group_id in suffixes))


def choose_modifier(token, side, choice, ordered_groups):
    selected = list(getattr(token, side))
    index = next((i for i, value in enumerate(selected) if value.group_id == choice.group_id), None)
    if index is not None:
        if selected[index].option_id == choice.option_id and selected[index].text == choice.text:
            del selected[index]
        else:
            selected[index] = choice
    else:
        if choice.group_id in ordered_groups:
            current = ordered_groups.index(choice.group_id)
            index = next((i for i, value in enumerate(selected) if value.group_id in ordered_groups
                          and ordered_groups.index(value.group_id) > current), len(selected))
        else:
            index = len(selected)
        selected.insert(index, choice)
    return replace(token, **{side: tuple(selected)})


def provisional_token(position, choice):
    return Token(position, position, "", choice)
