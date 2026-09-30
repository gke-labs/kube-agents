#!/bin/bash
# Read GKE's Recommender for each zone: every DiagnosisInsight and every DiagnosisRecommender recommendation.
# Saves the raw responses under evidence/recommender/<stamp>/ (the proof) and writes recommender.json, which maps
# each cluster to what was published about it (results.py reads that). Workload-level insights are mapped to
# the cluster in their resource path.
# Insights live in the cluster's own location, so every zone a scenario cluster sat in is read, including the zones the scenario 18 GPU runs chased capacity through.
DEFAULT_ZONES="us-central1-a us-central1-c us-east4-c us-west1-b europe-west4-b asia-southeast1-b us-east1-d europe-west1-b us-central1-b us-west1-a europe-west4-a asia-east1-a"
P=${PROJECT:?set PROJECT to the GCP project the scenario clusters are in}; ZONES=${ZONES:-$DEFAULT_ZONES}; H=$(cd "$(dirname "$0")" && pwd)
INSIGHT_TYPE=google.container.DiagnosisInsight; RECOMMENDER=google.container.DiagnosisRecommender
STAMP=$(date -u +%Y-%m-%dT%H%MZ); OUT="$H/evidence/recommender/$STAMP"; mkdir -p "$OUT"
for Z in $ZONES; do
  gcloud recommender insights list --project "$P" --location "$Z" --insight-type "$INSIGHT_TYPE" --format=json >"$OUT/insights-$Z.json"
  gcloud recommender recommendations list --project "$P" --location "$Z" --recommender "$RECOMMENDER" --format=json >"$OUT/recommendations-$Z.json"
done
python3 - "$OUT" "$H/recommender.json" <<'PY'
import glob, json, re, sys, collections
out_dir, dest = sys.argv[1], sys.argv[2]
seen = collections.defaultdict(dict); newest = ""
def cluster_of(path):
    m = re.search(r"/clusters/([^/]+)", path or ""); return m.group(1) if m else None
for kind, pattern, subkey, targets in (("insight", "insights-*.json", "insightSubtype", lambda r: r.get("targetResources", [])),
                                     ("recommendation", "recommendations-*.json", "recommenderSubtype",
                                      lambda r: [o.get("resource", "") for g in r.get("content", {}).get("operationGroups", []) for o in g.get("operations", [])])):
    for r in (r for f in sorted(glob.glob(f"{out_dir}/{pattern}")) for r in json.load(open(f))):
        newest = max(newest, r.get("lastRefreshTime", ""))
        for c in {cluster_of(t) for t in targets(r)} - {None}:
            key = (kind, r[subkey])
            prev = seen[c].get(key)
            if not prev or r.get("lastRefreshTime", "") > prev["lastRefreshTime"]:
                seen[c][key] = {"kind": kind, "subtype": r[subkey], "lastRefreshTime": r.get("lastRefreshTime", ""),
                                "state": r.get("stateInfo", {}).get("state", ""), "description": r.get("description", "")[:200],
                                "name": r.get("name", "")}
zones = sorted(re.sub(r"^insights-|\.json$", "", f.rsplit("/", 1)[-1]) for f in glob.glob(f"{out_dir}/insights-*.json"))
result = {"read_at": out_dir.rsplit("/", 1)[-1], "newest_refresh": newest, "raw": out_dir.split("/evidence/", 1)[-1], "zones": zones,
          "clusters": {c: sorted(v.values(), key=lambda x: (x["subtype"], x["kind"])) for c, v in sorted(seen.items())}}
json.dump(result, open(dest, "w"), indent=1)
print("read at", result["read_at"], "| newest refresh:", newest)
for c, items in result["clusters"].items(): print(f"{c:22s}", sorted({i["subtype"] for i in items}))
PY
