#!/usr/bin/env python3
"""Backfill FTB Quests SNBT files with localized text.

This helper scans every SNBT file inside ``overrides/config/ftbquests/quests``
for string literals that only contain a translation key (e.g.
"{chapter.18.quest.1.description.1}"). When a matching key is found in any of
the ``en_us.json`` language files under ``overrides/kubejs/assets``, the
placeholder is replaced with the actual localized text, escaping characters so
the SNBT syntax remains valid.

Run the script directly in VS Code or via python:

    python tool/backfill.py

Configuration is hardcoded in the main function.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple, Optional
import re

LANG_ROOT = Path("overrides/kubejs/assets")
QUESTS_ROOT = Path("overrides/config/ftbquests/quests")
PLACEHOLDER_RE = re.compile(r"(?P<quote>\"|')\{(?P<key>[^\s{}\"'\\]+)\}(?P=quote)")
IGNORED_MISSING_KEYS = {"@pagebreak"}
TARGET_ARRAY_KEYS = ("description", "hover")
ARRAY_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<key>" + "|".join(TARGET_ARRAY_KEYS) + r")\s*:\s*\["
)


@dataclass
class ReplacementReport:
    path: Path
    replacements: int
    keys: Sequence[str]
    newline_splits: int
    arrays_reformatted: int


def load_translations(lang_root: Path) -> tuple[Dict[str, str], Dict[str, List[str]]]:
    """Gather every string translation defined in en_us.json files."""

    translations: Dict[str, str] = {}
    duplicates: Dict[str, List[str]] = defaultdict(list)

    json_paths = sorted(lang_root.rglob("lang/en_us.json"))
    if not json_paths:
        raise FileNotFoundError(f"No en_us.json files found under {lang_root}")

    for json_path in json_paths:
        try:
            raw_text = json_path.read_text(encoding="utf-8")
            # Some lang files contain literal control characters (e.g. tabs)
            # inside strings, which vanilla JSON rejects. strict=False lets us
            # accept them without having to mutate the source files.
            data = json.loads(raw_text, strict=False)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to parse {json_path}: {exc}") from exc

        if not isinstance(data, dict):
            continue

        for key, value in data.items():
            if not isinstance(value, str):
                continue

            if key in translations:
                duplicates[key].append(str(json_path))
            translations[key] = value

    return translations, duplicates


def encode_snbt_string(value: str, quote: str = '"') -> str:
    """Escape characters so the output stays ASCII-friendly SNBT."""

    def escape_char(ch: str) -> str:
        if ch == quote:
            return f"\\{quote}"
        if ch == "\\":
            return "\\\\"
        if ch == "\n":
            return "\\n"
        if ch == "\r":
            return "\\r"
        if ch == "\t":
            return "\\t"
        if ch == "\b":
            return "\\b"
        if ch == "\f":
            return "\\f"
        if ord(ch) < 32:
            return f"\\u{ord(ch):04x}"
        return ch

    escaped = ''.join(escape_char(ch) for ch in value)
    return f"{quote}{escaped}{quote}"


def replace_placeholders(text: str, translations: Dict[str, str]) -> tuple[str, List[str], List[str]]:
    """Replace translation placeholders inside a single SNBT blob."""

    replaced_keys: List[str] = []
    missing_keys: List[str] = []

    def _sub(match: re.Match[str]) -> str:
        key = match.group("key")
        quote = match.group("quote")
        value = translations.get(key)
        if value is None:
            if key not in IGNORED_MISSING_KEYS:
                missing_keys.append(key)
            return match.group(0)

        replaced_keys.append(key)
        return encode_snbt_string(value, quote)

    updated_text = PLACEHOLDER_RE.sub(_sub, text)
    return updated_text, replaced_keys, missing_keys


def split_target_arrays(text: str) -> tuple[str, int, int]:
    """Normalize and split multi-line string arrays for target keys."""

    parts: List[str] = []
    total_splits = 0
    arrays_changed = 0
    last_idx = 0

    for match in ARRAY_PATTERN.finditer(text):
        start = match.start()
        if start < last_idx:
            continue

        indent_line = match.group("indent")
        bracket_start = match.end() - 1
        body, closing_idx = _extract_array_body(text, bracket_start)
        transformed_body, splits, changed = _transform_array_body(body, indent_line)

        if not changed:
            continue

        parts.append(text[last_idx:bracket_start + 1])
        parts.append(transformed_body)
        parts.append(']')
        last_idx = closing_idx + 1
        total_splits += splits
        arrays_changed += 1

    if not parts:
        return text, 0, 0

    parts.append(text[last_idx:])
    return ''.join(parts), total_splits, arrays_changed


def _extract_array_body(text: str, bracket_start: int) -> tuple[str, int]:
    depth = 0
    pos = bracket_start
    in_string = False
    escape = False

    while pos < len(text):
        ch = text[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    return text[bracket_start + 1:pos], pos
        pos += 1

    raise ValueError("Unterminated array while processing SNBT text")


def _transform_array_body(body: str, indent_line: str) -> tuple[str, int, bool]:
    tokens = _parse_string_tokens(body)
    if not tokens:
        return body, 0, False

    final_values: List[str] = []
    splits = 0
    requires_change = len(tokens) > 1

    for token in tokens:
        value = _decode_snbt_string(token)
        segments = _split_value_segments(value)
        splits += max(0, len(segments) - 1)
        if len(segments) > 1:
            requires_change = True
        final_values.extend(segments)

    if not requires_change and splits == 0:
        return body, 0, False

    inner_indent = indent_line + "\t"
    lines: List[str] = ["\n"]
    for segment in final_values:
        lines.append(inner_indent)
        lines.append(encode_snbt_string(segment))
        lines.append("\n")
    lines.append(indent_line)

    return ''.join(lines), splits, True


def _parse_string_tokens(body: str) -> Optional[List[str]]:
    tokens: List[str] = []
    idx = 0
    length = len(body)

    while idx < length:
        ch = body[idx]
        if ch in " \t\r\n,":
            idx += 1
            continue
        if ch != '"':
            return None

        start = idx
        idx += 1
        escape = False
        while idx < length:
            current = body[idx]
            if escape:
                escape = False
            elif current == '\\':
                escape = True
            elif current == '"':
                break
            idx += 1

        if idx >= length:
            return None

        tokens.append(body[start:idx + 1])
        idx += 1

    return tokens


def _decode_snbt_string(token: str) -> str:
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token

    # Manual decoder to handle SNBT quirks (like variable length unicode escapes)
    inner = token[1:-1]
    result: List[str] = []
    idx = 0
    length = len(inner)

    while idx < length:
        ch = inner[idx]
        if ch != '\\':
            result.append(ch)
            idx += 1
            continue

        idx += 1
        if idx >= length:
            break
        esc = inner[idx]
        if esc == 'n':
            result.append('\n')
        elif esc == 'r':
            result.append('\r')
        elif esc == 't':
            result.append('\t')
        elif esc == 'b':
            result.append('\b')
        elif esc == 'f':
            result.append('\f')
        elif esc == '\\':
            result.append('\\')
        elif esc == '"':
            result.append('"')
        elif esc == '/':
            result.append('/')
        elif esc == 'u':
            if idx + 1 < length:
                hex_start = idx + 1
                hex_end = hex_start
                while hex_end < length and hex_end - hex_start < 6 and inner[hex_end] in '0123456789abcdefABCDEF':
                    hex_end += 1
                hex_value = inner[hex_start:hex_end]
                if len(hex_value) >= 4:
                    try:
                        result.append(chr(int(hex_value, 16)))
                        idx = hex_end - 1
                    except ValueError:
                        result.append('u')
                else:
                    result.append('u')
            else:
                result.append('u')
        else:
            result.append(esc)
        idx += 1

    decoded_str = ''.join(result)
    # Fix surrogate pairs if any were decoded individually
    try:
        return decoded_str.encode('utf-16', 'surrogatepass').decode('utf-16')
    except Exception:
        return decoded_str


def _split_value_segments(value: str) -> List[str]:
    if "\n" not in value:
        return [value]

    parts = value.split('\n')
    segments: List[str] = []
    idx = 0
    length = len(parts)

    while idx < length:
        part = parts[idx]
        if part != '':
            segments.append(part)
            idx += 1
            continue

        run_start = idx
        while idx < length and parts[idx] == '':
            idx += 1
        blank_run = idx - run_start
        blanks_to_add = (blank_run + 1) // 2
        segments.extend([''] * blanks_to_add)

    if not segments:
        return ['']

    return segments


def process_files(
    quests_root: Path,
    translations: Dict[str, str],
    dry_run: bool,
) -> tuple[List[ReplacementReport], Dict[str, List[Path]]]:
    """Apply replacements to every SNBT file inside ``quests_root``."""

    reports: List[ReplacementReport] = []
    missing_locations: Dict[str, List[Path]] = defaultdict(list)

    for snbt_path in sorted(quests_root.rglob("*.snbt")):
        original = snbt_path.read_text(encoding="utf-8")
        updated, keys, missing = replace_placeholders(original, translations)
        updated, newline_splits, arrays_changed = split_target_arrays(updated)

        if keys or newline_splits or arrays_changed:
            reports.append(
                ReplacementReport(
                    snbt_path,
                    len(keys),
                    keys,
                    newline_splits,
                    arrays_changed,
                )
            )
            if not dry_run and updated != original:
                snbt_path.write_text(updated, encoding="utf-8")

        for key in missing:
            missing_locations[key].append(snbt_path)

    return reports, missing_locations


def summarize(
    reports: Sequence[ReplacementReport],
    missing: Dict[str, List[Path]],
    duplicates: Dict[str, List[str]],
    dry_run: bool,
) -> None:
    total_replacements = sum(r.replacements for r in reports)
    total_newline_splits = sum(r.newline_splits for r in reports)
    total_arrays = sum(r.arrays_reformatted for r in reports)
    changed_files = len(reports)
    mode = "DRY-RUN" if dry_run else "APPLY"

    print(
        f"[{mode}] {changed_files} files would be updated with"
        f" {total_replacements} placeholder replacements,"
        f" {total_newline_splits} newline splits, and"
        f" {total_arrays} array reformat(s)."
    )

    if duplicates:
        print(f"Warning: {len(duplicates)} duplicate translation keys detected (last definition wins):")
        for key, paths in sorted(duplicates.items()):
            print(f"  {key}: {', '.join(paths)}")

    if missing:
        print("Missing translations:")
        for key in sorted(missing):
            locations = ', '.join(str(p) for p in sorted(set(missing[key])))
            print(f"  {key}: {locations}")


def main() -> None:
    # Hardcoded configuration
    lang_root = LANG_ROOT
    quests_root = QUESTS_ROOT
    dry_run = False  # Set to True to preview changes, False to apply them

    print(f"Starting backfill process...")
    print(f"Language root: {lang_root}")
    print(f"Quests root: {quests_root}")
    print(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

    translations, duplicates = load_translations(lang_root)
    print(f"Loaded {len(translations)} translation entries from {lang_root}.")

    reports, missing_locations = process_files(quests_root, translations, dry_run=dry_run)
    summarize(reports, missing_locations, duplicates, dry_run=dry_run)


if __name__ == "__main__":
    main()
