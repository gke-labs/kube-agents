"""How much of the proxy's traffic carries an eval run tag, from its JSON lines.

Reads the request lines replay_proxy.py writes (any mode) and reports the
share of request lines, and of prompt tokens, that carry `eval_run`
(X-Kube-Agents-Eval-Run, set by the agent's eval_run_tag plugin; see
deploy/docker/patches/eval_run_tag.py), overall and by group:

- by agent: the profile in `eval_agent` (`<profile>;<source>`), or
  `(no agent header)` for a request the plugin never saw (an auxiliary call,
  a process without the plugin, an install with tagging off);
- by tag source: `card`, `session` or `none` from `eval_agent`;
- by model group (`litellm_model_group`, else the request's `model`).

Untagged lines are listed by agent and source, so a gap reads as "the
platform worker found no tag" or "no plugin ran", not just a percentage. A
line whose analysis was lost has no prompt tokens and counts as a request
only. Per run, the case/repetition pairs seen are listed with their line and
token counts.

Usage: python3 tag_coverage.py LOG [LOG ...] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Iterable, List, Optional

import schema as F

PERCENT = 100.0
PERCENT_DECIMALS = 1
NO_AGENT = "(no agent header)"
NO_MODEL = "(unknown)"
AGENT_SEPARATOR = ";"
TAG_SEPARATOR = "/"
TAG_PARTS = 3


def load(paths: Iterable[str]) -> List[Dict[str, Any]]:
    """Every request line in `paths`; other lines and unparsable ones are skipped."""
    lines = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get(F.EVENT) == F.REQUEST_EVENT:
                    lines.append(entry)
    return lines


def _tokens(entry: Dict[str, Any]) -> int:
    value = entry.get(F.PROMPT_TOKENS)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _agent(entry: Dict[str, Any]) -> tuple[str, str]:
    value = entry.get(F.EVAL_AGENT)
    if not isinstance(value, str) or AGENT_SEPARATOR not in value:
        return NO_AGENT, NO_AGENT
    profile, source = value.split(AGENT_SEPARATOR, 1)
    return profile, source


def _model(entry: Dict[str, Any]) -> str:
    return str(entry.get(F.LITELLM_MODEL_GROUP) or entry.get(F.MODEL) or NO_MODEL)


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def _bucket() -> Dict[str, Any]:
    return {"requests": 0, "tagged_requests": 0, "prompt_tokens": 0, "tagged_prompt_tokens": 0}


def _add(bucket: Dict[str, Any], tagged: bool, tokens: int) -> None:
    bucket["requests"] += 1
    bucket["prompt_tokens"] += tokens
    if tagged:
        bucket["tagged_requests"] += 1
        bucket["tagged_prompt_tokens"] += tokens


def _finish(bucket: Dict[str, Any]) -> Dict[str, Any]:
    bucket["tagged_request_pct"] = percent(bucket["tagged_requests"], bucket["requests"])
    bucket["tagged_token_pct"] = percent(bucket["tagged_prompt_tokens"], bucket["prompt_tokens"])
    return bucket


def summarize(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    overall = _bucket()
    groups: Dict[str, Dict[str, Dict[str, Any]]] = {"by_agent": {}, "by_source": {}, "by_model_group": {}}
    untagged: Dict[str, Dict[str, int]] = {}
    runs: Dict[str, Dict[str, Dict[str, int]]] = {}
    for entry in lines:
        tag = entry.get(F.EVAL_RUN)
        tagged = isinstance(tag, str) and bool(tag)
        tokens = _tokens(entry)
        profile, source = _agent(entry)
        _add(overall, tagged, tokens)
        for name, key in (("by_agent", profile), ("by_source", source), ("by_model_group", _model(entry))):
            _add(groups[name].setdefault(key, _bucket()), tagged, tokens)
        if tagged:
            parts = tag.split(TAG_SEPARATOR, TAG_PARTS - 1)
            if len(parts) == TAG_PARTS:
                run, case, rep = parts
                cell = runs.setdefault(run, {}).setdefault(f"{case}/{rep}", {"requests": 0, "prompt_tokens": 0})
                cell["requests"] += 1
                cell["prompt_tokens"] += tokens
        else:
            label = profile if profile == NO_AGENT else f"{profile}{AGENT_SEPARATOR}{source}"
            cell = untagged.setdefault(label, {"requests": 0, "prompt_tokens": 0})
            cell["requests"] += 1
            cell["prompt_tokens"] += tokens
    report: Dict[str, Any] = {"overall": _finish(overall)}
    for name, buckets in groups.items():
        report[name] = {key: _finish(bucket) for key, bucket in sorted(buckets.items())}
    report["untagged_by_agent"] = dict(sorted(untagged.items(), key=lambda kv: -kv[1]["prompt_tokens"]))
    report["runs"] = {run: dict(sorted(cases.items())) for run, cases in sorted(runs.items())}
    return report


def render(report: Dict[str, Any]) -> str:
    def row(key: str, b: Dict[str, Any]) -> str:
        return (f"  {key}: {b['tagged_requests']}/{b['requests']} requests ({b['tagged_request_pct']}%), "
                f"{b['tagged_prompt_tokens']}/{b['prompt_tokens']} prompt tokens ({b['tagged_token_pct']}%)")

    out = [row("overall", report["overall"]).strip()]
    for name in ("by_agent", "by_source", "by_model_group"):
        out.append(f"{name.replace('_', ' ')}:")
        out += [row(key, bucket) for key, bucket in report[name].items()]
    out.append("untagged, by agent (requests, prompt tokens):")
    out += [f"  {label}: {c['requests']}, {c['prompt_tokens']}" for label, c in report["untagged_by_agent"].items()]
    out.append("runs (case/repetition: requests, prompt tokens):")
    for run, cases in report["runs"].items():
        out.append(f"  {run}")
        out += [f"    {case}: {c['requests']}, {c['prompt_tokens']}" for case, c in cases.items()]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="+", help="JSONL files written by replay_proxy.py")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = summarize(load(args.logs))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
