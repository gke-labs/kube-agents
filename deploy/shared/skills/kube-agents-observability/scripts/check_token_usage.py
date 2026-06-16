import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone

# Parse arguments
parser = argparse.ArgumentParser(description="Query token usage delta for GKE Managed Service for Prometheus metrics")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
args = parser.parse_args()

project_id = args.project_id

# Calculate time range
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=24)
end_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')
start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()


def get_token_delta(metric_name):
    # Filter for the specific metric
    filter_str = f'metric.type="{metric_name}"'
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?filter={filter_str}&interval.startTime={start_str}&interval.endTime={end_str}"
    
    # Construct curl command
    cmd = [
        'curl', '-s',
        '-H', f'Authorization: Bearer {token}',
        url
    ]
    
    output = subprocess.check_output(cmd).decode()
    data = json.loads(output)
    
    # Calculate the delta across all time series (pods)
    total_delta = 0
    for ts in data.get('timeSeries', []):
        points = ts.get('points', [])
        if len(points) >= 2:
            # Sort by time to get absolute delta from start to end of 24h
            points.sort(key=lambda x: x['interval']['endTime'])
            start_val = points[0].get('value', {}).get('doubleValue', 0)
            end_val = points[-1].get('value', {}).get('doubleValue', 0)
            delta = end_val - start_val
            # Handle potential counter resets
            if delta >= 0:
                total_delta += delta
            else:
                total_delta += end_val
    return total_delta

# Fetch the specific metrics
input_tokens = get_token_delta("prometheus.googleapis.com/litellm_input_tokens_metric_total/counter")
output_tokens = get_token_delta("prometheus.googleapis.com/litellm_output_tokens_metric_total/counter")
cached_input_tokens = get_token_delta("prometheus.googleapis.com/litellm_input_cached_tokens_metric_total/counter")

print(json.dumps({
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "cached_input_tokens": cached_input_tokens
}, indent=2))
