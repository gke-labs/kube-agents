"""Third-party downloads retry, and the envtest fetch is cached (#1603).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

A CDN blip fails a pull request whose diff contains none of the code the job
builds: two jobs on #1562 went red four minutes apart on a 504 from GitHub's
release-asset CDN. Nothing about that failure is visible in a normal run, and
nothing about the fix is either -- dropping `--retry` from a curl line, or the
`sleep` from the retry loop, fails no build and reports green until the next bad
minute, by which time the connection to this change is lost.

So the loop is *run*, not read: `RetryLoopBehaviourTest` points the recipe at a
stub `setup-envtest` and counts invocations, which is what catches a backoff
deleted from the macro. Asserting the recipe text cannot -- a mutant with no
`sleep` still contains every other token. `scripts/test_ci_connectivity_retry.py`
is the same pattern.

The Dockerfile and shell guard is a pure function over text with its own
fixtures (`DownloadCheckerTest`), for the same reason: a walk that silently
matches nothing passes just as well as one that finds everything.
"""

import functools
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import run_make  # noqa: E402

REPO_ROOT = _HERE.parent
OPERATOR_DIR = REPO_ROOT / "k8s-operator"
OPERATOR_MAKEFILE = OPERATOR_DIR / "Makefile"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: The workflows that fetch the envtest binaries. Both must cache them: they
#: pull the same assets from the same CDN, and a cache in one leaves the other
#: exposed on every pull request that touches its filter paths.
ENVTEST_WORKFLOWS = ("k8s-operator-test.yml", "a2a-test.yml")

#: What the cache steps store. setup-envtest lays the binaries down in
#: <bin-dir>/k8s/<version>-<os>-<arch>; caching k8s-operator/bin wholesale would
#: also capture the go-installed tools, whose versions the key does not carry.
ENVTEST_CACHE_PATH = "k8s-operator/bin/k8s"

#: The Makefile target a workflow reads the pinned version from, so the literal
#: is declared once. A workflow that hardcodes the version instead drifts from
#: the Makefile silently. The drift is a permanent cache miss rather than wrong
#: binaries -- `use` selects by exact version out of the store, so the key only
#: decides hit or miss.
ENVTEST_VERSION_TARGET = "envtest-k8s-version"

#: The Makefile targets that fetch envtest binaries. Only envtest-path is on a
#: CI path -- both workflows and `make test` go through it -- while
#: setup-envtest is what a developer runs by hand, and the one in-repo caller
#: (the root coverage target's Go half) sits inside the branch CI skips with
#: COVERAGE_SKIP_GO=1. Both are here because a retry that covers only the
#: target CI happens to use leaves the other to fail on the next bad second.
ENVTEST_FETCH_TARGETS = ("envtest-path", "setup-envtest")

#: Build- and CI-time downloads live in these. `hack/` is here because
#: ci-env.sh fetches helm from the same host deploy/sandbox does, and a guard
#: that walks only the images cannot see it. Hand-maintained: a new file that
#: downloads something is not covered until it is added here, and nothing in
#: the repository notices that it is missing.
DOWNLOAD_SOURCES = (
    REPO_ROOT / "deploy" / "docker" / "Dockerfile",
    REPO_ROOT / "deploy" / "sandbox" / "Dockerfile",
    REPO_ROOT / "hack" / "ci-env.sh",
    WORKFLOWS_DIR / "validate.yml",
)

#: The start of a curl command. What follows it up to the next separator is
#: that command's own text, which is what the checks below read: a line in
#: these files is usually an `&&` chain of several commands, and reading the
#: whole line attributes one command's flags to another's.
CURL_COMMAND = re.compile(r"\bcurl\b")

#: The separators that end a curl command, longest first: matched the other way
#: round, `cmd || other` reads as a pipeline and exempts the fetch above it.
COMMAND_SEPARATORS = ("||", "&&", ";", "|")

#: The separator that makes a fetch a pipeline. curl retries a transfer by
#: restarting it, so on `curl ... | consumer` the consumer has already been fed
#: the bytes of the failed attempt; a piped download has to be rewritten to
#: fetch to a file before it can retry at all, which is a change of its own.
#: The guard exempts the shape rather than naming the lines -- the one in
#: DOWNLOAD_SOURCES today is the Cloud SDK apt key going into `gpg --dearmor`
#: -- and the cost is that a piped download added later is exempt silently.
PIPE = "|"

#: Quotes the separator scan honours. A `;` inside `-H "Accept: ...;q=0.9"` is
#: data, and reading it as a command boundary cuts the fetch off before its URL
#: -- which makes the fetch invisible rather than merely misread.
QUOTES = "\"'"

