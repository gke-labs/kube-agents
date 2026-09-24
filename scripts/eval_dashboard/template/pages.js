/* The Brief (index.html), the PR view (run.html), the Grid (grid.html),
 * the Cases page (cases.html), the Nightly report (nightly.html) and the
 * Trend page (trend.html) share this script.
 *
 * render.py inlines it into every page after two JSON data elements
 * (PAGE.inlineBrief and PAGE.inlineHealth): brief.json -- the per-run
 * classification classify.py produced, the per-case record, the current
 * health verdict, the health history, the recent merges and the
 * release-candidate runs -- and, when there is a verdict, that verdict again
 * under its own id (the same normalized document as brief.health). Each page
 * renders itself from those in the browser. There is no server-side HTML
 * for any page: everything a reader sees is computed here from the inlined
 * copy, with no request beyond the page itself.
 *
 * The poll of the published brief.json and health.json every PAGE.refreshMs
 * is a best-effort refresh on top, not what the page depends on: the whole
 * page is republished every PAGE.republishMinutes by the workflow, so a
 * host that will not answer an XHR (storage.cloud.google.com answers one
 * with a login redirect) still shows a page at most that old. A poll that
 * fails leaves the inlined data on screen and the badge saying so; only
 * data older than its own stale_after_s reads STALE.
 *
 * Every time a reader sees is America/Toronto ("ET"), formatted with
 * Intl.DateTimeFormat. URL parameters stay ISO 8601 UTC and travel in the
 * fragment, which a login redirect on the published host preserves where
 * it drops the query string:
 *   index.html#since=<ISO>&until=<ISO>&cases=a,b&view=gate|agent
 *   run.html#build=<prow build id>
 *   grid.html#since=<ISO>&until=<ISO>&cases=a,b[&window=6h|24h|36h|7d][&rows=all|admitted|failing]
 *   cases.html#<case>, or cases.html#sort=worst|domain|name&show=all|blocking|held
 *   trend.html#cases=a,b | trend.html#domain=<domain> [&metric=<judged metric>][&since=<ISO>[&until=<ISO>]]
 * The older query form (`?cases=a,b&since=<ISO>&until=<ISO>` before a bare
 * `#gate` or `#agent`; `?build=<id>` on run.html; the same on the Grid and
 * the Cases page) is still read, so a link already posted opens the same
 * page wherever its query survives.
 */
"use strict";

const PAGE = {
  // The ids of the data elements render.py inlines (its INLINE_*_ID).
  inlineBrief: "inline-brief",
  inlineHealth: "inline-health",
  refreshMs: 60000,
  // The ci-health workflow's cron: how old the inlined copy can be at most.
  republishMinutes: 15,
  tz: "America/Toronto",
  tzLabel: "ET",
  // Dates inside this many days of "now" read as a weekday ("Sun 7:30 AM ET");
  // older ones carry the month and day.
  weekdayWithinMs: 6 * 24 * 3600 * 1000,
  dayMs: 24 * 3600 * 1000,
  hourMs: 3600 * 1000,
  numbersWindowMs: 24 * 3600 * 1000,
  // The lookback for "what changed right before" when no green run precedes
  // the incident in the data: the same six hours the shared-break rule uses.
  mergesLookbackMs: 6 * 3600 * 1000,
  // The adjudicator's STORM_COOLDOWN: retest this long after the last storm-hit run.
  stormCooldownMs: 30 * 60 * 1000,
  // An incident's window opens this long before its `since`: the rule's own
  // lookback (shared break 6 h; storm, delegation ceiling, setup deaths,
  // lost pods and deadline kills 2 h), so
  // the runs that made the bot declare it are on the page, not only the ones
  // after.
  incidentLeadMs: { shared_break: 6 * 3600 * 1000, storm: 2 * 3600 * 1000, setup_deaths: 2 * 3600 * 1000, lost_pods: 2 * 3600 * 1000, delegation_ceiling: 2 * 3600 * 1000, deadline_kill: 2 * 3600 * 1000 },
  recoveryGreenRuns: 3,
  // A shared break "explains" the reds when at least this share of red runs
  // in the window collapsed one of its cases; below it the headline says "most".
  everyPrShare: 0.9,
  // Storm facts: below this share of repetitions lost, "the agent ran fully".
  stormNoiseShare: 0.1,
  maxLinkCases: 50,
  caseIdRe: /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/,
  isoParamRe: /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$/,
  // The Brief's two views (`view=` in a link, or the bare `#gate`/`#agent`
  // anchors older links carry); each is also the id of the section it lands on.
  views: { gate: "gate", agent: "agent" },
  states: { GREEN: "hs-green", DEGRADED: "hs-amber", OUTAGE: "hs-red", PAST: "hs-past" },
  glyphs: { GREEN: "🟢", DEGRADED: "🟡", OUTAGE: "🔴", PAST: "⚪" },
  // health.py's pool verdicts (rule 8). The verdict picks the lede's
  // sentence, so an unrecognised one renders nothing rather than guessing.
  poolVerdicts: ["BREACH", "UNMEASURED", "STALE"],
  // The breached day, as pool_pressure.py buckets it: a UTC calendar date.
  // The lede prints it verbatim, so it is shape-checked and not only typed.
  poolDayRe: /^\d{4}-\d{2}-\d{2}$/,
  spyglass: "https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents",
  job: "pull-kube-agents-smoke-test",
  prUrl: "https://github.com/gke-labs/kube-agents/pull",
  issueUrl: "https://github.com/gke-labs/kube-agents/issues",
  rulesUrl: "https://github.com/gke-labs/kube-agents/blob/main/scripts/eval_dashboard/classify.py",
  rosterUrl: "https://github.com/gke-labs/kube-agents/blob/main/docs/eval-gate-roster.md",
  briefFile: "brief.json",
  healthFile: "health.json",
  // The trend block, published beside brief.json and polled by the Trend
  // page alone: nothing else reads it and it grows every night.
  trendFile: "trend.json",
  pages: { brief: "index.html", run: "run.html", grid: "grid.html", cases: "cases.html", nightly: "nightly.html", trend: "trend.html" },
  titles: { brief: "kube-agents · smoke gate brief", run: "kube-agents · smoke run", grid: "kube-agents · cases by run", cases: "kube-agents · how reliable is each test", nightly: "kube-agents · last night's run", trend: "kube-agents · scores over time on main" },
  // The Grid's window chips, and the one it opens with when the URL names none.
  gridWindows: [["6h", 6 * 3600 * 1000], ["24h", 24 * 3600 * 1000], ["36h", 36 * 3600 * 1000], ["7d", 7 * 24 * 3600 * 1000]],
  gridDefaultWindow: "36h",
  // Merge markers get a label each up to this many in the window; beyond it
  // the lines stay and the merges are listed under the grid instead.
  gridLabelledMergesMax: 6,
  // The Cases page's colour bands for a pass rate. Display bands only -- the
  // admission rule is prose in docs/eval-gate-roster.md, not a threshold.
  rateOkMin: 0.9,
  rateWarnMin: 0.75,
  // The words for a case's roster status (render.py's STATUS_* values).
  statusWords: { blocking: "blocking", held_out: "held out", demoted: "demoted", nightly_only: "nightly only", retired: "not in any matrix" },
  // Verdict pill colour by the banner hack/ci-eval-rc.sh prints. NOT RUN is the
  // deploy-failed path: nothing was measured, which is neither a pass nor a
  // judgement on the candidate, so it takes the neutral infra colour.
  releaseVerdictClass: { GREEN: "p-pass", RED: "p-fail", "NOT RUN": "p-infra" },
  // The Nightly report: a case's state (nightly.py's STATE_*) as a pill,
  // and how many nights the page lists beside last night's.
  nightStateClass: { pass: "p-pass", partial: "p-partial", fail: "p-fail", infra: "p-infra" },
  nightStateWords: { pass: "passed all reps", partial: "failed some reps", fail: "failed all reps", infra: "quota / infra, not graded" },
  nightsListed: 14,
  // The Trend page (trend.py): a judged metric's name as a URL parameter,
  // the chart geometry (SVG user units; the chart scales to its card), and
  // where "score" is defined once.
  metricRe: /^[A-Za-z][A-Za-z0-9_]{0,39}$/,
  trendChart: { width: 520, height: 170, left: 38, right: 14, top: 22, bottom: 26, barMax: 24, gap: 2, dot: 4 },
  // Days of padding around the nights on the time axis, so a single night
  // and an incident marker both land inside the plot.
  trendPadDays: 1,
  scoreDocUrl: "https://github.com/gke-labs/kube-agents/blob/main/docs/designs/eval-scorer.md#what-a-score-is",
};

function inlineJson(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  try { return JSON.parse(el.textContent); } catch (err) { return null; }
}

let brief = inlineJson(PAGE.inlineBrief);
// No usable data element (a truncated upload, a hand-edited page): say so
// rather than render an empty dashboard that reads as "nothing happened".
let briefLoaded = brief != null && typeof brief === "object";
brief = briefLoaded ? brief : {};
let health = normalizeHealth(inlineJson(PAGE.inlineHealth) ?? brief.health);
// True once a brief.json poll has succeeded; until then (a file:// preview,
// a host that redirects XHRs) the page is as fresh as its last publish.
let live = false;
// What the reader has clicked on the Grid and the Cases page. URL parameters
// seed it; a chip or a cell changes it and re-renders.
const ui = { sort: null, show: null, window: null, rows: null, markers: { merge: true, incident: true }, selected: null, showHeld: false, showRetired: false, openTables: new Set() };

const esc = (value) => String(value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
const enc = encodeURIComponent;

function parseIso(value) {
  if (typeof value !== "string") return null;
  let text = value;
  // ISO 8601 with a space separator is what fromisoformat reads as UTC too.
  if (/^\d{4}-\d{2}-\d{2} \d/.test(text)) text = text.replace(" ", "T");
  // ECMA-262's date-time format wants the colon in the offset; a bare
  // ±HHMM (which isoParamRe and fromisoformat both admit) parses in V8
  // and not elsewhere, so it is normalised before Date.parse sees it.
  if (text.includes("T")) text = text.replace(/([+-]\d\d)(\d\d)$/, "$1:$2");
  if (text.includes("T") && !/(?:[zZ]|[+-]\d\d:\d\d)$/.test(text)) text += "Z";
  const ms = Date.parse(text);
  return Number.isNaN(ms) ? null : ms;
}
const utcIso = (ms) => new Date(ms).toISOString().slice(0, 19) + "Z";
const nowMs = () => parseIso(brief.generated_at) ?? Date.now();

/* ---- ET formatting: the one place a time becomes text ---- */

function fmtParts(ms, options) {
  return new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: PAGE.tz }, options)).format(new Date(ms));
}
function etDay(ms, anchor = nowMs()) {
  return Math.abs(anchor - ms) < PAGE.weekdayWithinMs
    ? fmtParts(ms, { weekday: "short" })
    : fmtParts(ms, { month: "short", day: "numeric" });
}
const etTime = (ms) => fmtParts(ms, { hour: "numeric", minute: "2-digit" });
// "Sun 7:30 AM ET" / "Sep 7, 7:30 AM ET"
function et(ms, anchor = nowMs()) {
  if (ms == null) return "unknown time";
  const day = etDay(ms, anchor);
  return `${day}${day.includes(" ") ? "," : ""} ${etTime(ms)} ${PAGE.tzLabel}`;
}
// A span: same ET day -> "Sun 7:30 AM – 3:00 PM ET", else both stamps.
function etSpan(fromMs, toMs, anchor = nowMs()) {
  if (toMs == null) return `since ${et(fromMs, anchor)}`;
  const sameDay = fmtParts(fromMs, { year: "numeric", month: "2-digit", day: "2-digit" })
    === fmtParts(toMs, { year: "numeric", month: "2-digit", day: "2-digit" });
  if (sameDay) return `${etDay(fromMs, anchor)} ${etTime(fromMs)} – ${etTime(toMs)} ${PAGE.tzLabel}`;
  return `${et(fromMs, anchor)} – ${et(toMs, anchor)}`;
}
function minutesText(ms) {
  if (ms == null || ms < 0) return "";
  const minutes = Math.round(ms / 60000);
  if (minutes < 120) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes - hours * 60}m`;
}
const plural = (n, word, suffix = "s") => `${n} ${word}${n === 1 ? "" : suffix}`;
const pct = (fraction) => `${Math.round(fraction * 100)}%`;

/* ---- health.json, normalized the same way render.py does ---- */

function normalizeHealth(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const state = String(raw.state || "").toUpperCase();
  if (!(state in PAGE.states) || state === "PAST") return null;
  const text = (key) => (typeof raw[key] === "string" ? raw[key] : "");
  const list = (key) => (Array.isArray(raw[key]) ? raw[key].filter((v) => typeof v === "string") : []);
  const incident = raw.incident && typeof raw.incident === "object" ? raw.incident : null;
  const slow = raw.slow && typeof raw.slow === "object" ? raw.slow : null;
  const pool = raw.pool && typeof raw.pool === "object" ? raw.pool : null;
  const count = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
  return {
    state,
    condition: typeof raw.condition === "string" ? raw.condition : null,
    since: parseIso(raw.since) != null ? raw.since : null,
    cause: text("cause"),
    advice: text("advice"),
    failing_cases: list("failing_cases"),
    tracking_issues: list("tracking_issues"),
    recovering: raw.recovering === true,
    stale: raw.stale === true,
    generated_at: parseIso(raw.generated_at) != null ? raw.generated_at : null,
    incident: incident ? {
      prs: Array.isArray(incident.prs) ? incident.prs : [],
      runs: typeof incident.runs === "number" ? incident.runs : null,
      window_start: parseIso(incident.window_start) != null ? incident.window_start : null,
      window_end: parseIso(incident.window_end) != null ? incident.window_end : null,
    } : null,
    // The slow-gate note (health.py rule 7), as render.py normalizes it.
    slow: slow ? {
      since: parseIso(slow.since) != null ? slow.since : null,
      runs: count(slow.runs),
      median_s: count(slow.median_s),
      baseline_p50_s: count(slow.baseline_p50_s),
      baseline_days: count(slow.baseline_days),
    } : null,
    // The pool note (health.py rule 8), as render.py normalizes it.
    pool: pool ? {
      verdict: PAGE.poolVerdicts.includes(pool.verdict) ? pool.verdict : null,
      since: parseIso(pool.since) != null ? pool.since : null,
      measured_at: parseIso(pool.measured_at) != null ? pool.measured_at : null,
      day: typeof pool.day === "string" && PAGE.poolDayRe.test(pool.day) ? pool.day : null,
      window_hours: count(pool.window_hours),
      p50_s: count(pool.p50_s),
      p95_s: count(pool.p95_s),
      // Tri-state, so only a real bool passes: null is "Deck was not read",
      // and the sentence must not reach it through a malformed field.
      waiting_now: typeof pool.waiting_now === "boolean" ? pool.waiting_now : null,
      waiting_since: parseIso(pool.waiting_since) != null ? pool.waiting_since : null,
      over_threshold: count(pool.over_threshold),
      threshold_p50_s: count(pool.threshold_p50_s),
      threshold_p95_s: count(pool.threshold_p95_s),
    } : null,
    tick: parseIso(raw.tick) != null ? raw.tick : null,
  };
}

// One sentence for the Brief's headline while the gate is slow; "" otherwise.
// Same numbers as the Chat note, without the range and the p90.
function slowSentence(h) {
  const s = h && h.slow;
  if (!s || s.median_s == null || s.baseline_p50_s == null) return "";
  const since = s.since ? ` since ${esc(et(parseIso(s.since)))}` : "";
  return ` Runs are slow${since}: the last ${s.runs ?? "few"} full runs took a median of ${Math.round(s.median_s / 60)} min against a ${s.baseline_days ?? "7"}-day typical of ${Math.round(s.baseline_p50_s / 60)}. Nothing is broken; /retest won't make yours faster.`;
}

// One sentence for the Brief's headline while runs are waiting to start; ""
// otherwise. Cause-free, like the digest's line: the Chat alert names the
// cause and what to do, this says the gate is queuing and nothing is broken.
function poolSentence(h) {
  // health.py's wait_text: the gate breaches on p50 or p95, so a p95-only
  // breach carries a sub-minute median that whole minutes print as "0 min".
  const waitText = (s) => (s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)} min`);
  const p = h && h.pool;
  if (!p || !p.verdict) return "";
  if (p.verdict === "STALE") {
    // No timestamp when the periodic ran and published nothing: there is no
    // last reading to date.
    const since = p.measured_at ? ` since ${esc(et(parseIso(p.measured_at)))}` : "";
    return ` No pool numbers${since}: the hourly pool check has stopped reporting.`;
  }
  if (p.verdict === "UNMEASURED") return " The queue wait is unknown: the hourly pool check ran but could not read how long recent runs waited.";
  // What tripped the verdict, which is a single day's row or the live queue --
  // never the seven-day window, which one bad day leaves inside its own limit.
  // The backlog's start while there is one, the episode's otherwise: the
  // verdict spans a week, so the episode can have opened days before the jam
  // the present tense below is describing.
  const began = p.waiting_since || p.since;
  const since = began ? ` since ${esc(et(parseIso(began)))}` : "";
  // Both halves, like post_health.pool_numbers: a day can breach on p95 with a
  // compliant median, and quoting the median alone puts a passing number
  // forward as the evidence.
  const found = [];
  // health.pool_span: the recent stretch when the periodic could judge it, the
  // worst breached day when it could not.
  const span = p.window_hours ? `over the last ${p.window_hours}h` : p.day != null ? `on ${esc(p.day)}` : null;
  if (span != null && p.p50_s != null && p.p95_s != null && p.threshold_p50_s != null && p.threshold_p95_s != null) {
    found.push(
      `${span} the median wait was ${waitText(p.p50_s)} against a ${Math.floor(p.threshold_p50_s / 60)} min limit` +
        `, p95 ${waitText(p.p95_s)} against ${Math.floor(p.threshold_p95_s / 60)}`,
    );
  }
  if (p.over_threshold && p.threshold_p95_s != null) {
    found.push(`${p.over_threshold} run${p.over_threshold === 1 ? "" : "s"} queued past the ${Math.floor(p.threshold_p95_s / 60)} min limit`);
  }
  if (!found.length) return "";
  // health.pool_note's waiting_now. The verdict lasts a week, so most renders
  // of an episode find the queue already drained, and "are waiting" then sends
  // a reader looking for a jam that ended on Monday. Null is Deck unread: past
  // tense, but no claim that it cleared either.
  const live = p.waiting_now;
  // False is "nothing has waited past the limit", not "the queue is empty".
  const cleared = live === false ? " No backlog right now." : "";
  return ` Runs ${live ? "are" : "were"} waiting to start${since}: ${found.join("; ")}.${cleared} Runs still pass; /retest makes the queue longer.`;
}

/* ---- URL contract ---- */

// The `cases` grammar, shared by the parser and the writer so a link the
// pages build is one the pages read whole: in-grammar ids, the first
// maxLinkCases of them.
function linkCaseIds(values) {
  return values.map((v) => String(v).trim()).filter((id) => PAGE.caseIdRe.test(id)).slice(0, PAGE.maxLinkCases);
}

// The one place a URL is read. The parameters are `key=value` pairs read
// from the query string first (the form links carried before the fragment
// form; a key present in both is the query's) and then from the fragment
// (`#since=…&until=…&cases=…&view=…`, `#build=…`, the Grid's `window=` and
// `rows=`, the Cases page's `sort=` and `show=`). A bare `#gate` or
// `#agent`, the old anchors, still selects that view; a bare case id on
// the Cases page names its row.
function linkState() {
  const out = { cases: new Set(), sinceMs: null, untilMs: null, build: null, view: "", caseHash: null, sort: null, show: null, window: null, rows: null, domain: null, metric: null };
  let query, fragment;
  try {
    query = new URLSearchParams(location.search);
    fragment = new URLSearchParams(location.hash.slice(1));
  } catch (err) { return out; }
  const param = (key) => query.get(key) ?? fragment.get(key) ?? "";
  for (const id of linkCaseIds(param("cases").split(","))) out.cases.add(id);
  const since = param("since");
  if (PAGE.isoParamRe.test(since)) out.sinceMs = parseIso(since);
  const until = param("until");
  if (out.sinceMs != null && PAGE.isoParamRe.test(until)) {
    const untilMs = parseIso(until);
    if (untilMs != null && untilMs >= out.sinceMs) out.untilMs = untilMs;
  }
  const build = param("build").trim();
  if (/^[0-9]{1,25}$/.test(build)) out.build = build;
  // The Grid's and the Cases page's view parameters: a value outside the
  // vocabulary is the default, never text on the page.
  const pick = (key, allowed) => (allowed.includes(param(key)) ? param(key) : null);
  out.sort = pick("sort", ["worst", "domain", "name"]);
  out.show = pick("show", ["all", "blocking", "held"]);
  out.window = pick("window", PAGE.gridWindows.map((w) => w[0]));
  out.rows = pick("rows", ["all", "admitted", "failing"]);
  // The Trend page's scope and metric: a domain slug (the case-id grammar
  // covers it) and a judged metric's name; a value outside its grammar is
  // no parameter, and the metric is checked against the metrics on record
  // when the page renders.
  const domain = param("domain").trim();
  if (PAGE.caseIdRe.test(domain)) out.domain = domain;
  const metric = param("metric").trim();
  if (PAGE.metricRe.test(metric)) out.metric = metric;
  const view = param("view") || location.hash.slice(1);
  out.view = Object.values(PAGE.views).includes(view) ? view : "";
  // A bare fragment that is a case id (`cases.html#<case>`) names the row to
  // highlight. One that is not valid percent-encoding is no case id; it must
  // not stop the page from rendering.
  let bare = "";
  try { bare = decodeURIComponent(location.hash.slice(1) || ""); } catch (err) { bare = ""; }
  if (bare && !out.view && PAGE.caseIdRe.test(bare)) out.caseHash = bare;
  return out;
}

