"""Eval dashboard: collect Prow smoke-test runs into data.json and render it.

The collector (`collect.py`) writes one data.json; its schema is a contract
shared with the renderer and the publisher -- see SCHEMA.md in this directory
before changing any field. `render.py` turns one data.json (schema_version 1)
into three pages -- `index.html` (the incident Brief), `run.html` (the per-run
PR view) and `legacy.html` (the two-band table) -- plus `brief.json`, the
per-run classification `classify.py` produces (the one place the "is this
red mine?" rule lives), and a copy of the data file; `publish.py` ships an out-dir to its serving
location. Everything measurable on the pages is computed from data.json
alone -- the optional extra inputs are `case-notes.yaml` (human one-line
annotations, issue links and badges per case), `events.yaml` (dated event
markers plus the human-classified catch and false-red counts), and the CI
health adjudicator's `health.json` / `health-history.jsonl` when published; render.py's docstring
owns the details.

Two more readers of the same data.json live here: `health.py` decides
whether the presubmit gate is GREEN / DEGRADED / OUTAGE and why, and
`post_health.py` tells a Google Chat space when that changes.
docs/ci-health.md is the page for both.
"""
