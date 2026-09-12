/* The Brief (index.html) and the PR view (run.html) share this script.
 *
 * render.py inlines it into both pages after two JSON data elements
 * (PAGE.inlineBrief and PAGE.inlineHealth): brief.json -- the per-run
 * classification classify.py produced, the current health verdict, the
 * health history and the recent merges -- and, when there is a verdict,
 * that verdict again under its own id (the same normalized document as
 * brief.health). Each page renders itself from those in the browser. There is no
 * server-side HTML for these two pages:
 * everything a reader sees is computed here from the inlined copy, with no
 * request beyond the page itself.
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
 * The older query form (`?cases=a,b&since=<ISO>&until=<ISO>` before a bare
 * `#gate` or `#agent`; `?build=<id>` on run.html) is still read, so a link
 * already posted opens the same page wherever its query survives.
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
  // lookback (shared break 6 h, storm, setup deaths and lost pods 2 h), so
  // the runs that made the bot declare it are on the page, not only the ones
  // after.
  incidentLeadMs: { shared_break: 6 * 3600 * 1000, storm: 2 * 3600 * 1000, setup_deaths: 2 * 3600 * 1000, lost_pods: 2 * 3600 * 1000 },
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
  spyglass: "https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents",
  job: "pull-kube-agents-smoke-test",
  prUrl: "https://github.com/gke-labs/kube-agents/pull",
  issueUrl: "https://github.com/gke-labs/kube-agents/issues",
  rulesUrl: "https://github.com/gke-labs/kube-agents/blob/main/scripts/eval_dashboard/classify.py",
  briefFile: "brief.json",
  healthFile: "health.json",
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

const esc = (value) => String(value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

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
    tick: parseIso(raw.tick) != null ? raw.tick : null,
  };
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
// (`#since=…&until=…&cases=…&view=…`, `#build=…`). A bare `#gate` or
// `#agent`, the old anchors, still selects that view.
function linkState() {
  const out = { cases: new Set(), sinceMs: null, untilMs: null, build: null, view: "" };
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
  const view = param("view") || location.hash.slice(1);
  out.view = Object.values(PAGE.views).includes(view) ? view : "";
  return out;
}

// The links the pages write, in the fragment form linkState reads. Nothing
// is percent-encoded: the case ids passed linkCaseIds' grammar and the
// timestamps are utcIso's `Z` form, so the text is the same one
// post_health.dashboard_link writes.
function briefHref(view, inc = {}) {
  const params = [];
  if (inc.sinceMs != null) params.push(`since=${utcIso(inc.sinceMs)}`);
  if (inc.untilMs != null) params.push(`until=${utcIso(inc.untilMs)}`);
  const cases = linkCaseIds(inc.cases || []);
  if (cases.length) params.push(`cases=${cases.join(",")}`);
  params.push(`view=${view}`);
  return `index.html#${params.join("&")}`;
}
const incidentHref = (inc) => briefHref(PAGE.views.gate, inc);
const numbersHref = () => briefHref(PAGE.views.agent);
const runHref = (run) => `run.html#build=${encodeURIComponent(run.build)}`;

/* ---- links out ---- */

const prLink = (pr) => (pr == null ? "no PR" : `<a href="${PAGE.prUrl}/${esc(pr)}">PR #${esc(pr)}</a>`);
const buildUrl = (run) => (run.pr == null ? null : `${PAGE.spyglass}/${encodeURIComponent(run.pr)}/${PAGE.job}/${encodeURIComponent(run.build)}`);
const transcriptUrl = (run, kase) => {
  const base = buildUrl(run);
  return base ? `${base}/artifacts/eval_${encodeURIComponent(kase)}_rep1.log` : null;
};
const issueLink = (issue) => {
  const match = /^#(\d+)$/.exec(String(issue).trim());
  return match ? `<a href="${PAGE.issueUrl}/${match[1]}">${esc(issue)}</a>` : esc(issue);
};
const projectShort = (project) => (project ? String(project).replace(/^kube-agents-/, "") : "unknown");

/* ---- runs and windows ---- */