// The links the pages write, in the fragment form linkState reads. Nothing
// is percent-encoded: the case ids passed linkCaseIds' grammar and the
// timestamps are utcIso's `Z` form, so the text is the same one
// post_health.dashboard_link writes.
function scopeParams(inc) {
  const params = [];
  if (inc.sinceMs != null) params.push(`since=${utcIso(inc.sinceMs)}`);
  if (inc.untilMs != null) params.push(`until=${utcIso(inc.untilMs)}`);
  const cases = linkCaseIds(inc.cases || []);
  if (cases.length) params.push(`cases=${cases.join(",")}`);
  return params;
}
const briefHref = (view, inc = {}) => `${PAGE.pages.brief}#${[...scopeParams(inc), `view=${view}`].join("&")}`;
const incidentHref = (inc) => briefHref(PAGE.views.gate, inc);
const numbersHref = () => briefHref(PAGE.views.agent);
// The Grid on the incident's own window: the same scope, no view.
const gridHref = (inc) => { const params = scopeParams(inc); return PAGE.pages.grid + (params.length ? `#${params.join("&")}` : ""); };
const caseHref = (name) => `${PAGE.pages.cases}#${enc(name)}`;
// The Trend page on a set of cases (the Cases page's link, the Brief's last
// incident with its start and end), or on a domain.
const trendHref = (cases, sinceMs = null, untilMs = null) => `${PAGE.pages.trend}#${scopeParams({ cases, sinceMs, untilMs }).join("&")}`;
const trendDomainHref = (domain) => `${PAGE.pages.trend}#domain=${enc(domain)}`;
// The Trend page's own state as a link: the current link with `patch`
// applied (`cases`, `domain`, `metric`, `sinceMs`, `untilMs`). The page's
// chips write this into the fragment rather than into `ui`: its domain and
// case titles are same-document navigations, and a chip state that
// outlived them pinned the view while the URL moved on. One source of
// truth, and a chip's choice is in a link that can be pasted.
function trendStateHref(link, patch) {
  const state = { cases: [...link.cases], domain: link.domain, metric: link.metric, sinceMs: link.sinceMs, untilMs: link.untilMs, ...patch };
  const params = scopeParams({ cases: state.cases, sinceMs: state.sinceMs, untilMs: state.untilMs });
  if (!state.cases.length && state.domain) params.push(`domain=${enc(state.domain)}`);
  if (state.metric) params.push(`metric=${enc(state.metric)}`);
  return `${PAGE.pages.trend}#${params.join("&")}`;
}
const runHref = (run) => `${PAGE.pages.run}#build=${enc(run.build)}`;

/* ---- links out ---- */

const prText = (pr) => (pr == null ? "no PR" : `PR #${pr}`);
const prLink = (pr) => (pr == null ? prText(pr) : `<a href="${PAGE.prUrl}/${esc(pr)}">${esc(prText(pr))}</a>`);
const buildUrl = (run) => (run.pr == null ? null : `${PAGE.spyglass}/${enc(run.pr)}/${PAGE.job}/${enc(run.build)}`);
const transcriptUrl = (run, kase, n = 1) => {
  const base = buildUrl(run);
  return base ? `${base}/artifacts/eval_${enc(kase)}_rep${enc(n)}.log` : null;
};
// The repetition whose reason and quote a case row shows (classify.py's
// rep_n); rep 1, as the pages always linked, when the row names none.
const repOf = (c) => (Number.isInteger(c.rep_n) && c.rep_n > 0 ? c.rep_n : 1);
const issueLink = (issue) => {
  const match = /^#(\d+)$/.exec(String(issue).trim());
  return match ? `<a href="${PAGE.issueUrl}/${match[1]}">${esc(issue)}</a>` : esc(issue);
};
const projectShort = (project) => (project ? String(project).replace(/^kube-agents-/, "") : "unknown");

/* ---- runs and windows ---- */

const runs = () => (Array.isArray(brief.runs) ? brief.runs.filter((r) => r && typeof r === "object") : []);
const runFinish = (run) => parseIso(run.finished) ?? parseIso(run.started);
const runStart = (run) => parseIso(run.started) ?? parseIso(run.finished);
const measured = (run) => Array.isArray(run.cases) && run.cases.length > 0;
const concluded = (run) => run.result === "SUCCESS" || run.result === "FAILURE";
const isGreen = (run) => run.result === "SUCCESS";
const gateFailures = (run) => (run.cases || []).filter((c) => c.admitted && c.outcome === "failed").map((c) => c.case);
const heldOutFailures = (run) => (run.cases || []).filter((c) => !c.admitted && c.outcome === "failed").map((c) => c.case);
// A superseded push (aborted, nothing recorded) says nothing about the gate;
// the Brief counts it and the Grid gives it no column.
const tellsSomething = (run) => measured(run) || run.setup_death || concluded(run);
// A Grid column: a run that graded cases, or died before them. A green run
// with no cases recorded (the gate revalidated the branch's earlier run) is
// neither; it gets no column rather than a row of "died" cells.
const gridColumnRun = (run) => measured(run) || run.setup_death || (concluded(run) && !isGreen(run));

function windowRuns(sinceMs, untilMs) {
  return runs().filter((run) => {
    const when = runFinish(run);
    return when != null && when >= sinceMs && (untilMs == null || when <= untilMs);
  }).sort((a, b) => runFinish(a) - runFinish(b));
}

/* ---- the incident the Brief is about ---- */

const historyIncidents = () => (brief.history && Array.isArray(brief.history.incidents) ? brief.history.incidents : []);

function incidentFromHealth(h) {
  return {
    state: h.state, condition: h.condition, cases: h.failing_cases.slice(),
    sinceMs: parseIso(h.since), untilMs: null, live: true, past: false,
    recovering: h.recovering, tracking: h.tracking_issues, advice: h.advice, cause: h.cause,
    stale: h.stale, windowEndMs: h.incident ? parseIso(h.incident.window_end) : null,
    prs: h.incident ? h.incident.prs : [],
  };
}

function incidentFromHistory(entry) {
  return {
    state: entry.state, condition: entry.condition, cases: (entry.failing_cases || []).slice(),
    sinceMs: parseIso(entry.since), untilMs: parseIso(entry.until), live: entry.until == null, past: entry.until != null,
    recovering: false, tracking: entry.tracking_issues || [], advice: entry.advice || "", cause: entry.cause || "",
    stale: false, windowEndMs: null, prs: [],
  };
}

// The link's since is matched to a history incident it falls inside (or
// within one tick of); without history the parameters describe the incident.
function resolveIncident(link) {
  if (link.sinceMs != null) {
    const slack = PAGE.hourMs;
    // The link names the current verdict's own start (a PR-view banner, a
    // Chat message): that is the live incident, with or without history.
    if (health && health.state !== "GREEN" && parseIso(health.since) != null && Math.abs(parseIso(health.since) - link.sinceMs) <= slack && link.untilMs == null) {
      const live = incidentFromHealth(health);
      if (link.cases.size) live.cases = [...link.cases];
      return live;
    }
    const hit = historyIncidents().map(incidentFromHistory).find((inc) => inc.sinceMs != null
      && link.sinceMs >= inc.sinceMs - slack && link.sinceMs <= (inc.untilMs ?? Infinity));
    if (hit) {
      if (link.cases.size) hit.cases = [...link.cases];
      if (link.untilMs != null) hit.untilMs = link.untilMs;
      if (hit.live && health && health.state !== "GREEN" && parseIso(health.since) === hit.sinceMs) {
        // The open incident is the current verdict: carry its live details.
        Object.assign(hit, { recovering: health.recovering, tracking: health.tracking_issues.length ? health.tracking_issues : hit.tracking, advice: health.advice || hit.advice, stale: health.stale, windowEndMs: health.incident ? parseIso(health.incident.window_end) : null });
      }
      return hit;
    }
    return {
      state: "PAST", condition: link.cases.size ? "shared_break" : null, cases: [...link.cases],
      sinceMs: link.sinceMs, untilMs: link.untilMs, live: false, past: true, recovering: false,
      tracking: [], advice: "", cause: "", stale: false, windowEndMs: null, prs: [],
    };
  }
  if (health && health.state !== "GREEN") return incidentFromHealth(health);
  return null;
}

const incidentEndMs = (inc) => inc.untilMs ?? nowMs();
const isBreak = (inc) => inc.condition === "shared_break" || (inc.condition == null && inc.cases.length > 0);
const incidentLeadMs = (inc) => PAGE.incidentLeadMs[isBreak(inc) ? "shared_break" : inc.condition] ?? 0;
const incidentStartMs = (inc) => (inc.sinceMs ?? incidentEndMs(inc) - PAGE.numbersWindowMs) - incidentLeadMs(inc);

/* ---- facts: the yes/no lines under "why we think" ---- */

const fact = (yes, html) => ({ yes, html });

function breakFacts(inc, inWindow) {
  const cases = new Set(inc.cases);
  const hit = inWindow.filter((run) => gateFailures(run).some((c) => cases.has(c)) || (run.cases || []).some((c) => cases.has(c.case) && c.outcome === "failed"));
  const prs = new Set(hit.map((r) => r.pr).filter((p) => p != null));
  const ranIn = new Set(inWindow.filter((r) => (r.cases || []).some((c) => cases.has(c.case))).map((r) => r.project).filter(Boolean));
  const failedIn = new Set(hit.map((r) => r.project).filter(Boolean));
  const facts = [];
  facts.push(fact(prs.size >= 3,
    `The same ${plural(cases.size, "case")} fail${cases.size === 1 ? "s" : ""} on <b>${plural(prs.size, "unrelated PR")}</b>${prs.size ? ` (${[...prs].slice(0, 8).map((p) => `#${esc(p)}`).join(", ")}${prs.size > 8 ? ", …" : ""})` : ""}.`));
  if (ranIn.size) {
    facts.push(fact(failedIn.size === ranIn.size && ranIn.size > 1,
      failedIn.size === ranIn.size
        ? `They fail in <b>every project</b> they ran in (${failedIn.size} of ${ranIn.size}). Not one bad cluster.`
        : `They fail in ${failedIn.size} of the ${ranIn.size} projects they ran in.`));
  }
  const mergeFact = mergesFact(inc, inWindow);
  if (mergeFact) facts.push(mergeFact);
  let reps = 0, storm = 0;
  for (const run of hit) for (const c of run.cases || []) { reps += repTotal(c.reps); storm += c.reps.infra; }
  const quiet = reps > 0 && storm / reps < PAGE.stormNoiseShare;
  facts.push(fact(!quiet, quiet
    ? `Not a quota storm: the agent ran and was graded on ${pct(1 - storm / reps)} of repetitions in these runs.`
    : `A quota storm overlaps: ${storm} of ${reps} repetitions in these runs were lost before the agent ran.`, quiet));
  return facts;
}

function stormFacts(inc, inWindow) {
  let reps = 0, storm = 0;
  const prs = new Set();
  const projects = new Set();
  for (const run of inWindow) {
    let mine = 0;
    for (const c of run.cases || []) { reps += repTotal(c.reps); storm += c.reps.infra; mine += c.reps.infra; }
    if (mine) { if (run.pr != null) prs.add(run.pr); if (run.project) projects.add(run.project); }
  }
  const graded = inWindow.flatMap((r) => (r.cases || []).filter((c) => c.admitted && c.outcome !== "infra"));
  const passed = graded.filter((c) => c.outcome === "passed" || c.outcome === "partial").length;
  const collapsed = new Map();
  for (const run of inWindow) for (const c of gateFailures(run)) collapsed.set(c, (collapsed.get(c) || new Set()).add(run.pr));
  const widest = Math.max(0, ...[...collapsed.values()].map((s) => s.size));
  return [
    fact(true, `<b>${storm} of ${reps} repetitions</b> came back with no agent run (429s, empty records) across ${plural(prs.size, "PR")}.`),
    fact(graded.length > 0, graded.length
      ? `When the agent did run it mostly passed: ${passed} of ${graded.length} graded gate cases.`
      : "Nothing was graded in this window at all."),
    fact(projects.size > 1, `Spread over ${plural(projects.size, "project")}, so not one bad cluster.`),
    fact(widest < 3, widest < 3
      ? `No single case fails everywhere: the widest shared failure is on ${plural(widest, "PR")}.`
      : `One case also fails on ${plural(widest, "PR")}; a shared break may be underneath.`),
  ];
}

// The delegation-ceiling brief's facts (#1874): the storm's shape over
// ceiling reps. `ceiling_reps` on the run doc anchors the incident's first run.
function ceilingFacts(inc, inWindow) {
  let reps = 0, ceiling = 0;
  const prs = new Set();
  const projects = new Set();
  for (const run of inWindow) {
    let mine = 0;
    for (const c of run.cases || []) { reps += repTotal(c.reps); ceiling += c.reps.ceiling || 0; mine += c.reps.ceiling || 0; }
    if (mine) { if (run.pr != null) prs.add(run.pr); if (run.project) projects.add(run.project); }
  }
  const graded = inWindow.flatMap((r) => (r.cases || []).filter((c) => c.admitted && c.outcome !== "infra"));
  const passed = graded.filter((c) => c.outcome === "passed" || c.outcome === "partial").length;
  const collapsed = new Map();
  for (const run of inWindow) for (const c of gateFailures(run)) collapsed.set(c, (collapsed.get(c) || new Set()).add(run.pr));
  const widest = Math.max(0, ...[...collapsed.values()].map((s) => s.size));
  return [
    fact(true, `<b>${ceiling} of ${reps} repetitions</b> ended at the harness's delegation wait with the worker still running, across ${plural(prs.size, "PR")}; none of them was graded or counted against a case.`),
    fact(graded.length > 0, graded.length
      ? `When a worker did finish it mostly passed: ${passed} of ${graded.length} graded gate cases.`
      : "Nothing was graded in this window at all."),
    fact(projects.size > 1, `Spread over ${plural(projects.size, "project")}, so not one bad cluster.`),
    fact(widest < 3, widest < 3
      ? `No single case fails everywhere: the widest shared failure is on ${plural(widest, "PR")}.`
      : `One case also fails on ${plural(widest, "PR")}; a shared break may be underneath.`),
  ];
}

function setupFacts(inc, inWindow) {
  const deaths = inWindow.filter((r) => r.setup_death);
  const prs = new Set(deaths.map((r) => r.pr).filter((p) => p != null));
  const projects = new Set(deaths.map((r) => r.project).filter(Boolean));
  const survived = inWindow.filter((r) => measured(r) && concluded(r));
  const green = survived.filter(isGreen).length;
  return [
    fact(true, `<b>${plural(deaths.length, "run")}</b> on ${plural(prs.size, "PR")} died within 5 minutes, before any case ran.`),
    fact(prs.size > 1, prs.size > 1 ? "More than one PR, so not one broken branch." : "Only one PR so far; it may be that branch."),
    fact(survived.length > 0, survived.length
      ? `Runs that got past setup in the same window: ${green} green of ${survived.length}.`
      : "No run has got past setup in this window yet."),
    fact(projects.size > 0, projects.size ? `The deaths hit ${plural(projects.size, "project")}: ${[...projects].map(projectShort).map(esc).join(", ")}.` : "The dying runs never leased a project."),
  ];
}

// health.Run.has_verdict: the eval's own verdict, or -- only for a record
// from before `eval_verdict` existed, which is never a kill -- a concluded
// run; either way with at least one graded repetition, since NOT EVALUATED
// records as RED. A recorded null is no verdict whatever the run carries.
const gradedAny = (r) => (r.cases || []).some((c) => c.outcome === "passed" || c.outcome === "partial" || c.outcome === "failed");
function hasVerdict(r) {
  if (!concluded(r) || !gradedAny(r)) return false;
  return "eval_verdict" in r ? r.eval_verdict != null : true;
}

