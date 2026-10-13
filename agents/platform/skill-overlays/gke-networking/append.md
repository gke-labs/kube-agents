<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Before you pass `--dns-endpoint`

The `get-credentials --dns-endpoint` example above works only on a cluster that publishes a DNS
endpoint **and** has `controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic` set to
true. Check first:

```bash
gcloud container clusters describe {cluster_name} --region {region} \
  --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)'
```

Do not infer support from the command succeeding. When external traffic is disabled, a caller that
Google treats as internal gets a warning rather than an error, plus a kubeconfig pointing at the
DNS endpoint that then returns HTTP 403 on first use — a failure that surfaces one step later than
its cause. `gcloud container clusters update {cluster_name} --enable-dns-access` turns the
setting on.

The Platform Agent's own tooling makes this decision per cluster in
`/opt/data/scripts/gke_endpoint.py`, so `switch_kube_context` and the Cluster Agent profile
scaffolding already pass the flag exactly when it applies; the check above is for the times you
run `get-credentials` by hand. That decision is re-read about once a minute per cluster, so after
enabling the setting, wait a moment before retrying rather than concluding it did not work.