const runs = () => (Array.isArray(brief.runs) ? brief.runs.filter((r) => r && typeof r === "object") : []);
const runFinish = (run) => parseIso(run.finished) ?? parseIso(run.started);
const measured = (run) => Array.isArray(run.cases) && run.cases.length > 0;
const concluded = (run) => run.result === "SUCCESS" || run.result === "FAILURE";
const isGreen = (run) => run.result === "SUCCESS";
const gateFailures = (run) => (run.cases || []).filter((c) => c.admitted && c.outcome === "failed").map((c) => c.case);
const heldOutFailures = (run) => (run.cases || []).filter((c) => !c.admitted && c.outcome === "failed").map((c) => c.case);

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
  for (const run of hit) for (const c of run.cases || []) { reps += c.reps.pass + c.reps.fail + c.reps.infra; storm += c.reps.infra; }
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
    for (const c of run.cases || []) { reps += c.reps.pass + c.reps.fail + c.reps.infra; storm += c.reps.infra; mine += c.reps.infra; }
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

/* ---- fragments shared by both pages ---- */

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
  return { head: inc.past ? "A past gate incident" : "The gate is degraded", lede: esc(inc.cause || "") };
}

function recoveryProgress(inc) {
  // No start on record: nothing is "after" the incident, so no run counts.
  if (inc.sinceMs == null) return 0;
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
  return "What the data shows";
}

function agentSawHtml(inc, inWindow) {
  const cases = new Set(inc.cases);
  let pick = null;
  for (const run of [...inWindow].reverse()) {
    for (const c of run.cases || []) {
      if (inc.condition === "storm" ? c.outcome === "infra" && c.reason : (cases.size ? cases.has(c.case) : c.admitted) && c.outcome === "failed" && c.reason) {
        pick = { run, c };
        break;
      }
    }
    if (pick) break;
  }
  if (inc.condition === "setup_deaths") {
    const death = [...inWindow].reverse().find((r) => r.setup_death);
    const url = death ? buildUrl(death) : null;
    return `<p>No agent ran. The build log is the evidence${url ? `: <a href="${esc(url)}">${prLink(death.pr).replace(/<[^>]+>/g, "")} at ${esc(et(runFinish(death)))}</a>` : ""}.</p>`;
  }
  if (!pick) return `<p class="mut">No failed repetition with a recorded reason in this window.</p>`;
  const url = transcriptUrl(pick.run, pick.c.case);
  const quote = pick.c.excerpt
    ? `<div class="q">“${esc(pick.c.excerpt)}”<small>From the agent's report on ${prLink(pick.run.pr)} · <code>${esc(pick.c.case)}</code>${url ? ` · <a href="${esc(url)}">full transcript</a>` : ""}</small></div>`
    : "";
  return `${quote}<div class="reason">${esc(pick.c.reason)}</div><small class="mut">The check that failed, as the grader wrote it, on ${prLink(pick.run.pr)} · <code>${esc(pick.c.case)}</code>${url ? ` · <a href="${esc(url)}">transcript (rep 1)</a>` : ""}. data.json carries no agent report text, so nothing here is quoted from the agent.</small>`;
}

function changedBeforeHtml(inc, inWindow) {
  if (!Array.isArray(brief.merges)) return "";
  const firstRed = inWindow.find((r) => isBreak(inc) ? gateFailures(r).some((c) => inc.cases.includes(c)) : (inc.condition === "setup_deaths" ? r.setup_death : (r.storm_reps || 0) > 0));
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
  if (inc.tracking.length) lines.push(`<p>Tracking ${inc.tracking.map(issueLink).join(", ")}. The gate comes back on its own once the fix lands: the bot reports healthy after ${PAGE.recoveryGreenRuns} clean runs on different PRs.</p>`);
  if (inc.recovering) lines.push(`<p><b>The condition has cleared.</b> A retest is reasonable now; ${recoveryProgress(inc)} of ${PAGE.recoveryGreenRuns} clean runs on distinct PRs so far.</p>`);
  else if (isBreak(inc) && !inc.tracking.length && !inc.past) lines.push(`<p>No issue is filed yet. File one with the <code>presubmit-gate</code> label and link this page. Demoting the case in <code>hack/ci-eval-pr.sh</code> unblocks merges while the fixture is fixed; re-admit it afterwards.</p>`);
  else if (inc.condition === "storm" && !inc.past) {
    const retest = stormRetestMs(inc);
    lines.push(`<p>Wait it out${retest != null ? `: retest after ${esc(et(retest))}` : ""}. The API quota is fixed, so fewer runs at once is the only lever; a retest inside the storm loses repetitions the same way.</p>`);
  } else if (inc.condition === "setup_deaths" && !inc.past) lines.push(`<p>Check the leased pool projects before spending another run: a stuck Helm release or a failing image pull is the usual cause. Retest once the deaths stop.</p>`);
  if (inc.past) lines.push(`<p class="mut">This incident is over${inc.untilMs != null ? `; the gate was reported healthy again at ${esc(et(inc.untilMs))}` : ""}.</p>`);
  if (inc.stale) lines.push(`<p class="stale">The data behind this state stopped refreshing; the state is as old as the data.</p>`);
  if (!lines.length) return "";
  return `<div class="next"><h2>${inc.past ? "What was done" : "What's being done"}</h2>${lines.join("")}</div>`;
}

