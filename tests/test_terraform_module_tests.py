"""Every Terraform test suite runs, against mocks, on every pull request.

A `*.tftest.hcl` file runs only if the `terraform-test` Makefile target's loop
reaches its directory (`terraform/modules/*/tests`, `terraform/examples/*/tests`)
and the `validate` job in validate.yml runs the target; a suite that neither
reaches is a set of cases that passes by never running, the trap AGENTS.md
"Where Tests Go" names for `PYTHON_TEST_DIRS`. And a suite that mocks one
provider but not another would reach a real API from CI on the first read
nobody overrode.

The Makefile side is read the way make reads it, not as text: the target is
run against a fake `terraform` on PATH, once green and once with one suite
failing, to see that every directory runs and a failure fails the target;
`make -n verify` shows whether `verify` reaches it; the recipes make actually
resolved (its `-p` database, duplicates and includes settled) are checked for
the loop glob, for the exact `verify` line, and for make's `-` ignore-errors
prefix; and a failing recipe fed through `--eval` shows whether anything, in
the Makefile or a file it includes, makes make ignore errors. The workflow side
pins the step: the command alone in its `run:`, no `if:` or
`continue-on-error` or `working-directory` at step or job level, a fail-fast
shell, no MAKEFLAGS ignoring errors in any `env:`, no `paths` filter on the
trigger.

The mock side demands an unaliased `mock_provider` block for every provider a
root needs: what it declares in `required_providers` in any of its `.tf`
files (block form or version-only shorthand), what its `resource`, `data`,
`ephemeral`, `action` and `provider` blocks imply or route to with a
`provider =` meta-argument, what every local module it calls declares, and
what a module a `run` block loads declares; a run whose `providers` map routes
to a configuration no mock declares is reported too, and the builtin
`terraform` provider is never demanded. MAKEFLAGS in any workflow `env:` is
refused whatever it holds, since make also takes `--eval`, `-n` and `-f`
from it. HCL is read through a small tokenizer
that skips comments, strings, heredocs and template interpolation; a
`.tftest.json` is parsed; a root carrying a `*.tf.json` fails loudly rather
than being read half-way. The helpers have fixture cases of their own below.
tests/test_shellcheck_gate_wiring.py pins a workflow step the same way.
"""

import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import unittest

import yaml

try:
    from tests._run_make import run_make
    from tests.test_shellcheck_gate_wiring import _JOB_ID, _WORKFLOW, _run_lines
except ImportError:  # run from inside tests/
    from _run_make import run_make
    from test_shellcheck_gate_wiring import _JOB_ID, _WORKFLOW, _run_lines

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TERRAFORM_DIR = _REPO_ROOT / "terraform"
#: The directories the loop reaches: the modules and the compositions, the
#: same set the validate job's init-and-validate loop covers.
_SUITE_PARENTS = (_TERRAFORM_DIR / "modules", _TERRAFORM_DIR / "examples")

#: Directories a repository walk never reads, the set the Python test
#: discovery guard keeps for the same walk: provider downloads, the docs
#: site's dependencies, git's store, and `.claude`, where a review command
#: leaves worktrees of other branches inside a maintainer's checkout.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from test_test_discovery import IGNORED_NAMES as _WALK_EXCLUDED_PARTS  # noqa: E402

#: `_JOB_ID`, `_WORKFLOW` and `_run_lines` are the shellcheck wiring test's:
#: the same job on main's required-status-checks list, read the same way.
_TARGET = "terraform-test"
_GATE_COMMAND = f"make {_TARGET}"
#: What lets a gate step or job run red without failing the check, or not
#: run at all while the check reads as satisfied: `continue-on-error` and
#: an `if:` at step or job level, make's ignore-errors flag reaching it
#: through the environment at any level, a `paths` filter on the trigger.
_CONTINUE_ON_ERROR = "continue-on-error"
_IF = "if"
_ENV = "env"
_RUN, _SHELL, _DEFAULTS = "run", "shell", "defaults"
#: Where the command runs: the repository root, whose Makefile the pins
#: read, and nothing else.
_WORKING_DIRECTORY = "working-directory"
_REPOSITORY_ROOT_DIRECTORIES = (None, ".", "./")
#: The shells whose default template fails fast; a template of one's own
#: (`bash {0}`) or another shell is not read as one.
_FAIL_FAST_SHELLS = (None, "bash", "sh")
#: MAKEFLAGS in a workflow `env:` is refused whatever it holds: make reads
#: `-i`, but also `--eval=.IGNORE:`, `-n`, `-f other`, `-C dir` and `-t`
#: from it, each a way for a failing suite to exit 0, and nothing in this
#: workflow needs the variable.
_MAKEFLAGS_VARIABLES = ("MAKEFLAGS", "GNUMAKEFLAGS")
_TRIGGER, _PULL_REQUEST_TRIGGER, _PATH_FILTERS = "on", "pull_request", ("paths", "paths-ignore")
#: An earlier step can hand MAKEFLAGS to every later one through
#: $GITHUB_ENV, so no step's run body may name the variable at all.
_STEPS = "steps"
#: How make is asked: its database (`-p`) with a trivial target so nothing
#: else runs, a dry run of `verify` (recursive `$(MAKE)` lines execute under
#: `-n`, so the target's own recipe shows if `verify` reaches it), and a
#: failing recipe fed through `--eval`, which exits 0 only when errors are
#: ignored from the Makefile or a file it includes.
_MAKE_TIMEOUT_SECONDS = 120
_PROBE_TARGET = "__terraform_test_probe"
_DATABASE_ARGS = ("-pn", f"--eval={_PROBE_TARGET}: ;@:", _PROBE_TARGET)
_FAILING_TARGET = "__terraform_test_failing_recipe"
_FAILING_RECIPE_ARGS = (f"--eval={_FAILING_TARGET}: ;@false", _FAILING_TARGET)
_DATABASE_COMMENT = "#"
#: The target's rule line in the database, with or without prerequisites
#: (`verify:`, `verify: build`, `verify: | build`).
_DATABASE_RULE = "^{target}:(\\s|$)"
#: The special target that ignores errors, bare or per target, as the
#: database prints it.
_IGNORE_SPECIAL_TARGET = re.compile(r"(?m)^\.IGNORE:")
_LINE_CONTINUATION = "\\"
#: The shell loop skips a name beginning with a dot, so such a directory
#: is never a suite the loop runs, whatever it holds.
_HIDDEN_PREFIX = "."
#: The loop that reaches every module rather than naming the ones that had
#: tests when it was written.
_LOOP_GLOB = "for dir in terraform/modules/*/ terraform/examples/*/; do"
_VERIFY_TARGET = "verify"
_TEST_COMMAND = "terraform test"
#: The whole logical recipe line, in the recipe's own `@echo "==> …";
#: command` shape or bare: make's `@` and `+` prefixes are fine, its `-`
#: (ignore errors) is not, and nothing may precede or follow the command.
_VERIFY_LINE_EXACT = re.compile(rf'^\t[ \t@+]*(?:echo "[^"]*"; *)?\$\(MAKE\) --no-print-directory {_TARGET}\s*$')
#: A recipe line's prefix cluster, blanks interleaved as make allows; `-`
#: in it tells make to ignore that line's status.
_RECIPE_PREFIX = re.compile(r"^\t([@+ \t-]*)")
_IGNORE_LINE_PREFIX = "-"
#: The fake terraform the target is run against, and what it records: the
#: physical working directory (`pwd -P`, the form the Python side resolves
#: to, since the shell keeps a logical `$PWD` through a symlink) and the
#: subcommand.
_FAKE_TERRAFORM = """#!/bin/sh
printf '%s %s\\n' "$(pwd -P)" "$1" >> "$TERRAFORM_FAKE_LOG"
if [ "$1" = version ]; then echo "${TERRAFORM_FAKE_VERSION_LINE:-Terraform v${TERRAFORM_FAKE_VERSION:-1.15.8}}"; exit 0; fi
if [ "$1" = test ] && [ "$(pwd -P)" = "${TERRAFORM_FAKE_FAIL_IN:-}" ]; then
  echo "Failure! 1 failed."; exit 1
fi
exit 0
"""
_FAKE_LOG_VARIABLE, _FAKE_FAIL_VARIABLE, _FAKE_VERSION_VARIABLE = "TERRAFORM_FAKE_LOG", "TERRAFORM_FAKE_FAIL_IN", "TERRAFORM_FAKE_VERSION"
#: The floor the target names, the Makefile's own constant.
_MIN_VERSION_VARIABLE = "TERRAFORM_TEST_MIN_VERSION"
_TOO_OLD_MESSAGE = "is too old"
_UNREADABLE_VERSION_MESSAGE = "could not read a Terraform version"
_FAKE_VERSION_LINE_VARIABLE = "TERRAFORM_FAKE_VERSION_LINE"
_FAILING_DIRECTORIES_LINE = "Failing Terraform test directories:"

