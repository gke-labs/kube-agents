<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Reads this install withholds

The Platform Agent's command gate refuses three reads this skill uses:
`gcloud iam service-accounts get-iam-policy` (Step 3), `gcloud asset search-all-iam-policies`
(Step 4) and `kubectl exec` (Step 5). Do not retry them in another spelling. Run the steps you can,
then hand the refused commands to the user with the placeholders filled in, say which output
decides the diagnosis, and ask for the result. A role granted on the whole project is readable
here: `gcloud projects get-iam-policy PROJECT_ID --format=json` shows it, so search its bindings
for the principal before handing Step 4 over.
