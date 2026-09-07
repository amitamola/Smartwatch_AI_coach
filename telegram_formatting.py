"""Small, dependency-free Markdown renderer for Telegram's HTML parse mode.

This is intentionally not a CommonMark implementation. Underscores are literal;
raw HTML is text. Pipe tables become labeled lists rather than a monospace grid.
"""

import html
import re
import string
from bisect import bisect_right
from html.parser import HTMLParser


__all__ = ["md_to_html", "html_to_plain", "html_chunks"]

_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*\n(.*?)```", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_SEPARATOR = re.compile(r":?-{3,}:?")
_CODE_HTML = re.compile(r"<code>.*?</code>", re.DOTALL)
_TAGS = frozenset(("b", "i", "u", "s", "code", "pre", "a"))


def _escape(text):
    return html.escape(text, quote=False)


def _code_span(text, start):
    """Return (end, contents) for matching backtick runs, or None."""
    width = 1
    while start + width < len(text) and text[start + width] == "`":
        width += 1
    cursor = start + width
    while cursor < len(text):
        end = text.find("`", cursor)
        if end < 0 or "\n" in text[cursor:end]:
            return None
        end_width = 1
        while end + end_width < len(text) and text[end + end_width] == "`":
            end_width += 1
        if end_width == width:
            return end + width, text[start + width:end]
        cursor = end + end_width
    return None


def _link(text, start):
    """Read an HTTP(S) link, including balanced parentheses in its URL."""
    depth = 1
    cursor = start + 1
    while cursor < len(text) and depth:
        char = text[cursor]
        if char == "\n":
            return None
        if char == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if char == "`":
            code = _code_span(text, cursor)
            if code:
                cursor = code[0]
                continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        cursor += 1
    if depth or cursor >= len(text) or text[cursor] != "(":
        return None
    label = text[start + 1:cursor - 1]
    url_start = cursor + 1
    if not text[url_start:].lower().startswith(("https://", "http://")):
        return None
    cursor = url_start
    depth = 1
    while cursor < len(text):
        char = text[cursor]
        if char.isspace():
            return None
        if char == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                url = text[url_start:cursor]
                url = re.sub(r"\\([\\()])", r"\1", url)
                return cursor + 1, label, url
        cursor += 1
    return None


def _closing_delimiter(text, start, delimiter):
    cursor = start + len(delimiter)
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if char == "`":
            code = _code_span(text, cursor)
            if code:
                cursor = code[0]
                continue
        if char == "[":
            link = _link(text, cursor)
            if link:
                cursor = link[0]
                continue
        if delimiter == "*" and char == "\n":
            return None
        if char == delimiter[0]:
            width = 1
            while cursor + width < len(text) and text[cursor + width] == char:
                width += 1
            if width == len(delimiter):
                if delimiter != "*" or (
                    not text[cursor - 1].isspace()
                    and (cursor + 1 == len(text)
                         or not (text[cursor + 1].isalnum()
                                 or text[cursor + 1] in "_*"))
                ):
                    return cursor
            cursor += width
            continue
        cursor += 1
    return None


def _wrap(tag, body, attributes=""):
    # Telegram code entities cannot overlap other formatting entities.
    opening, closing = "<" + tag + attributes + ">", "</" + tag + ">"
    parts = []
    cursor = 0
    for match in _CODE_HTML.finditer(body):
        if match.start() > cursor:
            parts.append(opening + body[cursor:match.start()] + closing)
        parts.append(match.group())
        cursor = match.end()
    if cursor < len(body):
        parts.append(opening + body[cursor:] + closing)
    return "".join(parts)


def _inline(text, allow_links=True, depth=0):
    # Unusually deep/unbalanced model output remains visible rather than
    # exhausting Python's recursion stack.
    if depth >= 32:
        return _escape(text)
    out = []
    cursor = 0
    while cursor < len(text):
        char = text[cursor]
        if (char == "\\" and cursor + 1 < len(text)
                and text[cursor + 1] in string.punctuation):
            out.append(_escape(text[cursor + 1]))
            cursor += 2
            continue
        if char == "`":
            code = _code_span(text, cursor)
            if code:
                out.append("<code>" + _escape(code[1]) + "</code>")
                cursor = code[0]
                continue
        if char == "[" and allow_links:
            link = _link(text, cursor)
            if link:
                # Escape the raw destination exactly once, not already-escaped
                # prose: otherwise query-string '&' becomes '&amp;amp;'.
                label = _inline(link[1], allow_links=False, depth=depth + 1)
                # A linked code label must be a link, not overlapping entities.
                label = _CODE_HTML.sub(
                    lambda match: match.group()[6:-7], label)
                out.append(_wrap(
                    "a", label, ' href="' + html.escape(link[2], quote=True) + '"'))
                cursor = link[0]
                continue
        delimiter = None
        if text.startswith("***", cursor):
            delimiter = "***"
        elif text.startswith("**", cursor):
            delimiter = "**"
        elif text.startswith("~~", cursor):
            delimiter = "~~"
        elif (char == "*" and cursor + 1 < len(text)
              and not text[cursor + 1].isspace()
              and (cursor == 0 or not (
                  text[cursor - 1].isalnum() or text[cursor - 1] in "_*"))):
            delimiter = "*"
        if delimiter:
            end = _closing_delimiter(text, cursor, delimiter)
            if end is not None and end > cursor + len(delimiter):
                body = _inline(
                    text[cursor + len(delimiter):end], allow_links, depth + 1)
                if delimiter == "***":
                    out.append(_wrap("b", _wrap("i", body)))
                else:
                    tag = {"**": "b", "*": "i", "~~": "s"}[delimiter]
                    out.append(_wrap(tag, body))
                cursor = end + len(delimiter)
                continue
        out.append(_escape(char))
        cursor += 1
    return "".join(out)


