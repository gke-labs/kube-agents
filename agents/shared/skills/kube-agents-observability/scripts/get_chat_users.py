import argparse
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Parse arguments
parser = argparse.ArgumentParser(description="List users and message counts who interacted with the system via chat in the last 24 hours")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
parser.add_argument("--hours", type=int, default=24, help="Time window in hours (default: 24)")
args = parser.parse_args()

project_id = args.project_id
hours = args.hours

# Calculate time range
start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

# Retrieve access token
try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)

# Cloud Logging API endpoint
url = "https://logging.googleapis.com/v2/entries:list"

# Request payload
filter_query = f'resource.type="k8s_container" "Logging incoming GChat event" timestamp >= "{start_str}"'
payload = {
    "resourceNames": [f"projects/{project_id}"],
    "filter": filter_query,
    "orderBy": "timestamp desc",
    "pageSize": 1000
}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode('utf-8'),
    headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    },
    method='POST'
)

try:
    # Set a 10s timeout to prevent hanging indefinitely
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code} querying Cloud Logging: {e.read().decode('utf-8')}")
    exit(1)
except urllib.error.URLError as e:
    print(f"Failed to connect to Cloud Logging API: {e.reason}")
    exit(1)

# Parse emails and count messages from the log payloads
email_pattern = re.compile(r'User=([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')
user_counts = {}

for entry in result.get('entries', []):
    text = entry.get('textPayload', '')
    json_payload = entry.get('jsonPayload')
    if not text and json_payload:
        if isinstance(json_payload, dict):
            text = json_payload.get('log', '')
            if not text:
                text = json.dumps(json_payload)
        else:
            text = str(json_payload)
            
    if not isinstance(text, str):
        text = str(text)
    match = email_pattern.search(text)
    if match:
        email = match.group(1)
        user_counts[email] = user_counts.get(email, 0) + 1


# Sort user counts by email address
sorted_user_counts = {k: user_counts[k] for k in sorted(user_counts.keys())}

print(json.dumps({
    "active_chat_users": sorted_user_counts,
    "time_window_hours": hours,
    "query_start_time": start_str
}, indent=2))
