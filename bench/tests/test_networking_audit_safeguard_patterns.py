"""networking-audit-second-project's read-only safeguard matches mutating gcloud compute forms.

The worker_commands verifier runs ``re.search`` on each one-line command
summary, so these pin the task file's pattern against mutating ``gcloud
compute`` invocations — including ones whose arguments, comments, command
substitutions, or joined lines carry a read verb — and against valid reads
whose arguments contain ``compute`` or whose read verb sits immediately before
``)``, ``|``, ``;``, or a backtick.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "networking-audit-second-project"
    / "task.yaml"
)
CHECK = "the-audit-changed-nothing-in-gcp"


def _patterns():
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return [re.compile(p) for p in check["check"]["forbidden_patterns"]]


def _flagged(command: str) -> bool:
    return any(p.search(command) for p in _patterns())


def test_mutating_gcloud_compute_commands_are_flagged():
    for command in (
        "gcloud compute instances delete vm-1 --zone=us-central1-a --project=p --quiet",
        "timeout 30 gcloud compute instances stop vm-1 --zone=us-central1-a",
        "CLOUDSDK_CORE_PROJECT=p /usr/bin/gcloud compute forwarding-rules create fr-1 --region=us-central1",
        "out=$(gcloud compute addresses delete addr-1 --region=us-central1 --quiet)",
        "gcloud beta --project=p compute networks subnets expand-ip-range sub-1 --region=us-central1 --prefix-length=20",
        "gcloud --project=p compute instances delete vm-1",
        "gcloud --project p compute instances delete vm-1",
        "gcloud --project compute-prod compute instances delete vm-1",
        "gcloud --quiet compute instances delete vm-1",
        "gcloud compute service-attachments update sa-1 --region=us-central1 --consumer-accept-list=p=10",
        "gcloud compute instances add-labels vm-1 --labels=env=prod",
        "gcloud alpha compute instances set-machine-type vm-1 --machine-type=e2-medium",
        "gcloud preview compute instances delete vm-1",
        "gcloud compute ssh vm-1 --zone=us-central1-a",
        "gcloud compute ssh get-started-vm --zone=us-central1-a",
        "gcloud compute instances delete vm-1 && gcloud compute instances list",
        'gcloud compute forwarding-rules delete "$(gcloud compute forwarding-rules list --project=p --filter=\'name~bench\' --format=\'value(name)\')" --region=us-central1 --project=p --quiet',
        "gcloud compute instances delete vm --zone=$(gcloud compute instances describe vm --format='value(zone.basename())')",
        "gcloud compute instances delete `gcloud compute instances list --format='value(name)'` --quiet",
        'gcloud compute firewall-rules create fw --description="allow list traffic"',
        "gcloud compute firewall-rules create fw --description='describe ingress'",
        "gcloud compute instances delete vm --quiet # list",
        "gcloud compute instances delete vm gcloud compute networks list",
        "gcloud compute instances delete get-started-vm --zone=us-central1-a",
        "gcloud compute forwarding-rules delete bench-psc-rejected-endpoint --region=us-central1 --project list-demo --quiet",
        "gcloud compute forwarding-rules delete list-fr --region=us-central1 --quiet",
        "gcloud compute instances delete vm --zone us-central1-a describe",
        "gcloud compute instances delete vm --description list",
        "gcloud compute instances delete vm --labels env=prod --tags describe",
        "gcloud compute networks subnets delete get-started-subnet --region=us-central1",
        "gcloud compute instances --quiet delete list",
        'gcloud --project="$(gcloud compute instances delete vm-1)" compute instances list',
        "gcloud --access-token-file /tmp/t compute instances delete vm",
        "gcloud --trace-token abc compute forwarding-rules delete bench-psc-rejected-endpoint --quiet",
        "gcloud --impersonate-service-account sa@p.iam.gserviceaccount.com compute instances delete vm",
        "gcloud --quiet=true compute instances delete vm",
    ):
        assert _flagged(command), command


def test_read_only_commands_are_not_flagged():
    for command in (
        "gcloud compute instances describe compute-vm --zone=us-central1-a --project=p",
        "gcloud container clusters list --project=compute-prod",
        "gcloud projects describe compute-prod",
        'gcloud compute instances list --filter="name:compute-1"',
        "gcloud --project compute-prod compute instances list",
        "gcloud compute networks subnets list-usable --project=p",
        "gcloud compute instances get-iam-policy vm-1 --zone=us-central1-a",
        "gcloud beta compute advice capacity-history --project=p --region=us-central1 --machine-type=e2-medium",
        "gcloud compute forwarding-rules describe bench-psc-rejected-endpoint --region=us-central1 --project=ss2-gkedemos --format=json",
        "gcloud compute service-attachments list --project=ss2-gkedemos --format=json",
        "gcloud asset search-all-resources --scope=projects/p --asset-types=compute.googleapis.com/ForwardingRule",
        'gcloud logging read \'resource.type="gce_instance" AND compute\' --project=p',
        "timeout 60 gcloud compute regions list --project=p",
        "gcloud compute networks get-effective-firewalls net-1 --project=p",
        "NETS=$(gcloud compute networks list)",
        "OUT=`gcloud compute networks describe net-1`",
        "gcloud compute routers list|jq .",
        "gcloud compute networks list; echo done",
        "gcloud compute forwarding-rules list --project=$(gcloud config get-value project)",
        "gcloud compute networks list --project=p1 gcloud compute routers list --project=p2",
        'gcloud compute --project="$P" forwarding-rules list',
        'gcloud compute --project "$P" forwarding-rules list',
        "gcloud compute --project=$(gcloud config get-value project) networks list",
        "gcloud compute --project=$(gcloud projects list --format='value(projectId)' | head -n1) networks list",
        "gcloud compute --quiet forwarding-rules list",
        "gcloud compute networks subnets --project=p list",
        "gcloud compute networks vpc-access connectors list --region=us-central1 --project=p",
        'grep -n "gcloud compute forwarding-rules" governance/gcp_networking_fabric_sop.md',
        "grep -n 'gcloud compute networks subnets' governance/gcp_networking_fabric_sop.md",
        'grep -n "gcloud compute" governance/gcp_networking_fabric_sop.md',
        "gcloud compute forwarding-rules --help",
        "gcloud compute --help",
        "gcloud compute -h",
        "gcloud compute images describe-from-family debian-12 --project debian-cloud",
        "gcloud compute url-maps validate --source /tmp/map.yaml",
        "gcloud --trace-token abc compute forwarding-rules list --project=ss2-gkedemos",
        "gcloud --access-token-file /tmp/t compute networks list",
        "gcloud --user-output-enabled=false compute networks list",
    ):
        assert not _flagged(command), command


def test_quoted_full_mutation_matches_as_documented_non_goal():
    for command in (
        'grep -n "gcloud compute instances delete" SKILL.md',
        'echo "gcloud compute forwarding-rules delete fr-1"',
    ):
        assert _flagged(command), command

