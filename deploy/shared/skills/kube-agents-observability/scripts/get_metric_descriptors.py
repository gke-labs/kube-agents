import argparse
import json
import subprocess

# Parse arguments
parser = argparse.ArgumentParser(description="List available metric descriptors matching 'litellm'")
parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
args = parser.parse_args()

project_id = args.project_id

# Construct the URL for the MetricDescriptors API
url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/metricDescriptors"

# Use active gcloud auth token to authenticate the API request
token = subprocess.check_output(['gcloud', 'auth', 'application-default', 'print-access-token']).decode().strip()


# Use curl to fetch the descriptor list
cmd = [
    'curl', '-s',
    '-H', f'Authorization: Bearer {token}',
    url
]

# Execute and load the JSON response
descriptors = json.loads(subprocess.check_output(cmd).decode())

# Filter and display only the metrics relevant to 'litellm'
litellm_metrics = [m['type'] for m in descriptors.get('metricDescriptors', []) if 'litellm' in m['type']]
print(json.dumps(litellm_metrics, indent=2))
