-- Quality-over-time dashboard over the eval baseline store.
--
-- The store is already the right shape for this: append-only, timestamped and
-- dimension-tagged, so there is no ETL job and no second copy. BigQuery reads
-- the same GCS objects `bench-gate` writes, and a new batch is queryable as
-- soon as it lands.
--
-- Run once to create the dataset, external table and views, substituting the
-- project and bucket. Everything below has been executed against a real bucket.
--
--   PROJECT=kube-agents-evals
--   BUCKET=kube-agents-evals-bench
--   bq --project_id=$PROJECT mk --dataset --location=us-central1 $PROJECT:eval_baselines
--   bq --project_id=$PROJECT mk --table \
--     --external_table_definition=./external-table.json \
--     $PROJECT:eval_baselines.evidence
--   bq --project_id=$PROJECT query --use_legacy_sql=false < ./dashboard.sql
--
-- The external table definition is a sibling file, `external-table.json`, and
-- it declares an EXPLICIT SCHEMA on purpose. Do not use `"autodetect": true`:
-- `blocked` and `infra` are omitted when zero and a judged metric is absent
-- when the run did not produce it, so autodetect infers the schema from
-- whatever happens to be in the sample and silently leaves out every field that
-- was not. A query for `blocked` then fails with "Unrecognized name" rather
-- than returning zero, and a metric added later is unqueryable until the table
-- is recreated. The record format's absent-never-zero rule is deliberate; the
-- schema has to be the thing that knows the full shape.

-- One flat row per batch, with the version key rendered as a single label.
-- Everything else selects from here.
CREATE OR REPLACE VIEW `eval_baselines.evidence_flat` AS
SELECT
  `case`                                          AS case_id,
  recorded_at,
  DATE_TRUNC(DATE(recorded_at), WEEK)             AS week,
  commit,
  key.setup_id, key.judge_model, key.scoring_version, key.fleet, key.verifiers,
  FORMAT('%s | %s | %s-f%d-v%d', key.setup_id, key.judge_model,
         key.scoring_version, key.fleet, key.verifiers) AS version_key,
  runs,
  passes,
  COALESCE(blocked, 0)                            AS blocked,
  COALESCE(infra, 0)                              AS infra,
  judged.OutcomeValidity.mean                     AS outcome_validity_mean,
  judged.OutcomeValidity.n                        AS outcome_validity_n
FROM `eval_baselines.evidence`;

-- 1. Pass rate over time. Is the agent getting better or worse?
--    Break every chart on `version_key`: a series plotted across a model bump
--    is two experiments drawn as one line, and it will be misread as a
--    regression. This is the single most important property of the dashboard.
CREATE OR REPLACE VIEW `eval_baselines.pass_rate_weekly` AS
SELECT case_id, week, version_key,
       SUM(passes) AS passes, SUM(runs) AS runs,
       SAFE_DIVIDE(SUM(passes), SUM(runs)) AS pass_rate
FROM `eval_baselines.evidence_flat`
GROUP BY case_id, week, version_key;

-- 2. Judged quality over time. This is where DRIFT lives.
--    A weekly pooled mean has a small enough standard error to show a 0.05
--    slide that rung 6's three-repetition margin of 0.5 will never catch, so
--    the dashboard is not a nicety -- it is where drift detection actually
--    happens, with rung 6 as the collapse alarm underneath it. Weighted by each
--    batch's own n, so a 20-run screening campaign outweighs a 3-run postsubmit.
CREATE OR REPLACE VIEW `eval_baselines.judged_weekly` AS
SELECT case_id, week, version_key,
       SUM(outcome_validity_mean * outcome_validity_n)
         / NULLIF(SUM(outcome_validity_n), 0) AS outcome_validity,
       SUM(outcome_validity_n)                AS n
FROM `eval_baselines.evidence_flat`
WHERE outcome_validity_mean IS NOT NULL
GROUP BY case_id, week, version_key;