def _pipe_row(line):
    """Split only unescaped pipes outside complete inline-code spans."""
    cells = []
    current = []
    cursor = 0
    while cursor < len(line):
        char = line[cursor]
        if char == "\\" and cursor + 1 < len(line):
            current.append(line[cursor:cursor + 2])
            cursor += 2
            continue
        if char == "`":
            code = _code_span(line, cursor)
            if code:
                current.append(line[cursor:code[0]])
                cursor = code[0]
                continue
        if char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        cursor += 1
    if not cells:
        return None
    cells.append("".join(current).strip())
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells or None


def _table_header(lines, index):
    if index + 1 >= len(lines):
        return None
    header = _pipe_row(lines[index])
    separator = _pipe_row(lines[index + 1])
    if (header and separator and len(header) == len(separator)
            and all(_SEPARATOR.fullmatch(cell) for cell in separator)):
        return header
    return None


def _table_cards(header, rows):
    width = max(len(header), *(len(row) for row in rows))
    labels = []
    for column in range(width):
        label = header[column] if column < len(header) else ""
        label = label or "Column " + str(column + 1)
        labels.append(_wrap("b", _inline(label) + ":"))
    cards = []
    for row in rows:
        cards.append("\n".join(
            "\u2022 " + labels[column] + " "
            + (_inline(row[column]) if column < len(row) else "")
            for column in range(width)
        ))
    return "\n\n".join(cards)


def _prose(text):
    lines = text.split("\n")
    out = []
    cursor = 0
    prose = []

    def flush_prose():
        if prose:
            out.append(_inline("\n".join(prose)))
            prose.clear()

    while cursor < len(lines):
        header = _table_header(lines, cursor)
        if header:
            rows = []
            end = cursor + 2
            while end < len(lines):
                row = _pipe_row(lines[end])
                if not row or _table_header(lines, end):
                    break
                rows.append(row)
                end += 1
            if rows:
                flush_prose()
                out.append(_table_cards(header, rows))
                cursor = end
                continue
        line = lines[cursor]
        if _RULE.fullmatch(line):
            cursor += 1
            continue
        heading = _HEADING.fullmatch(line)
        bullet = _BULLET.fullmatch(line)
        if heading:
            flush_prose()
            out.append(_wrap("b", _inline(heading[1])))
        elif bullet:
            prose.append(("  " if bullet[1] else "") + "\u2022 " + bullet[2])
        else:
            prose.append(line)
        cursor += 1
    flush_prose()
    return "\n".join(out)


