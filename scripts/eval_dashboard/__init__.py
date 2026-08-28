"""Eval dashboard: render a static page from the collector's data.json.

`render.py` turns one data.json (schema_version 1, produced by the results
collector) into `out/index.html` plus a copy of the data file; `publish.py`
ships an out-dir to its serving location. Everything on the page is computed
from data.json alone -- the one optional extra input is `case-notes.yaml`,
which carries human one-line annotations and issue links per case.
"""