#: The HCL the pin reads, and the spelling it refuses.
_TF_FILE_GLOB = "*.tf"
_TF_JSON_GLOB = "*.tf.json"
#: Tokenizer pieces: an identifier (`google-beta` included, hence the
#: hyphen), a heredoc opener, the comment and block characters.
_IDENT = re.compile(r"[A-Za-z_][\w-]*")
_HEREDOC_OPEN = re.compile(r"<<-?([\w-]+)\r?\n")
_LINE_COMMENT_OPENERS = ("#", "//")
_BLOCK_COMMENT_OPEN, _BLOCK_COMMENT_CLOSE = "/*", "*/"
_TEMPLATE_OPENERS = ("${", "%{")
#: The escapes for a literal `${` and `%{`, which open nothing.
_TEMPLATE_ESCAPES = ("$${", "%%{")
_WHITESPACE = " \t\r\n"
#: Token kinds.
_STR, _WORD, _OPEN, _CLOSE, _EQUALS, _OTHER = "str", "word", "{", "}", "=", "other"
#: The blocks read: providers inside `required_providers { … }` as `name = {`
#: or the pre-0.13 `name = "version"`; a `module "x" { source = "../…" }`
#: whose providers the root needs too, since Terraform gives such a child a
#: default provider the root never declared (full-install never declares
#: `http`; the scope resolver it calls requires it); and a test file's
#: `mock_provider "name" { … }`, which mocks the default provider only when
#: it carries no `alias`.
_REQUIRED_PROVIDERS_BLOCK = "required_providers"
#: Blocks whose type prefix implies a provider the root loads whether or
#: not it is declared: `resource "google_x"`, `data "http"`, `provider "tls"`.
_IMPLYING_BLOCKS = (("resource", 2), ("data", 2), ("ephemeral", 2), ("action", 2), ("provider", 1))
_TYPE_PREFIX_SEPARATOR = "_"
#: The meta-argument that routes a resource to another provider
#: (`provider = google-beta`), which is loaded whether or not declared.
_PROVIDER_META_ARGUMENT = "provider"
#: `terraform_data` and `terraform_remote_state` belong to the builtin
#: provider, which is never fetched and cannot be mocked.
_BUILTIN_PROVIDER_PREFIX = "terraform"
#: A run's `providers = { http = http.live }` routes the module's `http` to
#: the `live` configuration; the pin wants that configuration mocked too.
_PROVIDERS_ATTRIBUTE = "providers"
_JSON_PROVIDERS_KEY = "providers"
_ALIAS_SEPARATOR = "."
#: HCL's object constructor admits `key: value` beside `key = value`.
_OBJECT_SEPARATOR = ":"
_MODULE_BLOCK = "module"
#: A test file's `run "x" { module { source = "./…" } }`, whose module's
#: providers the file has to mock too; in JSON, `run.<name>.module.source`.
_RUN_BLOCK = "run"
_JSON_RUN_KEY, _JSON_MODULE_KEY, _JSON_SOURCE_KEY = "run", "module", "source"
_SOURCE_ATTRIBUTE = "source"
_LOCAL_SOURCE_PREFIXES = ("./", "../")
_MOCK_PROVIDER_BLOCK = "mock_provider"
_ALIAS_ATTRIBUTE = "alias"
#: Both spellings Terraform loads from a suite; the JSON form carries its
#: mocks under this key, one object or a list of them per provider.
_TEST_FILE_SUFFIXES = (".tftest.hcl", ".tftest.json")
_TEST_FILE_JSON_SUFFIX = ".tftest.json"
_TESTS_DIR = "tests"


# ─── Where test files are, and which the loop runs ───────────────────────────


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.endswith(_TEST_FILE_SUFFIXES)


def _suite_files(root: pathlib.Path) -> list:
    """The test files `terraform test` loads when the loop runs it in `root`:
    those in tests/ and those beside it. The loop runs only where tests/
    exists, so a root-level file with no tests/ beside it runs nowhere."""
    tests_dir = root / _TESTS_DIR
    if not tests_dir.is_dir():
        return []
    return sorted(p for directory in (root, tests_dir) for p in directory.iterdir() if _is_test_file(p))


def _loop_directories(parents=_SUITE_PARENTS) -> list:
    """The directories the Makefile loop runs `terraform test` in: every
    non-hidden root under the parents with a tests/ directory, whatever it
    holds (a tests/ left with only a setup module still runs, and reports
    0 passed)."""
    return sorted(
        root for parent in parents for root in parent.iterdir()
        if root.is_dir() and not root.name.startswith(_HIDDEN_PREFIX) and (root / _TESTS_DIR).is_dir()
    )


def _suites(parents=_SUITE_PARENTS) -> dict:
    return {
        root: _suite_files(root)
        for parent in parents
        for root in sorted(parent.iterdir())
        if root.is_dir() and not root.name.startswith(_HIDDEN_PREFIX) and _suite_files(root)
    }


def _unreached_test_files(repo_root: pathlib.Path, parents) -> list:
    reached = {path for files in _suites(parents).values() for path in files}
    # Filtered on the path below the repository, not the absolute one: a
    # checkout that itself sits under an ignored name (a review worktree
    # under .claude/) would otherwise exclude every file and pass vacuously.
    everywhere = {
        path
        for path in repo_root.rglob("*")
        if _is_test_file(path) and not _WALK_EXCLUDED_PARTS & set(path.relative_to(repo_root).parts)
    }
    return sorted(everywhere - reached)


def _make(*args, extra_env=None):
    return run_make(list(args), timeout=_MAKE_TIMEOUT_SECONDS, extra_env=extra_env)


def _database() -> str:
    return _make(*_DATABASE_ARGS).stdout


def _resolved_recipe(database: str, target: str) -> list:
    """The recipe make resolved for `target`, from its database: the last
    definition wins and included files are read, as make does. Physical
    lines a trailing backslash continues are joined into the logical line
    the shell receives."""
    lines = database.split("\n")
    rule = re.compile(_DATABASE_RULE.format(target=re.escape(target)))
    start = next((i for i, line in enumerate(lines) if rule.match(line)), None)
    if start is None:
        raise AssertionError(f"make's database has no `{target}:` target")
    physical = []
    for line in lines[start + 1:]:
        if line.startswith(_DATABASE_COMMENT):
            continue
        if not line.startswith("\t"):
            break
        physical.append(line)
    logical = []
    for line in physical:
        if logical and logical[-1].rstrip().endswith(_LINE_CONTINUATION):
            logical[-1] = logical[-1].rstrip()[:-1] + line.lstrip()
        else:
            logical.append(line)
    return logical


