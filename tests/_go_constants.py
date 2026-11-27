"""Read a string constant out of a Go source file.

A Python test that holds something to a value the Go code declares reads the
value from the source, so that the test moves with the code. Not a test module
itself: `test_*.py` is the discovery pattern.
"""

import pathlib
import re


def go_string(path: pathlib.Path, name: str) -> str:
    """The value of a Go string constant, typed or not."""
    found = re.search(rf"^\s*{name}\s+(?:\w+\s+)?=\s*\"([^\"]*)\"", path.read_text(), re.MULTILINE)
    if not found:
        raise AssertionError(f"{path} no longer declares the string constant {name}")
    return found.group(1)