function deadlineFacts(inc, inWindow) {
  const killed = inWindow.filter((r) => r.cls === "deadline-kill");
  const prs = new Set(killed.map((r) => r.pr).filter((p) => p != null));
  const graded = inWindow.filter(hasVerdict);
  return [
    fact(true, `<b>${plural(killed.length, "run")}</b> on ${plural(prs.size, "PR")} ran to the job deadline and ended with no verdict.`),
    fact(prs.size > 1, prs.size > 1 ? "More than one PR, so not one slow branch." : "Only one PR so far; it may be that branch."),
    fact(graded.length > 0, graded.length ? `Runs that reached a verdict in the same window: ${graded.length}.` : "No run in this window has reached a verdict."),
  ];
}

function mergesFact(inc, inWindow) {
  if (!Array.isArray(brief.merges)) return null;
  const firstRed = inWindow.find((r) => gateFailures(r).some((c) => inc.cases.includes(c)));
  const firstRedMs = firstRed ? runFinish(firstRed) : inc.sinceMs;
  // No red run in the window and no parseable `since`: nothing anchors
  // "before", so the line is dropped rather than dated from epoch zero.
  if (firstRedMs == null) return null;
  const greensBefore = runs().filter((r) => isGreen(r) && measured(r) && runFinish(r) < firstRedMs).sort((a, b) => runFinish(a) - runFinish(b));
  const fromMs = greensBefore.length ? runFinish(greensBefore[greensBefore.length - 1]) : firstRedMs - PAGE.mergesLookbackMs;
  const merges = brief.merges.filter((m) => { const at = parseIso(m.at); return at != null && at >= fromMs && at <= firstRedMs; });
  if (!merges.length) return fact(false, `Nothing merged to main between the last green run (${esc(et(fromMs))}) and the first red one.`);
  return fact(true, `${plural(merges.length, "merge")} to main between the last green run and the first red one: ${merges.slice(0, 4).map((m) => m.pr != null ? `<a href="${PAGE.prUrl}/${esc(m.pr)}">#${esc(m.pr)}</a>` : esc(String(m.sha).slice(0, 7))).join(", ")}${merges.length > 4 ? ", …" : ""}.`);
}

/* ---- fragments shared by the pages ---- */

function pillHtml(state, text) {
  const key = state in PAGE.states ? state : "PAST";
  return `<span class="hpill ${PAGE.states[key]}">${PAGE.glyphs[key]} ${esc(text)}</span>`;
}

function factsHtml(facts) {
  return `<ul class="facts">${facts.map((f) => `<li><span class="${f.yes ? "y" : "n"}">${f.yes ? "yes" : "no"}</span><span>${f.html}</span></li>`).join("")}</ul>`;
}

function stateWord(inc) {
  if (inc.recovering) return "RECOVERING";
  if (inc.past) return `PAST ${inc.state === "PAST" ? "INCIDENT" : inc.state}`;
  return inc.state;
}

function stormRetestMs(inc) {
  if (inc.windowEndMs != null) return inc.windowEndMs + PAGE.stormCooldownMs;
  return null;
}

// One chip row: [{key, value, label, on}] as buttons the click handler reads.
const chip = (key, value, label, on) => `<button type="button" data-${key}="${esc(value)}"${on ? ' class="on"' : ""}>${esc(label)}</button>`;

/* ---- the Brief ---- */

function briefHeadline(inc, inWindow) {
  const k = inc.cases.length;
  const caseWord = plural(k, "gate case");
  if (isBreak(inc)) {
    const reds = inWindow.filter((r) => concluded(r) && !isGreen(r) && measured(r));
    const covered = reds.filter((r) => gateFailures(r).some((c) => inc.cases.includes(c)));
    const every = reds.length > 0 && covered.length / reds.length >= PAGE.everyPrShare;
    const verb = inc.past ? (k === 1 ? "failed" : "failed") : (k === 1 ? "fails" : "fail");
    const head = k
      ? `${caseWord} ${verb} on ${every ? "every" : "most"} PR${every ? "" : "s"}`
      : (inc.past ? "A past gate incident" : "The gate is broken for every PR");
    const tracking = inc.tracking.length ? ` Tracking ${inc.tracking.map(issueLink).join(", ")}.` : "";
    const lede = `${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `Since ${esc(et(inc.sinceMs))}`}. ${covered.length} of the ${plural(reds.length, "red gate run")} in this window ${reds.length === 1 ? "is" : "are"} red because of ${k === 1 ? "this case" : "these cases"}.${tracking}`;
    return { head, lede };
  }
  if (inc.condition === "storm") {
    const retest = stormRetestMs(inc);
    return {
      head: inc.past ? "The agent could not run: repetitions were lost to API quota" : "The agent isn't getting to run: repetitions are being lost to API quota",
      lede: `${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `Since ${esc(et(inc.sinceMs))}`}. Runs started inside the storm come back with 429s and empty records instead of a graded answer.${retest != null && !inc.past ? ` Retest after ${esc(et(retest))}.` : ""}`,
    };
  }
  if (inc.condition === "setup_deaths") {
    return {
      head: inc.past ? "Runs died before any case ran" : "Runs are dying before any case runs",
      lede: `${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `Since ${esc(et(inc.sinceMs))}`}. The leased project failed at clone or deploy, so the agent was never started.`,
    };
  }
  if (inc.condition === "delegation_ceiling") {
    return {
      head: inc.past ? "Workers did not finish: repetitions ended at the harness's delegation wait" : "Workers aren't finishing: repetitions are ending at the harness's delegation wait",
      lede: `${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `Since ${esc(et(inc.sinceMs))}`}. The front door delegated and the card was still running when the eval stopped waiting, so nothing was graded; those runs read not evaluated, not red. The gateway log in a run's artifacts says whether the dispatcher stalled (#1879).`,
    };
  }
  if (inc.condition === "deadline_kill") {
    return {
      head: inc.past ? "Runs were killed at the job deadline with nothing graded" : "Runs are being killed at the job deadline with nothing graded",
      lede: `${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `Since ${esc(et(inc.sinceMs))}`}. Prow ended each run at the job's timeout before the eval reached a verdict, so no pull request can pass; nothing about those pull requests is implied.`,
    };
  }
  return { head: inc.past ? "A past gate incident" : "The gate is degraded", lede: esc(inc.cause || "") };
}

// The bar out of a deadline-kill outage is a verdict either way (health.py
// `recovered`): a red that graded proves the gate grades again.
function verdictRecovery(inc) { return inc.condition === "deadline_kill"; }
function recoveryBarText(inc) { return verdictRecovery(inc) ? "runs with a verdict" : "clean runs"; }

function recoveryProgress(inc) {
  // No start on record: nothing is "after" the incident, so no run counts.
  if (inc.sinceMs == null) return 0;
  if (verdictRecovery(inc)) {
    // health.recovered's deadline branch: the newest RECOVERY_GREEN_RUNS
    // verdict runs must all follow the last kill and sit on distinct PRs. A
    // zero-task kill is in the list because it is what stops the count.
    const later = runs().filter((r) => (hasVerdict(r) || r.cls === "deadline-kill") && runFinish(r) > inc.sinceMs).sort((a, b) => runFinish(b) - runFinish(a));
    const newest = [];
    for (const run of later) {
      if (run.cls === "deadline-kill") break;
      newest.push(run);
      if (newest.length >= PAGE.recoveryGreenRuns) break;
    }
    return new Set(newest.map((r) => r.pr)).size;
  }
  const later = runs().filter((r) => measured(r) && concluded(r) && runFinish(r) > inc.sinceMs).sort((a, b) => runFinish(b) - runFinish(a));
  const prs = new Set();
  let count = 0;
  for (const run of later) {
    if (!isGreen(run) || run.matches_incident) break;
    if (run.pr != null && prs.has(run.pr)) continue;
    prs.add(run.pr);
    count += 1;
    if (count >= PAGE.recoveryGreenRuns) break;
  }
  return count;
}

function whyTitle(inc) {
  if (isBreak(inc)) return "Why we think it's the environment, not a PR";
  if (inc.condition === "storm") return "Why we think it's a quota storm";
  if (inc.condition === "setup_deaths") return "Why we think it's the setup, not the PRs";
  if (inc.condition === "delegation_ceiling") return "Why we think it's the workers, not the PRs";
  if (inc.condition === "deadline_kill") return "Why we think it's the gate, not the PRs";
  return "What the data shows";
}

function agentSawHtml(inc, inWindow) {
  const cases = new Set(inc.cases);
  let pick = null;
  for (const run of [...inWindow].reverse()) {
    for (const c of run.cases || []) {
      if (inc.condition === "storm" || inc.condition === "delegation_ceiling" ? c.outcome === "infra" && c.reason : (cases.size ? cases.has(c.case) : c.admitted) && c.outcome === "failed" && c.reason) {
        pick = { run, c };
        break;
      }
    }
    if (pick) break;
  }
  if (inc.condition === "deadline_kill") {
    const killed = [...inWindow].reverse().find((r) => r.cls === "deadline-kill");
    const url = killed ? buildUrl(killed) : null;
    return `<p>No verdict was reached. The build log is the evidence${url ? `: <a href="${esc(url)}">${esc(prText(killed.pr))} at ${esc(et(runFinish(killed)))}</a>` : ""}; it shows how far the units got (on 2026-09-22 every unit ended at the delegation ceiling, #1880).</p>`;
  }
  if (inc.condition === "setup_deaths") {
    const death = [...inWindow].reverse().find((r) => r.setup_death);
    const url = death ? buildUrl(death) : null;
    return `<p>No agent ran. The build log is the evidence${url ? `: <a href="${esc(url)}">${esc(prText(death.pr))} at ${esc(et(runFinish(death)))}</a>` : ""}.</p>`;
  }
  if (!pick) return `<p class="mut">No failed repetition with a recorded reason in this window.</p>`;
  const n = repOf(pick.c);
  const url = transcriptUrl(pick.run, pick.c.case, n);
  const quote = pick.c.excerpt
    ? `<div class="q">“${esc(pick.c.excerpt)}”<small>From the agent's report on ${prLink(pick.run.pr)} · <code>${esc(pick.c.case)}</code>${url ? ` · <a href="${esc(url)}">full transcript</a>` : ""}</small></div>`
    : "";
  return `${quote}<div class="reason">${esc(pick.c.reason)}</div><small class="mut">The check that failed, as the grader wrote it, on ${prLink(pick.run.pr)} · <code>${esc(pick.c.case)}</code>${url ? ` · <a href="${esc(url)}">transcript (rep ${n})</a>` : ""}.${pick.c.excerpt ? "" : " The build log carried no report excerpt for this repetition, so nothing here is quoted from the agent."}</small>`;
}

function changedBeforeHtml(inc, inWindow) {
  if (!Array.isArray(brief.merges)) return "";
  const firstRed = inWindow.find((r) => isBreak(inc) ? gateFailures(r).some((c) => inc.cases.includes(c)) : (inc.condition === "setup_deaths" ? r.setup_death : inc.condition === "delegation_ceiling" ? (r.ceiling_reps || 0) > 0 : inc.condition === "deadline_kill" ? r.cls === "deadline-kill" : (r.storm_reps || 0) > 0));
  const firstRedMs = firstRed ? runFinish(firstRed) : inc.sinceMs;
  if (firstRedMs == null) {
    // Same anchor as mergesFact: without it there is no "before" to show.
    return `<div class="sec"><h2>What changed right before</h2><p class="mut">The incident has no start time on record and no run in this window anchors it, so the merges before it cannot be picked out.</p></div>`;
  }
  const fromMs = firstRedMs - PAGE.mergesLookbackMs;
  const merges = brief.merges.filter((m) => { const at = parseIso(m.at); return at != null && at >= fromMs && at <= firstRedMs; });
  const body = merges.length
    ? `<ul class="merges">${merges.map((m) => `<li><span class="mut">${esc(et(parseIso(m.at)))}</span> ${m.pr != null ? `<a href="${PAGE.prUrl}/${esc(m.pr)}">#${esc(m.pr)}</a>` : `<code>${esc(String(m.sha).slice(0, 7))}</code>`} ${esc(m.title || "")}</li>`).join("")}</ul>`
    : `<p>Nothing merged to main in the ${Math.round(PAGE.mergesLookbackMs / PAGE.hourMs)} hours before the first red run (${esc(et(firstRedMs))}).</p>`;
  return `<div class="sec"><h2>What changed right before</h2>${body}</div>`;
}

function beingDoneHtml(inc) {
  const lines = [];
  if (inc.tracking.length) lines.push(`<p>Tracking ${inc.tracking.map(issueLink).join(", ")}. The gate comes back on its own once the fix lands: the bot reports healthy after ${PAGE.recoveryGreenRuns} ${recoveryBarText(inc)} on different PRs.</p>`);
  if (inc.recovering) lines.push(`<p><b>The condition has cleared.</b> A retest is reasonable now; ${recoveryProgress(inc)} of ${PAGE.recoveryGreenRuns} ${recoveryBarText(inc)} on distinct PRs so far.</p>`);
  else if (isBreak(inc) && !inc.tracking.length && !inc.past) lines.push(`<p>No issue is filed yet. File one with the <code>presubmit-gate</code> label and link this page. Demoting the case in <code>hack/eval/blocking-roster.txt</code> unblocks merges while the fixture is fixed; re-admit it afterwards.</p>`);
  else if (inc.condition === "storm" && !inc.past) {
    const retest = stormRetestMs(inc);
    lines.push(`<p>Wait it out${retest != null ? `: retest after ${esc(et(retest))}` : ""}. The API quota is fixed, so fewer runs at once is the only lever; a retest inside the storm loses repetitions the same way.</p>`);
  } else if (inc.condition === "setup_deaths" && !inc.past) lines.push(`<p>Check the leased pool projects before spending another run: a stuck Helm release or a failing image pull is the usual cause. Retest once the deaths stop.</p>`);
  else if (inc.condition === "delegation_ceiling" && !inc.past) lines.push(`<p>Retest once workers are finishing again. Read <code>platform-agent-gateway.log</code> in a run's artifacts for <code>kanban dispatcher stuck</code> and <code>RESOURCE_EXHAUSTED</code> lines: a stalled dispatcher is #1879, starved workers are the quota.</p>`);
  else if (inc.condition === "deadline_kill" && !inc.past) lines.push(`<p>Don't retest: a run started now ends the same way. Each killed run's <code>build-log.txt</code> shows how far its units got, and the gateway and dispatcher lines in the eval project's Cloud Logging say what the workers were doing. The bot reports healthy once the newest ${PAGE.recoveryGreenRuns} runs with a verdict, green or red, are on distinct PRs and all follow the last kill.</p>`);
  if (inc.past) lines.push(`<p class="mut">This incident is over${inc.untilMs != null ? `; the gate was reported healthy again at ${esc(et(inc.untilMs))}` : ""}.</p>`);
  if (inc.stale) lines.push(`<p class="stale">The data behind this state stopped refreshing; the state is as old as the data.</p>`);
  if (!lines.length) return "";
  return `<div class="next"><h2>${inc.past ? "What was done" : "What's being done"}</h2>${lines.join("")}</div>`;
}

function runsListHtml(inWindow, inc, title) {
  const cases = new Set(inc ? inc.cases : []);
  const shown = inWindow.filter(tellsSomething);
  const hidden = inWindow.length - shown.length;
  const rows = [...shown].reverse().map((run) => {
    const failed = gateFailures(run);
    const held = heldOutFailures(run).length;
    const chips = failed.map((c) => `<span class="chip ${cases.has(c) ? "hit" : "miss"}">${esc(c)}</span>`).join("");
    let note = "";
    if (run.setup_death) note = '<span class="chip inf">died in setup</span>';
    else if (!measured(run)) note = `<span class="chip inf">${run.result === "ABORTED" ? "aborted" : "no cases recorded"}</span>`;
    // A run whose every recorded case went ungraded (a storm, or every
    // worker at the delegation ceiling) passed nothing; the chips say why.
    else if (!failed.length) note = (run.cases || []).some((c) => c.outcome !== "infra") ? '<span class="chip ok">all gate cases passed</span>' : '<span class="chip inf">nothing graded</span>';
    const stormChip = (run.storm_reps || 0) >= 5 ? `<span class="chip inf">${run.storm_reps} reps lost</span>` : "";
    const ceilingChip = (run.ceiling_reps || 0) >= 5 ? `<span class="chip inf">${run.ceiling_reps} reps at the delegation ceiling</span>` : "";
    return `<a class="runrow v-${esc(run.verdict || "infra")}" href="${esc(runHref(run))}">` +
      `<span class="rpr">#${esc(run.pr ?? "?")}</span><span class="rwhen">${esc(et(runFinish(run)))}</span>` +
      `<span class="rproj">${esc(projectShort(run.project))}</span><span class="rcases">${chips}${note}${stormChip}${ceilingChip}${held ? `<span class="mut small">+${held} held-out</span>` : ""}</span></a>`;
  });
  // The incident's own window on the Grid: the same cases, since and until.
  const grid = inc ? `<p><a href="${esc(gridHref(inc))}">See it in the grid →</a></p>` : "";
  return `<div class="sec"><h2>${esc(title)}</h2><div class="runs">${rows.join("") || '<p class="mut">No runs in this window.</p>'}</div>` +
    `<p class="mut small">Each row opens that run's page. Red chips are the incident's cases; amber ones are other gate failures.${hidden ? ` ${plural(hidden, "aborted run")} not listed.` : ""}</p>${grid}</div>`;
}

function numbers(sinceMs, untilMs) {
  const all = windowRuns(sinceMs, untilMs);
  const full = all.filter(measured);
  const done = full.filter(concluded);
  const green = done.filter(isGreen);
  const reds = done.length - green.length;
  const own = done.filter((r) => !isGreen(r) && r.verdict === "red").length;
  const deaths = all.filter((r) => r.setup_death).length;
  const walls = done.map((r) => (parseIso(r.finished) ?? 0) - (parseIso(r.started) ?? 0)).filter((w) => w > 0).sort((a, b) => a - b);
  const p = (q) => (walls.length ? walls[Math.round(q * (walls.length - 1))] : null);
  let reps = 0, lost = 0;
  for (const run of full) for (const c of run.cases || []) { reps += repTotal(c.reps); lost += c.reps.infra; }
  return { full: full.length, prs: new Set(full.map((r) => r.pr).filter((x) => x != null)).size, green: green.length, reds, own, infra: reds - own + deaths, deaths, p50: p(0.5), p90: p(0.9), lostShare: reps ? lost / reps : null, aborted: all.filter((r) => !concluded(r)).length };
}

function tile(key, value, detail) {
  return `<div class="tile"><div class="k">${esc(key)}</div><div class="v">${value}</div><div class="d2">${esc(detail)}</div></div>`;
}

function numbersHtml(sinceMs, untilMs) {
  const n = numbers(sinceMs, untilMs);
  return `<div class="tiles">` +
    tile("Runs", `${n.full}`, `${plural(n.prs, "PR")} · ${n.aborted} aborted or unfinished`) +
    tile("Green", n.full ? `${n.green}<small>/ ${n.green + n.reds}</small>` : "—", n.green + n.reds ? `${pct(n.green / (n.green + n.reds))} of concluded runs` : "no concluded runs") +
    tile("Reds", `${n.reds}`, `${n.own} look like the PR · ${n.infra} the gate's (incl. ${n.deaths} setup ${n.deaths === 1 ? "death" : "deaths"})`) +
    tile("Wall clock", n.p50 != null ? `${Math.round(n.p50 / 60000)}<small>min p50</small>` : "—", n.p90 != null ? `${Math.round(n.p90 / 60000)} min p90` : "no timings") +
    tile("Reps lost", n.lostShare != null ? `${(100 * n.lostShare).toFixed(1)}<small>%</small>` : "—", "429s and empty records, over all repetitions") +
    `</div>`;
}

function lastIncidentHtml() {
  const past = historyIncidents().map(incidentFromHistory).filter((inc) => inc.sinceMs != null).sort((a, b) => b.sinceMs - a.sinceMs);
  if (!past.length) return brief.history ? `<p class="mut">No incident on record yet.</p>` : `<p class="mut">No incident history is published yet, so only the current state is shown.</p>`;
  const inc = past[0];
  const what = isBreak(inc) ? `${plural(inc.cases.length, "gate case")} failing on every PR` : inc.condition === "storm" ? "a quota storm" : inc.condition === "setup_deaths" ? "runs dying in setup" : inc.condition === "delegation_ceiling" ? "workers not finishing (delegation ceiling)" : inc.condition === "deadline_kill" ? "runs killed at the job deadline" : "a degraded gate";
  return `<p>${pillHtml(inc.state, `PAST ${inc.state}`)} <b>${esc(etSpan(inc.sinceMs, inc.untilMs))}</b> — ${what}${inc.cases.length ? ` (<code>${inc.cases.map(esc).join("</code>, <code>")}</code>)` : ""}. <a href="${esc(incidentHref(inc))}">Open the brief for it →</a> <a href="${esc(trendHref(inc.cases, inc.sinceMs, inc.untilMs))}">The record on main around the night it started →</a></p>`;
}

/* ---- the Brief's release table (SCHEMA.md: releases[]) ---- */

const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

function releaseRow(r) {
  const tag = r.rc_tag || "unknown candidate";
  const name = r.artifacts_url ? `<a href="${esc(r.artifacts_url)}" rel="noopener">${esc(tag)}</a>` : esc(tag);
  const bits = [];
  if (r.commit) bits.push(String(r.commit));
  if (r.build) bits.push(`build ${r.build}`);
  // The banner is missing, so Prow's own result is the only thing left that
  // says whether the job survived. Say so rather than showing a blank row
  // that reads like a quiet pass.
  if (!r.verdict) bits.push(`no eval banner · job ${r.result || "unknown"}`);
  const cls = PAGE.releaseVerdictClass[r.verdict || ""] || "p-infra";
  let rate;
  if (!isNumber(r.pass_rate)) rate = `—<div class="tnote">not reported</div>`;
  else {
    let note = "no baseline — baselines maturing";
    if (isNumber(r.baseline_rate)) {
      note = `main ${(r.baseline_rate * 100).toFixed(1)}%`;
      // Points, not percent: the margin is a difference of two rates.
      if (isNumber(r.margin)) note += ` · margin ${r.margin >= 0 ? "+" : "−"}${(Math.abs(r.margin) * 100).toFixed(1)}pt`;
    }
    rate = `${(r.pass_rate * 100).toFixed(1)}%<div class="tnote">${esc(note)}</div>`;
  }
  const cases = r.cases
    ? `${r.cases.passed}/${r.cases.graded}<div class="tnote">${r.cases.infra ? `${r.cases.infra} infra excluded` : "none excluded"}</div>`
    : `—<div class="tnote">no cases parsed</div>`;
  const started = parseIso(r.started);
  return `<tr><td><b>${name}</b><div class="tnote">${esc(bits.join(" · "))}</div></td>` +
    `<td class="mut">${esc(r.tier || "—")}</td>` +
    `<td><span class="pill ${cls}">${esc(r.verdict || "NO VERDICT")}</span></td>` +
    `<td class="num">${rate}</td><td class="num">${cases}</td>` +
    `<td class="mut">${esc(started != null ? et(started) : "unknown time")}</td>` +
    `<td class="mut">${esc(r.duration_s ? minutesText(r.duration_s * 1000) : "")}</td></tr>`;
}

function releasesHtml() {
  const releases = (Array.isArray(brief.releases) ? brief.releases : []).filter((r) => r && typeof r === "object");
  const body = releases.length
    ? `<table class="rel"><thead><tr><th>Candidate</th><th>Tier</th><th>Verdict</th><th>Admitted rate</th><th>Cases passed</th><th>Started</th><th>Eval took</th></tr></thead>` +
      `<tbody>${releases.map(releaseRow).join("")}</tbody></table>` +
      `<p class="mut small">One row per <code>post-kube-agents-eval-rc</code> run: the full suite against a release candidate's own images. Advisory: this lane reports and does not gate, and the non-inferiority comparison stays advisory while the baseline store is maturing. The admitted rate covers only the cases admitted to the gate; cases passed counts every graded case in the run.</p>`
    : `<p class="mut">No release-candidate eval run on record. When the next RC cuts, its run appears here: candidate, tier, verdict and the advisory non-inferiority number.</p>`;
  return `<div class="sec" id="releases"><h2>Release candidates</h2>${body}</div>`;
}

function briefHtml(link) {
  const inc = resolveIncident(link);
  const anchor = nowMs();
  if (link.view === PAGE.views.agent || !inc) {
    const sinceMs = anchor - PAGE.numbersWindowMs;
    const healthy = !inc && !!health;
    const noVerdict = !inc && !health;
    const n = numbers(sinceMs, null);
    let head, lede, pill;
    if (healthy) {
      head = "Smoke gate is healthy";
      lede = `No shared breaks, quota storms or setup failures right now. ${n.green} of ${n.green + n.reds} concluded runs in the last 24 hours were green.${health.stale ? " The data behind this state has stopped refreshing." : ""}${slowSentence(health)}${poolSentence(health)}`;
      pill = pillHtml(health.stale ? "DEGRADED" : "GREEN", health.stale ? "HEALTHY · STALE" : "HEALTHY");
    } else if (noVerdict) {
      // No health.json beside the data: the runs alone cannot say the gate is healthy.
      head = "No gate verdict is published";
      lede = `The adjudicator's health.json is not beside brief.json, so this page cannot say whether the gate is healthy. The counts below come from the runs alone; each run's page still tags its failures.`;
      pill = pillHtml("PAST", "NO VERDICT");
    } else {
      head = "The last 24 hours in numbers";
      // The pool sentence rides here too, unlike the slow one: a different
      // job measuring different data cannot be this incident's own symptom,
      // and when leases are the incident the wait is the explanation.
      lede = `The gate is ${inc.recovering ? "recovering" : inc.state.toLowerCase()} — <a href="${PAGE.pages.brief}">read the brief</a>. These are the plain counts.${poolSentence(health)}`;
      pill = pillHtml(inc.state, stateWord(inc));
    }
    return `<div class="sec head">${pill}<h1>${esc(head)}</h1><div class="lede">${lede}</div></div>` +
      `<div class="sec" id="agent"><h2>Last 24 hours</h2>${numbersHtml(sinceMs, null)}</div>` +
      (healthy || noVerdict ? `<div class="sec"><h2>Last incident</h2>${lastIncidentHtml()}</div>` : "") +
      runsListHtml(windowRuns(sinceMs, null), inc, "Runs in the last 24 hours") +
      nightlyBriefHtml() + releasesHtml() + footHtml();
  }
  const inWindow = windowRuns(incidentStartMs(inc), inc.untilMs);
  if (!inWindow.some(measured) && !inWindow.some((r) => r.setup_death || r.cls === "deadline-kill")) {
    // Nothing on record for the window (older than brief.json's run_days, or
    // the link points at a time with no runs): say so instead of counting zeros.
    const pillText = `${stateWord(inc)} · ${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `since ${et(inc.sinceMs)}`}`;
    return `<div class="sec head">${pillHtml(inc.state, pillText)}<h1>No runs on record for this window</h1>` +
      `<div class="lede">${inc.cases.length ? `The incident named <code>${inc.cases.map(esc).join("</code>, <code>")}</code>. ` : ""}This page carries the runs of the last ${esc(brief.run_days ?? "?")} days; the window ${esc(etSpan(inc.sinceMs, inc.untilMs))} has none of them.</div></div>` +
      beingDoneHtml(inc) + footHtml();
  }
  const { head, lede } = briefHeadline(inc, inWindow);
  const facts = isBreak(inc) ? breakFacts(inc, inWindow) : inc.condition === "storm" ? stormFacts(inc, inWindow) : inc.condition === "setup_deaths" ? setupFacts(inc, inWindow) : inc.condition === "deadline_kill" ? deadlineFacts(inc, inWindow) : inc.condition === "delegation_ceiling" ? ceilingFacts(inc, inWindow) : [];
  const pillText = `${stateWord(inc)} · ${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `since ${et(inc.sinceMs)}`}${inc.stale ? " · STALE" : ""}`;
  let recoveringLine = "";
  if (inc.recovering) recoveringLine = `<div class="lede">The condition has cleared; ${recoveryProgress(inc)} of ${PAGE.recoveryGreenRuns} ${recoveryBarText(inc)} on distinct PRs so far. A retest is reasonable.</div>`;
  return `<div class="sec head">${pillHtml(inc.recovering ? "DEGRADED" : inc.state, pillText)}<h1>${head}</h1><div class="lede">${lede}</div>${recoveringLine}</div>` +
    (facts.length ? `<div class="sec" id="gate"><h2>${esc(whyTitle(inc))}</h2>${factsHtml(facts)}</div>` : "") +
    `<div class="sec"><h2>What the agent saw</h2>${agentSawHtml(inc, inWindow)}</div>` +
    changedBeforeHtml(inc, inWindow) +
    beingDoneHtml(inc) +
    runsListHtml(inWindow, inc, "Runs in this window") +
    nightlyBriefHtml() + releasesHtml() + footHtml();
}

function footHtml() {
  const generated = parseIso(brief.generated_at);
  return `<div class="foot"><span>Every case by run: <a href="${PAGE.pages.grid}">grid</a></span><span>How reliable is each test: <a href="${PAGE.pages.cases}">cases</a></span><span>Last night's run: <a href="${PAGE.pages.nightly}">nightly</a></span><span>Scores over time on main: <a href="${PAGE.pages.trend}">trend</a></span><span><a href="${esc(numbersHref())}">The last 24 hours in numbers</a></span><span><a href="${PAGE.rulesUrl}">How the tags are decided</a></span><span class="mut">data generated ${esc(generated != null ? et(generated) : "unknown")}${brief.run_days ? ` · runs from the last ${esc(brief.run_days)} days` : ""}</span></div>`;
}

/* ---- the PR view ---- */

function healthAtRun(run) {
  const at = run.health_at && typeof run.health_at === "object" ? run.health_at : null;
  if (at) return Object.assign({ source: "history" }, at);
  return health ? Object.assign({ source: "current" }, health, { since: health.since }) : null;
}

function bannerHtml(run) {
  const h = healthAtRun(run);
  if (!h) return "";
  const when = h.source === "history" ? "at the time of this run" : "right now";
  const sinceMs = parseIso(h.since);
  const cases = h.failing_cases || [];
  const href = incidentHref({ cases, sinceMs, untilMs: h.until ? parseIso(h.until) : null });
  let text;
  if (h.state === "GREEN") text = `<b>Gate healthy ${when}.</b> No shared break, storm or setup failures. <a href="${PAGE.pages.brief}">Brief →</a>`;
  else if (h.condition === "storm") text = `<b>Quota storm ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs lose repetitions to 429s and empty records. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "setup_deaths") text = `<b>Setup failures ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs die before any case runs. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "lost_pods") text = `<b>Build nodes lost ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs died with the node under them; nothing about the branch. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "deadline_kill") text = `<b>Runs killed at the deadline ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs reach the job timeout with no verdict, so nothing can pass; nothing about the branch. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "delegation_ceiling") text = `<b>Workers not finishing ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: repetitions end at the harness's delegation wait with the card still running; those runs read not evaluated, not red. <a href="${esc(href)}">Read the brief →</a>`;
  else text = `<b>Gate ${h.recovering ? "recovering" : "outage"} ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: ${cases.length ? `<code>${cases.map(esc).join("</code>, <code>")}</code> fail${cases.length === 1 ? "s" : ""} on every PR` : esc(h.cause || "a shared break")}. <a href="${esc(href)}">Read the brief →</a>`;
  const state = h.recovering ? "DEGRADED" : h.state;
  return `<div class="banner ${PAGE.states[state] || "hs-past"}">${pillHtml(state, h.recovering ? "RECOVERING" : h.state)}<span>${text}</span></div>`;
}

