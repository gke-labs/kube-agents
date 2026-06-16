import argparse
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

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

try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)


def get_token_delta(metric_name):
    # Filter for the specific metric
    filter_str = f'metric.type="{metric_name}"'
    params = {
        "filter": filter_str,
        "interval.startTime": start_str,
        "interval.endTime": end_str
    }
    query_string = urllib.parse.urlencode(params)
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries?{query_string}"

    
    # Construct urllib Request
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/json')
    
    # Execute request and load response
    try:
        # Set a 10s timeout to prevent hanging indefinitely
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} querying metric {metric_name}: {e.read().decode('utf-8')}")
        return 0
    except urllib.error.URLError as e:
        print(f"Failed to connect to Monitoring API for metric {metric_name}: {e.reason}")
        return 0
    
    # Helper to parse point value supporting float and integer formats
    def parse_value(pt):
        val_obj = pt.get('value') or {}
        val = val_obj.get('doubleValue')
        if val is None:
            # Fallback to int64Value which is returned as string in REST API
            val_str = val_obj.get('int64Value', '0')

            try:
                val = int(val_str)
            except ValueError:
                val = 0
        return val

    # Calculate the delta across all time series (pods)
    total_delta = 0
    for ts in data.get('timeSeries', []):
        points = ts.get('points', [])
        if not points:
            continue
            
        if len(points) >= 2:

            # Sort by time ascending with safety fallback if endTime is missing
            try:
                points.sort(key=lambda x: (x.get('interval') or {}).get('endTime', ''))
            except (AttributeError, KeyError):
                pass

                
            ts_delta = 0
            prev_val = None
            for pt in points:
                val = parse_value(pt)
                if prev_val is not None:
                    diff = val - prev_val
                    if diff >= 0:
                        ts_delta += diff
                    else:
                        # Counter reset (prev_val -> 0 -> val)
                        ts_delta += val
                prev_val = val
            total_delta += ts_delta
            
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
