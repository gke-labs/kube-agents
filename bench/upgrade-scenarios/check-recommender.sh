#!/bin/bash
# Read GKE's Recommender for each zone: every DiagnosisInsight and every DiagnosisRecommender recommendation.
# Saves the raw responses under evidence/recommender/<stamp>/ (the proof) and writes recommender.json, which maps
# each cluster to what was published about it (results.py reads that). Workload-level insights are mapped to
# the cluster in their resource path; a record that names no cluster is listed, not dropped silently.
# Insights live in the cluster's own location, so every zone a scenario cluster sat in is read, including the zones the scenario 18 GPU runs chased capacity through.
DEFAULT_ZONES="us-central1-a us-central1-c us-east4-c us-west1-b europe-west4-b asia-southeast1-b us-east1-d europe-west1-b us-central1-b us-west1-a europe-west4-a asia-east1-a"
P=${PROJECT:?set PROJECT to the GCP project the scenario clusters are in}; ZONES=${ZONES:-$DEFAULT_ZONES}; H=$(cd "$(dirname "$0")" && pwd)
INSIGHT_TYPE=google.container.DiagnosisInsight; RECOMMENDER=google.container.DiagnosisRecommender
PROJECT_NUMBER=$(gcloud projects describe "$P" --format='value(projectNumber)') && [ -n "$PROJECT_NUMBER" ] ||
  { echo "cannot read the project number of $P; recommender.json would keep it unscrubbed" >&2; exit 1; }
STAMP=$(date -u +%Y-%m-%dT%H%MZ); OUT="$H/evidence/recommender/$STAMP"; mkdir -p "$OUT"
for Z in $ZONES; do   # a read that fails stops here: an empty file would otherwise read as a zone with nothing published
  gcloud recommender insights list --project "$P" --location "$Z" --insight-type "$INSIGHT_TYPE" --format=json >"$OUT/insights-$Z.json" || { echo "insights read failed for $Z; recommender.json left unchanged" >&2; exit 1; }
  gcloud recommender recommendations list --project "$P" --location "$Z" --recommender "$RECOMMENDER" --format=json >"$OUT/recommendations-$Z.json" || { echo "recommendations read failed for $Z; recommender.json left unchanged" >&2; exit 1; }
done
python3 - "$OUT" "$H/recommender.json" "$P" "$PROJECT_NUMBER" <<'PY'
import glob, json, re, sys, collections
out_dir, dest, project_id, project_number = sys.argv[1:5]
# recommender.json is checked in, so the project ID and number become placeholders in the fields that carry them
# (resource names, service-account addresses, console links in a description). Cluster names are the keys results.py
# matches on and are written as they are; a cluster whose own name contains the project is reported at the end.
def scrub(s):
    for value, placeholder in ((project_id, "<PROJECT_ID>"), (project_number, "<PROJECT_NUMBER>")):   # ID first: an ID can contain the number, never the reverse
        if value: s = s.replace(value, placeholder)
    return s
seen = collections.defaultdict(dict); newest = ""; unmatched = []
def cluster_of(path):
    m = re.search(r"/clusters/([^/]+)", path or ""); return m.group(1) if m else None
for kind, pattern, subkey, targets in (("insight", "insights-*.json", "insightSubtype", lambda r: r.get("targetResources", [])),
                                     ("recommendation", "recommendations-*.json", "recommenderSubtype",
                                      lambda r: r.get("targetResources", []) + [o.get("resource", "") for g in r.get("content", {}).get("operationGroups", []) for o in g.get("operations", [])])):
    for r in (r for f in sorted(glob.glob(f"{out_dir}/{pattern}")) for r in json.load(open(f))):
        newest = max(newest, r.get("lastRefreshTime", ""))
        clusters = {cluster_of(t) for t in targets(r)} - {None}
        if not clusters: unmatched.append(f"{kind} {r.get(subkey, '?')} {r.get('name', '')}")
        for c in clusters:
            key = (kind, r.get(subkey, "?"))
            prev = seen[c].get(key)
            if not prev or r.get("lastRefreshTime", "") > prev["lastRefreshTime"]:
                seen[c][key] = {"kind": kind, "subtype": r.get(subkey, "?"), "lastRefreshTime": r.get("lastRefreshTime", ""),
                                "state": r.get("stateInfo", {}).get("state", ""), "description": scrub(r.get("description", ""))[:200],   # scrub the whole text, then cut: a cut identifier would escape the scrub
                                "name": scrub(r.get("name", ""))}
zones = sorted(re.sub(r"^insights-|\.json$", "", f.rsplit("/", 1)[-1]) for f in glob.glob(f"{out_dir}/insights-*.json"))
result = {"read_at": out_dir.rsplit("/", 1)[-1], "newest_refresh": newest, "raw": out_dir.split("/evidence/", 1)[-1], "zones": zones,
          "clusters": {c: sorted(v.values(), key=lambda x: (x["subtype"], x["kind"])) for c, v in sorted(seen.items())}}
open(dest, "w").write(json.dumps(result, indent=1) + "\n")
named = [c for c in result["clusters"] if project_id in c or (project_number and project_number in c)]
print("read at", result["read_at"], "| newest refresh:", newest)
for c, items in result["clusters"].items(): print(f"{c:22s}", sorted({i["subtype"] for i in items}))
print(len(unmatched), "records named no cluster and are in no row:")
for u in unmatched: print("  ", u)
if named: print("NOTE these cluster names contain the project ID or number and are written as they are, since results.py matches on them:", *named, file=sys.stderr)
PY