function runsListHtml(inWindow, inc, title) {
  const cases = new Set(inc ? inc.cases : []);
  // A superseded push (aborted, nothing recorded) says nothing about the gate; count it, don't list it.
  const shown = inWindow.filter((run) => measured(run) || run.setup_death || concluded(run));
  const hidden = inWindow.length - shown.length;
  const rows = [...shown].reverse().map((run) => {
    const failed = gateFailures(run);
    const held = heldOutFailures(run).length;
    const chips = failed.map((c) => `<span class="chip ${cases.has(c) ? "hit" : "miss"}">${esc(c)}</span>`).join("");
    let note = "";
    if (run.setup_death) note = '<span class="chip inf">died in setup</span>';
    else if (!measured(run)) note = `<span class="chip inf">${run.result === "ABORTED" ? "aborted" : "no cases recorded"}</span>`;
    else if (!failed.length) note = '<span class="chip ok">all gate cases passed</span>';
    const stormChip = (run.storm_reps || 0) >= 5 ? `<span class="chip inf">${run.storm_reps} reps lost</span>` : "";
    return `<a class="runrow v-${esc(run.verdict || "infra")}" href="${esc(runHref(run))}">` +
      `<span class="rpr">#${esc(run.pr ?? "?")}</span><span class="rwhen">${esc(et(runFinish(run)))}</span>` +
      `<span class="rproj">${esc(projectShort(run.project))}</span><span class="rcases">${chips}${note}${stormChip}${held ? `<span class="mut small">+${held} held-out</span>` : ""}</span></a>`;
  });
  return `<div class="sec"><h2>${esc(title)}</h2><div class="runs">${rows.join("") || '<p class="mut">No runs in this window.</p>'}</div>` +
    `<p class="mut small">Each row opens that run's page. Red chips are the incident's cases; amber ones are other gate failures.${hidden ? ` ${plural(hidden, "aborted run")} not listed.` : ""}</p></div>`;
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
  for (const run of full) for (const c of run.cases || []) { reps += c.reps.pass + c.reps.fail + c.reps.infra; lost += c.reps.infra; }
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
  const what = isBreak(inc) ? `${plural(inc.cases.length, "gate case")} failing on every PR` : inc.condition === "storm" ? "a quota storm" : inc.condition === "setup_deaths" ? "runs dying in setup" : "a degraded gate";
  return `<p>${pillHtml(inc.state, `PAST ${inc.state}`)} <b>${esc(etSpan(inc.sinceMs, inc.untilMs))}</b> — ${what}${inc.cases.length ? ` (<code>${inc.cases.map(esc).join("</code>, <code>")}</code>)` : ""}. <a href="${esc(incidentHref(inc))}">Open the brief for it →</a></p>`;
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
      lede = `No shared breaks, quota storms or setup failures right now. ${n.green} of ${n.green + n.reds} concluded runs in the last 24 hours were green.${health.stale ? " The data behind this state has stopped refreshing." : ""}`;
      pill = pillHtml(health.stale ? "DEGRADED" : "GREEN", health.stale ? "HEALTHY · STALE" : "HEALTHY");
    } else if (noVerdict) {
      // No health.json beside the data: the runs alone cannot say the gate is healthy.
      head = "No gate verdict is published";
      lede = `The adjudicator's health.json is not beside data.json, so this page cannot say whether the gate is healthy. The counts below come from the runs alone; each run's page still tags its failures.`;
      pill = pillHtml("PAST", "NO VERDICT");
    } else {
      head = "The last 24 hours in numbers";
      lede = `The gate is ${inc.recovering ? "recovering" : inc.state.toLowerCase()} — <a href="index.html">read the brief</a>. These are the plain counts.`;
      pill = pillHtml(inc.state, stateWord(inc));
    }
    return `<div class="sec head">${pill}<h1>${esc(head)}</h1><div class="lede">${lede}</div></div>` +
      `<div class="sec" id="agent"><h2>Last 24 hours</h2>${numbersHtml(sinceMs, null)}</div>` +
      (healthy || noVerdict ? `<div class="sec"><h2>Last incident</h2>${lastIncidentHtml()}</div>` : "") +
      runsListHtml(windowRuns(sinceMs, null), inc, "Runs in the last 24 hours") +
      footHtml();
  }
  const inWindow = windowRuns(incidentStartMs(inc), inc.untilMs);
  if (!inWindow.some(measured) && !inWindow.some((r) => r.setup_death)) {
    // Nothing on record for the window (older than brief.json's run_days, or
    // the link points at a time with no runs): say so instead of counting zeros.
    const pillText = `${stateWord(inc)} · ${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `since ${et(inc.sinceMs)}`}`;
    return `<div class="sec head">${pillHtml(inc.state, pillText)}<h1>No runs on record for this window</h1>` +
      `<div class="lede">${inc.cases.length ? `The incident named <code>${inc.cases.map(esc).join("</code>, <code>")}</code>. ` : ""}This page carries the runs of the last ${esc(brief.run_days ?? "?")} days; the window ${esc(etSpan(inc.sinceMs, inc.untilMs))} has none of them.</div></div>` +
      beingDoneHtml(inc) + footHtml();
  }
  const { head, lede } = briefHeadline(inc, inWindow);
  const facts = isBreak(inc) ? breakFacts(inc, inWindow) : inc.condition === "storm" ? stormFacts(inc, inWindow) : inc.condition === "setup_deaths" ? setupFacts(inc, inWindow) : [];
  const pillText = `${stateWord(inc)} · ${inc.past ? esc(etSpan(inc.sinceMs, inc.untilMs)) : `since ${et(inc.sinceMs)}`}${inc.stale ? " · STALE" : ""}`;
  let recoveringLine = "";
  if (inc.recovering) recoveringLine = `<div class="lede">The condition has cleared; ${recoveryProgress(inc)} of ${PAGE.recoveryGreenRuns} clean runs on distinct PRs so far. A retest is reasonable.</div>`;
  return `<div class="sec head">${pillHtml(inc.recovering ? "DEGRADED" : inc.state, pillText)}<h1>${head}</h1><div class="lede">${lede}</div>${recoveringLine}</div>` +
    (facts.length ? `<div class="sec" id="gate"><h2>${esc(whyTitle(inc))}</h2>${factsHtml(facts)}</div>` : "") +
    `<div class="sec"><h2>What the agent saw</h2>${agentSawHtml(inc, inWindow)}</div>` +
    changedBeforeHtml(inc, inWindow) +
    beingDoneHtml(inc) +
    runsListHtml(inWindow, inc, "Runs in this window") +
    footHtml();
}

