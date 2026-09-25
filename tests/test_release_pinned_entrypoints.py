"""Every documented way in has to name a release.

`install.sh`, `upgrade.sh` and `uninstall.sh` carry the version they belong to:
release automation stamps `BAKED_RELEASE_VERSION` when it publishes a release,
and the scripts read it to decide which chart, which CRDs and which teardown
engine to use. A copy that carries no version has no default, and each script
then falls back to something the install never asked for — `main`'s engine, or a
refusal that reads like a missing flag.

GitHub Pages serves copies built from `main`, so they are exactly those
unstamped copies. Documentation and agent skills therefore point at release
artifacts (`raw.githubusercontent.com/.../<RELEASE_VERSION>/...` or the release
bundle) rather than at the Pages copies, and this test keeps it that way.
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The three front doors served unstamped from GitHub Pages.
_PAGES_SCRIPT_URL = re.compile(r"gke-labs\.github\.io/kube-agents/(?:install|upgrade|uninstall)\.sh")
# The same problem by another route: a raw/blob URL pinned to a moving branch.
_MOVING_RAW_URL = re.compile(
    r"(?:raw\.githubusercontent\.com/gke-labs/kube-agents/(?:refs/heads/)?(?:main|HEAD)/|"
    r"github\.com/gke-labs/kube-agents/(?:raw/(?:refs/heads/)?(?:main|HEAD)/|blob/(?:refs/heads/)?(?:main|HEAD)/(?:install|upgrade|uninstall)\.sh))"
)

# Where a reader or an agent is told how to run something. The three scripts are
# included because their own header comments are the first thing a reader sees,
# and `.github/` because a workflow, an issue template or a release note names
# these URLs to exactly the same effect as a page does.
_SEARCHED_PREFIXES = (
    "docs/",
    ".agents/",
    ".github/",
    "agents/",
    "charts/",
    "terraform/",
)
_SEARCHED_FILES = (
    "README.md",
    "INSTALL.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "install.sh",
    "upgrade.sh",
    "uninstall.sh",
)
_SEARCHED_SUFFIXES = (".md", ".mdx", ".sh", ".yml", ".yaml")


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(_REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [pathlib.PurePosixPath(p) for p in out.split("\0") if p]


def _files_to_scan():
    selected = []
    for path in _tracked_files():
        posix = path.as_posix()
        if posix in _SEARCHED_FILES:
            selected.append(path)
            continue
        if posix.startswith(_SEARCHED_PREFIXES) and path.suffix in _SEARCHED_SUFFIXES:
            selected.append(path)
    return selected


class ReleasePinnedEntryPointsTest(unittest.TestCase):
    def setUp(self):
        self.files = _files_to_scan()
        # Fail closed: a rename that empties the selection would otherwise turn
        # this test into one that always passes.
        self.assertGreater(len(self.files), 50, "the file selection collapsed; the patterns above are stale")

    def _offenders(self, pattern):
        found = []
        for path in self.files:
            text = (_REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    found.append(f"{path}:{number}: {line.strip()}")
        return found

    def test_no_document_points_at_the_pages_copies(self):
        """The Pages copies are built from main and carry no baked version."""
        offenders = self._offenders(_PAGES_SCRIPT_URL)
        self.assertEqual(
            offenders,
            [],
            "point these at a release artifact instead:\n" + "\n".join(offenders),
        )

    def test_no_document_fetches_a_script_from_a_moving_branch(self):
        """A raw URL on main is the same unstamped copy by another route."""
        offenders = self._offenders(_MOVING_RAW_URL)
        self.assertEqual(
            offenders,
            [],
            "pin these to a release tag instead:\n" + "\n".join(offenders),
        )

    def test_the_feedback_link_is_untouched(self):
        """The Pages site itself is fine; only the scripts served from it are not.

        Guards the patterns above against being widened into something that also
        rejects the published site, which several agent prompts link to.
        """
        for legitimate in (
            "https://gke-labs.github.io/kube-agents/feedback",
            "https://gke-labs.github.io/kube-agents/install/upgrade/",
        ):
            with self.subTest(url=legitimate):
                self.assertIsNone(_PAGES_SCRIPT_URL.search(legitimate))
                self.assertIsNone(_MOVING_RAW_URL.search(legitimate))


if __name__ == "__main__":
    unittest.main()