def _ignored_recipe_lines(logical_lines: list) -> list:
    """Recipe lines whose prefix cluster carries `-`, make's ignore-errors."""
    return [
        line for line in logical_lines
        if (prefix := _RECIPE_PREFIX.match(line)) is not None and _IGNORE_LINE_PREFIX in prefix.group(1)
    ]


def _run_target_against_fake_terraform(fail_in: str = "", version: str = "", version_line: str = "") -> tuple:
    """`make terraform-test` with a fake `terraform` first on PATH that
    records each call's directory and subcommand, failing `test` in
    `fail_in`; returns (exit code, stdout, recorded calls)."""
    with tempfile.TemporaryDirectory() as scratch:
        scratch = pathlib.Path(scratch)
        fake = scratch / "terraform"
        fake.write_text(_FAKE_TERRAFORM)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        log = scratch / "calls.log"
        log.touch()
        result = _make(
            _TARGET,
            extra_env={
                "PATH": f"{scratch}{os.pathsep}{os.environ.get('PATH', '')}",
                _FAKE_LOG_VARIABLE: str(log),
                _FAKE_FAIL_VARIABLE: fail_in,
                _FAKE_VERSION_VARIABLE: version,
                _FAKE_VERSION_LINE_VARIABLE: version_line,
            },
        )
        # The subcommand never holds a space; the directory may, so split
        # from the right.
        calls = [tuple(line.rsplit(" ", 1)) for line in log.read_text().splitlines()]
    return result.returncode, result.stdout + result.stderr, calls


# ─── Reading HCL ─────────────────────────────────────────────────────────────


def _string_end(text: str, start: int) -> int:
    """Index after the quote closing the string opened at `start`, escapes
    and `${ … }` / `%{ … }` templates (which may hold quotes) skipped."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index + 1
        elif text[index : index + 3] in _TEMPLATE_ESCAPES:
            index += 3
        elif text[index : index + 2] in _TEMPLATE_OPENERS:
            index = _template_end(text, index + 2)
        else:
            index += 1
    return len(text)


def _template_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _string_end(text, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _heredoc_end(text: str, start: int, marker: str) -> int:
    index = start
    while index < len(text):
        line_end = text.find("\n", index)
        line_end = len(text) if line_end < 0 else line_end
        if text[index:line_end].strip() == marker:
            return line_end
        index = line_end + 1
    return len(text)


def _tokens(text: str) -> list:
    """HCL as (kind, value) pairs, comments dropped, strings and heredocs
    each one token, so nothing inside them reads as syntax."""
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index : index + 2]
        if char in _WHITESPACE:
            index += 1
        elif char == _LINE_COMMENT_OPENERS[0] or pair == _LINE_COMMENT_OPENERS[1]:
            line_end = text.find("\n", index)
            index = len(text) if line_end < 0 else line_end
        elif pair == _BLOCK_COMMENT_OPEN:
            close = text.find(_BLOCK_COMMENT_CLOSE, index + 2)
            index = len(text) if close < 0 else close + 2
        elif char == '"':
            end = _string_end(text, index)
            tokens.append((_STR, text[index + 1 : end - 1]))
            index = end
        elif (heredoc := _HEREDOC_OPEN.match(text, index)) is not None:
            end = _heredoc_end(text, heredoc.end(), heredoc.group(1))
            tokens.append((_STR, text[heredoc.end() : end]))
            index = end
        elif char in (_OPEN, _CLOSE, _EQUALS):
            tokens.append((char, char))
            index += 1
        elif (word := _IDENT.match(text, index)) is not None:
            tokens.append((_WORD, word.group(0)))
            index = word.end()
        else:
            tokens.append((_OTHER, char))
            index += 1
    return tokens


def _body(tokens: list, open_index: int) -> list:
    """The tokens between the brace at `open_index` and its match, each with
    its depth relative to that block (1 = directly inside); a nested block's
    own braces sit at the depth that encloses them."""
    body = []
    depth = 0
    for token in tokens[open_index:]:
        if token[0] == _OPEN:
            if depth > 0:
                body.append((token, depth))
            depth += 1
        elif token[0] == _CLOSE:
            depth -= 1
            if depth == 0:
                return body
            body.append((token, depth))
        else:
            body.append((token, depth))
    return body


def _blocks(tokens: list, name: str, labels: int) -> list:
    """Each block `name "label"… {` at the top level, as (labels, body)."""
    blocks = []
    depth = 0
    for index, token in enumerate(tokens):
        if token[0] == _OPEN:
            depth += 1
        elif token[0] == _CLOSE:
            depth -= 1
        elif (
            depth == 0
            and token == (_WORD, name)
            and all(tokens[index + 1 + n][0] == _STR for n in range(labels) if index + 1 + n < len(tokens))
            and index + 1 + labels < len(tokens)
            and tokens[index + 1 + labels][0] == _OPEN
        ):
            found = [tokens[index + 1 + n][1] for n in range(labels)]
            blocks.append((found, _body(tokens, index + 1 + labels)))
    return blocks


def _blocks_at_any_depth(tokens: list, name: str, labels: int) -> list:
    """Every `name "label"… {` block, whatever encloses it, as (labels,
    body): a `check` block's scoped data source implies its provider like
    a top-level one."""
    found = []
    for index, token in enumerate(tokens):
        if (
            token == (_WORD, name)
            and index + 1 + labels < len(tokens)
            and all(tokens[index + 1 + n][0] == _STR for n in range(labels))
            and tokens[index + 1 + labels][0] == _OPEN
        ):
            found.append(([tokens[index + 1 + n][1] for n in range(labels)], _body(tokens, index + 1 + labels)))
    return found


def _nested_blocks(body: list, name: str) -> list:
    """Bodies of `name {` blocks directly inside a body (no labels)."""
    flat = [token for token, _depth in body]
    nested = []
    for index, (token, depth) in enumerate(body):
        if depth == 1 and token == (_WORD, name) and index + 1 < len(body) and body[index + 1][0][0] == _OPEN:
            nested.append(_body(flat, index + 1))
    return nested


def _attributes(body: list) -> dict:
    """`name = value` pairs directly inside a body: the value's kind and text."""
    tokens = [token for token, depth in body if depth == 1]
    attributes = {}
    for index in range(len(tokens) - 2):
        if tokens[index][0] == _WORD and tokens[index + 1][0] == _EQUALS:
            attributes[tokens[index][1]] = tokens[index + 2]
    return attributes


def _providers_in(text: str) -> set:
    tokens = _tokens(text)
    providers = set()
    for _labels, body in _blocks(tokens, "terraform", 0):
        for block in _nested_blocks(body, _REQUIRED_PROVIDERS_BLOCK):
            providers.update(
                name for name, (kind, _value) in _attributes(block).items() if kind in (_OPEN, _STR)
            )
    for block_name, labels in _IMPLYING_BLOCKS:
        for found, body in _blocks_at_any_depth(tokens, block_name, labels):
            providers.add(found[0].split(_TYPE_PREFIX_SEPARATOR)[0])
            routed = _attributes(body).get(_PROVIDER_META_ARGUMENT)
            if routed is not None and routed[0] == _WORD:
                providers.add(routed[1])
    providers.discard(_BUILTIN_PROVIDER_PREFIX)
    return providers


def _local_module_sources_in(text: str) -> list:
    sources = []
    for _labels, body in _blocks(_tokens(text), _MODULE_BLOCK, 1):
        source = _attributes(body).get(_SOURCE_ATTRIBUTE)
        if source is not None and source[0] == _STR and source[1].startswith(_LOCAL_SOURCE_PREFIXES):
            sources.append(source[1])
    return sources


