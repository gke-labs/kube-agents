"""A scripted stand-in for ``devops-bench``, for the ``bench-run`` tests.

``bench-run`` starts one ``devops-bench`` subprocess per repetition; pointing
``BENCH_RUN_COMMAND`` at this script lets the tests exercise the whole loop --
scheduling, locks, run-directory discovery, grading, the summary -- in
seconds, with no agent and no judge.

The script honours the three flags the runner passes (``--results-root``,
``--run-id``, ``--agent-type``) and decides each repetition's outcome from
``FAKE_OUTCOMES``: a JSON object mapping a case id to a list of outcomes by
repetition (``pass``, ``fail``, ``missing``), the last entry repeating. A
``pass`` copies the captured ``kanban_green_1`` fixture into a fresh run
directory, a ``fail`` copies ``kanban_red_1``, and ``missing`` writes nothing,
which the scorer classifies as a harness death. ``FAKE_SLEEP_S`` adds a delay
so the tests can observe concurrency; ``FAKE_TRACE`` names a file the script
appends ``start``/``end`` lines to for the same purpose.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "runs"
FIXTURE_BY_OUTCOME = {"pass": "kanban_green_1", "fail": "kanban_red_1"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--agent-type")
    parser.add_argument("--results-root", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    case_id = Path(args.task).parent.name
    rep = int(args.run_id.rsplit("rep", 1)[-1])
    outcomes = json.loads(os.environ.get("FAKE_OUTCOMES", "{}")).get(case_id, ["pass"])
    outcome = outcomes[min(rep, len(outcomes)) - 1]

    trace = os.environ.get("FAKE_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(
                f"start {case_id} {rep} {time.time():.3f} port={os.environ.get('AGENT_LOCAL_PORT')} "
                f"delegation={os.environ.get('AGENT_DELEGATION_TIMEOUT')}\n"
            )
    time.sleep(float(os.environ.get("FAKE_SLEEP_S", "0")))

    if outcome in FIXTURE_BY_OUTCOME:
        # The pinned reporter's shape: run_<UTC stamp>_<sanitised run id>.
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        run_dir = Path(args.results_root) / f"run_{stamp}_{args.run_id}"
        shutil.copytree(FIXTURES / FIXTURE_BY_OUTCOME[outcome], run_dir)
        print(f"ran 1 task(s), 0 failed; results: {run_dir / 'results.json'}")
    else:
        print("fake devops-bench: died before writing anything", file=sys.stderr)

    if trace:
        with open(trace, "a", encoding="utf-8") as fh:
            fh.write(f"end {case_id} {rep} {time.time():.3f}\n")
    return 0 if outcome == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