#: A line whose first non-blank character is this is commented out in both
#: Dockerfiles and in shell. Skipping it keeps a commented-out download from
#: failing the gate; the cost is that `RUN foo # curl https://...` is read
#: whole, which is harmless -- it can only add a check, never drop one.
COMMENT = "#"

#: A fetch over the network, as opposed to `curl --version` or an `apt-get
#: install curl`. Deliberately not a search for `-o`: `--output` is the same
#: flag spelled out and `>` redirects just as well, and a guard that greps for
#: one spelling passes the other two.
NETWORK_URL = re.compile(r"https?://")

#: An `echo "..."` and whatever follows it up to the next command separator, so
#: the redirect can be read off the tail. Quote-aware on purpose: the retry
#: message contains a semicolon of its own, and splitting the recipe on `;`
#: mistakes it for a command boundary.
QUOTED_ECHO = re.compile(r'echo\s+"(?:[^"\\]|\\.)*"(?P<tail>[^;]*)')

#: `--retry N`, with the count attached however curl accepts it. A substring
#: test for `--retry` cannot stand in: `--retry-all-errors` contains it, so a
#: line carrying only the second flag would read as retrying when curl does not
#: retry it at all. A literal count has to be non-zero -- `--retry 0` is the
#: default, and spelling out the default is not a retry -- while a count held
#: in a shell variable is taken on trust, since the walk cannot evaluate one
#: and hack/ci-env.sh names its retry count the way the Bash rule asks.
RETRY_COUNT = re.compile(r"--retry[ =](?:[1-9]|[\"']?\$)")

#: Required alongside the count. Plain `--retry` covers timeouts and 5xx but
#: not a connection reset mid-transfer, which is the other half of a bad minute
#: at a CDN, so a line with the count alone is still a line that fails a build.
RETRY_ALL_ERRORS = "--retry-all-errors"

#: A line the shell continues onto the next one.
CONTINUATION = "\\"

MAKE_TIMEOUT_SECONDS = 60

#: Attempts the behaviour tests ask the macro for. Small and explicit: the
#: assertion is that the recipe honours the number, not that the default is 4.
STUB_ATTEMPTS = 4

#: `make test`'s prerequisites, passed as `-o` so make treats them as already
#: up to date. Running them for real means regenerating manifests and sweeping
#: every Python test in the repository, which is minutes; the guard under test
#: is in the recipe, not in any of these.
TEST_PREREQUISITES = ("manifests", "generate", "fmt", "vet", "test-python")

#: Where the `go` stub records that it was asked to run tests. Read from the
#: environment by the stub rather than baked into it, so the script itself is
#: a constant.
GO_MARKER_VAR = "GO_TEST_MARKER"

#: Stands in for the Go toolchain for the length of one test. `go test` is the
#: line that must not be reached; `go list` feeds it the package list, and the
#: bare `go env` the Makefile runs while parsing has to answer something.
GO_STUB = """#!/bin/sh
case "$1" in
  test) echo ran >> "$%s" ;;
  list) echo ./... ;;
esac
exit 0
""" % GO_MARKER_VAR

#: The distinctive half of the assets guard's error in k8s-operator/Makefile.
#: Asserted on so a run that failed earlier cannot read as the guard firing.
GUARD_MESSAGE = "holds no kube-apiserver"

#: The variable k8s-operator/Makefile declares the pin in, and the declaration
#: itself. The workflows read it through the target rather than copying it.
ENVTEST_VERSION_VAR = "ENVTEST_K8S_VERSION"
ENVTEST_VERSION_DECLARATION = re.compile(r"^%s \?= (.+)$" % ENVTEST_VERSION_VAR, re.MULTILINE)


def _joined_commands(text):
    """(joined text, [(offset, line number)]) per backslash-continued line.

    A curl written across continuation lines is one command to the shell and to
    docker, but to a walk over physical lines it is a `curl` with no URL on it
    -- unseen rather than exempt, which is the failure mode that reports green.
    Continuation is the prevailing style in both Dockerfiles, so it is the
    natural way to write the next download.
    """
    buffer = ""
    marks = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.rstrip()
        if not buffer and stripped.lstrip().startswith(COMMENT):
            continue
        continued = stripped.endswith(CONTINUATION)
        marks.append((len(buffer), number))
        buffer += (stripped[: -len(CONTINUATION)] if continued else stripped) + " "
        if not continued:
            yield buffer, marks
            buffer, marks = "", []
    if buffer:
        yield buffer, marks


