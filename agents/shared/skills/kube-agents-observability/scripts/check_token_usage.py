import argparse
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone

def parse_duration(duration_str):
    if not duration_str:
        raise argparse.ArgumentTypeError("Duration string cannot be empty")
    unit = duration_str[-1].lower()
    try:
        val = int(duration_str[:-1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid duration value: {duration_str}")
        
    if unit == 'h':
        return timedelta(hours=val)
    elif unit == 'd':
        return timedelta(days=val)
    elif unit == 'm':
        return timedelta(minutes=val)
    elif unit == 's':
        return timedelta(seconds=val)
    else:
        raise argparse.ArgumentTypeError(f"Invalid duration unit: '{unit}' (must be s, m, h, or d)")


# Parse arguments
parser = argparse.ArgumentParser(description="Query token usage delta for GKE Managed Service for Prometheus metrics")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
parser.add_argument("--duration", type=parse_duration, default="24h", help="Time window duration (e.g. 24h, 6h, 1d) (default: 24h)")
args = parser.parse_args()

project_id = args.project_id

# Calculate time range
end_time = datetime.now(timezone.utc)
start_time = end_time - args.duration
end_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')
start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')
duration_seconds = int((end_time - start_time).total_seconds())

try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)


def get_token_delta(metric_name, group_by_field=None):
    # Filter for the specific metric
    filter_str = f'metric.type="{metric_name}"'
    params = {
        "filter": filter_str,
        "interval.startTime": start_str,
        "interval.endTime": end_str,
        "aggregation.alignmentPeriod": f"{duration_seconds}s",
        "aggregation.perSeriesAligner": "ALIGN_DELTA",
        "aggregation.crossSeriesReducer": "REDUCE_SUM"
    }
    if group_by_field:
        params["aggregation.groupByFields"] = f"metric.label.{group_by_field}"

    query_string = urllib.parse.urlencode(params)
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?{query_string}"

    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/json')
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} querying metric {metric_name}: {e.read().decode('utf-8')}")
        return {} if group_by_field else 0
    except urllib.error.URLError as e:
        print(f"Failed to connect to Monitoring API for metric {metric_name}: {e.reason}")
        return {} if group_by_field else 0

    def parse_value_obj(val_obj):
        val = val_obj.get('doubleValue')
        if val is None:
            val_str = val_obj.get('int64Value', '0')
            try:
                val = int(val_str)
            except ValueError:
                val = 0
        return val

    if group_by_field:
        # Group by the specified label field and return a dictionary of deltas
        deltas = {}
        for ts in data.get('timeSeries', []):
            label_val = ts.get('metric', {}).get('labels', {}).get(group_by_field, 'unknown')
            points = ts.get('points', [])
            if points:
                # We align to the full 24h window so there is exactly 1 aggregated point
                deltas[label_val] = parse_value_obj(points[0].get('value') or {})
        return deltas
    else:
        # Return a single aggregated delta value
        ts_list = data.get('timeSeries', [])
        if not ts_list:
            return 0
        points = ts_list[0].get('points', [])
        if not points:
            return 0
        return parse_value_obj(points[0].get('value') or {})


# Fetch the specific metrics
litellm_input = get_token_delta("prometheus.googleapis.com/litellm_input_tokens_metric_total/counter")
litellm_output = get_token_delta("prometheus.googleapis.com/litellm_output_tokens_metric_total/counter")
litellm_cached_input = get_token_delta("prometheus.googleapis.com/litellm_input_cached_tokens_metric_total/counter")

hermes_deltas = get_token_delta("prometheus.googleapis.com/hermes.token.usage/counter", group_by_field="token_type")

# Map Hermes keys to matching LiteLLM labels for comparison
hermes_input = hermes_deltas.get("input", 0)
hermes_output = hermes_deltas.get("output", 0)
hermes_cached_input = hermes_deltas.get("cacheRead", 0)

comparison = {
    "litellm": {
        "input_tokens": litellm_input,
        "output_tokens": litellm_output,
        "cached_input_tokens": litellm_cached_input
    },
    "hermes": {
        "input_tokens": hermes_input,
        "output_tokens": hermes_output,
        "cached_input_tokens": hermes_cached_input
    }
}

# Include any other unexpected hermes token types, if present
for k, v in hermes_deltas.items():
    if k not in ("input", "output", "cacheRead"):
        comparison["hermes"][f"raw_{k}_tokens"] = v

print(json.dumps(comparison, indent=2))


