#!/bin/bash
sed -n 's/^  - name: //p' /var/lib/kube-agents/clusters.yaml > /app/answer.md