def _line_of(marks, offset):
    number = marks[0][1]
    for start, line_number in marks:
        if start > offset:
            break
        number = line_number
    return number


def _next_separator(text):
    """(offset, separator) of the first separator outside quotes, or None.

    A character scan rather than a regex because the regex cannot know whether
    the `;` it found is a command boundary or part of a quoted header value.
    """
    quote = None
    index = 0
    while index < len(text):
        character = text[index]
        if quote:
            if character == quote:
                quote = None
        elif character in QUOTES:
            quote = character
        else:
            for separator in COMMAND_SEPARATORS:
                if text.startswith(separator, index):
                    return index, separator
        index += 1
    return None


def network_fetches(text):
    """(line number, command text, piped) for every curl that fetches.

    Split out so the floor test can count what the walk sees: a checker that
    silently matches nothing reports success exactly as a working one does.
    """
    for command, marks in _joined_commands(text):
        for match in CURL_COMMAND.finditer(command):
            rest = command[match.end() :]
            separator = _next_separator(rest)
            own_text = rest[: separator[0]] if separator else rest
            if not NETWORK_URL.search(own_text):
                continue
            piped = separator is not None and separator[1] == PIPE
            yield _line_of(marks, match.start()), own_text, piped


def unretried_downloads(text):
    """Line numbers of curl fetches over the network that do not retry.

    A pure function so the fixtures below can exercise it against text the
    repository does not contain. Piped fetches are excluded, per PIPE; a fetch
    needs both RETRY_COUNT and RETRY_ALL_ERRORS to pass.
    """
    return [
        number
        for number, own_text, piped in network_fetches(text)
        if not piped and (not RETRY_COUNT.search(own_text) or RETRY_ALL_ERRORS not in own_text)
    ]


def _make(args, cwd=OPERATOR_DIR, env=None, extra_env=None):
    """Run make in k8s-operator/ and return the CompletedProcess.

    `env` becomes `VAR=value` arguments, which is how you override a Makefile
    variable; `extra_env` goes into the process environment, which is how you
    shadow a binary the recipe calls. Wraps tests/_run_make.py rather than
    reimplementing the MAKEFLAGS and MAKELEVEL scrubbing, which that module
    owns.
    """
    if env:
        args = [*args, *(f"{k}={v}" for k, v in env.items())]
    return run_make(list(args), timeout=MAKE_TIMEOUT_SECONDS, cwd=cwd, extra_env=extra_env)


def _make_ok(args, **kwargs):
    result = _make(args, **kwargs)
    if result.returncode != 0:
        raise AssertionError(
            "make %s failed (%d):\n%s" % (" ".join(args), result.returncode, result.stderr)
        )
    return result.stdout


@functools.lru_cache(maxsize=None)
def _recipe(target):
    """The shell `make` would run for a target, without running it.

    `-n` prints the recipe rather than running it, so this reaches no network.
    Memoised because each `make` here costs about a second of Makefile parsing
    and several assertions want the same recipe; the behaviour tests above are
    deliberately not memoised, since they run the recipe for its side effects.
    """
    return _make_ok(["-n", target])


def _declared_version():
    """The envtest pin, read from the one file entitled to declare it."""
    declared = ENVTEST_VERSION_DECLARATION.search(OPERATOR_MAKEFILE.read_text())
    if not declared:
        raise AssertionError(
            "k8s-operator/Makefile must declare %s; without it there is no pin to "
            "compare anything against" % ENVTEST_VERSION_VAR
        )
    return declared.group(1).strip()


def _makefile_recipe(target):
    """The recipe lines the Makefile declares for a target, read from the file.

    `_recipe` above cannot stand in for every target: `make -n` runs a recipe
    line that holds $(MAKE) rather than printing it, so asking it for `test`
    executes the sub-make and then dies on the assets guard, printing an error
    where the recipe should be.
    """
    lines = OPERATOR_MAKEFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("%s:" % target))
    recipe = []
    for line in lines[start + 1 :]:
        if line.startswith("\t"):
            recipe.append(line)
        elif line.startswith("#") or not line.strip():
            continue
        else:
            break
    if not recipe:
        raise AssertionError("no recipe found for the %s target" % target)
    return "\n".join(recipe)


def _load_workflow(name):
    return yaml.safe_load((WORKFLOWS_DIR / name).read_text())


def _steps(workflow):
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            yield step


def _cache_steps(workflow, action):
    return [
        step
        for step in _steps(workflow)
        if str(step.get("uses", "")).startswith("actions/cache/%s@" % action)
        and step.get("with", {}).get("path") == ENVTEST_CACHE_PATH
    ]