function tagFor(c) {
  if (!c.admitted) return '<span class="tag held">held out</span>';
  if (c.cls === "shared") return `<span class="tag shared">${c.also_failing_prs >= 1 ? `failing on ${plural(c.also_failing_prs, "other PR")}` : "in the current outage"}</span>`;
  if (c.cls === "only-this-pr") return '<span class="tag yours">only your PR</span>';
  if (c.cls === "storm") return '<span class="tag storm">quota storm</span>';
  if (c.cls === "delegation-ceiling") return '<span class="tag storm">delegation ceiling</span>';
  return '<span class="tag unclear">unexplained</span>';
}

// A case's repetitions by kind. `ceiling` is the repetitions the harness
// stopped watching at its delegation wait with the worker still running
// (#1874): ungraded like a storm-lost rep, counted apart from it, and part of
// the total or a three-rep case would read "all 0 reps lost".
function repTotal(reps) { return reps.pass + reps.fail + reps.infra + (reps.ceiling || 0); }
function lostText(reps) {
  const parts = [];
  if (reps.infra) parts.push(`${reps.infra} lost`);
  if (reps.ceiling) parts.push(`${reps.ceiling} at the delegation ceiling`);
  return parts.length ? ` (${parts.join(", ")})` : "";
}
function ungradedText(reps) {
  const total = plural(repTotal(reps), "rep");
  if (reps.ceiling && !reps.infra) return `all ${total} hit the delegation ceiling with the worker still running`;
  if (reps.ceiling) return `all ${total} ungraded: ${reps.infra} lost before grading, ${reps.ceiling} at the delegation ceiling`;
  return `all ${total} lost before grading`;
}

function caseCard(run, c) {
  const reps = c.reps || { pass: 0, fail: 0, infra: 0, ceiling: 0 };
  const total = repTotal(reps);
  const how = c.outcome === "failed" ? `failed all ${plural(reps.fail, "graded rep")}${lostText(reps)}` : c.outcome === "infra" ? ungradedText(reps) : `${reps.pass} of ${total} reps passed${lostText(reps)}`;
  const rate = c.pass_rate_30d != null ? ` · this case passed ${pct(c.pass_rate_30d)} of the time over the last 30 days on PRs` : "";
  // The nightly's record beside the gate's: the newest night within two
  // days of this run, when one graded the case. Evidence about main, never
  // a tag -- the tags above answer "is this mine?" from the presubmit alone.
  const nightly = c.nightly_failed_recent === true ? " · it also failed every repetition on the latest nightly run"
    : c.nightly_failed_recent === false ? " · the latest nightly run passed it" : "";
  const n = repOf(c);
  const url = transcriptUrl(run, c.case, n);
  const log = buildUrl(run);
  return `<div class="case"><div class="hd"><h3>${esc(c.case)}</h3>${tagFor(c)}</div>` +
    `<div class="sub">${esc(how)}${esc(rate)}${esc(nightly)}</div>` +
    (c.reason ? `<div class="reason">${esc(c.reason)}</div>` : "") +
    (c.excerpt ? `<div class="quote">“${esc(c.excerpt)}”</div>` : "") +
    (c.do ? `<div class="do"><b>Do:</b> ${esc(c.do)}</div>` : "") +
    `<div class="links">${url ? `<a href="${esc(url)}">transcript (rep ${n})</a>` : ""}${log ? `<a href="${esc(log)}">build log</a>` : ""}<a href="${esc(caseHref(c.case))}">this case's history</a></div></div>`;
}

// classify.py's run-level `do` for a run with no cases, as "<imperative>.
// <why>." Bolding its own first sentence rather than prefixing one is what
// lets the advice differ: a conflicted merge says rebase, not retest (#1608).
function runDoHtml(text) {
  const cut = text.indexOf(". ");
  const lead = cut < 0 ? text : text.slice(0, cut + 1);
  return `<li><b>${esc(lead)}</b>${cut < 0 ? "" : " " + esc(text.slice(cut + 2))}</li>`;
}

function whatToDoHtml(run) {
  const items = [];
  // A deadline kill keeps its `do` whether or not cases finished before it.
  if ((!measured(run) || run.cls === "deadline-kill") && run.do) items.push(runDoHtml(run.do));
  else if (run.setup_death || (!measured(run) && run.verdict === "infra")) items.push("<li><b>Retest.</b> Nothing ran, so nothing here is about your change.</li>");
  else if (!measured(run) && run.verdict === "green") items.push("<li><b>Nothing.</b> The gate revalidated this branch's earlier green run.</li>");
  else if (!measured(run)) items.push("<li><b>Read the build log.</b> The failure is before the eval loop; a broken image build or deploy on this branch looks like this.</li>");
  else if (run.verdict === "green") items.push("<li><b>Nothing.</b> This run is green.</li>");
  else if (run.verdict === "infra") {
    items.push("<li><b>Nothing right now.</b> Retesting before the gate is healthy will fail the same way.</li>");
    items.push("<li>Run <code>/retest</code> once the brief says healthy again (the Chat space announces it).</li>");
    items.push('<li>If a case here were marked <span class="tag yours">only your PR</span>, the fix would be on you: the transcript usually names the problem.</li>');
  } else {
    const yours = (run.cases || []).filter((c) => c.cls === "only-this-pr").length;
    items.push(`<li><b>Fix the PR.</b> ${yours ? "Retesting won't change this: the case passes for everyone else." : "Nothing on other PRs matches the unexplained failure, so treat it as yours until the transcript says otherwise."}</li>`);
    items.push("<li>Start with the transcript link above. For image or manifest changes, check that what the PR builds is what the run deployed.</li>");
    items.push("<li>If you believe the check is wrong, file an issue with the <code>presubmit-gate</code> label and link this page.</li>");
  }
  return `<div class="next"><h2>What to do</h2><ul>${items.join("")}</ul></div>`;
}

function runHtml(link) {
  if (!link.build) return `<div class="sec head"><h1>Which run?</h1><div class="lede">Open this page as <code>run.html#build=&lt;prow build id&gt;</code>; the gate comment on a PR links here. <a href="${PAGE.pages.brief}">Back to the brief →</a></div></div>` + footHtml();
  const run = runs().find((r) => String(r.build) === link.build);
  if (!run) return `<div class="sec head"><h1>No run with that id in the last ${esc(brief.run_days ?? "?")} days.</h1><div class="lede">Build <code>${esc(link.build)}</code> is not in the data behind this page: older than its window, still running, or never uploaded. <a href="${PAGE.pages.brief}">Back to the brief →</a></div></div>` + footHtml();
  const startMs = parseIso(run.started), finishMs = parseIso(run.finished);
  const log = buildUrl(run);
  const crumb = [`Smoke run for ${prLink(run.pr)}`, startMs != null ? `started ${esc(et(startMs))}` : "", finishMs != null ? `finished ${esc(startMs != null && etDay(startMs) === etDay(finishMs) ? `${etTime(finishMs)} ${PAGE.tzLabel}` : et(finishMs))}` : "",
    startMs != null && finishMs != null ? esc(minutesText(finishMs - startMs)) : "", `project ${esc(projectShort(run.project))}`, run.head_sha ? `<code>${esc(run.head_sha)}</code>` : "", log ? `<a href="${esc(log)}">build log</a>` : ""].filter(Boolean).join(" · ");
  const cases = run.cases || [];
  const failed = cases.filter((c) => c.admitted && c.outcome === "failed");
  const order = { "only-this-pr": 0, null: 1, storm: 2, shared: 3 };
  failed.sort((a, b) => (order[a.cls] ?? 1) - (order[b.cls] ?? 1));
  const lost = cases.filter((c) => c.admitted && c.outcome === "infra");
  const partial = cases.filter((c) => c.admitted && c.outcome === "partial");
  const passed = cases.filter((c) => c.admitted && c.outcome === "passed");
  const heldPassed = cases.filter((c) => !c.admitted && (c.outcome === "passed" || c.outcome === "partial"));
  const heldFailed = cases.filter((c) => !c.admitted && c.outcome === "failed");
  let body = "";
  if (failed.length) body += `<div class="sec"><h2>Failed gate cases · ${failed.length}</h2>${failed.map((c) => caseCard(run, c)).join("")}</div>`;
  if (lost.length) body += `<div class="sec"><h2>Not graded · ${lost.length}</h2>${lost.map((c) => caseCard(run, c)).join("")}</div>`;
  if (partial.length) body += `<div class="sec"><h2>Passed on retry · ${partial.length}</h2><div class="passed">${partial.map((c) => `<span>${esc(c.case)}</span>`).join("")}</div><p class="mut small">Some repetitions failed; the gate counts a case as failed only when every graded repetition fails.</p></div>`;
  if (passed.length || heldPassed.length) body += `<div class="sec"><h2>Passed · ${passed.length + heldPassed.length}</h2><div class="passed">${passed.map((c) => `<span>${esc(c.case)}</span>`).join("")}${heldPassed.length ? `<span class="held">+${heldPassed.length} held out</span>` : ""}</div></div>`;
  if (heldFailed.length) body += `<div class="sec"><h2>Held out · failed · ${heldFailed.length}</h2><div class="passed">${heldFailed.map((c) => `<span class="held">${esc(c.case)}</span>`).join("")}</div><p class="mut small">Held-out cases are measured but never block a PR.</p></div>`;
  return `<div class="crumb">${crumb}</div><h1>${esc(run.headline || "")}</h1><div class="lede">${esc(run.lede || "")}</div>` +
    bannerHtml(run) + body + whatToDoHtml(run) + footHtml();
}

/* ---- the per-case record (brief.json's cases{}) ---- */

function caseDocs() {
  const raw = brief.cases && typeof brief.cases === "object" && !Array.isArray(brief.cases) ? brief.cases : {};
  return Object.keys(raw).filter((name) => raw[name] && typeof raw[name] === "object").map((name) => Object.assign({
    domain: "unknown", status: "retired", rates: {}, strip: [], issues: [], last_failure: null,
  }, raw[name], { name }));
}
const isBlocking = (c) => c.status === "blocking";
const isHeldOut = (c) => c.status === "held_out" || c.status === "demoted";
const byName = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
const domainWords = (domain) => String(domain).replace(/-/g, " ");
// [passed, failed] -> the fraction, or null when nothing was graded.
const rateOf = (pair) => (Array.isArray(pair) && pair.length === 2 && pair[0] + pair[1] > 0 ? pair[0] / (pair[0] + pair[1]) : null);
const rate7d = (c) => rateOf((c.rates && c.rates.presubmit || [])[0]);
// Worst first; a case with no graded repetition sorts after every rate.
const worstKey = (c) => (rate7d(c) == null ? 2 : rate7d(c));
const byWorst = (a, b) => worstKey(a) - worstKey(b) || byName(a, b);

function statusPill(c) {
  const word = c.status === "demoted" && c.demoted_on ? `demoted ${String(c.demoted_on).slice(5)}` : (PAGE.statusWords[c.status] || String(c.status));
  return `<span class="st ${esc(c.status)}">${esc(word)}</span>`;
}

/* ---- the Cases page ---- */

function rateCell(pair) {
  const rate = rateOf(pair);
  if (rate == null) return `<span class="rate none">—</span>`;
  const cls = rate >= PAGE.rateOkMin ? "ok" : rate >= PAGE.rateWarnMin ? "warn" : "bad";
  const n = pair[0] + pair[1];
  return `<span class="rate ${cls}" title="${pair[0]} of ${n} graded repetitions passed">${pct(rate)}<small>(${n})</small></span>`;
}

const stripWords = { pass: "passed all reps", fail: "failed all reps", partial: "failed some reps", infra: "quota / infra, not counted" };

function stripHtml(c) {
  const strip = Array.isArray(c.strip) ? c.strip : [];
  if (!strip.length) return `<span class="mut small">no presubmit run on record</span>`;
  const bars = strip.map((s) => {
    const title = `${s.pr != null ? `#${s.pr}` : "no PR"} · ${et(parseIso(s.at))} · ${stripWords[s.state] || s.state}${s.event ? " · whole run broken, not counted in the rates" : ""}`;
    return `<i class="${esc(s.state)}" title="${esc(title)}"></i>`;
  });
  return `<div class="spark" title="${esc(`last ${strip.length} presubmit runs, oldest first`)}">${bars.join("")}</div>`;
}

function lastFailureHtml(c) {
  const f = c.last_failure;
  if (!f || typeof f !== "object") return `<span class="lbl">Last failure was</span> <span class="mut">none on record</span>`;
  const reps = f.reps || { pass: 0, fail: 0, infra: 0 };
  let head;
  if (f.state === "partial") head = `${reps.fail} of ${reps.pass + reps.fail} reps failed (the gate counts that as a pass)`;
  else if (f.cls === "shared") head = f.also_failing_prs >= 1 ? `the gate's: failing on ${plural(f.also_failing_prs, "other PR")} at the time` : "the gate's: in the outage at the time";
  else if (f.cls === "storm") head = "a quota storm: repetitions lost before grading";
  else if (f.cls === "delegation-ceiling") head = "the delegation ceiling: the harness stopped waiting with the worker still running";
  else if (f.cls === "only-this-pr") head = "PR-caused: passed on other PRs around it";
  else if (f.event) head = "failed all reps in a run where almost everything failed";
  else head = "failed all reps";
  // The run page carries the last run_days only; an older failure links to
  // the pull request itself rather than to a page that says "no run".
  const onRunPage = runs().some((r) => String(r.build) === String(f.build));
  const who = f.pr == null ? (f.tier === "nightly" ? "the nightly" : "a run")
    : onRunPage ? `<a href="${esc(runHref(f))}">#${esc(f.pr)}</a>`
    : `<a href="${PAGE.prUrl}/${enc(f.pr)}">#${esc(f.pr)}</a> <span class="mut">(older than this page's ${esc(brief.run_days ?? "")}-day run window)</span>`;
  const tier = f.tier === "nightly" ? " · nightly run; the presubmit has no failure on record" : "";
  return `<span class="lf"><span class="lbl">Last failure was</span> <span><b>${esc(head)}</b> · ${who} · ${esc(et(parseIso(f.at)))}${esc(tier)}</span>` +
    (f.reason ? `<span class="rsn" title="${esc(f.reason)}">${esc(f.reason)}</span>` : "") + `</span>` +
    (f.excerpt ? `<div class="quote small">“${esc(f.excerpt)}”</div>` : "");
}

// Two rows per case: the numbers, then the last failure in a line of its
// own -- a grader's reason is too long to share a row with eight columns.
const CASE_COLUMNS = 8;

function caseRow(c, link) {
  const rates = c.rates || {};
  const pre = rates.presubmit || [], night = rates.nightly || [];
  const classes = [link.caseHash === c.name ? "hl" : "", c.status === "retired" ? "dim" : ""].filter(Boolean).join(" ");
  const issues = (c.issues || []).map(issueLink).join(" · ") || `<span class="mut">—</span>`;
  return `<tr id="case-${esc(c.name)}" class="main ${classes}"><td class="nm">${esc(c.name)}${c.note ? `<small>${esc(c.note)}</small>` : ""}<small><a href="${esc(trendHref([c.name]))}">trend on main →</a></small></td>` +
    `<td>${stripHtml(c)}</td><td>${rateCell(pre[0])}</td><td>${rateCell(pre[1])}</td><td>${rateCell(night[0])}</td><td>${rateCell(night[1])}</td>` +
    `<td>${statusPill(c)}</td><td class="iss">${issues}</td></tr>` +
    `<tr class="why ${classes}"><td colspan="${CASE_COLUMNS}">${lastFailureHtml(c)}</td></tr>`;
}

function caseGroups(cases, sort) {
  if (sort === "name") return [{ label: null, cases: [...cases].sort(byName) }];
  const groups = [];
  const primary = new Map();
  for (const c of cases.filter(isBlocking)) {
    if (!primary.has(c.domain)) primary.set(c.domain, []);
    primary.get(c.domain).push(c);
  }
  for (const [domain, list] of primary) groups.push({ label: domainWords(domain), cases: list, key: Math.min(...list.map(worstKey)) });
  if (sort === "worst") {
    groups.sort((a, b) => a.key - b.key || a.label.localeCompare(b.label));
    for (const g of groups) g.cases.sort(byWorst);
  } else {
    groups.sort((a, b) => a.label.localeCompare(b.label));
    for (const g of groups) g.cases.sort(byName);
  }
  const rest = [
    ["held out", cases.filter(isHeldOut), false],
    ["nightly only", cases.filter((c) => c.status === "nightly_only"), false],
    ["not in any matrix", cases.filter((c) => c.status === "retired"), !ui.showRetired],
  ];
  for (const [label, list, collapsed] of rest) {
    if (list.length) groups.push({ label: `${label} · ${plural(list.length, "case")}`, cases: list.sort(sort === "worst" ? byWorst : byName), collapsed });
  }
  return groups;
}

function casesHtml(link) {
  const sort = ui.sort || link.sort || "worst";
  const show = ui.show || link.show || "all";
  const all = caseDocs();
  const counts = { all: all.length, blocking: all.filter(isBlocking).length, held: all.filter(isHeldOut).length };
  const cases = show === "blocking" ? all.filter(isBlocking) : show === "held" ? all.filter(isHeldOut) : all;
  const columns = CASE_COLUMNS;
  const rows = [];
  for (const group of caseGroups(cases, sort)) {
    if (group.label) rows.push(`<tr class="grp"><td colspan="${columns}">${esc(group.label)}</td></tr>`);
    if (group.collapsed) {
      rows.push(`<tr class="more"><td colspan="${columns}">${plural(group.cases.length, "case")} that no matrix runs on this checkout, kept for their history · <button type="button" data-toggle="retired" class="linklike">show</button></td></tr>`);
      continue;
    }
    for (const c of group.cases) rows.push(caseRow(c, link));
  }
  if (!rows.length) rows.push(`<tr><td colspan="${columns}" class="mut">No case on record.</td></tr>`);
  const windows = Array.isArray(brief.rate_windows_days) && brief.rate_windows_days.length === 2 ? brief.rate_windows_days : [7, 30];
  const catches = brief.catches && typeof brief.catches === "object" ? brief.catches : null;
  const caught = catches && Number.isInteger(catches.product_bugs)
    ? ` The suite has caught ${plural(catches.product_bugs, "real product bug")}${Number.isInteger(catches.prs_blocked) ? ` and stopped ${plural(catches.prs_blocked, "PR")} from merging` : ""}${catches.ledger ? ` (catch ledger ${issueLink(catches.ledger)})` : ""}.`
    : "";
  const generated = parseIso(brief.generated_at);
  return `<div class="sec head"><h1>How reliable is each test?</h1><div class="lede">One row per case. Pass rates count only repetitions the grader scored: quota and setup losses are left out, and a run where almost everything failed is charged to the run, not the cases. Blocking means the case is on the admitted roster and can red a PR.</div></div>` +
    `<div class="ctl">sort by ${chip("sort", "worst", `worst ${windows[0]}d`, sort === "worst")}${chip("sort", "domain", "domain", sort === "domain")}${chip("sort", "name", "name", sort === "name")}` +
    `<span class="sep">·</span>show ${chip("show", "all", `all ${counts.all}`, show === "all")}${chip("show", "blocking", `blocking only · ${counts.blocking}`, show === "blocking")}${chip("show", "held", `held out only · ${counts.held}`, show === "held")}</div>` +
    `<table class="cases"><thead><tr><th>Case</th><th>Last ${esc(brief.strip_runs ?? 30)} runs</th><th>${windows[0]}d</th><th>${windows[1]}d</th><th>Nightly ${windows[0]}d</th><th>Nightly ${windows[1]}d</th><th>Status</th><th>Issue</th></tr></thead><tbody>${rows.join("")}</tbody></table>` +
    `<div class="foot block"><p>Colours in the strip: green passed all reps · red failed all reps · faded red failed some reps · violet quota or infra, not counted. Rate colours are display bands (green from ${pct(PAGE.rateOkMin)}, amber from ${pct(PAGE.rateWarnMin)}), not the admission rule.</p>` +
    `<p>A case blocks when it is named in <code>BOOTSTRAP_ADMITTED</code>; one that reds a PR its diff cannot explain is demoted the same day and comes back when its issue's re-admission bar holds — <a href="${PAGE.rosterUrl}">how the roster works</a>.${caught}</p>` +
    `<p class="mut">Presubmit rates are the gate's own history on PRs; nightly rates are the case's record on main, kept apart. Data generated ${esc(generated != null ? et(generated) : "unknown")}.</p></div>`;
}

/* ---- the Grid ---- */

// The column a run gets: its state for a case is read from the run's
// classified cases, so a cell and the PR view agree by construction.
const cellOutcome = { passed: "pass", failed: "fail", partial: "partial", infra: "infra" };

function gridWindow(link, inc) {
  const key = ui.window || link.window;
  const toMs = link.sinceMs != null ? (link.untilMs ?? nowMs()) : nowMs();
  if (link.sinceMs != null && (!key || key === "linked")) return { fromMs: inc ? incidentStartMs(inc) : link.sinceMs, toMs, key: "linked", live: link.untilMs == null };
  const chosen = PAGE.gridWindows.find((w) => w[0] === (key || PAGE.gridDefaultWindow)) || PAGE.gridWindows[0];
  return { fromMs: toMs - chosen[1], toMs, key: chosen[0], live: link.untilMs == null };
}

function gridColumns(win) {
  const inWindow = runs().filter((r) => { const t = runStart(r); return t != null && t >= win.fromMs && t <= win.toMs; });
  const cols = inWindow.filter(gridColumnRun).map((r) => ({ kind: "run", run: r, t: runStart(r), build: String(r.build) }));
  const hidden = inWindow.length - cols.length;
  if (win.live) {
    for (const p of Array.isArray(brief.pending) ? brief.pending : []) {
      const t = parseIso(p && p.first_seen);
      if (t != null && t >= win.fromMs) cols.push({ kind: "pending", t, build: String(p.build) });
    }
  }
  cols.sort((a, b) => a.t - b.t || (a.build < b.build ? -1 : 1));
  return { cols, hidden };
}

function cellState(col, name) {
  if (col.kind === "pending") return "running";
  if (!measured(col.run)) return "died";
  const c = (col.run.cases || []).find((x) => x.case === name);
  return c ? (cellOutcome[c.outcome] || "none") : "none";
}

function gridMarkers(win, cols) {
  const markers = [];
  if (ui.markers.merge && Array.isArray(brief.merges)) {
    for (const m of brief.merges) {
      const t = parseIso(m.at);
      if (t != null && t >= win.fromMs && t <= win.toMs) markers.push({ t, cls: "merge", label: m.pr != null ? `merge #${m.pr}` : `merge ${String(m.sha).slice(0, 7)}`, title: `${et(t)} · ${m.title || ""}`, href: m.pr != null ? `${PAGE.prUrl}/${enc(m.pr)}` : null });
    }
  }
  if (ui.markers.incident) {
    const seen = [];
    const add = (t, cls, label) => {
      if (t == null || t < win.fromMs || t > win.toMs) return;
      if (seen.some((s) => s.cls === cls && Math.abs(s.t - t) <= PAGE.hourMs)) return;
      seen.push({ t, cls });
      markers.push({ t, cls, label, title: `${et(t)} · ${label}` });
    };
    const what = (inc) => (isBreak(inc) ? "shared break" : inc.condition === "storm" ? "quota storm" : inc.condition === "setup_deaths" ? "setup deaths" : inc.condition === "delegation_ceiling" ? "delegation ceiling" : inc.condition === "deadline_kill" ? "deadline kills" : "degraded");
    for (const inc of historyIncidents().map(incidentFromHistory)) {
      add(inc.sinceMs, "incident", `${inc.state.toLowerCase()}: ${what(inc)}`);
      add(inc.untilMs, "recovered", "healthy again");
    }
    if (health && health.state !== "GREEN") add(parseIso(health.since), "incident", `${health.state.toLowerCase()}: ${what(incidentFromHealth(health))}`);
  }
  // Position: the boundary before the first column that starts after the
  // marker, as a fraction of the column area.
  const n = Math.max(cols.length, 1);
  for (const m of markers) m.at = cols.filter((c) => c.t <= m.t).length / n;
  markers.sort((a, b) => a.t - b.t);
  return markers;
}

function markerHtml(markers, cols) {
  const mergeCount = markers.filter((m) => m.cls === "merge").length;
  const labelMerges = mergeCount <= PAGE.gridLabelledMergesMax;
  const left = (m) => `left:calc(var(--namew) + (100% - var(--namew)) * ${m.at.toFixed(4)})`;
  const labelled = markers.filter((m) => m.cls !== "merge" || labelMerges);
  const lines = markers.map((m) => `<div class="mk ${m.cls}" style="${left(m)}" title="${esc(m.title)}"></div>`).join("");
  const labels = labelled.map((m, i) => `<div class="mkl ${m.cls}" style="${left(m)};top:${i % 2 ? 2 : 16}px" title="${esc(m.title)}">${esc(m.label)}</div>`).join("");
  const merges = !labelMerges ? markers.filter((m) => m.cls === "merge") : [];
  const list = merges.length ? `<p class="mut small">${plural(merges.length, "merge")} to main in this window, marked as lines: ${merges.slice(0, 12).map((m) => `${m.href ? `<a href="${esc(m.href)}">${esc(m.label.replace("merge ", ""))}</a>` : esc(m.label)} ${esc(etTime(m.t))}`).join(" · ")}${merges.length > 12 ? " · …" : ""}</p>` : "";
  return { lines, labels, list, padTop: labelled.length ? 34 : 0, colsCount: cols.length };
}

