#!/bin/bash
if grep -Eq '(^|[^/[:alnum:]-])kube-agents([^/[:alnum:]-]|$)' /app/answer.md && grep -q payments-prod /app/answer.md && grep -q orbital-7 /app/answer.md; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