class RetryLoopBehaviourTest(unittest.TestCase):
    """The recipe is executed against a stub, not read.

    `ENVTEST=<stub>` substitutes the binary the macro calls, so these run the
    real recipe out of the real Makefile with no network and no envtest.

    The stub is laid out the way go-install-tool leaves a tool it has already
    installed: the script in `<name>-<version>`, with `<name>` a symlink to it.
    That is exactly what the guard at the top of that macro tests for, so the
    rule building `$(ENVTEST)` finds nothing to do. Handed a bare file instead,
    the guard fails, and on a fresh checkout -- where `$(LOCALBIN)` is created
    in the same run and is therefore newer than the stub, so the rule does
    fire -- the recipe deletes the stub and tries to `go install` over it.
    Paths are resolved because the recipe rewrites the symlink through
    `realpath`, and an unresolved one would not match on the next run.
    """

    #: Substituted for ENVTEST_VERSION so the fixture does not have to track
    #: the real pin; only the `<name>-<version>` spelling has to agree.
    STUB_VERSION = "test-stub"

    def setUp(self):
        # mkdtemp rather than a name built from the pid: the teardown below is
        # then a single rmtree, and a test that adds a file to the fixture
        # cannot turn its own failure into a "Directory not empty" on the way
        # out. Resolved because the recipe rewrites the stub symlink through
        # `realpath`, and an unresolved path would not match on the next run.
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="envtest-retry-")).resolve()
        self.stub = self.tmp / "setup-envtest-stub"
        self.versioned = self.tmp / ("setup-envtest-stub-%s" % self.STUB_VERSION)
        self.calls = self.tmp / "calls"
        self.calls.write_text("")
        self.go_marker = self.tmp / "go-test-ran"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _executable(self, path, script):
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _write_stub(self, script):
        self._executable(self.versioned, script)
        self.stub.unlink(missing_ok=True)
        self.stub.symlink_to(self.versioned)

    def _write_go_stub(self):
        """Put a fake `go` ahead of the real one and return the PATH to use."""
        self._executable(self.tmp / "go", GO_STUB)
        return "%s%s%s" % (self.tmp, os.pathsep, os.environ["PATH"])

    def _run(self, attempts=STUB_ATTEMPTS, delay=0):
        return _make(
            ["-s", "envtest-path"],
            env={
                "ENVTEST": str(self.stub),
                "ENVTEST_VERSION": self.STUB_VERSION,
                "ENVTEST_DOWNLOAD_ATTEMPTS": attempts,
                "ENVTEST_RETRY_DELAY_SECONDS": delay,
            },
        )

    def _call_count(self):
        return len(self.calls.read_text().splitlines())

    def test_a_failing_fetch_is_attempted_the_configured_number_of_times(self):
        self._write_stub(
            '#!/bin/sh\necho call >> "%s"\necho "504 from the CDN" >&2\nexit 1\n' % self.calls,
        )
        result = self._run()
        self.assertEqual(
            self._call_count(),
            STUB_ATTEMPTS,
            "the recipe should have retried up to the ceiling, stderr was:\n%s" % result.stderr,
        )
        self.assertNotEqual(result.returncode, 0, "exhausting the attempts must fail the target")
        self.assertEqual(result.stdout, "", "a failed fetch must put nothing on stdout")

    def test_it_stops_at_the_first_success_and_prints_only_the_path(self):
        """Two failures then a success: the path reaches stdout uncontaminated.

        a2a-test.yml captures this target's stdout with
        `ASSETS="$(make -C k8s-operator -s envtest-path)"` and then checks that
        `$ASSETS/kube-apiserver` is executable. A retry notice on stdout would
        land inside `$ASSETS` and fail that guard on a run that had recovered --
        turning a transient blip into the same red check this change exists to
        prevent, one step further along.
        """
        self._write_stub(
            "#!/bin/sh\n"
            'echo call >> "%s"\n'
            'if [ "$(wc -l < "%s")" -lt 3 ]; then\n'
            '  echo "504 from the CDN" >&2\n'
            "  exit 1\n"
            "fi\n"
            "echo /the/assets/path\n" % (self.calls, self.calls),
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._call_count(), 3, "it should stop at the first success")
        self.assertEqual(
            result.stdout.strip(),
            "/the/assets/path",
            "stdout must carry the assets path and nothing else; got %r" % result.stdout,
        )

    def test_it_waits_between_attempts(self):
        """The backoff, which asserting on recipe text cannot see.

        Two attempts and a one-second base delay: one sleep, so the floor is
        real without making the suite wait. A macro with the sleep deleted
        returns in milliseconds and fails here.
        """
        self._write_stub('#!/bin/sh\necho call >> "%s"\nexit 1\n' % self.calls)
        started = time.monotonic()
        self._run(attempts=2, delay=1)
        elapsed = time.monotonic() - started
        self.assertEqual(self._call_count(), 2)
        self.assertGreaterEqual(
            elapsed,
            1.0,
            "two attempts with a 1s base delay must sleep at least once; "
            "took %.2fs, so the backoff is not happening" % elapsed,
        )

    def test_an_empty_ceiling_fails_instead_of_looping(self):
        """An empty tunable is a stop, not an unbounded retry.

        `?=` does not defend against one: an exported empty variable, or
        `make ... ENVTEST_DOWNLOAD_ATTEMPTS=`, reaches the recipe. Without the
        guard `[ "$attempt" -ge "" ]` errors, `if` reads the error as false, the
        ceiling never fires, and the loop retries with a doubling delay until
        something outside it gives up.
        """
        self._write_stub('#!/bin/sh\necho call >> "%s"\nexit 1\n' % self.calls)
        result = self._run(attempts="", delay=0)
        self.assertNotEqual(result.returncode, 0, "an empty ceiling must fail the target")
        self.assertEqual(
            self._call_count(), 0, "it must refuse before fetching, not after an attempt"
        )

    def test_the_test_target_stops_when_the_assets_do_not_resolve(self):
        """`make test` must fail, not skip, when the fetch yields no binaries.

        This is the mode the whole change exists to prevent, one step along:
        `KUBEBUILDER_ASSETS` empty makes every envtest-backed test `t.Skip`
        (internal/controller/platformagent_observed_generation_envtest_test.go
        and platformagent_a2a_callout_rbac_test.go both key off the variable
        being unset), so `go test` reports success over a suite that did not
        run. Only the guard in the recipe turns that into a red build, and the
        rest of this file asserts the guard's *text* -- delete the `exit 1` and
        every one of those assertions still passes.

        A `go` on PATH ahead of the real one records whether the guarded line
        was reached, so the assertion does not rest on what the Go suite would
        have exited with. The prerequisites are marked old because the guard is
        in the recipe; running them would cost minutes and prove nothing.
        """
        self._write_stub("#!/bin/sh\nexit 0\n")  # succeeds, prints no path
        path = self._write_go_stub()
        old = [flag for target in TEST_PREREQUISITES for flag in ("-o", target)]
        result = _make(
            ["-s", *old, "test"],
            env={
                "ENVTEST": str(self.stub),
                "ENVTEST_VERSION": self.STUB_VERSION,
                "ENVTEST_DOWNLOAD_ATTEMPTS": 1,
                "ENVTEST_RETRY_DELAY_SECONDS": 0,
            },
            extra_env={"PATH": path, GO_MARKER_VAR: str(self.go_marker)},
        )
        self.assertFalse(
            self.go_marker.exists(),
            "the tests ran with no assets resolved, which skips them and reports "
            "success; the recipe must stop at the guard instead",
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "unresolvable assets must fail the target, stderr was:\n%s" % result.stderr,
        )
        # Both assertions above hold just as well when the recipe dies before it
        # reaches the guard -- an unmirrored addition to TEST_PREREQUISITES, or
        # anything broken in envtest-path -- and the test would then pass while
        # testing nothing. The guard owns this sentence, so requiring it pins
        # which failure happened.
        self.assertIn(
            GUARD_MESSAGE,
            result.stderr,
            "the target failed somewhere other than the assets guard, so this test "
            "no longer reaches what it is aiming at; stderr was:\n%s" % result.stderr,
        )


