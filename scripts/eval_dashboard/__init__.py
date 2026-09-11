"""Eval dashboard: collect Prow smoke-test runs into data.json and render it.

The collector (`collect.py`) writes one data.json; its schema is a contract
shared with the renderer and the publisher -- see SCHEMA.md in this directory
before changing any field. `render.py` turns one data.json (schema_version 1)
into five pages -- `index.html` (the incident Brief), `run.html` (the per-run
PR view), `grid.html` (every case by every run, with the merges and
incidents marked), `cases.html` (how reliable each test is) and
`nightly.html` (last night's run of the nightly tier, `nightly.py`) -- plus
`brief.json`, the document they all render from: the per-run classification
`classify.py` produces (the one place the "is this red mine?" rule lives)
and the per-case record; and a copy of the data file. `publish.py` ships an
out-dir to its serving location. Everything measurable on the pages is
computed from data.json alone -- the optional extra inputs are
`case-notes.yaml` (a human one-line note and issue links per case),
`events.yaml` (the human-classified catch counts), and the CI health
adjudicator's `health.json` / `health-history.jsonl` when published;
render.py's docstring owns the details.

Two more readers of the same data.json live here: `health.py` decides
whether the presubmit gate is GREEN / DEGRADED / OUTAGE and why, and
`post_health.py` tells a Google Chat space when that changes, with one line
on last night's nightly run in its daily digest.
docs/ci-health.md is the page for both.
"""