def md_to_html(text):
    """Render Markdown-ish text as escaped Telegram HTML.

    Supports headings, bullets, asterisk emphasis, strike, code, fenced code,
    HTTP(S) links and header/separator/body pipe tables. Ragged table rows retain
    extra cells under numbered labels; missing cells remain visibly empty.
    A single fence around the entire message is unwrapped, as in the bridge.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped = text.strip()
    if (stripped.startswith("```") and stripped.endswith("```")
            and stripped.count("```") == 2):
        stripped = re.sub(r"^```[ \t]*[A-Za-z0-9_+-]*\n?", "", stripped)
        text = stripped[:-3].strip("\n")

    parts = []
    cursor = 0
    for match in _FENCE.finditer(text):
        parts.append(_prose(text[cursor:match.start()]))
        parts.append("<pre>" + _escape(match[1].rstrip("\n")) + "</pre>")
        cursor = match.end()
    parts.append(_prose(text[cursor:]))
    return "".join(parts)


class _PlainParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def html_to_plain(html_text):
    """Strip generated tags and decode entities once, preserving visible text."""
    parser = _PlainParser()
    parser.feed(html_text)
    parser.close()
    return "".join(parser.parts)


class _ChunkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.tokens = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in _TAGS:
            raise ValueError("Unsupported Telegram HTML tag: " + tag)
        if tag == "a":
            if (len(attrs) != 1 or attrs[0][0] != "href" or not attrs[0][1]
                    or not attrs[0][1].lower().startswith(("http://", "https://"))):
                raise ValueError("Links must have exactly one HTTP(S) href")
        elif attrs:
            raise ValueError("Unexpected attributes on Telegram HTML tag: " + tag)
        opening = self.get_starttag_text()
        self.stack.append(tag)
        self.tokens.append(("open", tag, opening))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ValueError("Unbalanced Telegram HTML closing tag: " + tag)
        self.stack.pop()
        self.tokens.append(("close", tag, "</" + tag + ">"))

    def handle_startendtag(self, tag, attrs):
        raise ValueError("Self-closing tags are not supported in Telegram HTML")

    def handle_data(self, data):
        if any(char in data for char in "<>&"):
            raise ValueError("Unescaped HTML metacharacter in Telegram text")
        self.tokens.append(("text", data, data))

    def handle_entityref(self, name):
        if name not in ("amp", "lt", "gt", "quot"):
            raise ValueError("Unsupported Telegram HTML entity: &" + name + ";")
        encoded = "&" + name + ";"
        self.tokens.append(("entity", html.unescape(encoded), encoded))

    def handle_charref(self, name):
        codepoint = int(name[1:], 16) if name.lower().startswith("x") else int(name)
        if not 0 < codepoint <= 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Invalid Unicode character reference in Telegram HTML")
        encoded = "&#" + name + ";"
        self.tokens.append(("entity", html.unescape(encoded), encoded))

    def handle_comment(self, data):
        raise ValueError("Comments are not supported in Telegram HTML")

    def handle_decl(self, decl):
        raise ValueError("Declarations are not supported in Telegram HTML")

    def handle_pi(self, data):
        raise ValueError("Processing instructions are not supported in Telegram HTML")

    def unknown_decl(self, data):
        raise ValueError("Declarations are not supported in Telegram HTML")


def _unicode_atoms(text):
    """Yield indivisible scalars (or explicit UTF-16 surrogate pairs)."""
    cursor = 0
    while cursor < len(text):
        char = text[cursor]
        codepoint = ord(char)
        if (0xD800 <= codepoint <= 0xDBFF and cursor + 1 < len(text)
                and 0xDC00 <= ord(text[cursor + 1]) <= 0xDFFF):
            yield text[cursor:cursor + 2], 2
            cursor += 2
        else:
            if 0xD800 <= codepoint <= 0xDFFF:
                raise ValueError("Unpaired Unicode surrogate in Telegram text")
            yield char, 2 if codepoint > 0xFFFF else 1
            cursor += 1


def _chunk_budgets(tokens, limit):
    offsets, newlines, spaces = [0], [], []
    for kind, value, _ in tokens:
        if kind in ("open", "close"):
            continue
        for atom, size in _unicode_atoms(value):
            if size > limit:
                raise ValueError("max_length is too small for a Unicode/entity atom")
            offsets.append(offsets[-1] + size)
            if atom == "\n":
                newlines.append(offsets[-1])
            if atom.isspace():
                spaces.append(offsets[-1])
    start, budgets = 0, []
    while offsets[-1] - start > limit:
        end = start + limit
        cut = offsets[bisect_right(offsets, end) - 1]
        for boundaries in (newlines, spaces):
            index = bisect_right(boundaries, end) - 1
            if index >= 0 and boundaries[index] - start >= max(1, limit // 2):
                cut = boundaries[index]
                break
        budgets.append(cut - start)
        start = cut
    budgets.append(offsets[-1] - start)
    return iter(budgets)


def html_chunks(rendered, max_length=4000):
    """Split generated HTML into independently balanced Telegram messages.

    The budget counts UTF-16 units of *decoded visible text*, not markup or
    entity spelling (Telegram limits text after entity parsing). Active tags,
    including link destinations, are closed/reopened around each split. Joining
    html_to_plain(chunk) reproduces html_to_plain(rendered) exactly. No trimming
    or ellipses are used. Empty input returns [""].

    Inputs must be balanced HTML from this renderer, or its supported tag subset.
    Invalid HTML, nonpositive budgets, and atoms larger than the budget raise
    ValueError. Splits prefer line/word boundaries and preserve entities and
    surrogate pairs; exceptionally long words or grapheme clusters may split.
    """
    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
        raise ValueError("max_length must be a positive integer")
    parser = _ChunkParser()
    parser.feed(rendered)
    parser.close()
    if parser.stack:
        raise ValueError("Unclosed Telegram HTML tag: " + parser.stack[-1])

    chunks = []
    current = []
    active = []
    length = 0
    budgets = _chunk_budgets(parser.tokens, max_length)
    budget = next(budgets)

    def append_atom(encoded, size):
        nonlocal current, length, budget
        if size > max_length:
            raise ValueError("max_length is too small for a Unicode/entity atom")
        if length + size > budget:
            chunks.append("".join(current)
                          + "".join("</" + tag + ">" for tag, _ in reversed(active)))
            current = [opening for _, opening in active]
            length = 0
            budget = next(budgets)
        current.append(encoded)
        length += size

    for kind, value, encoded in parser.tokens:
        if kind == "open":
            active.append((value, encoded))
            current.append(encoded)
        elif kind == "close":
            active.pop()
            current.append(encoded)
        elif kind == "entity":
            append_atom(encoded, sum(size for _, size in _unicode_atoms(value)))
        else:
            for atom, size in _unicode_atoms(value):
                append_atom(atom, size)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks
