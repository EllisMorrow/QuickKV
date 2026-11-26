
"""Lossless text and the structured metadata owned by an editing session."""

from dataclasses import asdict, dataclass, field, replace
import re
from uuid import uuid4


_LINE_END = re.compile(r"\r\n|\r|\n|\u2029")
_LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])[ \t]+")


def new_id():
    return uuid4().hex


def project(raw):
    """Qt's paragraph breaks are represented by LF in projected positions."""
    return _LINE_END.sub("\n", raw)


def qt_offset(text, position):
    return len(text[:position].encode("utf-16-le")) // 2


def python_offset(text, position):
    if position < 0:
        raise ValueError("Negative Qt position")
    if text.isascii():
        if position > len(text):
            raise ValueError("Qt position past document end")
        return position
    data = text.encode("utf-16-le")
    if position * 2 > len(data):
        raise ValueError("Qt position past document end")
    return len(data[:position * 2].decode("utf-16-le"))


@dataclass(frozen=True)
class Choice:
    text: str
    group_id: str = ""
    option_id: str = ""


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    literal: str
    main: Choice
    prefixes: tuple[Choice, ...] = ()
    suffixes: tuple[Choice, ...] = ()
    id: str = field(default_factory=new_id)

    def direction_at(self, position):
        offset = 0
        spans = []
        for role, choices in (("prefixes", self.prefixes), ("main", (self.main,)), ("suffixes", self.suffixes)):
            for choice in choices:
                if not choice.text:
                    continue
                start = self.literal.find(choice.text, offset)
                if start < 0 or self.literal[offset:start].strip(" +-\t"):
                    return ""
                offset = start + len(choice.text)
                spans.append((start, offset, role))
        if self.literal[offset:].strip():
            return ""
        return next((role for start, stop, role in spans
                     if self.start + start <= position < self.start + stop), "")

    def render(self):
        return "+".join(filter(None, (
            "-".join(value.text for value in self.prefixes),
            self.main.text,
            "-".join(value.text for value in self.suffixes),
        )))

    @classmethod
    def load(cls, data):
        return cls(
            start=data["start"], end=data["end"], literal=data["literal"],
            main=Choice(**data["main"]),
            prefixes=tuple(Choice(**c) for c in data["prefixes"]),
            suffixes=tuple(Choice(**c) for c in data["suffixes"]), id=data["id"],
        )


@dataclass(frozen=True)
class Node:
    start: int
    id: str = field(default_factory=new_id)
    collapsed: bool = False
    observation: str = ""
    statuses: tuple[tuple[str, str], ...] = ()
    slots: tuple[tuple[int, str, tuple[str, ...], tuple[str, ...]], ...] = ()
    show_observations: bool = True


def line_starts(text):
    return (0, *(match.end() for match in re.finditer("\n", text)))


