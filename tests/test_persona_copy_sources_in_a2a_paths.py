"""Every script the cluster persona copies has its source in the A2A job's path lists.

`TestTheClusterPersonaSkillFilesMatchTheirSources` (a2a/lib/persona/claudecode)
holds each copied script to its source, named in `scriptSources`. It runs only
when `a2a-test.yml` runs. A source missing from the workflow's push `paths:`
let a change to that source alone skip the comparison and reach main with a
stale copy (#6661). This holds every `scriptSources` source to the push list;
`test_push_paths_match_change_filter.py` holds the push list to the filter.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PERSONA_TEST = REPO / "a2a/lib/persona/claudecode/cluster_persona_test.go"
WORKFLOW = REPO / ".github/workflows/a2a-test.yml"
# The test's paths are relative to its own package directory.
_PREFIX = "../../../../"
# Each value in the scriptSources map: a quoted path under agents/.
_SOURCE = re.compile(r'"' + re.escape(_PREFIX) + r'(agents/[^"]+)"')
# stall_report.py, audit_cluster.sh, workload_triage.py and vcs.py; a read that
# found fewer saw too little.
_SOURCES_FLOOR = 4


def _script_sources():
    text = PERSONA_TEST.read_text()
    start = text.index("var scriptSources = map[string]string{")
    end = text.index("\n}", start)
    return _SOURCE.findall(text[start:end])


def _push_paths():
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    on = doc.get("on", doc.get(True))
    return on["push"]["paths"]


def _covered(path, entries):
    for entry in entries:
        if entry == path:
            return True
        if entry.endswith("/**") and path.startswith(entry[: -len("**")]):
            return True
    return False


class PersonaCopySourcesInA2APathsTest(unittest.TestCase):
    def test_every_copied_scripts_source_runs_the_a2a_job(self):
        sources = _script_sources()
        self.assertGreaterEqual(
            len(sources), _SOURCES_FLOOR, f"read {sources} from scriptSources; the regex saw too little"
        )
        paths = _push_paths()
        missing = [s for s in sources if not _covered(s, paths)]
        self.assertEqual(
            missing,
            [],
            "scriptSources names these sources, which a2a-test.yml's push paths and a2a filter "
            "don't list, so a change to one alone skips the copy comparison: add each to both",
        )


FLEET_AUDIT_TEST = REPO / "a2a/lib/persona/claudecode/fleet_audit_persona_test.go"


class FleetAuditPersonaCopySourcesInA2APathsTest(unittest.TestCase):
    """The same for the fleet-audit persona's copies (fleetAuditVendored), and
    the persona's own directory, which its tests read too."""

    def test_every_copied_source_and_the_persona_run_the_a2a_job(self):
        text = FLEET_AUDIT_TEST.read_text()
        start = text.index("var fleetAuditVendored = map[string]string{")
        sources = _SOURCE.findall(text[start : text.index("\n}", start)])
        self.assertTrue(sources, "read nothing from fleetAuditVendored; the regex saw too little")
        paths = _push_paths()
        missing = [s for s in sources + ["agents/fleet-audit/persona/persona.yaml"] if not _covered(s, paths)]
        self.assertEqual(missing, [], "add each to a2a-test.yml's push paths and its a2a filter")


if __name__ == "__main__":
    unittest.main()