class EnvtestFetchWiringTest(unittest.TestCase):
    """Every fetch in the Makefile goes through the retrying macro."""

    def test_both_targets_use_the_macro(self):
        for target in ENVTEST_FETCH_TARGETS:
            with self.subTest(target=target):
                recipe = _recipe(target)
                self.assertIn(
                    "setup-envtest use",
                    recipe,
                    "%s no longer fetches envtest; this test is watching the wrong target"
                    % target,
                )
                self.assertRegex(
                    recipe,
                    r"while\s+:;\s*do",
                    "%s must retry the fetch, not fail on the first error" % target,
                )

    def test_the_ceiling_is_overridable(self):
        self.assertIn(
            'attempts="4"', _recipe("envtest-path"), "the default ceiling should reach the recipe"
        )
        self.assertIn(
            'attempts="9"',
            _make_ok(["-n", "envtest-path", "ENVTEST_DOWNLOAD_ATTEMPTS=9"]),
            "ENVTEST_DOWNLOAD_ATTEMPTS must be overridable, so a caller can shorten "
            "the wait without editing the Makefile",
        )

    def test_the_test_target_resolves_the_assets_safely(self):
        """`make test` is what CI runs, and it used to call `use` itself.

        A third call site is a fetch that does not retry. It is also a fetch
        whose failure goes unnoticed: a command substitution resolved by
        `$(shell ...)`, or written as a prefix assignment on the `go test` line,
        has its exit status discarded either way, so an exhausted retry leaves
        KUBEBUILDER_ASSETS empty -- and every envtest-backed test then skips
        while the target reports success.

        Read rather than run: exercising this target means running the
        operator's Go suite, and `make -n` is no help either, because a recipe
        line holding $(MAKE) is executed even under -n -- it runs the sub-make
        and then fails the guard rather than printing anything to assert on.
        """
        recipe = _makefile_recipe("test")
        self.assertNotIn(
            "$(shell",
            recipe,
            "the test target must not resolve the assets with $(shell ...), which "
            "discards the exit status",
        )
        self.assertIn("envtest-path", recipe, "the test target must go through envtest-path")
        self.assertNotRegex(
            recipe,
            r"KUBEBUILDER_ASSETS=\"\$\$\(.*go test",
            "resolving the path in a prefix assignment on the go test line discards "
            "the exit status as surely as $(shell ...) does; assign on a line of its own",
        )
        self.assertRegex(
            recipe,
            r"-x \"\$\$assets/kube-apiserver\"",
            "the resolved path must be checked for the binary it is supposed to "
            "hold: a setup-envtest that fails after printing nothing leaves it "
            "empty, and a stray line of make output leaves it non-empty and wrong",
        )
        self.assertIn(
            "--no-print-directory",
            recipe,
            "-s hides only the directory banners -C turns on implicitly; an "
            "explicit -w inherited through MAKEFLAGS puts them on the sub-make's "
            "stdout and into the captured path",
        )

    def test_the_recipe_keeps_stdout_clean(self):
        """Static half of the stdout contract the behaviour test exercises."""
        recipe = _recipe("envtest-path")
        echoes = list(QUOTED_ECHO.finditer(recipe))
        self.assertEqual(
            len(echoes),
            recipe.count("echo "),
            "every echo in the recipe must be the quoted form this test can read "
            "the redirect of; an unquoted one would be skipped silently",
        )
        for echo in echoes:
            with self.subTest(echo=echo.group(0)):
                self.assertIn(">&2", echo.group("tail"))