-- 3. Flake rate. Unreliable, or broken?
--    A case with many partial batches and no total failures is flaky, which the
--    ladder deliberately tolerates; a case with total failures is broken. The
--    two want different responses and look identical in a pass rate alone.
CREATE OR REPLACE VIEW `eval_baselines.flakiness` AS
SELECT case_id, version_key,
       COUNTIF(passes > 0 AND passes < runs) AS partial_batches,
       COUNTIF(runs > 0 AND passes = 0)      AS total_failures,
       COUNTIF(runs > 0 AND passes = runs)   AS clean_batches,
       SAFE_DIVIDE(COUNTIF(passes > 0 AND passes < runs),
                   COUNTIF(runs > 0))        AS flake_rate
FROM `eval_baselines.evidence_flat`
GROUP BY case_id, version_key;

-- 4. Infra health. Is the harness the real problem?
--    blocked and infra stay out of the pass rate but are kept on the line, so
--    a case that crashes half the time cannot look perfectly reliable here.
CREATE OR REPLACE VIEW `eval_baselines.infra_health` AS
SELECT case_id, week,
       SUM(runs) AS scored, SUM(blocked) AS blocked, SUM(infra) AS infra,
       SAFE_DIVIDE(SUM(blocked) + SUM(infra),
                   SUM(runs) + SUM(blocked) + SUM(infra)) AS lost_fraction
FROM `eval_baselines.evidence_flat`
GROUP BY case_id, week;

-- 5. Admission state. Which cases can actually block a pull request, and since
--    when? This mirrors what `baselines.py` computes at gate time: pool the
--    newest batches AT ONE KEY newest-first until the run bar is met. A version
--    bump correctly shows everything falling back to unadmitted until it is
--    re-screened, which is the behaviour most likely to be mistaken for a bug.
CREATE OR REPLACE VIEW `eval_baselines.admission_state` AS
WITH ranked AS (
  SELECT case_id, version_key, recorded_at, runs, passes,
         SUM(runs) OVER (PARTITION BY case_id, version_key
                         ORDER BY recorded_at DESC
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_runs
  FROM `eval_baselines.evidence_flat`
),
-- Whole batches only, overshooting the bar rather than trimming one: a trimmed
-- batch would invent a sub-record nobody measured.
window_ AS (
  SELECT * FROM ranked
  WHERE cum_runs - runs < 20
)
SELECT case_id, version_key,
       SUM(runs)   AS runs_in_window,
       SUM(passes) AS passes_in_window,
       SAFE_DIVIDE(SUM(passes), SUM(runs)) AS rate,
       MAX(recorded_at) AS newest_evidence,
       SUM(runs) >= 20
         AND SAFE_DIVIDE(SUM(passes), SUM(runs)) >= 0.95 AS admitted
FROM window_
GROUP BY case_id, version_key;

-- 6. Drift under green. The case this whole file exists for: a case passing
--    every run while its judged quality slides. The gate sees nothing; this
--    view is the only place it is visible.
CREATE OR REPLACE VIEW `eval_baselines.drift_under_green` AS
SELECT p.case_id, p.version_key,
       MIN(p.week) AS first_week, MAX(p.week) AS last_week,
       SUM(p.passes) AS passes, SUM(p.runs) AS runs,
       SAFE_DIVIDE(SUM(p.passes), SUM(p.runs)) AS pass_rate,
       -- oldest and newest weekly judged mean at this key
       ARRAY_AGG(j.outcome_validity ORDER BY j.week ASC  LIMIT 1)[SAFE_OFFSET(0)] AS validity_first,
       ARRAY_AGG(j.outcome_validity ORDER BY j.week DESC LIMIT 1)[SAFE_OFFSET(0)] AS validity_last
FROM `eval_baselines.pass_rate_weekly` p
JOIN `eval_baselines.judged_weekly` j
  USING (case_id, week, version_key)
GROUP BY p.case_id, p.version_key
HAVING pass_rate = 1.0 AND validity_last < validity_first;