def _declared_providers(root: pathlib.Path) -> list:
    unread = sorted(root.glob(_TF_JSON_GLOB))
    if unread:
        raise AssertionError(
            f"{root} carries {[p.name for p in unread]}: the mock-provider pin reads HCL only, "
            "so a provider or module call declared in JSON syntax would go unseen; write it as .tf"
        )
    providers = set()
    for tf_file in sorted(root.glob(_TF_FILE_GLOB)):
        providers.update(_providers_in(tf_file.read_text()))
    return sorted(providers)


def _needed_providers(root: pathlib.Path, seen=None) -> list:
    """What the root declares, plus what every local module it calls declares."""
    root = root.resolve()
    seen = set() if seen is None else seen
    if root in seen:
        return []
    seen.add(root)
    providers = set(_declared_providers(root))
    for tf_file in sorted(root.glob(_TF_FILE_GLOB)):
        for source in _local_module_sources_in(tf_file.read_text()):
            child = (root / source).resolve()
            if child.is_dir():
                providers.update(_needed_providers(child, seen))
    return sorted(providers)


def _json_bodies(value) -> list:
    """The bodies of a labelled JSON block: `{"a": body}` or `[{"a": body}]`,
    each body itself one object or a list of them."""
    entries = value.values() if isinstance(value, dict) else []
    if isinstance(value, list):
        entries = [body for element in value if isinstance(element, dict) for body in element.values()]
    bodies = []
    for entry in entries:
        bodies.extend(entry if isinstance(entry, list) else [entry])
    return [body for body in bodies if isinstance(body, dict)]