class EnvtestVersionIsDeclaredOnceTest(unittest.TestCase):
    """The workflow cache key reads the version rather than copying it."""

    def test_the_target_prints_the_makefile_value(self):
        printed = _make_ok(["-s", ENVTEST_VERSION_TARGET]).strip()
        self.assertEqual(
            printed,
            _declared_version(),
            "%s must print the version the Makefile declares, or the workflows key "
            "the cache on a version the tests never use" % ENVTEST_VERSION_TARGET,
        )
        self.assertEqual(len(printed.splitlines()), 1, "the workflows interpolate this into a key")


class EnvtestCacheTest(unittest.TestCase):
    """Restored everywhere, written only on main."""

    def test_both_workflows_restore_the_cache(self):
        for name in ENVTEST_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertEqual(
                    len(_cache_steps(_load_workflow(name), "restore")),
                    1,
                    "%s must restore %s; without it every run of every pull request "
                    "re-fetches the tarball" % (name, ENVTEST_CACHE_PATH),
                )

    def test_saving_is_confined_to_main(self):
        """A pull-request ref that writes is invisible to other refs and still
        charges the one repository cache budget, which is over its limit."""
        for name in ENVTEST_WORKFLOWS:
            with self.subTest(workflow=name):
                saves = _cache_steps(_load_workflow(name), "save")
                self.assertEqual(len(saves), 1, "%s must have exactly one save step" % name)
                condition = saves[0]["if"]
                self.assertIn("github.ref == 'refs/heads/main'", condition)
                self.assertIn("github.event_name == 'push'", condition)
                self.assertIn(
                    "cache-hit != 'true'",
                    condition,
                    "%s re-uploads the entry it just restored without this" % name,
                )

    def test_the_two_workflows_agree_on_one_key(self):
        keys = set()
        for name in ENVTEST_WORKFLOWS:
            workflow = _load_workflow(name)
            for action in ("restore", "save"):
                for step in _cache_steps(workflow, action):
                    keys.add(re.sub(r"steps\.[\w-]+\.outputs", "steps.OUTPUT", step["with"]["key"]))
        self.assertEqual(
            len(keys),
            1,
            "the jobs must share one key so one entry serves both; got %r" % sorted(keys),
        )

    def test_the_key_is_derived_from_the_makefile_not_copied(self):
        for name in ENVTEST_WORKFLOWS:
            with self.subTest(workflow=name):
                steps = list(_steps(_load_workflow(name)))
                resolvers = [
                    step
                    for step in steps
                    if ENVTEST_VERSION_TARGET in str(step.get("run", ""))
                    and "GITHUB_OUTPUT" in str(step.get("run", ""))
                ]
                self.assertEqual(
                    len(resolvers),
                    1,
                    "%s must read the version from `make -s %s`" % (name, ENVTEST_VERSION_TARGET),
                )
                run = resolvers[0]["run"]
                self.assertNotRegex(
                    run,
                    r'echo\s+"[^"]*\$\(make',
                    "a failing make inside echo's argument leaves the step green and the "
                    "key version-less; assign first, then echo",
                )
                self.assertIn(
                    "steps.%s.outputs" % resolvers[0]["id"],
                    _cache_steps(_load_workflow(name), "restore")[0]["with"]["key"],
                )

    def test_the_restore_runs_before_the_fetch(self):
        """A restore placed after the fetch restores nothing in time."""
        for name in ENVTEST_WORKFLOWS:
            with self.subTest(workflow=name):
                steps = list(_steps(_load_workflow(name)))
                restore_at = next(
                    i
                    for i, step in enumerate(steps)
                    if str(step.get("uses", "")).startswith("actions/cache/restore@")
                )
                fetch_at = next(
                    i
                    for i, step in enumerate(steps)
                    if re.search(r"make .*(test|envtest-path)", str(step.get("run", "")))
                    and ENVTEST_VERSION_TARGET not in str(step.get("run", ""))
                )
                self.assertLess(restore_at, fetch_at, "%s restores the cache too late" % name)