function gridRows(cols, link, rowsFilter) {
  const linked = link.cases;
  const states = new Map();
  const visible = [];
  for (const c of caseDocs()) {
    const row = cols.map((col) => cellState(col, c.name));
    const present = row.some((s) => s !== "none" && s !== "died" && s !== "running");
    if (!present && !linked.has(c.name)) continue;
    states.set(c.name, row);
    visible.push(c);
  }
  const failing = (c) => states.get(c.name).some((s) => s === "fail" || s === "partial");
  let cases = visible;
  if (rowsFilter === "admitted") cases = cases.filter((c) => isBlocking(c) || linked.has(c.name));
  else if (rowsFilter === "failing") cases = cases.filter((c) => failing(c) || linked.has(c.name));
  const groups = [];
  const pinned = cases.filter((c) => linked.has(c.name)).sort(byName);
  if (pinned.length) groups.push({ label: "in this incident", cases: pinned, linked: true });
  const primary = new Map();
  for (const c of cases.filter((c) => isBlocking(c) && !linked.has(c.name))) {
    if (!primary.has(c.domain)) primary.set(c.domain, []);
    primary.get(c.domain).push(c);
  }
  for (const [domain, list] of [...primary.entries()].sort((a, b) => a[0].localeCompare(b[0]))) groups.push({ label: `${domainWords(domain)} · blocking`, cases: list.sort(byName) });
  const held = cases.filter((c) => !isBlocking(c) && !linked.has(c.name)).sort(byName);
  if (held.length) {
    const shown = ui.showHeld ? held : held.filter(failing);
    groups.push({ label: "held out · not blocking", cases: shown, more: held.length - shown.length });
  }
  return { groups, states };
}

function gridHtml(link) {
  const inc = resolveIncident(link);
  const win = gridWindow(link, inc);
  const rowsFilter = ui.rows || link.rows || "all";
  const { cols, hidden } = gridColumns(win);
  const { groups, states } = gridRows(cols, link, rowsFilter);
  const markers = gridMarkers(win, cols);
  const mk = markerHtml(markers, cols);
  const inWindow = windowRuns(win.fromMs, win.toMs);
  let banner = "";
  if (inc) {
    const { head } = briefHeadline(inc, inWindow);
    banner = `<div class="banner ${PAGE.states[inc.recovering ? "DEGRADED" : inc.state] || "hs-past"}">${pillHtml(inc.recovering ? "DEGRADED" : inc.state, stateWord(inc))}<span><b>${head}</b>${inc.sinceMs != null ? ` · ${esc(inc.past ? etSpan(inc.sinceMs, inc.untilMs) : `since ${et(inc.sinceMs)}`)}` : ""} · <a href="${esc(incidentHref(inc))}">read the brief →</a></span><span class="right">showing ${esc(etSpan(win.fromMs, win.toMs))}</span></div>`;
  }
  const windowChips = (link.sinceMs != null ? chip("window", "linked", "this incident", win.key === "linked") : "") + PAGE.gridWindows.map((w) => chip("window", w[0], w[0], win.key === w[0])).join("");
  const ctl = `<div class="ctl">window ${windowChips}<span class="sep">·</span>rows ${chip("rows", "all", "all cases", rowsFilter === "all")}${chip("rows", "admitted", "admitted only", rowsFilter === "admitted")}${chip("rows", "failing", "failing only", rowsFilter === "failing")}` +
    `<span class="sep">·</span>markers ${chip("marker", "merge", "merges", ui.markers.merge)}${chip("marker", "incident", "incidents", ui.markers.incident)}</div>`;
  if (!cols.length) {
    return `<div class="sec head"><h1>Cases by run</h1><div class="lede">Every case by every presubmit run in the window, in time order; a cell opens that run's detail for the case.</div></div>${banner}${ctl}` +
      `<div class="gridwrap"><p class="mut">No presubmit run started ${esc(etSpan(win.fromMs, win.toMs))}${hidden ? ` (${plural(hidden, "run")} without cases not shown)` : ""}. This page carries the runs of the last ${esc(brief.run_days ?? "?")} days.</p></div>` + footHtml();
  }
  // Column heads are written vertically: a busy day is a hundred columns,
  // and a PR number needs its digits more than the grid needs the width.
  const head = `<div></div>` + cols.map((col) => col.kind === "pending"
    ? `<div class="hd" title="${esc(`build ${col.build} · still running · first seen ${et(col.t)}`)}"><b>running</b> ${esc(etTime(col.t))}</div>`
    : `<div class="hd" title="${esc(`${col.run.pr != null ? `PR #${col.run.pr}` : "no PR"} · started ${et(col.t)} · ${projectShort(col.run.project)}`)}"><b>#${esc(col.run.pr ?? "?")}</b> ${esc(etTime(col.t))}</div>`).join("");
  const cell = (c, col, state) => {
    const sel = ui.selected && ui.selected.build === col.build && ui.selected.case === c.name ? " sel" : "";
    const clickable = state !== "none" && state !== "died" && state !== "running";
    const title = col.kind === "pending" ? "still running" : `${c.name} · #${col.run.pr ?? "?"} · ${cellWords[state] || state}`;
    return clickable
      ? `<button type="button" class="c ${state}${sel}" data-build="${esc(col.build)}" data-case="${esc(c.name)}" title="${esc(title)}" aria-label="${esc(title)}"></button>`
      : `<div class="c ${state}" title="${esc(title)}"></div>`;
  };
  const body = groups.map((g) => `<div class="grp">${esc(g.label)}</div><div class="grp-fill"></div>` +
    g.cases.map((c) => `<div class="nm${g.linked ? " lk" : isBlocking(c) ? "" : " h"}" title="${esc(c.name)}">${esc(c.name)}${!isBlocking(c) && !g.linked ? `<small>${esc(c.status === "demoted" && c.demoted_on ? `demoted ${String(c.demoted_on).slice(5)}` : PAGE.statusWords[c.status] || "")}</small>` : ""}</div>` +
      cols.map((col, i) => cell(c, col, states.get(c.name)[i])).join("")).join("") +
    (g.more ? `<div class="more">${plural(g.more, "more held-out case")} passed everything in this window · <button type="button" data-toggle="held">show</button></div><div class="grp-fill"></div>` : "")).join("");
  const legend = `<div class="legend"><span><i class="pass"></i>passed</span><span><i class="fail"></i>failed all reps</span><span><i class="partial"></i>failed some reps</span><span><i class="infra"></i>quota / infra</span><span><i class="died"></i>run died before the cases</span><span><i class="running"></i>still running</span><span><i></i>not in run</span>` +
    (ui.markers.merge ? `<span><i class="mkmerge"></i>merge to main</span>` : "") + (ui.markers.incident ? `<span><i class="mkincident"></i>incident start</span><span><i class="mkrecovered"></i>healthy again</span>` : "") +
    `<span style="margin-left:auto">${plural(cols.length, "column")} · presubmit runs in start order${hidden ? ` · ${plural(hidden, "run")} without cases (aborted, or green on revalidation) not shown` : ""}</span></div>`;
  return `<div class="sec head"><h1>Cases by run</h1><div class="lede">Every case by every presubmit run in the window, in time order. Blocking cases first, by domain; held-out cases that passed everything are folded away. Click a cell for that run's detail.</div></div>${banner}${ctl}` +
    `<div class="gridwrap"><div class="gscroll"><div class="g" style="grid-template-columns:var(--namew) repeat(${cols.length},minmax(var(--colmin),1fr));padding-top:${mk.padTop}px">${mk.lines}${mk.labels}${head}${body}</div></div>${legend}${mk.list}</div>` +
    detailHtml() + footHtml();
}

const cellWords = { pass: "passed all reps", fail: "failed all reps", partial: "failed some reps", infra: "quota / infra, not graded", died: "run died before the cases", running: "still running", none: "not in this run" };

function detailHtml() {
  const sel = ui.selected;
  if (!sel) return "";
  const run = runs().find((r) => String(r.build) === sel.build);
  const c = run ? (run.cases || []).find((x) => x.case === sel.case) : null;
  if (!run || !c) return "";
  const reps = c.reps || { pass: 0, fail: 0, infra: 0, ceiling: 0 };
  const total = repTotal(reps);
  const how = c.outcome === "failed" ? `failed all ${plural(reps.fail, "graded rep")}${lostText(reps)}` : c.outcome === "infra" ? ungradedText(reps) : c.outcome === "partial" ? `${reps.fail} of ${total} reps failed${lostText(reps)}` : `passed all ${plural(reps.pass, "rep")}${lostText(reps)}`;
  const others = (run.cases || []).filter((x) => x.case !== c.case && x.outcome === "passed").length;
  const startMs = parseIso(run.started), finishMs = parseIso(run.finished);
  const length = startMs != null && finishMs != null ? ` · ${minutesText(finishMs - startMs)} run` : "";
  const tag = c.outcome === "failed" || c.outcome === "infra" ? ` · ${tagFor(c)}` : "";
  const n = repOf(c), url = transcriptUrl(run, c.case, n), log = buildUrl(run);
  return `<div class="detail" id="detail"><h3><code>${esc(c.case)}</code> · ${prLink(run.pr)} · ${esc(et(runFinish(run)))} · project ${esc(projectShort(run.project))}<button type="button" class="close" data-toggle="close">close ×</button></h3>` +
    `<div class="sub">${esc(how)}${esc(length)} · ${plural(others, "other case")} passed in this run${tag}</div>` +
    (c.reason ? `<div class="reason">${esc(c.reason)}</div>` : "") +
    (c.excerpt ? `<div class="quote">“${esc(c.excerpt)}”</div>` : "") +
    (c.do ? `<div class="do"><b>Do:</b> ${esc(c.do)}</div>` : "") +
    `<div class="links">${url ? `<a href="${esc(url)}">full transcript (rep ${n})</a>` : ""}${log ? `<a href="${esc(log)}">build log</a>` : ""}<a href="${esc(runHref(run))}">this run's page</a><a href="${esc(caseHref(c.case))}">this case's history →</a></div></div>`;
}

/* ---- the Nightly report (SCHEMA.md: brief.json's nightly block) ---- */

const nights = () => (brief.nightly && Array.isArray(brief.nightly.nights) ? brief.nightly.nights.filter((n) => n && typeof n === "object" && n.counts) : []);
const nightHref = (night) => `${PAGE.pages.nightly}#build=${enc(night.build)}`;
const nightStart = (night) => parseIso(night.started) ?? parseIso(night.finished);
// The night's headline state, worst first: cut short, failed cases, partial
// cases, nothing recorded or nothing graded, a pass short of the matrix
// (cases lost to infra or never recorded), then clean. Green means every
// expected case was graded and passed -- a quota storm that grades nothing
// is not a pass. `word` is the pill on the page and the Brief, `short` the
// chip in the other-nights list.
function nightVerdict(night) {
  const c = night.counts;
  if (night.truncated) return { cls: "p-infra", word: `TRUNCATED · ${c.recorded} of ${c.expected || "?"} cases recorded`, short: "cut short" };
  if (c.failed) return { cls: "p-fail", word: `${plural(c.failed, "case")} failed all reps`, short: `${c.failed} failed` };
  if (c.partial) return { cls: "p-partial", word: `${plural(c.partial, "case")} failed some reps`, short: `${c.partial} partial` };
  if (!c.recorded) return { cls: "p-infra", word: "no case recorded", short: "no cases" };
  if (!c.passed) return { cls: "p-infra", word: `nothing graded · ${plural(c.infra, "case")} lost to infra`, short: "nothing graded" };
  const gaps = [c.infra ? `${plural(c.infra, "case")} lost to infra` : "", c.missing ? `${c.missing} not recorded` : ""].filter(Boolean);
  if (gaps.length) return { cls: "p-infra", word: `${c.passed} passed · ${gaps.join(" · ")}`, short: [c.infra ? `${c.infra} lost` : "", c.missing ? `${c.missing} missing` : ""].filter(Boolean).join(" · ") };
  return { cls: "p-pass", word: "every case passed", short: "clean" };
}
function nightPill(night) {
  const v = nightVerdict(night);
  return `<span class="pill ${v.cls}">${esc(v.word)}</span>`;
}
// The night's date on the reader's clock: an 8 PM ET start is "the night of" that day.
const nightDay = (night) => { const ms = nightStart(night); return ms != null ? fmtParts(ms, { weekday: "short", month: "short", day: "numeric" }) : "an unknown day"; };

// A nightly build the collector listed with no finished.json yet
// (brief.json's nightly.running): a night in flight, said in one line so
// nobody reads "no night" while the job is still going.
function runningNoteHtml() {
  const running = brief.nightly && Array.isArray(brief.nightly.running) ? brief.nightly.running.filter((r) => r && typeof r === "object" && r.build != null) : [];
  if (!running.length) return "";
  const r = running[running.length - 1];
  const seen = parseIso(r.first_seen);
  return `<p class="mut">A night is running now: build ${r.log_url ? `<a href="${esc(r.log_url)}">${esc(r.build)}</a>` : `<code>${esc(r.build)}</code>`}, first seen ${esc(seen != null ? et(seen) : "at an unknown time")}. Its report is here once the collector records it.</p>`;
}

function nightlyBriefHtml() {
  const list = nights();
  const night = list[0];
  let body;
  if (!night) body = `<p class="mut">No night on record yet. Once <code>${esc(brief.nightly && brief.nightly.job || "the nightly periodic")}</code> has run, last night's report is here and one line of it goes into the 9 AM digest.</p>`;
  else {
    const c = night.counts;
    const summary = night.truncated
      ? `truncated after ${night.duration_s != null ? minutesText(night.duration_s * 1000) : "an unknown time"}: ${c.recorded} of ${c.expected || "?"} cases recorded`
      : `${plural(c.recorded, "case")} · ${c.passed} passed all reps · ${c.partial} partial · ${c.failed} failed${c.infra ? ` · ${c.infra} infra` : ""}${night.newly_failing.length ? ` · newly failing: <code>${night.newly_failing.map(esc).join("</code>, <code>")}</code>` : ""}${night.duration_s != null ? ` · ${minutesText(night.duration_s * 1000)}` : ""}`;
    body = `<p>${nightPill(night)} <b>${esc(nightDay(night))}</b> — ${summary}. <a href="${esc(nightHref(night))}">Read the report →</a></p>`;
  }
  return `<div class="sec" id="nightly"><h2>Last night's run</h2>${body}${runningNoteHtml()}</div>`;
}

function nightCaseRow(night, c, newly) {
  const reps = c.reps || { pass: 0, fail: 0, infra: 0 };
  const total = reps.pass + reps.fail + reps.infra;
  const repText = c.state === "infra" ? `${plural(total, "rep")} lost` : `${reps.pass}/${reps.pass + reps.fail}${reps.infra ? ` (+${reps.infra} lost)` : ""}`;
  const links = [c.transcript_url ? `<a href="${esc(c.transcript_url)}">transcript</a>` : "", `<a href="${esc(caseHref(c.case))}">history</a>`].filter(Boolean).join(" · ");
  return `<tr class="${newly.has(c.case) ? "newly" : ""}"><td class="nm">${esc(c.case)}</td>` +
    `<td><span class="pill ${PAGE.nightStateClass[c.state] || "p-infra"}" title="${esc(PAGE.nightStateWords[c.state] || c.state)}">${esc(c.state)}</span></td>` +
    `<td class="num" title="repetitions passed / graded">${esc(repText)}</td>` +
    `<td>${c.reason ? `<span class="rsn" title="${esc(c.reason)}">${esc(c.reason)}</span>` : `<span class="mut">—</span>`}</td>` +
    `<td class="mut small">${links}</td></tr>`;
}

function nightCasesTable(night) {
  const cases = Array.isArray(night.cases) ? night.cases.filter((c) => c && typeof c === "object") : [];
  if (!cases.length) return `<p class="mut">This night recorded no case${night.truncated ? ": the job was ended before any case finished" : ""}.</p>`;
  const newly = new Set(night.newly_failing || []);
  const rows = [];
  let domain = null;
  for (const c of cases) {
    if (c.domain !== domain) { domain = c.domain; rows.push(`<tr class="grp"><td colspan="5">${esc(domainWords(domain))}</td></tr>`); }
    rows.push(nightCaseRow(night, c, newly));
  }
  return `<table class="rel night"><thead><tr><th>Case</th><th>State</th><th>Reps</th><th>Grader's reason</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function nightChangesHtml(night) {
  if (night.previous_build == null) return `<p class="mut">First night on record: nothing to compare with yet.</p>`;
  const prev = nights().find((n) => String(n.build) === String(night.previous_build));
  const prevText = prev ? `<a href="${esc(nightHref(prev))}">${esc(nightDay(prev))}</a>` : `the night before (build ${esc(night.previous_build)})`;
  const list = (names) => `<code>${names.map(esc).join("</code>, <code>")}</code>`;
  const parts = [];
  parts.push(night.newly_failing.length ? `<p><b>Newly failing</b> against ${prevText}: ${list(night.newly_failing)} — failed every rep tonight and did not the night before.</p>` : `<p>Nothing newly failing against ${prevText}.</p>`);
  if (night.fixed.length) parts.push(`<p><b>Passing again:</b> ${list(night.fixed)} — failed every rep the night before, passed every rep tonight.</p>`);
  if (night.missing.length) parts.push(`<p><b>Not recorded</b> (${plural(night.missing.length, "case")} the nightly matrix on this checkout expects): ${list(night.missing)}.</p>`);
  return parts.join("");
}

function nightsListHtml(current) {
  const list = nights().slice(0, PAGE.nightsListed);
  if (list.length < 2) return "";
  const items = list.map((n) => {
    const v = nightVerdict(n);
    const label = `${esc(nightDay(n))}<span class="pill ${v.cls}">${esc(v.short)}</span>`;
    return String(n.build) === String(current.build) ? `<span class="now">${label}</span>` : `<a href="${esc(nightHref(n))}">${label}</a>`;
  });
  return `<div class="sec"><h2>Other nights</h2><div class="nights">${items.join("")}</div><p class="mut small">The last ${plural(list.length, "night")} on record, newest first; the Cases page carries each case's nightly pass rate over 7 and 30 days.</p></div>`;
}

