import argparse
import json
import subprocess
import urllib.error
import urllib.request

# Parse arguments
parser = argparse.ArgumentParser(description="List available metric descriptors matching 'litellm'")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
args = parser.parse_args()

project_id = args.project_id

# Construct the URL for the MetricDescriptors API
url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/metricDescriptors"

# Use active gcloud auth token to authenticate the API request
try:
    token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()
except FileNotFoundError:
    print("Error: The 'gcloud' command-line tool was not found on your system. Please install the Google Cloud SDK.")
    exit(1)
except subprocess.CalledProcessError as e:
    print(f"Error retrieving active access token: {e}")
    exit(1)

# Construct urllib Request
req = urllib.request.Request(url)
req.add_header('Authorization', f'Bearer {token}')
req.add_header('Accept', 'application/json')

# Execute request and load response
try:
    # Set 10s timeout to prevent hanging
    with urllib.request.urlopen(req, timeout=10) as response:
        descriptors = json.loads(response.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(f"HTTP Error {e.code} querying metrics API: {e.read().decode('utf-8')}")
    exit(1)
except urllib.error.URLError as e:
    print(f"Failed to connect to Monitoring API: {e.reason}")
    exit(1)

# Filter and display only the metrics relevant to 'litellm'
litellm_metrics = [m.get('type') for m in descriptors.get('metricDescriptors', []) if m.get('type') and 'litellm' in m.get('type')]

print(json.dumps(litellm_metrics, indent=2))
