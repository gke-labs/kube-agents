import argparse
import json
import subprocess
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
token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()


# URL for the Cloud Trace API v1 (list traces)
url = f"https://cloudtrace.googleapis.com/v1/projects/{project_id}/traces?startTime={start_str}&endTime={end_str}&pageSize=10"

# Execute request using curl
cmd = [
    'curl', '-s', '-X', 'GET',
    '-H', f'Authorization: Bearer {token}',
    url
]

output = subprocess.check_output(cmd).decode()
data = json.loads(output)

print(json.dumps(data, indent=2))
