# Pool-pressure fixtures

Captured input for `scripts/test_pool_pressure.py`, read through
`pool_pressure.py --from-dir`. Two directories, each a day: `breach/` is
2026-08-26, the day
[oss-test-infra#2666](https://github.com/GoogleCloudPlatform/oss-test-infra/issues/2666)
was filed about, and `quiet/` is 2026-08-27, the day after.

Using the real incident rather than a synthetic stall is the point. Issue #1069
asks that a simulated breach go red; a fixture built to breach proves the
arithmetic, and this one proves the check would have caught the thing it was
written for.

## What is real and what is not

## Which builds, and why those

Five per day. Five is `MIN_SAMPLES_FOR_DAILY_VERDICT`, below which no day is
judged at all, so it is the smallest set the breach tests can run on. Adding
more buys no coverage: what the percentiles need is spread, not volume.

Each build is three files sharing a name. The name says what the build is for,
because a Twitter snowflake reads as noise in a diff and the check takes the
build ID from `status.build_id` inside the JSON rather than from the filename.

| `breach/` — 2026-08-26 | setup | PR  | build ID              |
| ---------------------- | ----- | --- | --------------------- |
| `worst-queue-stall`    | 175.9 | 961 | `2092660728946233344` |
| `long-queue-stall`     | 139.5 | 977 | `2092616949556056064` |
| `moderate-wait`        | 9.9   | 805 | `2092526388937494528` |
| `fast-1`               | 0.8   | 865 | `2092507059223269376` |
| `fast-2`               | 0.8   | 865 | `2092520409604820992` |

| `quiet/` — 2026-08-27      | setup | PR  | build ID              |
| -------------------------- | ----- | --- | --------------------- |
| `normal-success`           | 0.5   | 965 | `2092786563451719680` |
| `normal-aborted-1`         | 0.4   | 956 | `2092819881450803200` |
| `normal-aborted-2`         | 0.4   | 980 | `2092904488661684224` |
| `clone-failure-no-lease-1` | 0.1   | 982 | `2092808526073171968` |
| `clone-failure-no-lease-2` | 0.1   | 956 | `2092817215492460544` |

The two fast breach builds are there so the median is not made of outliers
alone; the two aborted quiet builds are there because aborts are the most
common terminal state and the check must not read them as stalls.

## What is real and what is not

`prowjobs/*.json` are **real**, copied from
`gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/<pr>/pull-kube-agents-smoke-test/<build>/prowjob.json`.
Three fields the check never reads are stripped: `spec.pod_spec`,
`metadata.managedFields`, and `metadata.annotations`. Everything the check does
read is untouched — `metadata.creationTimestamp`, `status.pendingTime`,
`status.build_id`, `spec.max_concurrency`, and `spec.refs.pulls`.

`started/*.json` are **real and unedited**, five fields exactly as the bucket
holds them. They carry the epoch second the container began running, which is
the boundary between the check's second and third segments.

Every JSON file here is one object on one line, which is how GCS holds them.
Expanding them adds some 1,500 lines to any diff that touches this directory and
makes them less like the objects they were captured from.

`logs/*.txt` are **real but cut at both ends**, and the two cuts are not the
same kind of thing. The tail cut is load-bearing: it stops a couple of hundred
bytes past the banner that follows the Boskos lease banner, mirroring the ranged
read the GCS path makes, so `--from-dir` exercises the parser on the same shape
of input — including a log that ends mid-run. The head cut is housekeeping:
`BANNER_PATTERN` matches nothing in the clone and setup output that precedes the
first banner, so five lines of context are kept for a reader and the rest
dropped. That took the ten logs from 164 kB to 10 kB. Under a kilobyte each.

The two `clone-failure-no-lease-*` builds failed in `clone` before the test
script ran, so they hold no banners at all — their logs are the last 20 lines
instead. They are what makes the per-segment counts differ in `quiet/`: 5 of 5
for the queue, 3 of 5 for the lease. They are the reason a missing lease reports
as `None` rather than a fast one, and 3 of 5 clears `SEGMENT_MIN_COVERAGE`, so
the segment still prints a median rather than reporting itself unmeasured.

`deck.json` and `boskos.json` are **hand-built**. A real Deck capture is 192 kB
of mostly-irrelevant items, but the reason not to capture one is stronger than
size: a live wait is `now - creationTimestamp`, so a fixture with real
timestamps in it grows by a minute every minute and any assertion about it
expires. The tests pin `now` with `--as-of` instead, and these two files are
small enough to read.

`breach/boskos.json` reports a fully leased pool — `{"busy": 15}`, with no
`free` key, which is how Boskos renders a state whose count is zero. It is
constructed to exercise the CAPACITY branch of `cause()`, the branch that
recommends spending money. The state is not hypothetical — Boskos reported
exactly that shape on 2026-09-02, with runs queued behind it.

## Keeping the two hand-built files consistent

The check cross-references Boskos lease owners against the build IDs Deck says
are running, and reports the difference as leaked leases. So the build IDs in
`deck.json` and the owner strings in `boskos.json` are the same list written
twice, and editing one without the other invents a leak that the fixture then
asserts is not there. `quiet/` did exactly that once — its owners came from the
real capture while its Deck items were synthesized — and reported ten leaks
against an idle pool.

Owner strings take the form `pull-kube-agents-smoke-test-<build_id>`, matching
what Boskos records. The empty-string owner is Boskos filing its unleased
resources; it is a count of nobody, not a holder.

## Re-capturing

```bash
BUILD=2092660728946233344
PR=961
NAME=worst-queue-stall   # what the build is for, not its ID; see the tables above
BASE="gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/${PR}/pull-kube-agents-smoke-test/${BUILD}"

gcloud storage cat "${BASE}/prowjob.json" | python3 -c 'import json,sys
d = json.load(sys.stdin)
d["spec"].pop("pod_spec", None)
for k in ("managedFields", "annotations"):
    d["metadata"].pop(k, None)
print(json.dumps(d, separators=(",", ":")))' > breach/prowjobs/"${NAME}".json

gcloud storage cat "${BASE}/started.json" > breach/started/"${NAME}".json
# 0-65535 is the same range pool_pressure.py reads. Then cut both ends: keep
# five lines of context before the first banner, and stop just past the banner
# after the lease banner. The head is clone output no pattern here matches.
gcloud storage cat -r 0-65535 "${BASE}/build-log.txt" > breach/logs/"${NAME}".txt
```

A ranged read that runs past the end of the object exits 1 having written the
whole body, so ignore its status and check what it wrote.

The build ID encodes the time the pod started (`build_id >> 22` plus the
Twitter-snowflake epoch), so it is enough on its own to find a build from a
given day; `pool_pressure.py` uses that to prefilter. The PR number comes from
the flat index at
`gs://kube-agents-prow/pr-logs/directory/pull-kube-agents-smoke-test/<build_id>.txt`,
whose body is the full artifact path.

Nothing here is sensitive, on the strength of the content rather than the
bucket's permissions — the bucket is not public, and an anonymous read of these
objects returns 401. What the files hold is PR numbers, timestamps and build IDs
from a public repository's CI, produced by a job config that is itself public in
oss-test-infra.
`gcs_credentials_secret` appears in the retained `decoration_config` as an empty
string — it is a secret's _name_ field, unset, not a secret.