function nightlyHtml(link) {
  const list = nights();
  const job = brief.nightly && brief.nightly.job || "the nightly periodic";
  if (!list.length) {
    return `<div class="sec head"><h1>No night on record yet</h1><div class="lede">The nightly tier (<code>${esc(job)}</code>, every case against <code>main</code> once a night) has not been collected yet. When it has, this page is last night's report: every case with its state and the grader's reason, what is newly failing against the night before, and whether the night ran to the end.</div>${runningNoteHtml()}</div>` + footHtml();
  }
  const night = (link.build && list.find((n) => String(n.build) === link.build)) || list[0];
  if (link.build && String(night.build) !== link.build) {
    return `<div class="sec head"><h1>No night with build ${esc(link.build)} on record</h1><div class="lede">This page carries the last ${esc(PAGE.nightsListed)} nights. <a href="${PAGE.pages.nightly}">Last night's report →</a></div></div>` + footHtml();
  }
  const c = night.counts;
  const startMs = nightStart(night), finishMs = parseIso(night.finished);
  const isLast = String(night.build) === String(list[0].build);
  const when = startMs != null ? (finishMs != null ? etSpan(startMs, finishMs) : et(startMs)) : "unknown time";
  const took = night.duration_s != null ? minutesText(night.duration_s * 1000) : "unknown wall clock";
  let ledeHow;
  if (night.truncated) ledeHow = `<b>The night was cut short:</b> Prow ended the job after ${esc(took)} with ${c.recorded} of ${c.expected || "?"} cases recorded, so the counts below are not comparable with a full night.`;
  else if (!night.complete) ledeHow = `<b>Incomplete:</b> the job concluded after ${esc(took)} but recorded ${c.recorded} of the ${c.expected} cases the nightly matrix on this checkout expects.`;
  else ledeHow = `The job ran to the end in ${esc(took)}: ${c.recorded} cases recorded${c.expected ? ` of ${c.expected} expected` : ""}.`;
  const head = `<div class="sec head">${nightPill(night)}<h1>${isLast ? "Last night's run" : `Night of ${esc(nightDay(night))}`}</h1>` +
    `<div class="lede">${esc(when)} · <code>${esc(job)}</code>${night.head_sha ? ` at <code>${esc(night.head_sha)}</code>` : ""}${night.project ? ` · project ${esc(projectShort(night.project))}` : ""}${night.log_url ? ` · <a href="${esc(night.log_url)}">build log and artifacts</a>` : ""}</div>` +
    `<div class="lede">${ledeHow}</div>${isLast ? runningNoteHtml() : ""}</div>`;
  const tiles = `<div class="sec"><h2>In numbers</h2><div class="tiles">` +
    tile("Cases", `${c.recorded}`, c.expected ? `of ${c.expected} in the nightly matrix` : "recorded") +
    tile("Passed all reps", `${c.passed}`, c.recorded ? `${pct(c.passed / c.recorded)} of recorded cases` : "no case recorded") +
    tile("Partial", `${c.partial}`, "failed some repetitions") +
    tile("Failed", `${c.failed}`, `failed every graded rep · ${plural(c.infra, "case")} lost to infra`) +
    tile("Newly failing", `${night.previous_build == null ? "—" : night.newly_failing.length}`, night.previous_build == null ? "first night on record" : "against the night before") +
    `</div></div>`;
  return head + tiles +
    `<div class="sec"><h2>Against the night before</h2>${nightChangesHtml(night)}</div>` +
    `<div class="sec"><h2>Every case, by domain</h2>${nightCasesTable(night)}<p class="mut small">States: pass = every graded rep passed · partial = some reps failed · fail = every graded rep failed · infra = nothing graded (quota or setup losses, never counted against a case). Reps read passed / graded. The transcript is the first repetition's.</p></div>` +
    nightsListHtml(night) + footHtml();
}

/* ---- the Trend page (SCHEMA.md: brief.json's trend block; trend.py) ---- */

const trendDoc = () => (brief.trend && typeof brief.trend === "object" && !Array.isArray(brief.trend) ? brief.trend : null);
const trendCases = (t) => (t && t.cases && typeof t.cases === "object" && !Array.isArray(t.cases) ? t.cases : {});
const trendDomains = (t) => (t && t.domains && typeof t.domains === "object" && !Array.isArray(t.domains) ? t.domains : {});
const trendNights = (t) => (t && Array.isArray(t.nights) ? t.nights.filter((n) => n && typeof n === "object" && typeof n.id === "string") : []);
const pointAt = (p) => parseIso(p.at);
const fmtRate = (passes, runs) => (runs ? `${pct(passes / runs)} (${passes}/${runs})` : "no graded run");
const fmtMean = (value) => (isNumber(value) ? value.toFixed(2) : "—");

// A night's label and link: dated by the record's own stamp (`at`, when the
// nightly wrote it), the one time source the whole page uses, so a night's
// bar, tick, marker, tooltip and table row agree; the collector's start
// (`started`) is not used for a date, or the same night would carry two.
// The report link only when the report carries the night; otherwise the
// build log, when the collector recorded one.
function trendNight(t, id) {
  const night = trendNights(t).find((n) => n.id === id);
  const ms = night ? parseIso(night.at) : null;
  const onReport = night && night.build != null && nights().some((n) => String(n.build) === String(night.build));
  return { night, ms, label: ms != null ? et(ms) : "an unknown night", href: onReport ? nightHref({ build: night.build }) : (night && night.log_url ? night.log_url : null) };
}

// The judged metric the page draws: the link's (a chip writes the link),
// when it is one the store carries; else the default (rung 6's metric when
// present).
function trendMetric(t, link) {
  const metrics = Array.isArray(t.metrics) ? t.metrics.filter((m) => typeof m === "string") : [];
  const wanted = link.metric;
  if (wanted && metrics.includes(wanted)) return wanted;
  return typeof t.default_metric === "string" && metrics.includes(t.default_metric) ? t.default_metric : (metrics[0] || null);
}

// What the page is scoped to, from the link alone (a chip writes the
// link): its cases, then its domain, else the overview of every domain.
function trendScope(t, link) {
  const domains = trendDomains(t);
  if (link.cases.size) return { cases: [...link.cases] };
  if (link.domain && domains[link.domain]) return { domain: link.domain };
  return { all: true };
}

// The time axis: the nights in the points, the incident when linked, padded
// a day each side so one night and a marker both sit inside the plot.
function trendRange(pointSets, link) {
  const stamps = [];
  for (const points of pointSets) for (const p of points) { const ms = pointAt(p); if (ms != null) stamps.push(ms); }
  if (link.sinceMs != null) stamps.push(link.sinceMs);
  if (link.untilMs != null) stamps.push(link.untilMs);
  if (!stamps.length) return null;
  const pad = PAGE.trendPadDays * PAGE.dayMs;
  return { fromMs: Math.min(...stamps) - pad, toMs: Math.max(...stamps) + pad };
}

// A point's version key: a case point carries one, a domain point the keys
// of its cases that night.
const keyOf = (p) => (typeof p.key === "string" ? p.key : Array.isArray(p.keys) ? p.keys.join(",") : "");
// The runs of consecutive points that `keep` and that `joins` to the point
// before, each at least two long: what one <path> may connect. A point that
// is not kept ends the run (a gap is drawn as a gap, never bridged).
function segments(points, keep, joins) {
  const out = [];
  let run = [];
  for (const p of points) {
    if (keep(p) && (!run.length || joins(run[run.length - 1], p))) run.push(p);
    else { if (run.length > 1) out.push(run); run = keep(p) ? [p] : []; }
  }
  if (run.length > 1) out.push(run);
  return out;
}

// One chart: `kind` is "rate" (bars for each night's pass rate, a line for
// the trailing admission window, the bar) or "judged" (a line of means with
// the spread band; a lone night is a hollow point). One series colour, the
// page accent; text in text tokens; the tooltip carries every value.
function trendChartHtml(t, points, kind, metric, range, markers, title, key) {
  const g = PAGE.trendChart;
  const plotW = g.width - g.left - g.right, plotH = g.height - g.top - g.bottom;
  const x = (ms) => g.left + (range.toMs === range.fromMs ? plotW / 2 : (ms - range.fromMs) / (range.toMs - range.fromMs) * plotW);
  const y = (v) => g.top + (1 - Math.max(0, Math.min(1, v))) * plotH;
  const spanDays = Math.max(1, (range.toMs - range.fromMs) / PAGE.dayMs);
  const slot = plotW / spanDays;
  const barW = Math.max(3, Math.min(g.barMax, slot * 0.6 - g.gap));
  const parts = [];
  // Grid: solid hairlines at 0, 25, 50, 75, 100 %, ticks in the muted ink.
  for (const v of [0, 0.25, 0.5, 0.75, 1]) parts.push(`<line class="grid" x1="${g.left}" x2="${g.left + plotW}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"></line><text class="axis" x="${g.left - 6}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end">${Math.round(v * 100)}%</text>`);
  // X ticks: one per night up to fourteen, thinned beyond; each a date.
  const nightsOnAxis = points.map((p) => pointAt(p)).filter((ms) => ms != null);
  const every = Math.max(1, Math.ceil(nightsOnAxis.length / 14));
  nightsOnAxis.forEach((ms, i) => { if (i % every === 0 || i === nightsOnAxis.length - 1) parts.push(`<text class="axis" x="${x(ms).toFixed(1)}" y="${g.height - 8}" text-anchor="middle">${esc(fmtParts(ms, { month: "short", day: "numeric" }))}</text>`); });
  const drawn = points.filter((p) => pointAt(p) != null);
  const label = (px, py, text) => (px > g.left + plotW * 0.66
    ? `<text class="lab" x="${(px - 8).toFixed(1)}" y="${(py + 4).toFixed(1)}" text-anchor="end">${esc(text)}</text>`
    : `<text class="lab" x="${(px + 8).toFixed(1)}" y="${(py + 4).toFixed(1)}">${esc(text)}</text>`);
  if (kind === "rate") {
    const bar = isNumber(t.bar && t.bar.rate) ? t.bar.rate : null;
    if (bar != null) parts.push(`<line class="thr" x1="${g.left}" x2="${g.left + plotW}" y1="${y(bar).toFixed(1)}" y2="${y(bar).toFixed(1)}"></line><text class="thr-l" x="${g.left + 4}" y="${(y(bar) - 4).toFixed(1)}">${Math.round(bar * 100)}% bar</text>`);
    for (const p of drawn) {
      if (!p.runs) continue;
      const rate = p.passes / p.runs, top = y(rate), h = Math.max(0, y(0) - top);
      const left = x(pointAt(p)) - barW / 2;
      // 4px rounded data end, square at the baseline: a path, not a rect.
      const r = Math.min(4, barW / 2, h);
      parts.push(`<path class="bar" d="M${left.toFixed(1)},${y(0).toFixed(1)} v${(-(h - r)).toFixed(1)} a${r},${r} 0 0 1 ${r},${-r} h${(barW - 2 * r).toFixed(1)} a${r},${r} 0 0 1 ${r},${r} v${(h - r).toFixed(1)} z"></path>`);
    }
    const line = drawn.filter((p) => p.window && p.window.runs > 0);
    // The window pools at one key, so its line breaks where the key changes.
    for (const seg of segments(line, (p) => true, (a, b) => keyOf(a) === keyOf(b))) parts.push(`<path class="line" d="${seg.map((p, i) => `${i ? "L" : "M"}${x(pointAt(p)).toFixed(1)},${y(p.window.passes / p.window.runs).toFixed(1)}`).join(" ")}"></path>`);
    for (const p of line) parts.push(`<circle class="dot${p.window.full ? "" : " lone"}" cx="${x(pointAt(p)).toFixed(1)}" cy="${y(p.window.passes / p.window.runs).toFixed(1)}" r="${g.dot}"></circle>`);
    const last = line[line.length - 1];
    if (last) parts.push(label(x(pointAt(last)), y(last.window.passes / last.window.runs), `${pct(last.window.passes / last.window.runs)} of ${last.window.runs}${last.window.full ? "" : last.window.cut ? " · window reaches past this read" : " · window not full"}`));
  } else if (metric) {
    const hasMetric = (p) => !!(p.judged && p.judged[metric]);
    const inBand = (p) => { if (!hasMetric(p)) return false; const j = p.judged[metric], s = j.spread || j; return isNumber(s.low) && isNumber(s.high) && (s.nights == null ? (s.cases || 0) > 1 : s.nights > 1); };
    // The band is a pooled spread at one key: it breaks where the key
    // changes, at a night the metric was not recorded, and at a lone night.
    // The line of means breaks at an unrecorded night only; a step at a key
    // change is the thing the marker beside it explains.
    for (const seg of segments(drawn, inBand, (a, b) => keyOf(a) === keyOf(b))) parts.push(`<path class="band" d="${seg.map((p, i) => `${i ? "L" : "M"}${x(pointAt(p)).toFixed(1)},${y((p.judged[metric].spread || p.judged[metric]).high).toFixed(1)}`).join(" ")} ${[...seg].reverse().map((p) => `L${x(pointAt(p)).toFixed(1)},${y((p.judged[metric].spread || p.judged[metric]).low).toFixed(1)}`).join(" ")} z"></path>`);
    for (const seg of segments(drawn, hasMetric, () => true)) parts.push(`<path class="line" d="${seg.map((p, i) => `${i ? "L" : "M"}${x(pointAt(p)).toFixed(1)},${y(p.judged[metric].mean).toFixed(1)}`).join(" ")}"></path>`);
    const withMetric = drawn.filter(hasMetric);
    for (const p of withMetric) {
      const j = p.judged[metric], s = j.spread || j;
      const lone = s.nights != null ? s.nights < 2 : (s.cases || 0) < 2;
      parts.push(`<circle class="dot${lone ? " lone" : ""}" cx="${x(pointAt(p)).toFixed(1)}" cy="${y(j.mean).toFixed(1)}" r="${g.dot}"></circle>`);
    }
    const last = withMetric[withMetric.length - 1];
    if (last) {
      const j = last.judged[metric], s = j.spread || j;
      const lone = s.nights != null ? s.nights < 2 : (s.cases || 0) < 2;
      const range = !lone && s.nights == null ? ` · ${fmtMean(s.low)}–${fmtMean(s.high)} across ${plural(s.cases, "case")}` : "";
      parts.push(label(x(pointAt(last)), y(j.mean), `${fmtMean(j.mean)} · n=${j.n}${lone ? (s.nights != null ? " · one night, no spread yet" : " · one case") : range}`));
    }
  }
  // Markers: the version key changed (label: the components), the incident.
  markers.forEach((m, i) => {
    if (m.ms == null || m.ms < range.fromMs || m.ms > range.toMs) return;
    const px = x(m.ms).toFixed(1);
    parts.push(`<line class="${m.cls}" x1="${px}" x2="${px}" y1="${g.top - 4}" y2="${y(0).toFixed(1)}"></line><text class="${m.cls}-l" x="${(Number(px) + 4).toFixed(1)}" y="${g.top - 8 + (i % 2) * 10}">${esc(m.label)}</text>`);
  });
  // Hit targets: one per night, disjoint (each reaches halfway to its
  // neighbours, to the plot's edge at the ends) and the plot tall, carrying
  // the readout for the tooltip and a native <title>; focusable. Disjoint
  // matters: SVG hit-testing returns the topmost element, so rects that
  // overlapped at a quarter's density named a later night than the one
  // under the pointer.
  const xs = drawn.map((p) => x(pointAt(p)));
  drawn.forEach((p, i) => {
    const cx = xs[i];
    const x0 = i ? (xs[i - 1] + cx) / 2 : g.left, x1 = i < xs.length - 1 ? (cx + xs[i + 1]) / 2 : g.left + plotW;
    const night = trendNight(t, p.night);
    const lines = [`${night.label}${p.commit ? ` · ${String(p.commit).slice(0, 7)}` : ""}`];
    if (kind === "rate") {
      lines.push(`nightly pass rate: ${fmtRate(p.passes, p.runs)}${p.cases ? ` over ${plural(p.cases, "case")}` : ""}`);
      if (p.window) lines.push(`trailing window: ${fmtRate(p.window.passes, p.window.runs)} across ${plural(p.window.lines, "night")}${p.window.full ? "" : p.window.cut ? " · reaches past this read" : " · not full yet"}`);
      if (p.blocked || p.infra) lines.push(`${p.blocked || 0} blocked · ${p.infra || 0} infra, not in the rate`);
    } else if (metric && p.judged && p.judged[metric]) {
      const j = p.judged[metric], s = j.spread || j;
      lines.push(`${metric}: ${fmtMean(j.mean)} mean · n=${j.n}`);
      if (s.nights != null) lines.push(s.nights > 1 ? `range of the last ${plural(s.nights, "nightly mean")}: ${fmtMean(s.low)} – ${fmtMean(s.high)}` : "one night at this key: no spread to show yet");
      else if (s.cases != null) lines.push(s.cases > 1 ? `range across ${plural(s.cases, "case")}: ${fmtMean(s.low)} – ${fmtMean(s.high)}` : "one case that night");
    } else if (metric) lines.push(`${metric}: not recorded that night`);
    if (p.key) lines.push(`key ${p.key}`);
    const tip = lines.join("\n");
    parts.push(`<rect class="hit" tabindex="0" data-hit="${esc(`${key}:${kind}:${i}`)}" x="${x0.toFixed(1)}" y="${g.top}" width="${Math.max(0, x1 - x0).toFixed(1)}" height="${plotH}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"><title>${esc(tip)}</title></rect>`);
  });
  return `<svg class="tchart" viewBox="0 0 ${g.width} ${g.height}" role="img" aria-label="${esc(title)}">${parts.join("")}</svg>`;
}

function trendLegendHtml(kind, metric, t) {
  const spread = isNumber(t.spread_nights) ? t.spread_nights : 7;
  if (kind === "rate") return `<div class="tlegend"><span><i></i>pass rate that night (passes / scored runs)</span><span><i class="line"></i>trailing window: newest nights at the same key pooled to ${esc(t.bar && t.bar.min_runs || 20)} runs (hollow: not full yet)</span><span><i class="thr"></i>the admission bar</span><span><i class="keym"></i>version key changed</span></div>`;
  return `<div class="tlegend"><span><i class="line"></i>${esc(metric || "judged")} mean per night (n in the tooltip)</span><span><i class="band"></i>range of the nightly means over the last ${spread} nights at the same key, or across a domain's cases</span><span><i class="keym"></i>version key changed</span></div>`;
}

function trendMarkers(t, keyChanges, link) {
  const out = [];
  for (const k of keyChanges || []) {
    // Dated like the bar it sits on: by the record (`k.at` is that night's
    // recorded_at), so the marker lands on the night, not beside it.
    const ms = parseIso(k.at) ?? trendNight(t, k.night).ms;
    out.push({ ms, cls: "keym", label: `key: ${(k.changed || []).join(", ") || "changed"}` });
  }
  if (link.sinceMs != null) out.push({ ms: link.sinceMs, cls: "incm", label: "incident start" });
  if (link.untilMs != null) out.push({ ms: link.untilMs, cls: "incm", label: "healthy again" });
  return out;
}

