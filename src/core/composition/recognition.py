
"""Conservative suggestions over original Markdown source positions."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import re

from core.composition.document import Choice, Token
from core.composition.boundaries import UNKNOWN_PART, expression_spans


FENCE = re.compile(r"^[ \t]*(?:>[ \t]*)?(?:[-+*][ \t]+)?([\x60~]{3,})")
INLINE = re.compile(r"\\.|\x60+|!\[\[|!?\[|<|\{\{")
NUMERIC_FORMAT = {"%Y": r"\d{4}", "%y": r"\d{2}", "%m": r"\d{2}", "%d": r"\d{2}",
                  "%H": r"\d{2}", "%M": r"\d{2}", "%S": r"\d{2}"}
SIMPLE_UNKNOWN = re.compile(r"[^+\s,，、；;。:：]{1,36}$")


@dataclass(frozen=True)
class Interpretation:
    main: Choice
    prefixes: tuple[Choice, ...] = ()
    suffixes: tuple[Choice, ...] = ()
    suggested_group: str = ""
    requires_confirmation: bool = False

    @property
    def certain(self):
        return bool(not self.requires_confirmation and self.main.group_id
                    and all(c.group_id for c in self.prefixes + self.suffixes))

    def token(self, start, end, literal):
        return Token(start, end, literal, self.main, self.prefixes, self.suffixes)


@dataclass(frozen=True)
class Candidate:
    start: int
    end: int
    literal: str
    interpretations: tuple[Interpretation, ...]

    @property
    def certain(self):
        return len(self.interpretations) == 1 and self.interpretations[0].certain


def _balanced(text, start, opener, closer):
    depth = 0
    pos = start
    while pos < len(text):
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if not depth:
                return pos + 1
        pos += 1
    return None


def protected_inline(text):
    pos = 0
    while match := INLINE.search(text, pos):
        start, end = match.span()
        marker = match.group()
        if marker.startswith("\\"):
            pass
        elif marker.startswith(chr(96)):
            for closing in re.finditer(r"\x60+", text[end:]):
                if closing.group() == marker:
                    end += closing.end()
                    break
            else:
                pos = end
                continue
        elif marker == "<":
            if end >= len(text) or not (text[end] in "/!?" or text[end].isascii() and text[end].isalpha()):
                pos = end
                continue
            found = text.find(">", end)
            end = len(text) if found < 0 else found + 1
        elif marker == "{{":
            found = text.find("}}", end)
            end = len(text) if found < 0 else found + 2
        else:
            bracket = text.find("[", start, end)
            close = _balanced(text, bracket, "[", "]")
            if close is None:
                end = len(text)
            elif close < len(text) and text[close] in "([":
                opener = text[close]
                end = _balanced(text, close, opener, ")" if opener == "(" else "]") or len(text)
            else:
                end = close
        yield start, end
        pos = end


def position_parts(text):
    protected = iter(protected_inline(text))
    hidden = next(protected, None)
    position = 0
    closing_fence = None
    for line in text.split("\n"):
        fence = FENCE.match(line)
        end = position + len(line)
        if closing_fence:
            if fence and set(fence[1]) == {closing_fence[0]} and len(fence[1]) >= closing_fence[1]:
                closing_fence = None
        elif fence and len(set(fence[1])) == 1:
            closing_fence = (fence[1][0], len(fence[1]))
        elif not re.match(r"^\s*\[.+\]:\s*", line) and not (
                line.startswith(("\t", "    ")) and not re.match(r"^[ \t]*(?:[-+*]|\d+[.)]) +", line)):
            visible = position
            while hidden and hidden[0] < end:
                if visible < hidden[0]:
                    yield visible, text[visible:hidden[0]]
                visible = max(visible, hidden[1])
                if hidden[1] > end:
                    break
                hidden = next(protected, None)
            if visible < end:
                yield visible, text[visible:end]
        position = end + 1
        while hidden and hidden[1] <= position:
            hidden = next(protected, None)


class Recognizer:
    def __init__(self, catalog):
        self.groups = catalog.groups
        self.group_choices = lru_cache(maxsize=512)(self._group_choices)
        self.by_id = {g.id: g for g in catalog.groups}
        self.options = {}
        self.timestamp_shapes = {}
        self.weekdays = {}
        for group in catalog.groups:
            indexed = {}
            for option in group.values():
                if option.pattern:
                    chunks = re.split(r"(%.)", option.pattern)
                    directives = chunks[1::2]
                    if directives and all(part in NUMERIC_FORMAT for part in directives):
                        self.timestamp_shapes[option.id] = re.compile(
                            "".join(NUMERIC_FORMAT.get(part, re.escape(part)) for part in chunks)
                        )
                    elif option.pattern in ("%a", "%A"):
                        self.weekdays[option.id] = {
                            option.value(datetime(2031, 4, 7) + timedelta(days=day))
                            for day in range(7)
                        }
                    continue
                indexed.setdefault(option.text, []).append(group.choice(option))
            self.options[group.id] = indexed

    def _group_choices(self, group_id, text):
        group = self.by_id[group_id]
        found = list(self.options[group_id].get(text, ()))
        if group.kind == "timestamp":
            for option in group.values():
                shape = self.timestamp_shapes.get(option.id)
                if shape and not shape.fullmatch(text):
                    continue
                if option.id in self.weekdays:
                    if text in self.weekdays[option.id]:
                        found.append(Choice(text, group.id, option.id))
                    continue
                try:
                    parsed = datetime.strptime(text, option.pattern)
                    if parsed.strftime(option.pattern) == text:
                        found.append(Choice(text, group.id, option.id))
                except (ValueError, TypeError):
                    continue
        elif group.kind == "number" and not found and (not group.numeric.unit or text.endswith(group.numeric.unit)):
            numeric = text[:-len(group.numeric.unit)] if group.numeric.unit else text
            try:
                if group.numeric.format_value(numeric) == text:
                    found.append(Choice(text, group.id))
            except ValueError:
                pass
        return tuple(found)

    def modifiers(self, text, sequence):
        if not text:
            return [()]
        seen = set()
        results = []

        def walk(remaining, selected, used):
            if len(results) >= 12:
                return
            if not remaining:
                if selected not in seen:
                    seen.add(selected)
                    results.append(selected)
                return
            for end in (*[m.start() for m in re.finditer("-", remaining)], len(remaining)):
                fragment = remaining[:end]
                if not fragment:
                    continue
                choices = tuple(value for group in sequence if group not in used
                                for value in self.group_choices(group, fragment))
                if not choices and "-" not in fragment and UNKNOWN_PART.fullmatch(fragment):
                    choices = (Choice(fragment),)
                for choice in choices:
                    walk(remaining[end+1:], (*selected, choice), used | {choice.group_id})

        walk(text, (), set())
        return results

    def interpret(self, text, *, contextual=False):
        if len(text) > 100 or not text:
            return ()
        cut = [match.start() for match in re.finditer(r"\+", text)]
        if len(cut) > 6:
            return ()
        known = []
        tentative = []
        for group in self.groups:
            for start in (0, *(p + 1 for p in cut)):
                for end in (*cut, len(text)):
                    if start >= end:
                        continue
                    main_text = text[start:end]
                    candidates = self.group_choices(group.id, main_text)
                    if not candidates and not (start and end < len(text) and SIMPLE_UNKNOWN.fullmatch(main_text)):
                        continue
                    if start == 0 and end == len(text) and not contextual:
                        continue
                    parts = self.modifiers(text[:start-1], group.prefixes) if start else [()]
                    others = self.modifiers(text[end+1:], group.suffixes) if end < len(text) else [()]
                    for pre in parts:
                        for post in others:
                            recognized_sides = sum(bool(c.group_id) for c in pre + post)
                            if len(known) >= 32:
                                return tuple(dict.fromkeys(known))
                            for main in candidates:
                                if contextual or recognized_sides:
                                    known.append(Interpretation(main, pre, post))
                            if not candidates and pre and post and all(c.group_id for c in pre + post):
                                tentative.append(Interpretation(Choice(main_text), pre, post, group.id))
        if known and not (cut and all(
                self.by_id[item.main.group_id].kind == "timestamp" and not item.certain for item in known)):
            best = max(sum(bool(c.group_id) for c in x.prefixes + x.suffixes) for x in known)
            return tuple(dict.fromkeys(x for x in known if sum(bool(c.group_id) for c in x.prefixes + x.suffixes) == best))
        if tentative:
            return tuple(dict.fromkeys(tentative[:16]))
        pieces = [part.strip() for part in text.split("+")]
        if not 2 <= len(pieces) <= 3:
            return ()
        recognized = {part: tuple(choice for group in self.groups
                                  for choice in self.group_choices(group.id, part)) for part in pieces}
        fields = [[part] if recognized[part] else [value.strip() for value in part.split("-")] for part in pieces]
        if not all(recognized.get(value) or UNKNOWN_PART.fullmatch(value)
                   for side in fields for value in side):
            return ()
        # Pure arithmetic or incomplete free text is not a combination.
        if not any(any(char.isalpha() for char in part) for part in pieces):
            return ()
        if (not contextual and len(pieces) != 3 and not any(recognized.values())
                and not all(re.search(r"[\u3400-\u9fff]", part) for part in pieces)):
            return ()

        def fallback_choice(value):
            matches = recognized.get(value, ())
            if len({choice.group_id for choice in matches}) == 1:
                return matches[0]
            return Choice(value)

        choices = []
        for index in range(len(fields)):
            if len(fields[index]) != 1 or (len(fields) == 3 and index != 1):
                continue
            prefix = tuple(fallback_choice(value) for side in fields[:index] for value in side)
            suffix = tuple(fallback_choice(value) for side in fields[index+1:] for value in side)
            for main in recognized[pieces[index]] or (Choice(pieces[index]),):
                choices.append(Interpretation(main, prefix, suffix, requires_confirmation=True))
        return tuple(choices)

    def scan(self, document, bounds=None):
        results = []
        start, end = bounds or (0, len(document.text))
        existing = iter(document.tokens)
        token = next(existing, None)
        for line_offset, part in position_parts(document.text):
            if line_offset + len(part) <= start or line_offset >= end:
                continue
            for offset, literal, contextual in expression_spans(part):
                position = line_offset + offset
                limit = position + len(literal)
                if position < start or limit > end:
                    continue
                while token and token.end <= position:
                    token = next(existing, None)
                if token and token.start < limit:
                    continue
                choices = self.interpret(literal, contextual=contextual)
                if choices:
                    results.append(Candidate(position, limit, literal, choices))
        return tuple(results)
