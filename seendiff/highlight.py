"""Pygments token-stream highlighting with two-pass old/new alignment (PLAN.md §11).

Per display row we emit a list of (short_css_class, text) pairs. Splitting token
values at newlines *before* emission makes per-line output correct by
construction — no spans ever cross a line boundary.
"""
from __future__ import annotations

from pygments.lexers import get_lexer_for_filename
from pygments.token import STANDARD_TYPES
from pygments.util import ClassNotFound

MAX_LINES = 10_000
MAX_BYTES = 1_000_000
MAX_MEAN_LINE_LEN = 500

# Pinned Pygments 2.20 ships dedicated JsxLexer/TsxLexer (verified
# empirically), so the fallback map is a safety net for other exotic cases.
_EXT_FALLBACK = {".jsx": "x.js", ".tsx": "x.ts"}


def _short_class(tok_type) -> str:
    while tok_type not in STANDARD_TYPES:
        tok_type = tok_type.parent
    return STANDARD_TYPES[tok_type]


def resolve_lexer(filename: str):
    try:
        return get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
    except ClassNotFound:
        for ext, alias in _EXT_FALLBACK.items():
            if filename.endswith(ext):
                return get_lexer_for_filename(alias, stripnl=False, ensurenl=False)
        raise


def lang_for(filename: str) -> str | None:
    try:
        lexer = resolve_lexer(filename)
    except ClassNotFound:
        return None
    return lexer.aliases[0] if lexer.aliases else lexer.name


def tokenize_lines(text: str, lexer, n_lines: int) -> list[list[tuple[str, str]]]:
    """Per line, a list of (css_class, text) pairs; padded/clamped to n_lines."""
    lines: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    for tok_type, value in lexer.get_tokens(text):
        cls = _short_class(tok_type)
        for i, piece in enumerate(value.split("\n")):
            if i:
                lines.append(cur)
                cur = []
            if piece:
                cur.append((cls, piece))
    if cur:
        lines.append(cur)
    while len(lines) < n_lines:
        lines.append([])
    return lines[:n_lines]


def guard(rows) -> str | None:
    """Return a reason to skip highlighting, or None."""
    if len(rows) > MAX_LINES:
        return f"file too large ({len(rows)} lines)"
    total = sum(len(r.text) for r in rows)
    if total > MAX_BYTES:
        return f"file too large ({total // 1024} kB)"
    if rows and total / len(rows) > MAX_MEAN_LINE_LEN:
        return "minified content (very long lines)"
    return None


def highlight_rows(rows, filename: str) -> list[list[tuple[str, str]]] | None:
    """Aligned token lists, one per display row, or None (render plain).

    Two passes: the new side (context + adds) and the old side (context +
    dels), lexed independently so a deleted line inside a since-removed
    multi-line construct still lexes in its own coherent context.
    """
    if guard(rows) is not None:
        return None
    try:
        lexer = resolve_lexer(filename)
    except ClassNotFound:
        return None
    try:
        new_rows = [r for r in rows if r.type != "del"]
        old_rows = [r for r in rows if r.type != "add"]
        new_toks = tokenize_lines("\n".join(r.text for r in new_rows), lexer,
                                  len(new_rows))
        old_toks = tokenize_lines("\n".join(r.text for r in old_rows), lexer,
                                  len(old_rows))
        out: list[list[tuple[str, str]]] = []
        ni = oi = 0
        for r in rows:
            if r.type == "del":
                out.append(old_toks[oi]); oi += 1
            elif r.type == "add":
                out.append(new_toks[ni]); ni += 1
            else:
                out.append(new_toks[ni]); ni += 1; oi += 1
        return out
    except Exception:
        # a lexer failure must never take down a file view (PLAN.md §11)
        return None
