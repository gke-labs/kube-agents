#!/usr/bin/env python3
"""What Slack added to a night's turns, from the A2A gateway's own metrics.

The Slack lane (hack/slack-lane/run.sh) scrapes each gateway pod's metrics
listener before its canaries and again after them, each pod's text under a
"# pod: <name>" line, and hands both scrapes here. The histograms count from
the pod's start, so what the canaries added is the difference, taken per pod;
a pod that restarted in between (its process_start_time_seconds moved, or a
count went down) counts from zero. A pod with no baseline (its before scrape
failed, or it came up during the run) is dropped and named under no_baseline,
since its after values alone are its lifetime totals. Out comes that run's
Slack overhead:

- delivery_lag_s: the mean of slack_ingress_lag_seconds, Slack's event time to
  the gateway receiving it over Socket Mode;
- post_s: the mean of slack_api_request_duration_seconds for chat.postMessage
  calls that Slack answered (outcome ok);
- first_ack_p50_s and first_ack_p95_s: slack_first_ack_seconds' quantiles, the
  gateway receiving an ask to its arrival reaction landing.

Each is None when the night recorded no sample. estimate() turns an inject
case's agent time and its bot-message count (chat_shape's bot_messages) into
an estimated end-to-end Slack time with them. It assumes the canaries' quiet
CI channel: no rate limiting under load, no contention with other posters.

Usage: overhead.py <after file> [<before file>]   (writes the run's overhead as JSON)
"""

import json
import math
import re
import sys

METRIC_PREFIX = "kubeagents_a2a_gateway_"
INGRESS_LAG = METRIC_PREFIX + "slack_ingress_lag_seconds"
API_DURATION = METRIC_PREFIX + "slack_api_request_duration_seconds"
FIRST_ACK = METRIC_PREFIX + "slack_first_ack_seconds"
POST_METHOD = "chat.postMessage"
OUTCOME_OK = "ok"
QUANTILE_P50 = 0.5
QUANTILE_P95 = 0.95
POD_MARKER = "# pod: "
PROCESS_START = "process_start_time_seconds"
SAMPLE_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _samples(text: str):
    """(name, labels, value) for every sample line of a Prometheus text exposition."""
    for line in text.splitlines():
        match = SAMPLE_LINE.match(line)
        if not match or line.startswith("#"):
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        yield match["name"], dict(LABEL.findall(match["labels"] or "")), value


def _pods(text: str) -> dict[str, str]:
    """Each pod's scrape, split on its "# pod: <name>" line; text with no marker is one pod."""
    pods: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith(POD_MARKER):
            current = line[len(POD_MARKER):].strip()
            continue
        pods.setdefault(current, []).append(line)
    return {pod: "\n".join(lines) for pod, lines in pods.items()}


def _counters(text: str) -> dict[tuple, float]:
    """Every sample of the three histograms, keyed by name and labels."""
    names = {INGRESS_LAG, API_DURATION, FIRST_ACK}
    return {(name, tuple(sorted(labels.items()))): value for name, labels, value in _samples(text)
            if name.rsplit("_", 1)[0] in names}


def _start(text: str) -> float | None:
    return next((value for name, _, value in _samples(text) if name == PROCESS_START), None)


def since(after: str, before: str | None = None) -> tuple[str, list[str]]:
    """after's three histograms less before's, pod by pod, as exposition text, and the pods
    left out for having no baseline. With no before at all, after is kept whole. A pod that
    restarted in between keeps after's values; one whose before scrape carries no
    process_start_time_seconds (the scrape failed, or the pod did not exist yet) is dropped."""
    earlier = _pods(before) if before is not None else {}
    lines, no_baseline = [], []
    for pod, text in _pods(after).items():
        if before is not None and _start(earlier.get(pod, "")) is None:
            if _counters(text):
                no_baseline.append(pod)
            continue
        now, then = _counters(text), _counters(earlier.get(pod, ""))
        restarted = before is None or _start(text) != _start(earlier[pod]) or any(
            then.get(key, 0.0) > value for key, value in now.items())
        for (name, labels), value in now.items():
            delta = value if restarted else value - then.get((name, labels), 0.0)
            label_text = ",".join(f'{k}="{v}"' for k, v in labels)
            lines.append(f"{name}{{{label_text}}} {delta}" if label_text else f"{name} {delta}")
    return "\n".join(lines), sorted(no_baseline)


def _histogram(text: str, name: str, **want: str) -> tuple[float, float, list[tuple[float, float]]]:
    """Sum, count and cumulative buckets of one histogram, summed over every series (and every
    scraped pod) whose labels include want."""
    total = count = 0.0
    buckets: dict[float, float] = {}
    for sample, labels, value in _samples(text):
        if any(labels.get(k) != v for k, v in want.items()):
            continue
        if sample == name + "_sum":
            total += value
        elif sample == name + "_count":
            count += value
        elif sample == name + "_bucket" and "le" in labels:
            bound = math.inf if labels["le"] == "+Inf" else float(labels["le"])
            buckets[bound] = buckets.get(bound, 0.0) + value
    return total, count, sorted(buckets.items())


def _mean(total: float, count: float) -> float | None:
    return round(total / count, 3) if count else None


def _quantile(q: float, buckets: list[tuple[float, float]]) -> float | None:
    """histogram_quantile's linear interpolation inside the bucket holding rank q."""
    if not buckets or buckets[-1][1] <= 0:
        return None
    rank = q * buckets[-1][1]
    lower_bound, lower_count = 0.0, 0.0
    for bound, cumulative in buckets:
        if cumulative >= rank:
            if math.isinf(bound):
                return round(lower_bound, 3)
            share = (rank - lower_count) / (cumulative - lower_count) if cumulative > lower_count else 1.0
            return round(lower_bound + (bound - lower_bound) * share, 3)
        lower_bound, lower_count = bound, cumulative
    return None


def overhead(text: str) -> dict:
    """The Slack overhead in one or more pods' metrics text (since()'s difference, or a scrape)."""
    lag_sum, lag_count, _ = _histogram(text, INGRESS_LAG)
    post_sum, post_count, _ = _histogram(text, API_DURATION, method=POST_METHOD, outcome=OUTCOME_OK)
    _, ack_count, ack_buckets = _histogram(text, FIRST_ACK)
    return {
        "delivery_lag_s": _mean(lag_sum, lag_count),
        "post_s": _mean(post_sum, post_count),
        "first_ack_p50_s": _quantile(QUANTILE_P50, ack_buckets),
        "first_ack_p95_s": _quantile(QUANTILE_P95, ack_buckets),
        "samples": {"delivery_lag": int(lag_count), "post": int(post_count), "first_ack": int(ack_count)},
    }


def estimate(agent_s: float | None, bot_messages: int | None, night: dict) -> float | None:
    """An inject case's estimated end-to-end Slack time: its agent time, plus one delivery lag,
    plus a post's time for each message the bot posted. None when any term is missing."""
    lag, post = night.get("delivery_lag_s"), night.get("post_s")
    if agent_s is None or bot_messages is None or lag is None or post is None:
        return None
    return round(agent_s + lag + bot_messages * post, 3)


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    texts = []
    for path in argv[1:]:
        with open(path, encoding="utf-8") as fh:
            texts.append(fh.read())
    text, no_baseline = since(*texts)
    print(json.dumps({**overhead(text), "no_baseline": no_baseline}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
