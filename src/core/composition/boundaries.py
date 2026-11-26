"""Shared lexical boundaries for source recognition and combination spans."""

import re

# Lindy natural-key boundaries. QuickKV keeps whitespace inside candidate
# values so registered phrases and timestamps can be recognized as a unit.
BOUNDARY_CHARS = " \t,，。；;！？!?()（）[]【】{}<>《》"
VALUE_SEPARATORS = "、,，"
CLAUSE_BOUNDARY = re.compile(r"[" + re.escape(BOUNDARY_CHARS.strip(" \t") + "、") + "]")
OUTLINE_MARKER = re.compile(r"^[ \t]*(?:(?:[-+*]|\d+[.)]|#{1,6}|>)[ \t]+)")
KEY = re.compile(r"^[ \t]*([^:：\n]{1,36})[:：][ \t]*")
NEXT_KEY = re.compile(r"[ \t]+([^\W\d_][\w-]{0,35})[:：][ \t]*")
UNKNOWN_PART = re.compile(r"[^\W\d_]{2,36}(?:[ \t]+[^\W\d_]{2,36})*$")


def expression_spans(part):
    """Keep key and value candidates at exact source offsets."""
    marker = OUTLINE_MARKER.match(part)
    start = marker.end() if marker else 0
    contextual = bool(marker)
    for end in (*[m.start() for m in CLAUSE_BOUNDARY.finditer(part, start)], len(part)):
        chunk = part[start:end]
        key = KEY.match(chunk)
        if key and not key[1].strip().isdigit() and not re.search(r"\d[ \t]*$", key[1]):
            raw_key = key[1]
            if "+" in raw_key:
                offset = start + key.start(1) + len(raw_key) - len(raw_key.lstrip())
                yield offset, raw_key.strip(), True
            start += key.end()
            contextual = True
        for following in NEXT_KEY.finditer(part, start, end):
            raw = part[start:following.start()]
            if not raw.strip() or "+" not in raw:
                continue
            yield start + len(raw) - len(raw.lstrip()), raw.strip(), contextual
            start = following.end()
            contextual = True
        raw = part[start:end]
        if value := raw.strip():
            yield start + len(raw) - len(raw.lstrip()), value, contextual
        start = end + 1
