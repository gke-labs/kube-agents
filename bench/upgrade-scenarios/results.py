#!/usr/bin/env python3
"""Render the results table in README.md between its BEGIN/END TABLE markers, and the same rows to results.csv.

Verdicts and proof quotes are judged by hand and kept in SCENARIOS below; each quote must appear verbatim in its
evidence file (the script checks). The Recommender columns come from recommender.json, written by
check-recommender.sh. A scenario counts as caught when a subtype GKE documents for that hazard was published on a
cluster carrying the hazard."""
import csv, json, os, re, sys
H = os.path.dirname(os.path.abspath(__file__))
BEGIN, END = "<!-- BEGIN TABLE -->", "<!-- END TABLE -->"
DOC, REC_FILE, CSV_FILE, EVIDENCE_DIR = "README.md", "recommender.json", "results.csv", "evidence"
CSV_HEADER = ["#", "Failure", "Reproduced?", "Evidence file", "Proof (quoted line)", "GKE Recommender check for it",
              "Clusters carrying the hazard", "Published on those clusters", "Caught?"]
QUOTE_MAX = 150
def esc(s): return s.replace("|", "\\|")   # a | inside a cell would split the row
def code(s):
    fence = "`" * (max((len(m) for m in re.findall(r"`+", s)), default=0) + 1)   # outlast any backtick run in s
    return f"{fence} {s} {fence}" if len(fence) > 1 else f"`{s}`"
