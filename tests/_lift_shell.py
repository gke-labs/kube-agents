"""Lift a bash function or constant out of a shipped script, by name.

The modules that test hack/check-image-inventory.sh extract pieces of it and
run them under bash, or read them as text, so the assertions are against the
code that ships rather than a copy. All need the same regexes, and the regexes
encode assumptions about how the script formats a definition -- `name() {` on
one line, the closing `}` in column 0 -- so a second copy is a place a fix can
miss. Not a test module itself: `test_*.py` is the discovery pattern.
"""

import pathlib
import re

_FUNCTION_RE = r"^{name}\(\) \{{\n.*?^\}}\n"
_CONSTANT_RE = r"^readonly {name}=.*$"


def lift_function(name: str, text: str, source: pathlib.Path) -> str:
    """The definition of bash function `name`, closing brace included."""
    match = re.search(_FUNCTION_RE.format(name=re.escape(name)), text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{source} no longer defines {name}()")
    return match.group(0)


def lift_constant(name: str, text: str, source: pathlib.Path) -> str:
    """The `readonly NAME=...` line declaring `name`, newline included."""
    match = re.search(_CONSTANT_RE.format(name=re.escape(name)), text, re.M)
    if match is None:
        raise AssertionError(f"{source} no longer declares {name}")
    return match.group(0) + "\n"
