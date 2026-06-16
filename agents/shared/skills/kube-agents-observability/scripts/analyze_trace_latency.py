import argparse
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone

# Parse arguments
parser = argparse.ArgumentParser(description="Analyze trace latencies to locate bottlenecks (e.g. slow tool or model calls)")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
parser.add_argument("--hours", type=int, default=24, help="Analyze traces within the last N hours (default: 24)")
parser.add_argument("--limit", type=int, default=5, help="Number of traces to fetch and analyze (default: 5)")
args = parser.parse_args()

project_id = args.project_id
hours = args.hours
limit = args.limit

# Calculate time range
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=hours)
end_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')
start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

# Retrieve active access token
try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)

# Helper function to query a URL
def fetch_api(url, method="GET", payload=None):
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/json')
    if payload:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(payload).encode('utf-8')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code} for URL {url}: {e.read().decode('utf-8')}")
        return None
    except urllib.error.URLError as e:
        print(f"Connection failure for URL {url}: {e.reason}")
        return None

# Step 1: List traces
params = {
    "startTime": start_str,
    "endTime": end_str,
    "pageSize": limit
}
query_string = urllib.parse.urlencode(params)
list_url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces?{query_string}"
print(f"Retrieving the last {limit} traces...")
list_data = fetch_api(list_url)


if not list_data or not list_data.get('traces'):
    print("No traces found in the specified window.")
    exit(0)

# Helper to parse trace timestamps supporting nanosecond precision and fallback timezones
def parse_timestamp(ts_str):
    if not ts_str:
        return datetime.now(timezone.utc)
    ts_str = ts_str.replace('Z', '+00:00')
    if '.' in ts_str:
        base, fraction_tz = ts_str.split('.', 1)

        # Extract timezone offset to truncate nanosecond precision to microsecond (6 digits) safely
        tz_idx = -1
        for i, char in enumerate(fraction_tz):
            if char in ('+', '-'):
                tz_idx = i
                break
        if tz_idx != -1:
            fraction = fraction_tz[:tz_idx]
            tz_offset = fraction_tz[tz_idx:]
            fraction = fraction[:6]
            ts_str = f"{base}.{fraction}{tz_offset}"
        else:
            fraction = fraction_tz[:6]
            ts_str = f"{base}.{fraction}"
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        try:
            return datetime.strptime(ts_str.split('.', 1)[0], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)



# Step 2: Query and analyze each trace
for trace in list_data.get('traces', []):
    trace_id = trace.get('traceId')
    detail_url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces/{trace_id}"
    detail = fetch_api(detail_url)
    
    if not detail or not detail.get('spans'):
        continue
        
    spans = detail.get('spans', [])
    print("=" * 70)
    print(f"Trace ID: {trace_id}")
    
    # Calculate trace-level details
    trace_start = None
    trace_end = None
    span_durations = []
    
    for span in spans:
        start_t_str = span.get('startTime')
        end_t_str = span.get('endTime')
        if not start_t_str or not end_t_str:
            continue
        try:
            start_t = parse_timestamp(start_t_str)
            end_t = parse_timestamp(end_t_str)
        except Exception:
            continue
            
        duration = (end_t - start_t).total_seconds()
        span_durations.append((span.get('name', 'unknown'), duration))
        
        if trace_start is None or start_t < trace_start:
            trace_start = start_t
        if trace_end is None or end_t > trace_end:
            trace_end = end_t
            
    total_duration = (trace_end - trace_start).total_seconds() if trace_start and trace_end else 0
    print(f"Total Duration: {total_duration:.3f} seconds | Total Spans: {len(spans)}")
    print("Breakdown of spans:")
    
    # Sort spans by duration descending to list bottlenecks first
    span_durations.sort(key=lambda x: x[1], reverse=True)
    for name, dur in span_durations[:10]: # Print top 10 bottleneck spans
        pct = (dur / total_duration) * 100 if total_duration > 0 else 0
        print(f"  - {name:50} : {dur:6.3f}s ({pct:4.1f}%)")
    if len(span_durations) > 10:
        print(f"  ... and {len(span_durations) - 10} more spans.")
