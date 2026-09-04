"""Bucket 2 -- conformance scenarios that need a cluster but no human.

Written, wired and skipped. They run when `KUBE_AGENTS_CONFORMANCE_CLUSTER`
names a context, which is a deliberate opt-in rather than a kubeconfig probe:
these scenarios attempt mutations, and a suite that starts doing that because a
developer happened to have credentials in their environment is a worse outcome
than one that never runs.

The intended home is the `rc` pipeline, against the environment it already
stands up. Nothing here is wired into the pull-request path.
"""
