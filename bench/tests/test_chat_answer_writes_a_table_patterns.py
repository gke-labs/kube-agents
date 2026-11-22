"""chat-answer-writes-a-table tells a pipe table from pipe-led prose.

The case's objective requires a GFM delimiter row. An earlier spelling of that
pattern used ``\\s``, which crosses a newline in Python ``re``, and checked
nothing past the first pipe, so a ``---`` rule above pipe-led lines and a line
opening with a dash cell both passed as tables. These fixtures run the whole
objective through ``check_passes`` with both required phrases present, so an
answer passes or fails on the table alone. Every fixture is made up; no run's
report text is committed (.agents/rules/public_surfaces.md).
"""

from __future__ import annotations

import pytest
from conftest import check_passes

CASE = "chat-answer-writes-a-table"
CHECK = "the-answer-carries-a-markdown-table-with-the-agents-own-namespace"

TABLES = {
    "bordered": "| Namespace | Status |\n|---|---|\n| kubeagents-system | Active |\n| kube-system | Active |",
    "borderless": "Namespace | Status\n--- | ---\nkubeagents-system | Active\nkube-system | Active",
    "one-dash": "| Namespace | Status |\n| - | - |\n| kubeagents-system | Active |\n| kube-system | Active |",
    "colon-aligned": "| Namespace | Status |\n|:--|--:|\n| kubeagents-system | Active |\n| kube-system | Active |",
}
NOT_TABLES = {
    "label-value lines": "Namespace: kubeagents-system | Status: Active\nNamespace: kube-system | Status: Active",
    "rule above pipe-led lines": "---\n| Namespace: kubeagents-system | Status: Active |\n| Namespace: kube-system | Status: Active |",
    "dash cell then prose": "- | Namespace: kubeagents-system | Status: Active\n- | Namespace: kube-system | Status: Active",
    "bordered dash cell then prose": "| - | Namespace: kubeagents-system | Status: Active |\n| - | Namespace: kube-system | Status: Active |",
}


@pytest.mark.parametrize("shape", sorted(TABLES))
def test_a_pipe_table_in_any_gfm_spelling_passes(shape):
    assert check_passes(CASE, CHECK, TABLES[shape])


@pytest.mark.parametrize("shape", sorted(NOT_TABLES))
def test_pipe_led_prose_without_a_delimiter_row_fails(shape):
    assert not check_passes(CASE, CHECK, NOT_TABLES[shape])