class DownloadCheckerTest(unittest.TestCase):
    """The guard itself, against text the repository does not contain.

    Without these, a checker that silently matches nothing passes exactly as a
    working one does.
    """

    def test_it_catches_every_spelling_of_writing_to_a_file(self):
        planted = "\n".join(
            [
                'RUN curl -fL -o /tmp/a.tgz "https://example.invalid/a.tgz"',
                'RUN curl -fL --output /tmp/b.tgz "https://example.invalid/b.tgz"',
                'RUN curl -fL "https://example.invalid/c.tgz" > /tmp/c.tgz',
            ]
        )
        self.assertEqual(unretried_downloads(planted), [1, 2, 3])

    def test_it_passes_a_retried_download(self):
        self.assertEqual(
            unretried_downloads(
                'RUN curl -fL --retry 5 --retry-all-errors -o /tmp/a.tgz "https://example.invalid/a.tgz"'
            ),
            [],
        )

    def test_it_catches_a_half_retried_download(self):
        """Each flag alone, since each leaves a real failure unretried.

        A count with no `--retry-all-errors` does not survive a connection
        reset, and `--retry-all-errors` with no count retries nothing -- and
        the second is invisible to a substring test for `--retry`, which its
        own spelling satisfies.
        """
        planted = "\n".join(
            [
                'RUN curl -fL --retry 5 -o /tmp/a.tgz "https://example.invalid/a.tgz"',
                'RUN curl -fL --retry-all-errors -o /tmp/b.tgz "https://example.invalid/b.tgz"',
            ]
        )
        self.assertEqual(unretried_downloads(planted), [1, 2])

    def test_it_exempts_a_piped_download(self):
        self.assertEqual(
            unretried_downloads(
                "RUN curl -sSL https://example.invalid/key.gpg | gpg --dearmor -o /etc/k.gpg"
            ),
            [],
        )

    def test_it_accepts_a_retry_count_held_in_a_variable(self):
        """What naming the constant looks like, which ci-env.sh does."""
        self.assertEqual(
            unretried_downloads(
                'curl -fsSL --retry "$HELM_DOWNLOAD_RETRIES" --retry-all-errors '
                '"https://example.invalid/a.tgz" -o /tmp/a.tgz'
            ),
            [],
        )

    def test_it_catches_a_retry_count_of_zero(self):
        """`--retry 0` is curl's default, so it is the flag without the retry."""
        self.assertEqual(
            unretried_downloads(
                'RUN curl -fL --retry 0 --retry-all-errors -o /tmp/a.tgz "https://example.invalid/a"'
            ),
            [1],
        )

    def test_a_separator_inside_quotes_does_not_end_the_command(self):
        """A `;` in a header value is data, not a command boundary.

        Read as a boundary it cuts the command off before its URL, and the
        fetch disappears from the walk entirely -- which is worse than being
        misread, because nothing reports it.
        """
        self.assertEqual(
            unretried_downloads(
                'RUN curl -fL -H "Accept: application/json;q=0.9" '
                '-o /tmp/a.json "https://example.invalid/a"'
            ),
            [1],
        )

    def test_it_ignores_a_commented_out_download(self):
        """A commented-out fetch is not a fetch, and failing on one is noise."""
        self.assertEqual(
            unretried_downloads(
                '# RUN curl -fL -o /tmp/a.tgz "https://example.invalid/a.tgz"\n'
                '  # curl -fL "https://example.invalid/b.tgz" > /tmp/b.tgz'
            ),
            [],
        )

    def test_it_does_not_mistake_a_nearby_bar_for_a_pipeline(self):
        """The two shapes that read as a pipe but consume nothing curl wrote.

        `||` is an or-list, and a pipe further along an `&&` chain belongs to
        the command it follows. Both leave the fetch's own bytes in a file, so
        both can retry -- and the `&&` chain is the house style in these files,
        which makes waving one through the way the guard quietly stops working.
        """
        planted = "\n".join(
            [
                'RUN curl -fL -o /tmp/a.tgz "https://example.invalid/a.tgz" || exit 1',
                'RUN curl -fL -o /tmp/b.tgz "https://example.invalid/b.tgz" && '
                "sha256sum /tmp/b.tgz | tee /tmp/b.sha",
            ]
        )
        self.assertEqual(unretried_downloads(planted), [1, 2])

    def test_it_reads_a_fetch_written_across_continuations(self):
        """One command to the shell must be one command to the guard.

        Split over continuation lines, no physical line holds both `curl` and
        the URL, so a line-based walk sees no fetch at all -- and reports the
        file clean rather than reporting the fetch.
        """
        planted = "\n".join(
            [
                "RUN curl -fL \\",
                "    -o /tmp/a.tgz \\",
                '    "https://example.invalid/a.tgz"',
            ]
        )
        self.assertEqual(unretried_downloads(planted), [1])

    def test_it_checks_each_command_in_a_chain_on_its_own(self):
        """A retry on one command is not a retry on the next.

        Both Dockerfiles install several tools in one `&&` chain, so reading
        the chain whole lets the first fetch's flags cover a later one that has
        none.
        """
        planted = (
            'RUN curl -fL --retry 5 --retry-all-errors -o /tmp/a.tgz "https://example.invalid/a" && \\\n'
            '    curl -fL -o /tmp/b.tgz "https://example.invalid/b"'
        )
        self.assertEqual(unretried_downloads(planted), [2])

    def test_it_ignores_lines_that_fetch_nothing(self):
        self.assertEqual(unretried_downloads("RUN apt-get install -y curl\nRUN echo https://x"), [])


