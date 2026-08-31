"""Eval dashboard: collect Prow smoke-test runs into data.json and render it.

The collector (`collect.py`) writes one data.json; its schema is a contract
shared with the renderer and the publisher -- see SCHEMA.md in this directory
before changing any field. `render.py` turns one data.json (schema_version 1)
into `out/index.html` plus a copy of the data file; `publish.py` ships an
out-dir to its serving location. Everything on the page is computed from
data.json alone -- the one optional extra input is `case-notes.yaml`, which
carries human one-line annotations and issue links per case.
"""
