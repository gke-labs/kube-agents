#!/usr/bin/env python3
"""Completes platform agent first-time bootstrap onboarding right cleanly."""

import json
import os
import subprocess
from pathlib import Path


def complete_bootstrap():
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))

    # 1. Create durable state marker right to lock out any future onboarding hook injections
    completed_marker = data_dir / ".bootstrap_completed"
    completed_marker.touch(exist_ok=True)
    print("✅ Bootstrap completion marker created (.bootstrap_completed).")

    # 2. Cleanly remove the one-off master inventory file
    for p in [data_dir / "INVENTORY.md", Path("./INVENTORY.md")]:
        if p.exists():
            try:
                p.unlink()
                print(f"✅ Removed {p.name} from workspace.")
            except Exception as e:
                print(f"Notice: could not remove {p}: {e}")

    # 3. Remove one-off bootstrap-inventory-scan cron routine via CLI (or scrub jobs.json fallback)
    hermes_bin = "/opt/hermes/.venv/bin/hermes"
    if not Path(hermes_bin).exists():
        hermes_bin = "hermes"

    removed_via_cli = False
    try:
        res = subprocess.run(
            [hermes_bin, "cron", "rm", "bootstrap-inventory-scan"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            removed_via_cli = True
            print("✅ One-off 'bootstrap-inventory-scan' job removed via hermes cron CLI.")
    except Exception as e:
        print(f"Notice: hermes cron rm command encountered error: {e}")

    if not removed_via_cli:
        for jobs_path in [
            data_dir / "cron/jobs.json",
            Path("./cron/jobs.json"),
            Path("/opt/data/cron/jobs.json"),
        ]:
            if jobs_path.exists():
                try:
                    data = json.loads(jobs_path.read_text(encoding="utf-8"))
                    if "jobs" in data and isinstance(data["jobs"], list):
                        original_len = len(data["jobs"])
                        data["jobs"] = [
                            j
                            for j in data["jobs"]
                            if j.get("id") != "bootstrap-inventory-scan"
                        ]
                        if len(data["jobs"]) != original_len:
                            jobs_path.write_text(
                                json.dumps(data, indent=2) + "\n", encoding="utf-8"
                            )
                            print(
                                f"✅ One-off 'bootstrap-inventory-scan' job removed right out of {jobs_path}."
                            )
                except Exception as e:
                    print(f"Notice: could not clean jobs from {jobs_path}: {e}")

    print("✅ First-time onboarding self-cleanup fully concluded.")


if __name__ == "__main__":
    complete_bootstrap()