// The table twin of a scope's charts: every value the charts draw.
// `key` names the card (its case or domain) so the table's open state
// survives a re-render (ui.openTables, kept by onToggle).
function trendTableHtml(t, points, metric, perDomain, key) {
  const rows = points.map((p) => {
    const night = trendNight(t, p.night);
    const when = night.href ? `<a href="${esc(night.href)}">${esc(night.label)}</a>` : esc(night.label);
    const j = metric && p.judged ? p.judged[metric] : null;
    const s = j ? (j.spread || j) : null;
    const spread = s && isNumber(s.low) && isNumber(s.high) && (s.nights != null ? s.nights > 1 : (s.cases || 0) > 1) ? `${fmtMean(s.low)} – ${fmtMean(s.high)}` : "—";
    return `<tr><td>${when}</td>${perDomain ? `<td>${p.cases}</td>` : ""}<td>${esc(fmtRate(p.passes, p.runs))}</td><td>${p.window ? esc(`${fmtRate(p.window.passes, p.window.runs)}${p.window.full ? "" : p.window.cut ? " · past this read" : " · not full"}`) : "—"}</td><td>${j ? `${fmtMean(j.mean)} <span class="mut">n=${j.n}</span>` : "—"}</td><td>${spread}</td><td class="mut">${esc(perDomain ? (p.keys || []).join(", ") : p.key || "")}</td></tr>`;
  }).reverse();
  return `<details class="tv" data-tv="${esc(key)}"${ui.openTables.has(key) ? " open" : ""}><summary>Table view · ${plural(points.length, "night")}</summary><table class="tt"><thead><tr><th>Night</th>${perDomain ? "<th>Cases</th>" : ""}<th>Pass rate</th><th>Trailing window</th><th>${esc(metric || "judged")}</th><th>Spread</th><th>Version key</th></tr></thead><tbody>${rows.join("")}</tbody></table></details>`;
}

function trendRecordHtml(rec) {
  if (!rec) return "";
  const words = { "would-admit": "the record would admit it", "would-demote": "the record would demote it", collecting: "collecting", cut: "not knowable from this read" };
  const bar = rec.bar || {};
  const detail = rec.state === "collecting"
    ? `${rec.passes}/${rec.runs} across ${plural(rec.lines, "night")} at the current key, ${Math.max(0, (bar.min_runs || 20) - rec.runs)} more runs before the window is full`
    : rec.state === "cut"
      ? `${rec.passes}/${rec.runs} across ${plural(rec.lines, "night")} at the current key inside this read; the store may hold older records at this key that admission pools and this page did not read`
      : `${rec.passes}/${rec.runs} across ${plural(rec.lines, "night")} at the current key against a bar of ${pct(bar.rate || 0.95)} over ${bar.min_runs || 20}`;
  return `<p class="rec"><b>Record today: ${esc(words[rec.state] || rec.state)}.</b> ${esc(detail)} <span class="mut">(as of ${esc(et(parseIso(rec.as_of)))}; the roster decides, the record informs)</span></p>`;
}

function trendKeyChangesHtml(t, changes) {
  if (!changes || !changes.length) return "";
  const items = changes.map((k) => { const night = trendNight(t, k.night); return `<li>${esc(night.label)}: <b>${esc((k.changed || []).join(", "))}</b> changed · <code>${esc(k.from)}</code> → <code>${esc(k.to)}</code></li>`; });
  return `<p class="rec"><b>Version key changed</b> (pooling never crosses one):</p><ul class="merges">${items.join("")}</ul>`;
}

// `key` is the card's stable name (a case or a domain): the hit targets and
// the table view carry it so focus and an open table survive a re-render.
function trendCardHtml(t, title, sub, points, keyChanges, metric, range, link, perDomain, footer, key) {
  const markers = trendMarkers(t, keyChanges, link);
  // The charts' accessible name is the card's name as text (`title` is markup).
  const name = key.startsWith("domain:") ? domainWords(key.slice("domain:".length)) : key.slice("case:".length);
  if (!points.length) return `<div class="tcard"><h3>${title}${sub ? `<small>${sub}</small>` : ""}</h3><p class="mut">No record in the store for this scope.</p></div>`;
  return `<div class="tcard"><h3>${title}${sub ? `<small>${sub}</small>` : ""}</h3><div class="tgrid">` +
    `<div><div class="small mut">Pass rate by night</div>${trendChartHtml(t, points, "rate", null, range, markers, `${name}: pass rate by night`, key)}${trendLegendHtml("rate", null, t)}</div>` +
    `<div><div class="small mut">Judged ${esc(metric || "quality")} by night · advisory</div>${metric ? trendChartHtml(t, points, "judged", metric, range, markers, `${name}: ${metric} by night`, key) : `<p class="mut">No judged metric in the store yet.</p>`}${metric ? trendLegendHtml("judged", metric, t) : ""}</div>` +
    `</div>${footer || ""}${trendTableHtml(t, points, metric, perDomain, key)}</div>`;
}

function trendStatusHtml(t) {
  if (!t || (!t.source && !t.read_at)) return `<div class="banner hs-amber">${pillHtml("DEGRADED", "STORE NOT READ")}<span>The evidence store was not read for this render (no <code>store.json</code> beside the data), so there is nothing to draw. The next scheduled refresh reads it; the other pages are unaffected.</span></div>`;
  const read = parseIso(t.read_at);
  const parts = [];
  if (t.error) parts.push(`<div class="banner hs-amber">${pillHtml("DEGRADED", "STALE READ")}<span>The store could not be read on the last refresh (${esc(String(t.error).slice(0, 200))}); this page shows the last good read, ${esc(read != null ? et(read) : "at an unknown time")}.</span></div>`);
  if (t.partial && isNumber(t.partial.remaining) && t.partial.remaining > 0) parts.push(`<p class="stale">The last read stopped at its time budget after ${esc(t.partial.fetched)} of ${esc(t.partial.fetched + t.partial.remaining)} objects; the cases the rest belong to are drawn without them until the next refresh finishes the read.</p>`);
  const truncated = t.truncated && typeof t.truncated === "object" ? Object.keys(t.truncated) : [];
  if (truncated.length) parts.push(`<p class="stale">The read was capped at ${esc(t.max_objects)} objects per case per version key for ${plural(truncated.length, "case")} (${truncated.slice(0, 6).map(esc).join(", ")}${truncated.length > 6 ? ", …" : ""}); their oldest nights inside the window are not drawn.</p>`);
  if (Array.isArray(t.warnings) && t.warnings.length) parts.push(`<p class="stale">${plural(t.warnings.length, "record")} in the store could not be read and ${t.warnings.length === 1 ? "is" : "are"} left out: <code>${esc(String(t.warnings[0]).slice(0, 160))}</code>${t.warnings.length > 1 ? " …" : ""}</p>`);
  return parts.join("");
}

function trendHtml(link) {
  const t = trendDoc();
  const lede = `Every night the nightly tier runs each case against <code>main</code> three times and appends one record per case to the evidence store; this page reads those records back, each night dated by when its record was written. <b>Pass rate is the gate's number</b>: passes over scored repetitions, with the trailing window computed admission reads drawn beside each night. <b>Judged quality is advisory</b> and is never shown as one point: the store carries a mean and its n per night, so the band is the range of nightly means over the last ${esc(t && t.spread_nights || 7)} nights at one version key (a domain's band is the range across its cases). A vertical marker is a night the version key changed, so a step has its explanation next to it. <a href="${PAGE.scoreDocUrl}">What a score is</a> is written once, in the eval-scorer design.`;
  const head = (title) => `<div class="sec head"><h1>${title}</h1><div class="lede">${lede}</div></div>`;
  if (!t || (!t.source && !t.read_at) || !t.records) {
    const why = t && t.source ? `<p class="mut">The store <code>${esc(t.source)}</code> was read ${esc(et(parseIso(t.read_at)))} and holds no record inside its ${esc(t.window_days || "")}-day window yet. The first night that records fills this page.</p>` : "";
    return head("Scores over time on main") + trendStatusHtml(t) + why + footHtml();
  }
  const metric = trendMetric(t, link);
  const scope = trendScope(t, link);
  const cases = trendCases(t), domains = trendDomains(t);
  const metrics = Array.isArray(t.metrics) ? t.metrics.filter((m) => typeof m === "string") : [];
  const domainNames = Object.keys(domains).sort();
  const ctl = `<div class="ctl">scope ${chip("scope", "", "all domains", !!scope.all)}${domainNames.map((d) => chip("scope", d, domainWords(d), scope.domain === d)).join("")}` +
    (metrics.length > 1 ? `<span class="sep">·</span>judged metric ${metrics.map((m) => chip("metric", m, m, m === metric)).join("")}` : "") + `</div>`;
  const read = parseIso(t.read_at);
  const readLine = `<p class="mut small">Store <code>${esc(t.source)}</code> read ${esc(read != null ? et(read) : "at an unknown time")} · ${plural(t.records, "record")} over ${plural(trendNights(t).length, "night")} inside the last ${esc(t.window_days)} days · nightly records only (a pull request's run never writes the store).</p>`;
  let body = "";
  if (scope.cases) {
    const found = scope.cases.filter((name) => cases[name]);
    const missing = scope.cases.filter((name) => !cases[name]);
    const range = trendRange(found.map((name) => cases[name].points || []), link);
    body = found.map((name) => {
      const c = cases[name];
      const footer = trendRecordHtml(c.record) + trendKeyChangesHtml(t, c.key_changes) + `<p class="small"><a href="${esc(caseHref(name))}">this case on the Cases page →</a> · <a href="${esc(trendDomainHref(c.domain))}">its domain, ${esc(domainWords(c.domain))} →</a></p>`;
      return trendCardHtml(t, `<code>${esc(name)}</code>`, esc(domainWords(c.domain)), c.points || [], c.key_changes, metric, range || { fromMs: nowMs() - PAGE.dayMs, toMs: nowMs() }, link, false, footer, `case:${name}`);
    }).join("");
    if (missing.length) body += `<div class="tcard"><h3><code>${missing.map(esc).join("</code>, <code>")}</code></h3><p class="mut">No record in the store for ${missing.length === 1 ? "this case" : "these cases"} inside the window: ${missing.length === 1 ? "it has" : "they have"} not run on a recording night yet, or the name is not a case.</p></div>`;
    const title = scope.cases.length === 1 ? `<code>${esc(scope.cases[0])}</code> on main` : `${plural(scope.cases.length, "case")} on main`;
    const inc = link.sinceMs != null ? `<div class="lede">Marked: the incident that started ${esc(et(link.sinceMs))}${link.untilMs != null ? ` and ended ${esc(et(link.untilMs))}` : ""}. The record either side of the marker is what main did around it.</div>` : "";
    return head(title) + inc + trendStatusHtml(t) + ctl + readLine + body + footHtml();
  }
  if (scope.domain) {
    const d = domains[scope.domain];
    const members = (d.cases || []).filter((name) => cases[name]);
    const range = trendRange([d.points || [], ...members.map((name) => cases[name].points || [])], link);
    body = trendCardHtml(t, esc(domainWords(scope.domain)), `${plural(members.length, "case")} pooled per night`, d.points || [], d.key_changes, metric, range, link, true, "", `domain:${scope.domain}`) +
      `<div class="sec"><h2>Each case in ${esc(domainWords(scope.domain))}</h2>${members.map((name) => trendCardHtml(t, `<a href="${esc(trendHref([name]))}"><code>${esc(name)}</code></a>`, "", cases[name].points || [], cases[name].key_changes, metric, range, link, false, trendRecordHtml(cases[name].record), `case:${name}`)).join("")}</div>`;
    return head(`${esc(domainWords(scope.domain))} on main`) + trendStatusHtml(t) + ctl + readLine + body + footHtml();
  }
  const range = trendRange(domainNames.map((name) => domains[name].points || []), link);
  body = domainNames.map((name) => trendCardHtml(t, `<a href="${esc(trendDomainHref(name))}">${esc(domainWords(name))}</a>`, `${plural((domains[name].cases || []).length, "case")} pooled per night`, domains[name].points || [], domains[name].key_changes, metric, range, link, true, "", `domain:${name}`)).join("");
  return head("Scores over time on main") + trendStatusHtml(t) + ctl + readLine + body + footHtml();
}

// The Trend page's tooltip: one element, fed by textContent from the hit
// target under the pointer (or the focused one), never markup.
function onTrendPointer(event) {
  const tip = document.getElementById("ttip");
  if (!tip) return;
  const hit = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
  if (!hit || event.type === "pointerout" || event.type === "focusout") {
    if (!hit || event.type === "pointerout" || event.type === "focusout") tip.style.display = "none";
    return;
  }
  tip.textContent = hit.dataset.tip;
  tip.style.display = "block";
  const box = hit.getBoundingClientRect();
  const px = event.clientX || box.left + box.width / 2, py = event.clientY || box.top;
  const left = Math.min(px + 14, window.innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, py - tip.offsetHeight - 12)}px`;
}

// The Trend page's table views: <details> keeps its open state in the DOM
// only, and the poll's re-render rebuilds the DOM, so the state is kept
// here by card. `toggle` does not bubble; the listener is on the capture
// phase.
function onToggle(event) {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement) || details.dataset.tv == null) return;
  if (details.open) ui.openTables.add(details.dataset.tv);
  else ui.openTables.delete(details.dataset.tv);
}

/* ---- clicks on the Grid and the Cases page ---- */

function onClick(event) {
  const target = event.target.closest("button[data-sort],button[data-show],button[data-window],button[data-rows],button[data-marker],button[data-toggle],button[data-build],button[data-scope],button[data-metric]");
  if (!target) return;
  const d = target.dataset;
  if (d.sort) ui.sort = d.sort;
  else if (d.show) ui.show = d.show;
  else if (d.window) ui.window = d.window;
  else if (d.rows) ui.rows = d.rows;
  else if (d.marker) ui.markers[d.marker] = !ui.markers[d.marker];
  else if (d.toggle === "held") ui.showHeld = true;
  else if (d.toggle === "retired") ui.showRetired = true;
  else if (d.toggle === "close") ui.selected = null;
  else if (d.build && d.case) ui.selected = { build: d.build, case: d.case };
  else if (d.scope != null || d.metric) {
    // The Trend page's chips navigate (trendStateHref); the hashchange
    // re-renders, and a chip that names the current state changes nothing.
    const href = d.metric ? trendStateHref(linkState(), { metric: d.metric }) : trendStateHref(linkState(), { cases: [], domain: d.scope || null });
    location.hash = href.slice(href.indexOf("#"));
    return;
  }
  renderAll();
  if (d.build && d.case) {
    const panel = document.getElementById("detail");
    if (panel) panel.scrollIntoView({ block: "nearest" });
  }
}

/* ---- freshness, poll, boot ---- */

function renderFreshness() {
  const el = document.getElementById("freshness");
  if (!el) return;
  const generated = parseIso(brief.generated_at);
  const staleAfterMs = 1000 * (typeof brief.stale_after_s === "number" ? brief.stale_after_s : 7200);
  const ageMin = generated != null ? Math.max(0, Math.round((Date.now() - generated) / 60000)) : null;
  let text = `updated ${generated != null ? et(generated, Date.now()) : "—"}${ageMin != null ? ` · ${ageMin}m ago` : ""}`;
  if (!live) text += ` · regenerated every ${PAGE.republishMinutes} min`;
  const stale = generated != null && Date.now() - generated > staleAfterMs;
  if (stale) text = `STALE · ${text}`;
  el.textContent = text;
  el.className = stale ? "fresh stale" : "fresh";
}

const renderers = { brief: briefHtml, run: runHtml, grid: gridHtml, cases: casesHtml, nightly: nightlyHtml, trend: trendHtml };

// `scroll` is true for a navigation (boot, a hash change): the page then
// scrolls to the `view=` section or the `#<case>` row it just rendered. The
// poll and a chip re-render with it false, so a reader who has scrolled on
// is not pulled back.
function renderAll(scroll = false) {
  const link = linkState();
  const app = document.getElementById("app");
  const page = document.body.dataset.page in renderers ? document.body.dataset.page : "brief";
  // A re-render (a click, the poll) must not move the Grid under the reader,
  // nor take the Trend page's focused night (its tooltip with it) from a
  // keyboard reader: the hit target is found again by its key after the
  // render and focused.
  const scrolled = document.querySelector(".gscroll");
  const keepLeft = scrolled ? scrolled.scrollLeft : null;
  const active = document.activeElement;
  const focusedHit = active && app.contains(active) && active.dataset && active.dataset.hit != null ? active.dataset.hit : null;
  try {
    if (!briefLoaded) throw new Error(`the page's data element (#${PAGE.inlineBrief}) is missing or unreadable`);
    app.innerHTML = renderers[page](link) + (page === "trend" ? '<div class="ttip" id="ttip" role="tooltip"></div>' : "");
  } catch (err) {
    app.innerHTML = `<div class="sec head"><h1>This page could not render.</h1><div class="lede">${esc(String(err && err.message || err))}. The data behind it is <a href="${PAGE.briefFile}">${PAGE.briefFile}</a>.</div></div>`;
  }
  document.title = PAGE.titles[page];
  renderFreshness();
  if (focusedHit != null) {
    const again = app.querySelector(`[data-hit="${CSS.escape(focusedHit)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
  // The section is rendered just above, after the browser looked for the
  // anchor, and a `view=` fragment names no element anyway: scroll by hand.
  if (scroll && link.view) {
    const target = document.getElementById(link.view);
    if (target) target.scrollIntoView();
  } else if (scroll && page === "cases" && link.caseHash) {
    const row = document.getElementById(`case-${link.caseHash}`);
    if (row) row.scrollIntoView({ block: "center" });
  }
  if (page === "grid") {
    // First paint: a live window is read from its newest run, a linked
    // incident from its start. Afterwards the reader's position stands.
    const wrap = document.querySelector(".gscroll");
    if (wrap) wrap.scrollLeft = keepLeft != null ? keepLeft : (link.sinceMs == null ? wrap.scrollWidth : 0);
  }
}

async function fetchJson(name) {
  const response = await fetch(name, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function refresh() {
  try {
    const next = await fetchJson(PAGE.briefFile);
    if (next && typeof next === "object" && Array.isArray(next.runs)) {
      // brief.json carries no trend block (trend.json does, below); the
      // one inlined or last polled stays.
      next.trend = brief.trend;
      brief = next;
      briefLoaded = true;
      health = normalizeHealth(next.health) ?? health;
      // Only a payload the page took counts as a successful poll.
      live = true;
    }
  } catch (err) {
    // The inlined data stays on screen; the badge says the page is as
    // fresh as its last publish.
    live = false;
  }
  try {
    const fresh = normalizeHealth(await fetchJson(PAGE.healthFile));
    if (fresh) health = fresh;
  } catch (err) {
    // health.json is optional; the inlined verdict (or none) stays.
  }
  if (document.body.dataset.page === "trend") {
    try {
      const next = await fetchJson(PAGE.trendFile);
      if (next && typeof next === "object" && !Array.isArray(next)) brief.trend = next;
    } catch (err) {
      // The inlined block stays, as brief.json's data does above.
    }
  }
  renderAll();
}

document.getElementById("app").addEventListener("click", onClick);
document.getElementById("app").addEventListener("toggle", onToggle, true);
for (const type of ["pointermove", "pointerout", "focusin", "focusout"]) document.getElementById("app").addEventListener(type, onTrendPointer);
renderAll(true);
// Polling is attempted everywhere, a file:// preview included: a failed
// poll costs nothing but the "regenerated every N min" suffix.
refresh();
setInterval(refresh, PAGE.refreshMs);
window.addEventListener("hashchange", () => renderAll(true));
setInterval(renderFreshness, 30000);