def _json_labelled(value) -> list:
    """(label, body) pairs of a labelled JSON block in either form."""
    elements = [value] if isinstance(value, dict) else [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []
    pairs = []
    for element in elements:
        for label, bodies in element.items():
            pairs.extend((label, body) for body in _json_unlabelled(bodies))
    return pairs


def _json_unlabelled(value) -> list:
    """An unlabelled JSON block: one object or a list of them."""
    entries = value if isinstance(value, list) else [value]
    return [entry for entry in entries if isinstance(entry, dict)]


def _json_runs(text: str) -> list:
    return _json_bodies(json.loads(text).get(_JSON_RUN_KEY, {})) if text.strip() else []


def _run_module_sources(text: str, name: str) -> list:
    """Local module sources a test file's run blocks load, relative to the root."""
    if name.endswith(_TEST_FILE_JSON_SUFFIX):
        sources = [
            module.get(_JSON_SOURCE_KEY)
            for run in _json_runs(text)
            for module in _json_unlabelled(run.get(_JSON_MODULE_KEY, []))
        ]
    else:
        sources = [
            _attributes(block).get(_SOURCE_ATTRIBUTE, (None, None))[1]
            for _labels, body in _blocks(_tokens(text), _RUN_BLOCK, 1)
            for block in _nested_blocks(body, _MODULE_BLOCK)
            if _attributes(block).get(_SOURCE_ATTRIBUTE, ("",))[0] == _STR
        ]
    return [source for source in sources if isinstance(source, str) and source.startswith(_LOCAL_SOURCE_PREFIXES)]


def _run_provider_routes(text: str, name: str) -> list:
    """Every `provider.alias` a run's `providers` map hands the module."""
    routes = []
    if name.endswith(_TEST_FILE_JSON_SUFFIX):
        for run in _json_runs(text):
            mapping = run.get(_JSON_PROVIDERS_KEY, {})
            routes.extend(v for v in mapping.values() if isinstance(v, str)) if isinstance(mapping, dict) else None
        return routes
    for _labels, body in _blocks(_tokens(text), _RUN_BLOCK, 1):
        flat = [token for token, _depth in body]
        for index, (token, depth) in enumerate(body):
            if depth == 1 and token == (_WORD, _PROVIDERS_ATTRIBUTE) and index + 2 < len(body) and body[index + 1][0][0] == _EQUALS and body[index + 2][0][0] == _OPEN:
                entries = [t for t, d in _body(flat, index + 2) if d == 1]
                position = 0
                while position + 2 < len(entries):
                    if entries[position][0] == _WORD and entries[position + 1] in ((_EQUALS, _EQUALS), (_OTHER, _OBJECT_SEPARATOR)) and entries[position + 2][0] == _WORD:
                        route = entries[position + 2][1]
                        if position + 4 < len(entries) and entries[position + 3] == (_OTHER, _ALIAS_SEPARATOR) and entries[position + 4][0] == _WORD:
                            route += _ALIAS_SEPARATOR + entries[position + 4][1]
                            position += 5
                        else:
                            position += 3
                        routes.append(route)
                    else:
                        position += 1
    return routes


def _needed_by_test_file(root: pathlib.Path, path: pathlib.Path) -> list:
    """What the root needs, plus what the modules this file's runs load need."""
    providers = set(_needed_providers(root))
    for source in _run_module_sources(path.read_text(), path.name):
        child = (root / source).resolve()
        if child.is_dir():
            providers.update(_needed_providers(child))
    return sorted(providers)


def _ignores_make_errors(scope: dict) -> bool:
    env = scope.get(_ENV) or {}
    return isinstance(env, dict) and any(variable in env for variable in _MAKEFLAGS_VARIABLES)


def _run_setting(scope: dict, key: str):
    """A `run:` setting on a step, or under `defaults.run` of a job or
    workflow."""
    if _DEFAULTS in scope:
        return ((scope.get(_DEFAULTS) or {}).get(_RUN) or {}).get(key)
    return scope.get(key)


def _shell_defect(scope: dict) -> bool:
    return _run_setting(scope, _SHELL) not in _FAIL_FAST_SHELLS


def _working_directory_defect(scope: dict) -> bool:
    return _run_setting(scope, _WORKING_DIRECTORY) not in _REPOSITORY_ROOT_DIRECTORIES


def _gate_defects(job: dict, workflow: dict = None) -> list:
    """Why the job would not gate on `make terraform-test`: no such step,
    more than one, an `if:` or `continue-on-error` on the step or the job,
    make's ignore-errors flag in the env at any level, or a paths filter on
    the pull_request trigger."""
    defects = []
    for step in job.get(_STEPS, []):
        if any(variable in str(step.get(_RUN, "")) for variable in _MAKEFLAGS_VARIABLES):
            defects.append(f"a step's run body names {'/'.join(_MAKEFLAGS_VARIABLES)}; through $GITHUB_ENV it reaches every later step, `{_GATE_COMMAND}` included")
    steps = [s for s in job.get(_STEPS, []) if _GATE_COMMAND in _run_lines(s)]
    if len(steps) != 1:
        defects.append(f"{len(steps)} steps run `{_GATE_COMMAND}`; exactly one must")
    for step in steps:
        if _run_lines(step) != [_GATE_COMMAND]:
            defects.append(f"the step's run body is more than `{_GATE_COMMAND}` alone; a line before it can set MAKEFLAGS, one after it can hide its status")
        if _shell_defect(step):
            defects.append("the step names a shell whose template is not fail-fast")
        if _working_directory_defect(step):
            defects.append(f"the step sets `{_WORKING_DIRECTORY}`, so `{_GATE_COMMAND}` would read another Makefile")
        if _IF in step:
            defects.append("the step carries an `if:`")
        if step.get(_CONTINUE_ON_ERROR):
            defects.append(f"the step carries `{_CONTINUE_ON_ERROR}`")
        if _ignores_make_errors(step):
            defects.append("the step's env sets MAKEFLAGS, through which make takes -i, --eval, -n or -f")
    if _shell_defect(job):
        defects.append("the job's default shell is not fail-fast")
    if _working_directory_defect(job):
        defects.append(f"the job's default `{_WORKING_DIRECTORY}` is not the repository root")
    if _IF in job:
        defects.append("the job carries an `if:`")
    if job.get(_CONTINUE_ON_ERROR):
        defects.append(f"the job carries `{_CONTINUE_ON_ERROR}`")
    if _ignores_make_errors(job):
        defects.append("the job's env sets MAKEFLAGS, through which make takes -i, --eval, -n or -f")
    if workflow is not None:
        if _ignores_make_errors(workflow):
            defects.append("the workflow's env sets MAKEFLAGS, through which make takes -i, --eval, -n or -f")
        if _shell_defect(workflow):
            defects.append("the workflow's default shell is not fail-fast")
        if _working_directory_defect(workflow):
            defects.append(f"the workflow's default `{_WORKING_DIRECTORY}` is not the repository root")
        # `on:` may be a mapping, a list or a bare string; only the mapping
        # form can carry a paths filter.
        triggers = workflow.get(_TRIGGER, workflow.get(True))
        trigger = (triggers or {}).get(_PULL_REQUEST_TRIGGER) if isinstance(triggers, dict) else None
        if isinstance(trigger, dict) and any(key in trigger for key in _PATH_FILTERS):
            defects.append("the pull_request trigger carries a paths filter")
    return defects


def _mock_configurations_in_hcl(text: str) -> set:
    """Each `mock_provider` block as `name` or `name.alias`."""
    configurations = set()
    for labels, body in _blocks(_tokens(text), _MOCK_PROVIDER_BLOCK, 1):
        alias = _attributes(body).get(_ALIAS_ATTRIBUTE)
        configurations.add(labels[0] if alias is None else f"{labels[0]}{_ALIAS_SEPARATOR}{alias[1]}")
    return configurations


def _mock_configurations_in_json(text: str) -> set:
    mocks = json.loads(text).get(_MOCK_PROVIDER_BLOCK, {}) if text.strip() else {}
    configurations = set()
    for name, entry in _json_labelled(mocks):
        alias = entry.get(_ALIAS_ATTRIBUTE)
        configurations.add(name if alias is None else f"{name}{_ALIAS_SEPARATOR}{alias}")
    return configurations


def _unmocked(text: str, providers, name: str = _TEST_FILE_SUFFIXES[0]) -> list:
    """Providers with no default mock, then every configuration a run's
    `providers` map routes to that no mock block declares."""
    is_json = name.endswith(_TEST_FILE_JSON_SUFFIX)
    mocked = _mock_configurations_in_json(text) if is_json else _mock_configurations_in_hcl(text)
    missing = [provider for provider in providers if provider not in mocked]
    missing.extend(route for route in _run_provider_routes(text, name) if route not in mocked and route not in missing)
    return missing


class TerraformModuleTestsWiringTest(unittest.TestCase):
    def test_at_least_one_module_carries_a_suite(self):
        self.assertTrue(_suites(), f"no {_TESTS_DIR}/*{_TEST_FILE_SUFFIXES[0]} under {_SUITE_PARENTS}; this test pins their wiring")

    def test_no_test_file_sits_where_the_loop_does_not_look(self):
        self.assertEqual(
            _unreached_test_files(_REPO_ROOT, _SUITE_PARENTS),
            [],
            f"these test files are not in, or beside, a {_TESTS_DIR}/ directory under {[p.name for p in _SUITE_PARENTS]}, the only places `{_GATE_COMMAND}` runs `{_TEST_COMMAND}`, so they never run",
        )

    def test_the_target_runs_every_suite_and_fails_when_one_fails(self):
        # Behaviour, not text: the real target against a fake terraform.
        # Compared as resolved paths, so two roots of one name under the
        # two parents stay distinct and the order is not a name's.
        entered = {root.resolve() for root in _loop_directories()}
        self.assertTrue({root.resolve() for root in _suites()} <= entered)
        code, out, calls = _run_target_against_fake_terraform()
        self.assertEqual(code, 0, f"`make {_TARGET}` failed with every suite green:\n{out}")
        tested = {pathlib.Path(d).resolve() for d, sub in calls if sub == "test"}
        self.assertEqual(tested, entered, f"`{_TEST_COMMAND}` did not run in exactly the directories the loop enters: {calls}")
        failing = sorted(entered)[0]
        code, out, calls = _run_target_against_fake_terraform(fail_in=str(failing))
        self.assertNotEqual(code, 0, f"`make {_TARGET}` exited 0 with the {failing.name} suite failing:\n{out}")
        self.assertEqual({pathlib.Path(d).resolve() for d, sub in calls if sub == "test"}, entered, f"a failing suite stopped the others from running: {calls}")
        self.assertIn(_FAILING_DIRECTORIES_LINE, out, f"the failing directory is not named at the end:\n{out}")
        self.assertIn(failing.name, out.split(_FAILING_DIRECTORIES_LINE, 1)[1])

    def test_a_terraform_below_the_floor_is_named_not_reported_as_failing_suites(self):
        floor = _make("-s", f"--eval=print-floor: ;@echo $({_MIN_VERSION_VARIABLE})", "print-floor").stdout.strip()
        self.assertRegex(floor, r"^\d+\.\d+\.\d+$", f"{_MIN_VERSION_VARIABLE} is not set in the Makefile")
        code, out, calls = _run_target_against_fake_terraform(version="1.6.6")
        self.assertNotEqual(code, 0)
        self.assertIn(_TOO_OLD_MESSAGE, out, f"an old terraform must be named up front, not read as two failing suites:\n{out}")
        self.assertIn(floor, out)
        self.assertNotIn(_FAILING_DIRECTORIES_LINE, out)
        self.assertEqual([sub for _d, sub in calls if sub == "test"], [], "no suite may run under a terraform below the floor")
        code, out, _calls = _run_target_against_fake_terraform(version=floor)
        self.assertEqual(code, 0, f"a terraform at the floor must run the suites:\n{out}")
        # A shim or another binary answering `version` with something else
        # is named as such, not as an old Terraform.
        code, out, calls = _run_target_against_fake_terraform(version_line="OpenTofu v1.9.0")
        self.assertNotEqual(code, 0)
        self.assertIn(_UNREADABLE_VERSION_MESSAGE, out, out)
        self.assertNotIn(_TOO_OLD_MESSAGE, out)
        self.assertEqual([sub for _d, sub in calls if sub == "test"], [])

    def test_the_resolved_recipes_carry_the_loop_and_no_ignored_line(self):
        database = _database()
        recipe = _resolved_recipe(database, _TARGET)
        self.assertTrue(
            any(_LOOP_GLOB in line for line in recipe),
            f"`{_TARGET}` must loop over every terraform/modules/*/ and terraform/examples/*/ rather than name directories: a new one's tests/ is otherwise a suite nothing runs; make resolved:\n" + "\n".join(recipe),
        )
        self.assertEqual(_ignored_recipe_lines(recipe), [], f"a `-` prefix on a `{_TARGET}` recipe line tells make to ignore its status, so the target would exit 0 on a failing suite")
        verify = _resolved_recipe(database, _VERIFY_TARGET)
        self.assertEqual(_ignored_recipe_lines(verify), [], f"a `-` prefix on a `{_VERIFY_TARGET}` recipe line tells make to ignore its status")
        self.assertEqual(
            len([line for line in verify if _VERIFY_LINE_EXACT.match(line)]),
            1,
            f"`make {_VERIFY_TARGET}` says it runs everything a pull request must pass offline; `{_TARGET}` is one of them, on a logical line of its own with no `-` prefix and nothing before or after that would skip it or swallow its exit status; make resolved:\n" + "\n".join(verify),
        )

    def test_make_verify_reaches_the_target(self):
        dry_run = _make("-n", _VERIFY_TARGET)
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn(_LOOP_GLOB, dry_run.stdout, f"`make -n {_VERIFY_TARGET}` never reached `{_TARGET}`'s recipe (recursive make lines run under -n, so it prints when reached)")

    def test_make_help_lists_the_target(self):
        self.assertIn(_TARGET, _make("help").stdout, f"`make help` does not list `{_TARGET}`; its recipe line lost its `## description`")

    def test_nothing_makes_make_ignore_a_failing_recipe(self):
        probe = _make(*_FAILING_RECIPE_ARGS)
        self.assertNotEqual(
            probe.returncode,
            0,
            "a recipe that runs `false` exited 0: a bare `.IGNORE` target or MAKEFLAGS carrying -i, in the Makefile or a file it includes, would report a failing suite as ignored",
        )
        self.assertIsNone(
            _IGNORE_SPECIAL_TARGET.search(_database()),
            f"make's database carries a `.IGNORE` target, bare or per target; `.IGNORE: {_VERIFY_TARGET}` would report a failing suite as ignored for that target alone",
        )

    def test_the_validate_job_runs_the_target_unconditionally(self):
        workflow = yaml.safe_load(_WORKFLOW.read_text())
        self.assertEqual(
            _gate_defects(workflow["jobs"][_JOB_ID], workflow),
            [],
            f"the `{_JOB_ID}` job in validate.yml must run `{_GATE_COMMAND}` in exactly one step, with no `if:` or `{_CONTINUE_ON_ERROR}` on the step or the job, no MAKEFLAGS ignoring errors at any level, and no paths filter on the trigger",
        )

    def test_every_test_file_mocks_every_provider_its_root_needs(self):
        for root, files in _suites().items():
            self.assertTrue(_needed_providers(root), f"{root.name} needs no provider in any {_TF_FILE_GLOB}, its own or a called module's")
            for path in files:
                providers = _needed_by_test_file(root, path)
                with self.subTest(root=root.name, file=path.name):
                    self.assertEqual(
                        _unmocked(path.read_text(), providers, path.name),
                        [],
                        f"{path.relative_to(_REPO_ROOT)} has no unaliased mock_provider for every provider its root, or a module its runs load, needs ({providers}); a read the file forgets to override would reach a real API from CI",
                    )


class TerraformModuleTestsHelpersTest(unittest.TestCase):
    """The judgements above, on the shapes they exist to catch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_providers_are_read_from_any_tf_file_and_only_inside_the_block(self):
        # A composition's shape: a provider configured before the terraform
        # block (which implies `helm`), with a URL (`//`) and a template
        # holding quotes in its strings; providers.tf, no versions.tf; a
        # `kubernetes = {` attribute inside a provider block after it; the
        # version-only shorthand; a resource implying `null`.
        (self.root / "providers.tf").write_text(
            'provider "helm" {\n  kubernetes = {\n    host  = "https://x.example/#frag"\n'
            '    token = "${var.a == "b" ? "c" : "d"} /* not a comment */"\n  }\n}\n\n'
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n"
            "    google-beta = {\n      source = \"hashicorp/google-beta\"\n    }\n"
            '    random = ">= 3.5"\n  }\n}\n'
        )
        (self.root / "main.tf").write_text("resource \"null_resource\" \"x\" {}\n")
        self.assertEqual(_declared_providers(self.root), ["google", "google-beta", "helm", "null", "random"])

    def test_an_undeclared_resource_data_or_provider_block_implies_its_provider(self):
        # A check block's scoped data source counts too; the builtin
        # terraform provider (terraform_data) never does.
        (self.root / "main.tf").write_text(
            'data "http" "x" {\n  url = "https://x"\n}\nresource "google_project_iam_member" "y" {}\n'
            'provider "tls" {}\nlocals {\n  z = 1\n}\nresource "terraform_data" "t" {}\n'
            'check "health" {\n  data "dns_a_record_set" "probe" {\n    host = "x"\n  }\n  assert {\n    condition     = true\n    error_message = "x"\n  }\n}\n'
        )
        self.assertEqual(_declared_providers(self.root), ["dns", "google", "http", "tls"])

    def test_a_provider_meta_argument_and_the_newer_block_kinds_imply_their_providers(self):
        (self.root / "main.tf").write_text(
            'resource "google_pubsub_topic" "t" {\n  provider = google-beta\n  name     = "x"\n}\n'
            'data "google_project" "p" {\n  provider = google.west\n}\n'
            'ephemeral "aws_secretsmanager_secret_version" "s" {}\naction "azurerm_run" "a" {}\n'
        )
        self.assertEqual(_declared_providers(self.root), ["aws", "azurerm", "google", "google-beta"])

    def test_a_root_declaring_nothing_reads_as_no_providers(self):
        (self.root / "main.tf").write_text("locals {\n  a = {\n    b = {}\n  }\n}\n")
        self.assertEqual(_declared_providers(self.root), [])

    def test_a_brace_or_comment_opener_in_a_comment_string_or_heredoc_is_not_syntax(self):
        (self.root / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    # the {google} entry below }\n    google = {\n"
            "      source  = \"hashicorp/google\"\n      version = \"} // not a comment\"\n    }\n    /* } */\n"
            "    http = {\n      source = \"hashicorp/http\"\n    }\n  }\n}\n\n"
            'variable "v" {\n  description = <<-EO-T\n    A "quote and a } and a # and https://x\n  EO-T\n}\n'
            'variable "w" {\n  default = "{"\n}\n'
        )
        self.assertEqual(_declared_providers(self.root), ["google", "http"])

    def test_an_escaped_template_opener_in_a_string_opens_nothing(self):
        # `$${` is a literal, not a template; the block after it is still read.
        (self.root / "main.tf").write_text(
            'variable "v" {\n  default = "$${"\n}\nvariable "w" {\n  default = "%%{ and ${var.x}"\n}\n'
            "terraform {\n  required_providers {\n    http = {\n      source = \"hashicorp/http\"\n    }\n  }\n}\n"
        )
        self.assertEqual(_declared_providers(self.root), ["http"])

    def test_a_root_with_a_tf_json_file_is_refused_not_half_read(self):
        (self.root / "versions.tf.json").write_text('{"terraform": {"required_providers": {"http": {}}}}')
        with self.assertRaises(AssertionError):
            _declared_providers(self.root)

    def test_a_called_local_module_adds_the_providers_it_declares(self):
        # A composition that declares google and calls a module requiring
        # http, with no providers map: the child gets a default http provider,
        # so the composition's tests must mock it too. The root file wins for
        # the declared set, the union for the needed set; a module calling
        # itself does not recurse forever; a remote source is ignored.
        composition = self.root / "examples" / "c"
        module = self.root / "modules" / "m"
        for directory in (composition, module):
            directory.mkdir(parents=True)
        (composition / "providers.tf").write_text(
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n  }\n}\n"
        )
        (composition / "main.tf").write_text(
            'module "m" {\n  source = "../../modules/m"\n}\n'
            'module "remote" {\n  source = "git::https://example.com/x.git//m"\n}\n'
        )
        (module / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    http = {\n      source = \"hashicorp/http\"\n    }\n  }\n}\n"
        )
        (module / "main.tf").write_text('module "self" {\n  source = "./"\n}\n')
        self.assertEqual(_declared_providers(composition), ["google"])
        self.assertEqual(_needed_providers(composition), ["google", "http"])

    def test_a_module_a_run_block_loads_adds_its_providers_for_that_file(self):
        root = self.root / "terraform" / "modules" / "m"
        setup = root / _TESTS_DIR / "setup"
        setup.mkdir(parents=True)
        (root / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n  }\n}\n"
        )
        (setup / "main.tf").write_text('data "http" "seed" {\n  url = "https://x"\n}\n')
        hcl = root / _TESTS_DIR / "a.tftest.hcl"
        hcl.write_text('mock_provider "google" {}\n\nrun "seed" {\n  module {\n    source = "./tests/setup"\n  }\n}\n')
        plain = root / _TESTS_DIR / "b.tftest.hcl"
        plain.write_text('mock_provider "google" {}\n\nrun "x" {\n  command = plan\n}\n')
        as_json = root / _TESTS_DIR / "c.tftest.json"
        as_json.write_text('{"mock_provider": {"google": {}}, "run": {"seed": {"module": {"source": "./tests/setup"}}}}')
        # The array forms HCL-JSON admits: a list of {label: body} runs, and
        # a list of module objects.
        as_array = root / _TESTS_DIR / "d.tftest.json"
        as_array.write_text('{"mock_provider": {"google": {}}, "run": [{"seed": {"module": [{"source": "./tests/setup"}]}}]}')
        self.assertEqual(_needed_providers(root), ["google"])
        self.assertEqual(_needed_by_test_file(root, hcl), ["google", "http"])
        self.assertEqual(_needed_by_test_file(root, plain), ["google"])
        self.assertEqual(_needed_by_test_file(root, as_json), ["google", "http"])
        self.assertEqual(_needed_by_test_file(root, as_array), ["google", "http"])
        self.assertEqual(_unmocked(hcl.read_text(), _needed_by_test_file(root, hcl)), ["http"])
        self.assertEqual(_unmocked(as_json.read_text(), _needed_by_test_file(root, as_json), as_json.name), ["http"])
        self.assertEqual(_unmocked(as_array.read_text(), _needed_by_test_file(root, as_array), as_array.name), ["http"])

    def test_a_run_routing_a_provider_to_a_live_configuration_is_reported(self):
        text = (
            'mock_provider "google" {}\nmock_provider "http" {}\nprovider "http" {\n  alias = "live"\n}\n'
            'run "x" {\n  providers = {\n    google = google\n    http   = http.live\n  }\n  command = plan\n}\n'
        )
        self.assertEqual(_unmocked(text, ["google", "http"]), ["http.live"])
        colon_form = text.replace("google = google", "google: google").replace("http   = http.live", "http: http.live")
        self.assertEqual(_unmocked(colon_form, ["google", "http"]), ["http.live"])
        mocked_alias = text.replace('provider "http" {\n  alias = "live"\n}', 'mock_provider "http" {\n  alias = "live"\n}')
        self.assertEqual(_unmocked(mocked_alias, ["google", "http"]), [])
        as_json = '{"mock_provider": {"google": {}, "http": [{}, {"alias": "live"}]}, "run": {"x": {"providers": {"http": "http.live"}}}}'
        self.assertEqual(_unmocked(as_json, ["google", "http"], "x.tftest.json"), [])
        routed_live = '{"mock_provider": {"google": {}, "http": {}}, "run": {"x": {"providers": {"http": "http.live"}}}}'
        self.assertEqual(_unmocked(routed_live, ["google", "http"], "x.tftest.json"), ["http.live"])

    def test_a_mock_in_a_comment_does_not_count(self):
        text = '# mock_provider "http" {}\nmock_provider "google" {}\n\nrun "x" {\n  command = plan\n}\n'
        self.assertEqual(_unmocked(text, ["google", "http"]), ["http"])
        blocked = '/*\nmock_provider "http" {}\n*/\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(blocked, ["google", "http"]), ["http"])
        slashed = '// mock_provider "http" {}\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(slashed, ["google", "http"]), ["http"])

    def test_an_aliased_mock_does_not_mock_the_default_provider(self):
        text = 'mock_provider "google" {\n  alias = "offline"\n}\nmock_provider "http" {\n}\n'
        self.assertEqual(_unmocked(text, ["google", "http"]), ["google"])
        both = 'mock_provider "google" {\n  alias = "offline"\n}\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(both, ["google"]), [])

    def test_an_indented_mock_block_counts(self):
        text = '  mock_provider "google-beta" {\n  }\n'
        self.assertEqual(_unmocked(text, ["google-beta"]), [])

    def test_a_json_test_file_is_read_as_json(self):
        text = '{"mock_provider": {"google": {}, "http": [{"alias": "x"}, {}]}, "run": {"x": {"command": "plan"}}}'
        self.assertEqual(_unmocked(text, ["google", "http"], "b.tftest.json"), [])
        array_form = '{"mock_provider": [{"google": {}}, {"http": {}}], "run": [{"x": {"command": "plan"}}]}'
        self.assertEqual(_unmocked(array_form, ["google", "http"], "b.tftest.json"), [])
        aliased = '{"mock_provider": {"google": {"alias": "x"}}}'
        self.assertEqual(_unmocked(aliased, ["google"], "b.tftest.json"), ["google"])
        self.assertEqual(_unmocked('{"run": {"x": {}}}', ["google"], "b.tftest.json"), ["google"])
        self.assertEqual(_unmocked("", ["google"], "b.tftest.json"), ["google"])

    def test_the_loop_enters_a_tests_directory_whatever_it_holds(self):
        parents = (self.root / "terraform" / "modules",)
        with_suite = self.root / "terraform" / "modules" / "a" / _TESTS_DIR
        setup_only = self.root / "terraform" / "modules" / "b" / _TESTS_DIR / "setup"
        hidden = self.root / "terraform" / "modules" / ".c" / _TESTS_DIR
        no_tests = self.root / "terraform" / "modules" / "d"
        for directory in (with_suite, setup_only, hidden, no_tests):
            directory.mkdir(parents=True)
        (with_suite / "a.tftest.hcl").write_text("")
        (setup_only / "main.tf").write_text("")
        self.assertEqual([r.name for r in _loop_directories(parents)], ["a", "b"])
        self.assertEqual([r.name for r in _suites(parents)], ["a"])

    def test_a_test_file_beside_tests_runs_and_one_without_tests_does_not(self):
        parents = (self.root / "terraform" / "modules",)
        with_tests = self.root / "terraform" / "modules" / "m"
        (with_tests / _TESTS_DIR).mkdir(parents=True)
        (with_tests / _TESTS_DIR / "a.tftest.hcl").write_text("")
        (with_tests / _TESTS_DIR / "b.tftest.json").write_text("{}")
        (with_tests / _TESTS_DIR / "notes.md").write_text("")
        (with_tests / "c.tftest.hcl").write_text("")
        without_tests = self.root / "terraform" / "modules" / "n"
        without_tests.mkdir()
        (without_tests / "d.tftest.hcl").write_text("")
        self.assertEqual(
            [p.name for p in _suites(parents)[with_tests]],
            ["c.tftest.hcl", "a.tftest.hcl", "b.tftest.json"],
        )
        self.assertEqual(_unreached_test_files(self.root, parents), [without_tests / "d.tftest.hcl"])

    def test_a_test_file_outside_the_reached_set_is_reported(self):
        # A dot-named module directory is one the shell glob skips, so its
        # suite is unreached however it is laid out.
        parents = (self.root / "terraform" / "modules",)
        reached = self.root / "terraform" / "modules" / "m" / "tests"
        stray = self.root / "bench" / "tf" / "fleet" / "tests"
        hidden = self.root / "terraform" / "modules" / ".archived" / "tests"
        ignored = self.root / "terraform" / "modules" / "m" / ".terraform" / "tests"
        worktree = self.root / ".claude" / "worktrees" / "pr-1" / "terraform" / "modules" / "m" / "tests"
        for directory in (reached, stray, hidden, ignored, worktree):
            directory.mkdir(parents=True)
            (directory / "a.tftest.hcl").write_text("")
        self.assertEqual(
            _unreached_test_files(self.root, parents),
            sorted([hidden / "a.tftest.hcl", stray / "a.tftest.hcl"]),
        )
        self.assertNotIn(hidden.parent, _suites(parents))

    def test_a_checkout_under_an_ignored_name_is_still_walked(self):
        # A review worktree lives under .claude/; the filter applies below
        # the repository root, not to the root's own ancestors.
        repo = self.root / ".claude" / "worktrees" / "pr-1"
        parents = (repo / "terraform" / "modules",)
        stray = repo / "bench" / "tf" / "fleet" / "tests"
        stray.mkdir(parents=True)
        (stray / "a.tftest.hcl").write_text("")
        (repo / "terraform" / "modules").mkdir(parents=True)
        self.assertEqual(_unreached_test_files(repo, parents), [stray / "a.tftest.hcl"])

    def test_a_resolved_recipe_is_the_last_definition_with_logical_lines(self):
        database = (
            "verify:\n#  recipe to execute (from 'Makefile', line 8):\n\t@true || \\\n\t$(MAKE) --no-print-directory terraform-test\n\t@echo done\n\n"
            "other:\n\t@echo x\n"
        )
        recipe = _resolved_recipe(database, "verify")
        self.assertEqual(recipe, ["\t@true || $(MAKE) --no-print-directory terraform-test", "\t@echo done"])
        self.assertEqual([line for line in recipe if _VERIFY_LINE_EXACT.match(line)], [])
        good = _resolved_recipe('verify:\n\t@echo "==> terraform test"; $(MAKE) --no-print-directory terraform-test\n\n', "verify")
        self.assertTrue(_VERIFY_LINE_EXACT.match(good[0]))
        for line in ("\t-$(MAKE) --no-print-directory terraform-test", "\t$(MAKE) --no-print-directory terraform-test || true", "\t$(MAKE) --no-print-directory terraform-test; true", '\t@echo "==> terraform test"; $(MAKE) --no-print-directory terraform-test || true'):
            with self.subTest(line=line):
                self.assertIsNone(_VERIFY_LINE_EXACT.match(line))
        with self.assertRaises(AssertionError):
            _resolved_recipe("other:\n\t@echo x\n", "verify")
        # A rule with prerequisites, ordinary or order-only, is still found;
        # a target whose name merely starts the same is not.
        self.assertEqual(_resolved_recipe("verify-docs:\n\t@echo d\nverify: build | tools\n\t@echo v\n", "verify"), ["\t@echo v"])
        self.assertTrue(_IGNORE_SPECIAL_TARGET.search(".IGNORE: verify\n#  Phony target\n"))
        self.assertTrue(_IGNORE_SPECIAL_TARGET.search("\n.IGNORE:\n"))
        self.assertIsNone(_IGNORE_SPECIAL_TARGET.search("# .IGNORE: is not set\nverify:\n"))

    def test_a_prefix_with_blanks_still_carries_the_ignore_flag(self):
        for line in ('\t-@failed=""; for dir in x; do \\', '\t -@failed=""; for dir in x; do', '\t@ -failed=""; for dir in x; do'):
            with self.subTest(line=line):
                self.assertEqual(_ignored_recipe_lines([line]), [line])
        self.assertEqual(_ignored_recipe_lines(['\t@failed=""; for dir in x; do', "\t  terraform test; \\"]), [])

    def test_a_gate_step_that_cannot_fail_the_job_is_a_defect(self):
        good = {"steps": [{"name": "x", "run": "make terraform-test"}]}
        self.assertEqual(_gate_defects(good), [])
        self.assertEqual(_gate_defects(good, {"on": {"pull_request": {"types": ["opened"]}}, "env": {"CI": "1"}}), [])
        self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test", "continue-on-error": True}]}))
        self.assertTrue(_gate_defects({"continue-on-error": True, "steps": [{"run": "make terraform-test"}]}))
        self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test", "if": "false"}]}))
        self.assertTrue(_gate_defects({"if": "github.actor != 'dependabot[bot]'", "steps": [{"run": "make terraform-test"}]}))
        self.assertTrue(_gate_defects({"steps": [{"run": "echo make terraform-test"}]}))
        for env in ({"MAKEFLAGS": "-i"}, {"MAKEFLAGS": "--eval=.IGNORE:"}, {"MAKEFLAGS": "-E .IGNORE:"}, {"MAKEFLAGS": "n"}, {"MAKEFLAGS": "-f other.mk"}, {"GNUMAKEFLAGS": "-C bench"}, {"MAKEFLAGS": "-j2"}, {"MAKEFLAGS": ""}):
            with self.subTest(env=env):
                self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test", "env": env}]}))
                self.assertTrue(_gate_defects({"env": env, "steps": [{"run": "make terraform-test"}]}))
                self.assertTrue(_gate_defects(good, {"env": env}))
        self.assertEqual(_gate_defects({"steps": [{"run": "make terraform-test", "env": {"TF_PLUGIN_CACHE_DIR": "/x"}}]}), [])
        self.assertTrue(_gate_defects(good, {"on": {"pull_request": {"paths": ["terraform/**"]}}}))
        self.assertTrue(_gate_defects(good, {True: {"pull_request": {"paths-ignore": ["docs/**"]}}}))
        for triggers in (["pull_request", "push"], "pull_request", {"pull_request": None}):
            with self.subTest(triggers=triggers):
                self.assertEqual(_gate_defects(good, {True: triggers}), [])
        # An earlier step can set MAKEFLAGS for every later one.
        self.assertTrue(_gate_defects({"steps": [{"run": 'echo "MAKEFLAGS=-i" >> "$GITHUB_ENV"'}, {"run": "make terraform-test"}]}))
        self.assertTrue(_gate_defects({"steps": [{"run": "export GNUMAKEFLAGS=i\nmake terraform-test"}]}))
        self.assertEqual(_gate_defects({"steps": [{"run": 'echo "TF_PLUGIN_CACHE_DIR=$HOME/x" >> "$GITHUB_ENV"'}, {"run": "make terraform-test"}]}), [])
        # The run body is the command alone, under a fail-fast shell.
        self.assertTrue(_gate_defects({"steps": [{"run": "export MAKEFLAGS=-i\nmake terraform-test"}]}))
        self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test\ntrue"}]}))
        self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test", "shell": "bash {0}"}]}))
        self.assertTrue(_gate_defects({"defaults": {"run": {"shell": "bash {0}"}}, "steps": [{"run": "make terraform-test"}]}))
        self.assertTrue(_gate_defects(good, {"defaults": {"run": {"shell": "bash {0}"}}}))
        self.assertEqual(_gate_defects({"steps": [{"run": "make terraform-test", "shell": "bash"}]}), [])
        self.assertEqual(_gate_defects(good, {"defaults": {"run": {"shell": "bash"}}}), [])
        # The command runs at the repository root, whose Makefile is read.
        self.assertTrue(_gate_defects({"steps": [{"run": "make terraform-test", "working-directory": "k8s-operator"}]}))
        self.assertTrue(_gate_defects({"defaults": {"run": {"working-directory": "k8s-operator"}}, "steps": [{"run": "make terraform-test"}]}))
        self.assertTrue(_gate_defects(good, {"defaults": {"run": {"working-directory": "bench"}}}))
        self.assertEqual(_gate_defects({"steps": [{"run": "make terraform-test", "working-directory": "."}]}), [])



if __name__ == "__main__":
    unittest.main()