function footHtml() {
  const generated = parseIso(brief.generated_at);
  return `<div class="foot"><span>Full history table: <a href="legacy.html">legacy view</a></span><span><a href="${esc(numbersHref())}">The last 24 hours in numbers</a></span><span><a href="${PAGE.rulesUrl}">How the tags are decided</a></span><span class="mut">data generated ${esc(generated != null ? et(generated) : "unknown")}${brief.run_days ? ` · runs from the last ${esc(brief.run_days)} days` : ""}</span></div>`;
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
  if (h.state === "GREEN") text = `<b>Gate healthy ${when}.</b> No shared break, storm or setup failures. <a href="index.html">Brief →</a>`;
  else if (h.condition === "storm") text = `<b>Quota storm ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs lose repetitions to 429s and empty records. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "setup_deaths") text = `<b>Setup failures ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs die before any case runs. <a href="${esc(href)}">Read the brief →</a>`;
  else if (h.condition === "lost_pods") text = `<b>Build nodes lost ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: runs died with the node under them; nothing about the branch. <a href="${esc(href)}">Read the brief →</a>`;
  else text = `<b>Gate ${h.recovering ? "recovering" : "outage"} ${when}</b>${sinceMs != null ? ` since ${esc(et(sinceMs))}` : ""}: ${cases.length ? `<code>${cases.map(esc).join("</code>, <code>")}</code> fail${cases.length === 1 ? "s" : ""} on every PR` : esc(h.cause || "a shared break")}. <a href="${esc(href)}">Read the brief →</a>`;
  const state = h.recovering ? "DEGRADED" : h.state;
  return `<div class="banner ${PAGE.states[state] || "hs-past"}">${pillHtml(state, h.recovering ? "RECOVERING" : h.state)}<span>${text}</span></div>`;
}

function tagFor(c) {
  if (!c.admitted) return '<span class="tag held">held out</span>';
  if (c.cls === "shared") return `<span class="tag shared">${c.also_failing_prs >= 1 ? `failing on ${plural(c.also_failing_prs, "other PR")}` : "in the current outage"}</span>`;
  if (c.cls === "only-this-pr") return '<span class="tag yours">only your PR</span>';
  if (c.cls === "storm") return '<span class="tag storm">quota storm</span>';
  return '<span class="tag unclear">unexplained</span>';
}

function caseCard(run, c) {
  const reps = c.reps || { pass: 0, fail: 0, infra: 0 };
  const total = reps.pass + reps.fail + reps.infra;
  const how = c.outcome === "failed" ? `failed all ${plural(reps.fail, "graded rep")}${reps.infra ? ` (${reps.infra} lost)` : ""}` : c.outcome === "infra" ? `all ${plural(total, "rep")} lost before grading` : `${reps.pass} of ${total} reps passed`;
  const rate = c.pass_rate_30d != null ? ` · this case passed ${pct(c.pass_rate_30d)} of the time over the last 30 days on PRs` : "";
  // The nightly's record beside the gate's: the newest night within two
  // days of this run, when one graded the case. Evidence about main, never
  // a tag -- the tags above answer "is this mine?" from the presubmit alone.
  const nightly = c.nightly_failed_recent === true ? " · it also failed every repetition on the latest nightly run"
    : c.nightly_failed_recent === false ? " · the latest nightly run passed it" : "";
  const url = transcriptUrl(run, c.case);
  const log = buildUrl(run);
  return `<div class="case"><div class="hd"><h3>${esc(c.case)}</h3>${tagFor(c)}</div>` +
    `<div class="sub">${esc(how)}${esc(rate)}${esc(nightly)}</div>` +
    (c.reason ? `<div class="reason">${esc(c.reason)}</div>` : "") +
    (c.excerpt ? `<div class="quote">“${esc(c.excerpt)}”</div>` : "") +
    (c.do ? `<div class="do"><b>Do:</b> ${esc(c.do)}</div>` : "") +
    `<div class="links">${url ? `<a href="${esc(url)}">transcript (rep 1)</a>` : ""}${log ? `<a href="${esc(log)}">build log</a>` : ""}<a href="legacy.html#gate">this case's history</a></div></div>`;
}

