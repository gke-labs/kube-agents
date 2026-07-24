# fleet/

Project-level (platform-tier) desired state: the **platform `Agent` CR** and its pre-created
read-only identity (project-scoped KSA/RBAC/WI), plus project-wide policy that applies across
clusters. This is the Platform Agent's home scope (one per project). Applied by CI/CD on merge;
human-reviewed.
