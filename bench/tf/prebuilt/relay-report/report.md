The scheduled fleet waste audit (`bench-relay-probe`) could not be executed because the `ledger-token-minter` pods in the `bench-relay` namespace are in a `CrashLoopBackOff` state. This prevents the audit's credential broker from refreshing the GitHub authentication tokens it needs.

The platform agent lacks the RBAC permissions to read logs from these pods, so the specific error is not known.

Because I cannot resolve this infrastructure dependency autonomously, the audit has not been run. Please investigate the status of the `ledger-token-minter` pods in the `bench-relay` namespace.