function whatToDoHtml(run) {
  const items = [];
  if (run.setup_death || (!measured(run) && run.verdict === "infra")) items.push("<li><b>Retest.</b> " + esc(run.do || "Nothing ran, so nothing here is about your change.") + "</li>");
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
  if (!link.build) return `<div class="sec head"><h1>Which run?</h1><div class="lede">Open this page as <code>run.html#build=&lt;prow build id&gt;</code>; the gate comment on a PR links here. <a href="index.html">Back to the brief →</a></div></div>` + footHtml();
  const run = runs().find((r) => String(r.build) === link.build);
  if (!run) return `<div class="sec head"><h1>No run with that id in the last ${esc(brief.run_days ?? "?")} days.</h1><div class="lede">Build <code>${esc(link.build)}</code> is not in the data behind this page: older than its window, still running, or never uploaded. <a href="index.html">Back to the brief →</a></div></div>` + footHtml();
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

// `scroll` is true for a navigation (boot, a hash change): the page then
// scrolls to the `view=` section it just rendered. The poll re-renders
// with it false, so a reader who has scrolled on is not pulled back.
function renderAll(scroll = false) {
  const link = linkState();
  const app = document.getElementById("app");
  const page = document.body.dataset.page;
  try {
    if (!briefLoaded) throw new Error(`the page's data element (#${PAGE.inlineBrief}) is missing or unreadable`);
    app.innerHTML = page === "run" ? runHtml(link) : briefHtml(link);
  } catch (err) {
    app.innerHTML = `<div class="sec head"><h1>This page could not render.</h1><div class="lede">${esc(String(err && err.message || err))}. <a href="legacy.html">Legacy view →</a></div></div>`;
  }
  const nav = document.getElementById("navrun");
  if (nav) nav.textContent = page === "run" && link.build ? `PR view` : "PR view";
  document.title = page === "run" ? "kube-agents · smoke run" : "kube-agents · smoke gate brief";
  renderFreshness();
  // The section is rendered just above, after the browser looked for the
  // anchor, and a `view=` fragment names no element anyway: scroll by hand.
  if (scroll && link.view) {
    const target = document.getElementById(link.view);
    if (target) target.scrollIntoView();
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
  renderAll();
}

renderAll(true);
// Polling is attempted everywhere, a file:// preview included: a failed
// poll costs nothing but the "regenerated every N min" suffix.
refresh();
setInterval(refresh, PAGE.refreshMs);
window.addEventListener("hashchange", () => renderAll(true));
setInterval(renderFreshness, 30000);
