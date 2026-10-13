# google_compute_firewall

From the `google` provider's source at 8.x (September 2026).

- **Forces replacement:** `name`, `direction`, `project`, `params`, and others the provider page marks.
- **Updatable in place:** `allow`/`deny`, `source_ranges`,
  `destination_ranges`, `source_tags`, `target_tags`,
  `target_service_accounts`, `priority`, `disabled`, `log_config`,
  `description`. `network` is not marked as forcing replacement in the
  provider, but changing a rule's network is not a change the Compute API is
  documented to accept: treat it as a replacement and stop for the user.
- **Disabling a rule** is `disabled = true`, never deleting the block.
- **Narrowing** a `source_ranges` list is the edit; never widen one to
  `0.0.0.0/0` unless the request says so in those words, and call it out in
  the PR body when it does.
- No `deletion_protection`. `deletion_policy` defaults to `DELETE`; leave
  it.

| `gcloud compute firewall-rules describe` field | Argument                                                   |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `allowed[]` (`IPProtocol`, `ports[]`)          | `allow { protocol, ports }`                                |
| `denied[]`                                     | `deny { protocol, ports }`                                 |
| `sourceRanges[]`, `destinationRanges[]`        | `source_ranges`, `destination_ranges`                      |
| `targetTags[]`, `sourceTags[]`                 | `target_tags`, `source_tags`                               |
| `direction`, `priority`, `disabled`            | `direction`, `priority`, `disabled`                        |
| `logConfig.enable`                             | `log_config { metadata }` (present = on)                   |
| `network` (a URL)                              | `network` (name or self link, as the repository writes it) |
