"""One filter for untrusted text bound for the model: control, zero-width and bidi characters out.

The set is the strictest the three callers used; it is not a complete list of every
invisible code point (lone surrogates and noncharacters pass).

Pod logs, issue text and memory text all reach the same model. A character class
stripped on one path and not another is a hole in whichever path forgot, so the
classes live here once. The module has no third-party imports, so the sandbox
scripts and the plugins can import it as it is.
"""

import re

# Tab and newline are the only control characters kept: they carry layout.
KEPT_CONTROL_CODES = frozenset({9, 10})
# C0 controls end below 32; DEL and the C1 block are 127 to 159.
C0_CONTROL_LIMIT = 32
DEL_CODE = 127
C1_CONTROL_LAST = 159
# Zero-width and bidirectional format characters, inclusive ranges:
# U+200B-U+200F zero-width space, non-joiner, joiner, LRM, RLM;
# U+202A-U+202E bidi embedding and override controls;
# U+2060-U+206F word joiner, invisible operators, bidi isolates.
INVISIBLE_RANGES = ((0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F))
# Single code points that hide, join or reorder text: byte-order mark, soft hyphen,
# combining grapheme joiner, Arabic letter mark, Mongolian vowel separator,
# line separator, paragraph separator, braille pattern blank.
INVISIBLE_CODES = frozenset({0xFEFF, 0x00AD, 0x034F, 0x061C, 0x180E, 0x2028, 0x2029, 0x2800})
# The Unicode tag block and everything above it: the invisible-ASCII smuggling vector.
TAG_BLOCK_START = 0xE0000
# 7-bit ESC sequences and 8-bit CSI sequences.
ANSI_ESCAPE_RE = re.compile(r"(?:\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9B[0-?]*[ -/]*[@-~])")


def is_safe_char(ch: str) -> bool:
    """True for a character that may reach the model as it is."""
    code = ord(ch)
    if code in KEPT_CONTROL_CODES:
        return True
    if code < C0_CONTROL_LIMIT or DEL_CODE <= code <= C1_CONTROL_LAST:
        return False
    if any(first <= code <= last for first, last in INVISIBLE_RANGES) or code in INVISIBLE_CODES:
        return False
    if code >= TAG_BLOCK_START:
        return False
    return True


def strip_unsafe(text: str) -> str:
    """`text` without carriage returns, ANSI escape sequences and the characters above."""
    if not text:
        return ""
    text = text.replace("\r", "")
    text = ANSI_ESCAPE_RE.sub("", text)
    return "".join(ch for ch in text if is_safe_char(ch))
