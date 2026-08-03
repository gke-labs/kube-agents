#!/usr/bin/env python3
import json
import os
import subprocess
import time

def main():
    project_id = os.environ.get("GCP_PROJECT_ID", "tomeklipski-izrhgv")
    context = os.environ.get("KUBECTL_CONTEXT", "gke_tomeklipski-izrhgv_us-east1_ka-mgmt")
    cluster_name = os.environ.get("TARGET_CLUSTER_NAME", "ka-production")
    topic = "gke-stockout-alerts-topic"
    test_id = f"test-stockout-{int(time.time())}"

    print(f"============================================================")
    print(f"Verifying GKE Stockout Investigator Extension (Python)")
    print(f"Project ID:      {project_id}")
    print(f"Kubectl Context: {context}")
    print(f"Target Cluster:  {cluster_name}")
    print(f"PubSub Topic:    {topic}")
    print(f"Test Event ID:   {test_id}")
    print(f"============================================================")

    payload = {
        "insertId": test_id,
        "logName": f"projects/{project_id}/logs/test-stockout",
        "resource": {
            "type": "k8s_cluster",
            "labels": {
                "cluster_name": cluster_name,
                "location": "us-east1"
            }
        },
        "jsonPayload": {
            "messageId": "scale.up.error.out.of.resources",
            "noDecisionStatus": {
                "noScaleUp": {
                    "unhandledPodGroups": [
                        {
                            "podGroup": {
                                "samplePod": {
                                    "namespace": "default",
                                    "controller": {
                                        "name": "ml-training-job-gpu-2"
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }
    }

    message_str = json.dumps(payload)
    print(f"Step 1: Publishing test event to PubSub topic '{topic}'...")
    res = subprocess.run([
        "gcloud", "pubsub", "topics", "publish", topic,
        "--project", project_id,
        "--message", message_str
    ], capture_output=True, text=True)

    print(f"Publish output: {res.stdout.strip()}")
    if res.returncode != 0:
        print(f"Error publishing: {res.stderr.strip()}")
        return

    print("\nStep 2: Waiting 8 seconds for PubSub adapter processing...")
    time.sleep(8)

    print("\nStep 3: Checking Hermes sessions in platform-agent-gateway container...")
    check_cmd = [
        "kubectl", "--context", context, "exec", "-n", "kubeagents-system",
        "deployment/platform-agent-gateway", "-c", "platform-agent", "--",
        "python3", "-c",
        "import sqlite3, json; conn = sqlite3.connect('/opt/data/state.db'); print(json.dumps([{'id': r[0], 'user_id': r[1], 'chat_id': r[2], 'started_at': r[3]} for r in conn.execute(\"SELECT id, user_id, chat_id, started_at FROM sessions WHERE user_id LIKE '%gke_stockout_alerts%' ORDER BY started_at DESC LIMIT 1\").fetchall()], indent=2))"
    ]
    res_check = subprocess.run(check_cmd, capture_output=True, text=True)
    print(f"Latest Session Query Output:\n{res_check.stdout.strip()}")

    if "gke_stockout_alerts" in res_check.stdout:
        print("\n✓ SUCCESS: PubSub adapter received test event and created Hermes session for 'gke_stockout_alerts'!")
    else:
        print("\nChecking latest container logs:")
        log_cmd = ["kubectl", "--context", context, "logs", "-n", "kubeagents-system", "deployment/platform-agent-gateway", "-c", "platform-agent", "--tail=30"]
        res_log = subprocess.run(log_cmd, capture_output=True, text=True)
        print(res_log.stdout)

if __name__ == "__main__":
    main()