# n: (failure, verdict, evidence file, verbatim quote, clusters carrying the hazard as name@zone, documented subtypes).
# The zone is what lets the renderer tell "read and nothing published" from "never read": a cluster whose zone the
# Recommender read did not cover is not a silent "no matching insight", it is a reason to refuse the table.
SCENARIOS = {
 1: ("A PodDisruptionBudget forbids the eviction", "reproduced", "01/budget.txt",
     "2026-09-29T15:00:13.790784Z\tservice-<PROJECT_NUMBER>@container-engine-robot.iam.gserviceaccount.com",
     ["upg-01@us-central1-a", "gemma-gpu@us-central1-a", "gemma-gpu-upgraded@us-central1-a"], ["PDB_UNPERMISSIVE"]),
 2: ("No spare capacity for the displaced pods", "reproduced", "02/capacity-availability.txt",
     "2026-09-29T14:45:24Z /Pending/", ["upg-02b@us-central1-a"], []),
 3: ("Every replica in one zone or on one node", "reproduced", "03/placement.txt", "samples=44 zero_serving=18", ["upg-03b@us-central1-a"], []),
 4: ("Data on the node is gone", "reproduced", "04/node-data.txt", "Tue Sep 29 15:17:26 UTC 2026", ["upg-04b@us-central1-a"], []),
 5: ("Maintenance window too short, or an exclusion ends mid-roll", "partial", "05/window.txt",
     "1.35.8-gke.1380000\thold-minor={'endTime': '2026-10-02T13:49:59Z', 'maintenanceExclusionOptions': {'scope': 'NO_MINOR_UPGRADES'}",
     ["upg-05@us-central1-a"], []),
 6: ("A served API version is removed", "reproduced", "06/removed-api.txt",
     "no longer serves flowcontrol.apiserver.k8s.io/v1beta3 (discovery returned 404)", ["upg-06h@us-central1-a", "gemma-gpu@us-central1-a"], ["DEPRECATION_K8S_1_32_API"]),
 7: ("A fail-closed webhook whose backend is not up", "reproduced", "07/webhook.txt",
     '14:10:50Z   FailedCreate        guarded-7cf87d9df4         Error creating: Internal error occurred: failed calling webhook "gate.scen.example.com"',
     ["upg-07@us-central1-a"], ["K8S_ADMISSION_WEBHOOK_UNAVAILABLE"]),
 8: ("A default changes in the new minor", "reproduced", "08/default-change.txt",
     "git-repo volume plugin has been disabled", ["upg-08h@us-central1-a"], []),
 9: ("A feature is deprecated but still served", "no break (as expected)", "09/final.txt",
     "2026-09-29T14:05:15Z   Completed          after                      Job completed", ["upg-09@us-central1-a"], []),
 10: ("Add-on and client skew", "partial", "10b/skew.txt",
      "gke-upg-10-work-pool-6fb8c002-0fdz      Ready    <none>   47m     v1.31.14-gke.2759000", ["upg-10@us-central1-a"], ["CLUSTER_VERSION_SKEW_UNSUPPORTED"]),
 11: ("The control plane is unreachable for minutes on a zonal cluster", "not reproduced", "11b/zonal.txt", "read_down=0", ["upg-11b@us-central1-a"], []),
 12: ("A node label is removed", "reproduced (GKE form)", "12/label.txt",
      "0/3 nodes are available: 1 node(s) were unschedulable, 2 node(s) didn't match Pod's node affinity/selector.", ["upg-12b@us-central1-a"], []),
 13: ("The container runtime changes", "reproduced", "13b/runtime.txt",
     "unknown service runtime.v1alpha2.RuntimeService", ["upg-13b@us-central1-a"], ["DEPRECATION_CONTAINERD_V1ALPHA2_CRI_API", "DEPRECATION_CONTAINERD_V1_SCHEMA_IMAGES"]),
 14: ("cgroup v2 under a runtime that cannot read it", "reproduced (GKE form)", "14c/cgroup.txt",
     "legacy-jvm-v1-6c5f68468-xq87r   gke-upg-14b-v1-pool-4922a2a8-cej9   Running   5          OOMKilled   137", ["upg-14b@us-central1-a"], []),
 15: ("The OOM killer starts killing the whole container", "symptom reproduced", "15/group-oom.txt",
     "forker-default   Running   4          OOMKilled", ["upg-15b@us-central1-a"], []),
 16: ("The network dataplane changes", "reproduced (GKE form)", "16/dataplane.txt", "wget: download timed out",
     ["upg-16h@us-central1-a", "seeded-a@us-central1-a", "gemma-gpu@us-central1-a", "gemma-gpu-upgraded@us-central1-a"], ["NETWORK_POLICIES_UNRECONCILED"]),
 17: ("A node networking agent fails on the new image", "partial", "17/node-agent.txt",
     "wget: can't connect to remote host (10.128.0.59): Connection refused", ["upg-17b@us-central1-a"], []),
 18: ("GPU driver mismatch", "partial", "18m/compat.txt",
     "Error 803: system has unsupported display driver / cuda driver combination", ["upg-18m@us-west1-a", "upg-18k@us-central1-b", "upg-18i@us-east1-d"], []),
 19: ("In-tree volumes lose their CSI path", "reproduced (GKE form)", "19c/csi.txt",
     "pd-user-5f66bd9f9b-v6kmc   0/3 nodes are available: 1 node(s) didn't match PersistentVolume's node affinity", ["upg-19c@us-central1-c"], []),
 20: ("Images on a retired registry", "reproduced", "20d/registry.txt",
     "retired-image-5b5c57c885-kzjq8   0/1     ImagePullBackOff", ["upg-20d@us-central1-c"], []),
}
def main():
    rec_path = f"{H}/{REC_FILE}"
    if not os.path.exists(rec_path):
        print(f"ERROR {REC_FILE} missing: run check-recommender.sh first", file=sys.stderr); return 1
    rec = json.load(open(rec_path))
    published = {c: {i["subtype"] for i in items} for c, items in rec.get("clusters", {}).items()}
    zones_read = set(rec.get("zones", []))
    unread = sorted({f"{c} ({z})" for v in SCENARIOS.values() for c, z in (e.split("@") for e in v[4]) if z not in zones_read})
    if unread:
        print(f"ERROR {REC_FILE} was read in {sorted(zones_read)} and does not cover: {', '.join(unread)}; re-run check-recommender.sh with every zone a scenario cluster sits in (ZONES=...), nothing written", file=sys.stderr); return 1
    rows = ["| # | Failure | Reproduced? | Proof (evidence file: quoted line) | GKE Recommender check for it | Published on the clusters carrying it | Caught? |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    caught = documented = 0; errors = []; csv_rows = []
    for n, (title, verdict, efile, quote, located, subtypes) in SCENARIOS.items():
        clusters = located   # name@zone, the key recommender.json uses
        epath = f"{H}/{EVIDENCE_DIR}/{efile}"
        if not os.path.exists(epath): errors.append(f"{n}: evidence file missing: {efile}"); continue
        text = open(epath).read()
        if quote and quote not in text: errors.append(f"{n}: quote not found in {efile}")
        proof = f"`{efile}`: {code(quote[:QUOTE_MAX].replace(chr(9), ' '))}" if quote else f"`{efile}`"
        hits = sorted({s for c in clusters for s in published.get(c, set()) if s in subtypes})
        def cell(c):
            match = sorted(published.get(c, set()) & set(subtypes)); other = sorted(published.get(c, set()) - set(subtypes))
            return f"`{c.split('@')[0]}`: " + (", ".join(match) if match else "no matching insight") + (f" (unrelated: {len(other)})" if other else "")
        where = "; ".join(cell(c) for c in clusters)
        documented += bool(subtypes); caught += bool(hits)
        verdict_cell = "caught (" + ", ".join(hits) + ")" if hits else ("not yet" if subtypes else "no check exists")
        rows.append("| " + " | ".join(esc(str(c)) for c in (n, title, verdict, proof, ", ".join(subtypes) or "none documented", where, verdict_cell)) + " |")
        csv_rows.append([n, title, verdict, f"{EVIDENCE_DIR}/{efile}", quote.replace("\t", " "), ", ".join(subtypes) or "none documented",
                         ", ".join(e.split("@")[0] for e in clusters), where.replace("`", ""), verdict_cell])
    tally = {}
    for v in SCENARIOS.values(): tally[v[1].split(" (")[0]] = tally.get(v[1].split(" (")[0], 0) + 1
    verdicts = "Verdicts: " + ", ".join(f"{k} {c}" for k, c in sorted(tally.items(), key=lambda kv: -kv[1])) + "."
    zones = rec.get("zones", [])
    summary = (f"{verdicts} Recommender read at {rec.get('read_at', 'never')} in {len(zones) or 'unrecorded'} zones "
               f"(newest refresh {rec.get('newest_refresh', 'n/a')}). **Caught: {caught} of {len(SCENARIOS)}.** "
               f"GKE documents a check for {documented} of the {len(SCENARIOS)}.")
    every = ["", "Every insight or recommendation published on a scenario cluster, including ones unrelated to its scenario:", "",
             "| Cluster (zone) | Subtypes | Last refresh |", "| --- | --- | --- |"]
    ours = sorted({e for v in SCENARIOS.values() for e in v[4]})
    for c in ours:
        items = rec.get("clusters", {}).get(c, [])
        every.append(f"| `{c.split('@')[0]}` ({c.split('@')[1]}) | {', '.join(sorted({i['subtype'] for i in items})) or 'none'} | {max((i['lastRefreshTime'] for i in items), default='')} |")
    if errors:
        for e in errors: print("ERROR", e, file=sys.stderr)
        return 1
    table = "\n".join([summary, "", *rows, *every])
    path = f"{H}/{DOC}"; doc = open(path).read()
    # Count the markers themselves: a stray second BEGIN or END still yields one matched region, and the substitution
    # would then delete the stray BEGIN and everything between the two, or leave the stray END below the table.
    b, e = doc.count(BEGIN), doc.count(END)
    if (b, e) != (1, 1) or doc.index(BEGIN) > doc.index(END):
        print(f"ERROR {DOC} has {b} BEGIN and {e} END markers, expected one pair in order; nothing written", file=sys.stderr); return 1
    doc = re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda _: f"{BEGIN}\n{table}\n{END}", doc, flags=re.S)
    open(path, "w").write(doc); print(summary)
    with open(f"{H}/{CSV_FILE}", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n"); w.writerow(CSV_HEADER); w.writerows(csv_rows)
    return 0
if __name__ == "__main__": sys.exit(main())