@dataclass(frozen=True)
class Document:
    text: str = ""
    breaks: tuple[str, ...] = ()
    tokens: tuple[Token, ...] = ()
    nodes: tuple[Node, ...] = field(default_factory=lambda: (Node(0),))

    @property
    def raw(self):
        lines = self.text.split("\n")
        return "".join(part + end for part, end in zip(lines, (*self.breaks, "")))

    @classmethod
    def from_raw(cls, raw):
        text = project(raw)
        return cls(text, tuple(_LINE_END.findall(raw)), nodes=tuple(Node(start) for start in line_starts(text)))

    def to_record(self):
        return asdict(self)

    @classmethod
    def load(cls, record):
        text = record["text"]
        nodes = tuple(Node(**{**n, "statuses": tuple(map(tuple, n["statuses"])),
                              "slots": tuple((s[0], s[1], tuple(s[2]), tuple(s[3])) for s in n.get("slots", ()))})
                      for n in record["nodes"])
        tokens = tuple(Token.load(token) for token in record["tokens"])
        document = cls(text, tuple(record["breaks"]), tokens, nodes)
        document.validate()
        return document

    def validate(self):
        text = self.text
        if (len(self.breaks) != text.count("\n")
                or any(end not in ("\n", "\r", "\r\n", "\u2029") for end in self.breaks)
                or project(text) != text):
            raise ValueError("Invalid original line separators")
        if tuple(node.start for node in self.nodes) != line_starts(text):
            raise ValueError("Invalid outline positions")
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("Duplicate outline identity")
        last_end = 0
        ids = set()
        for token in self.tokens:
            if not (last_end <= token.start < token.end <= len(text)
                    and token.literal == text[token.start:token.end]
                    and "\n" not in token.literal and token.main.text and token.id not in ids):
                raise ValueError("Invalid token range, text, or identity")
            last_end = token.end
            ids.add(token.id)

    def _break_slice(self, start, end):
        return self.breaks[self.text.count("\n", 0, start):self.text.count("\n", 0, end)]

    def slice(self, start, end):
        lines = self.text[start:end].split("\n")
        return "".join(part + sep for part, sep in zip(lines, (*self._break_slice(start, end), "")))

    def splice(self, start, end, raw, *, tokens=(), nodes=(), breaks=None):
        old_text = self.text
        if not 0 <= start <= end <= len(old_text):
            raise ValueError("Invalid editing range")
        inserted = project(raw) if breaks is None else raw
        if breaks is None:
            breaks = tuple(_LINE_END.findall(raw))
        updated_breaks = (*self._break_slice(0, start), *breaks, *self._break_slice(end, len(old_text)))
        new_text = old_text[:start] + inserted + old_text[end:]
        shift = len(inserted) - (end - start)
        kept = tuple(
            token if token.end <= start else replace(token, start=token.start + shift, end=token.end + shift)
            for token in self.tokens if token.end <= start or token.start >= end
        )
        token_list = sorted((*kept, *tokens), key=lambda token: token.start)

        starts = line_starts(new_text)
        if not nodes and "\n" not in inserted and "\n" not in old_text[start:end] and not any(node.slots for node in self.nodes):
            if not shift:
                return Document(new_text, updated_breaks, tuple(token_list), self.nodes)
            # A single-line character edit preserves every row identity and its metadata.
            unchanged = old_text.count("\n", 0, start) + 1
            fresh_nodes = (*self.nodes[:unchanged], *(Node(
                node.start + shift, node.id, node.collapsed, node.observation, node.statuses, node.slots,
                node.show_observations
            ) for node in self.nodes[unchanged:]))
            return Document(new_text, updated_breaks, tuple(token_list), fresh_nodes)
        positions = set(starts)
        kept_nodes = {}
        ends_at_boundary = any(n.start == end for n in self.nodes)
        for node in self.nodes:
            if node.start < start or node.start == start and (
                end > start and not ends_at_boundary
                or end == start and not inserted.endswith("\n")
            ):
                if node.start in positions:
                    kept_nodes[node.start] = node
            elif node.start >= end:
                position = node.start + shift
                if position in positions:
                    kept_nodes[position] = replace(node, start=position)
        if any(node.slots for node in self.nodes):
            from bisect import bisect_right
            kept_nodes = {pos: replace(node, slots=()) for pos, node in kept_nodes.items()}
            for node in self.nodes:
                for local, group, pre, post in node.slots:
                    old = node.start + local
                    if start <= old < end or old == start == end and inserted and inserted.strip():
                        continue
                    position = old + shift if old >= end else old
                    if not 0 <= position <= len(new_text):
                        continue
                    line_start = starts[bisect_right(starts, position) - 1]
                    slot_node = kept_nodes.get(line_start) or Node(line_start)
                    kept_nodes[line_start] = replace(slot_node, slots=(*slot_node.slots, (
                        position - line_start, group, pre, post
                    )))
        kept_nodes.update({node.start: node for node in nodes})
        return Document(new_text, updated_breaks, tuple(token_list), tuple(
            kept_nodes.get(position) or Node(position) for position in starts
        ))

    def attach(self, token):
        result = replace(self, tokens=tuple(sorted((*self.tokens, token), key=lambda item: item.start)))
        result.validate()
        return result

    def detach(self, identity):
        return replace(self, tokens=tuple(t for t in self.tokens if t.id != identity))

    def replace_token(self, token):
        literal = token.render()
        if not literal or "\n" in project(literal):
            raise ValueError("A token must contain text on a single line")
        updated = replace(token, end=token.start + len(literal), literal=literal)
        result = self.detach(token.id).splice(token.start, token.end, literal, tokens=(updated,))
        result.validate()
        return result

    def token_at(self, position):
        return next((token for token in self.tokens if token.start <= position <= token.end), None)

    def line_index(self, position):
        return self.text.count("\n", 0, position)

    def line_bounds(self, index):
        start = self.nodes[index].start
        end = self.nodes[index + 1].start if index + 1 < len(self.nodes) else len(self.text)
        return start, end

    def list_item(self, index):
        start, end = self.line_bounds(index)
        return _LIST_ITEM.match(self.text[start:end])

    def branch_bounds(self, index):
        start, end = self.line_bounds(index)
        root = self.list_item(index)
        if not root:
            return start, end
        indent = len(root["indent"].expandtabs(4))
        for child in range(index + 1, len(self.nodes)):
            child_start, child_end = self.line_bounds(child)
            line = self.text[child_start:child_end].rstrip("\n")
            if line.strip() and len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip()) <= indent:
                break
            end = child_end
        return start, end

    def copy_fragment(self, start, end):
        return (
            self.text[start:end], self._break_slice(start, end),
            tuple(replace(t, start=t.start - start, end=t.end - start)
                  for t in self.tokens if start <= t.start and t.end <= end),
            tuple(replace(n, start=n.start - start)
                  for n in self.nodes if start <= n.start < end),
        )

    def insert_fragment(self, position, fragment, *, clone=False, prefix="", suffix=""):
        text, breaks, tokens, nodes = fragment
        prefix_text = project(prefix)
        offset = position + len(prefix_text)
        return self.splice(
            position, position, prefix_text + text + project(suffix),
            breaks=(*_LINE_END.findall(prefix), *breaks, *_LINE_END.findall(suffix)),
            tokens=tuple(
                replace(t, start=t.start + offset, end=t.end + offset, id=new_id() if clone else t.id)
                for t in tokens
            ),
            nodes=tuple(replace(n, start=n.start + offset, id=new_id() if clone else n.id) for n in nodes),
        )