class ShippedDownloadsRetryTest(unittest.TestCase):
    """Every download in DOWNLOAD_SOURCES that writes to a file retries."""

    def test_no_unretried_downloads(self):
        for source in DOWNLOAD_SOURCES:
            with self.subTest(source=source.name):
                unretried = unretried_downloads(source.read_text())
                self.assertEqual(
                    unretried,
                    [],
                    "%s downloads without a retry at line(s) %s, so a CDN blip fails "
                    "the build or the job (#1603)" % (source, unretried),
                )

    def test_the_walk_actually_reaches_the_downloads(self):
        """A floor, so a walk that matches nothing cannot report success."""
        fetches = [
            fetch for source in DOWNLOAD_SOURCES for fetch in network_fetches(source.read_text())
        ]
        self.assertGreaterEqual(
            len(fetches),
            5,
            "expected at least the apt key, gh, yq, helm and ci-env helm fetches; a "
            "walk finding fewer is matching the wrong shape",
        )
        exempt = [number for number, _, piped in fetches if piped]
        self.assertEqual(
            len(exempt),
            1,
            "exactly one fetch is exempt as a pipeline today (the Cloud SDK apt key); "
            "%s are, so either a download needs rewriting to fetch to a file first or "
            "the separator scan is reading a shape wrong" % (exempt,),
        )


if __name__ == "__main__":
    unittest.main()
