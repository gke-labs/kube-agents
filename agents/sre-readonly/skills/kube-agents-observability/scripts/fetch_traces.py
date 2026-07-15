import argparse
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timedelta, timezone

parser = argparse.ArgumentParser(description="Fetch traces from Cloud Trace API")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
parser.add_argument("--hours", type=int, default=24, help="Retrieve traces for the last N hours (default: 24)")
args = parser.parse_args()

project_id = args.project_id
hours = args.hours

# Calculate time range
end_time = datetime.now(timezone.utc)
start_time = end_time - timedelta(hours=hours)
end_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')
start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

# Get active auth token from gcloud
try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)

# URL for the Cloud Trace API v1 (list traces)
params = {
    "startTime": start_str,
    "endTime": end_str,
    "pageSize": 10
}
query_string = urllib.parse.urlencode(params)
url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces?{query_string}"


# Construct urllib Request
req = urllib.request.Request(url)
req.add_header('Authorization', f'Bearer {token}')
req.add_header('Accept', 'application/json')

# Execute request and load response
try:
    # Set 10s timeout to prevent hanging
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code} querying trace API: {e.read().decode('utf-8')}")
    exit(1)
except urllib.error.URLError as e:
    print(f"Failed to connect to Trace API: {e.reason}")
    exit(1)

print(json.dumps(data, indent=2))
