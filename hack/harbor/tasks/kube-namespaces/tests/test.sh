#!/bin/bash
if grep -qi kube-system /app/answer.md && grep -qi kubeagents-system /app/answer.md; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
