"""Literal, single-pass output transcription rules."""
import json
import re
from functools import lru_cache


class TranscriptionError(ValueError):
    pass


@lru_cache(maxsize=16)
def parse_rules(source):
    rules = {}
    decoder = json.JSONDecoder()
    for number, line in enumerate(source.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            original, end = decoder.raw_decode(line)
            rest = line[end:].lstrip()
            if not rest.startswith('=>'):
                raise ValueError('缺少 =>')
            replacement = json.loads(rest[2:].strip())
            if not isinstance(original, str) or not isinstance(replacement, str):
                raise ValueError('两侧必须是双引号字符串')
            if not original:
                raise ValueError('识别内容不能为空')
            if original in rules:
                raise ValueError('识别内容重复')
        except ValueError as error:
            raise TranscriptionError(f'第 {number} 行：{error}') from error
        rules[original] = replacement
    return tuple(rules.items())


@lru_cache(maxsize=16)
def _compile_rules(source):
    rules = dict(parse_rules(source))
    pattern = re.compile('|'.join(re.escape(key) for key in sorted(rules, key=len, reverse=True))) if rules else None
    return pattern, rules


def transcribe(text, source):
    pattern, rules = _compile_rules(source)
    return pattern.sub(lambda match: rules[match.group()], text) if pattern else text
