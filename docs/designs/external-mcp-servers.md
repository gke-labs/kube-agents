# External MCP Servers Behind the Credential Broker

A proposal for decision. Nothing here is built, and no code follows before the decision. This is
the design for the five shared pieces of
[#1989](https://github.com/gke-labs/kube-agents/issues/1989) (Datadog reads) and for the third
point of [#7242](https://github.com/gke-labs/kube-agents/issues/7242) (external MCP server URLs).
The security approver decides, because the design changes what the credential broker holds and
which hosts it calls. The decider answers each "Proposed" row of [Decisions](#decisions) with yes
or no, and each "To decide" row with one of its choices. Row 12 also waits for one probe. The list
under the table names the rows that also need the view of the owners that
[`docs/ownership.md`](../ownership.md) names for the areas "Credential proxy" and "Operator and
install".

The security approver decided rows 7, 15, 33 and 36 on 2026-11-27
([the decision](https://github.com/gke-labs/kube-agents/pull/7414#issuecomment-7207673924)). Each of these rows starts with "Decided".

Related designs:

- [The broker surface of a profile](profile-broker-surface.md) owns each row of the `profile` role
  on the broker. This design asks it for one row and does not own that row.
- [The profile worker hooks](profile-worker-hooks.md) keeps the rule that a persona names MCP
  servers on the broker's relay only (its row 13, which waits for an answer).
- [The unattended write permit](unattended-write-permit.md) records that a credential with no
  target does not reach the MCP relay (its row 9).
- [The Cloud API relay](gcp-api-relay.md) is the sibling relay. Its security review and its
  rejected alternatives are the model for this document.
- The design of the inbound alert endpoint for
  [#2002](https://github.com/gke-labs/kube-agents/issues/2002) is a separate proposal
  ([#7406](https://github.com/gke-labs/kube-agents/pull/7406)). The border: that endpoint takes
  only what a source pushes, and it makes no call to the source. Each read of the source is a tool
  call through the path of this document. Row 18 says which runs get that read.

## Problem

An install wants its agents to read data of other systems during an investigation. The requests
have two shapes:

- **A server that a vendor hosts.** The first request is Datadog: monitors, logs and incidents.
- **A server that the owner of the install hosts**, for example for clusters on another cloud or
  for cost data. Its URL, its tools and its sign-in are the owner's.

A client of an MCP server usually signs in through a browser and keeps the token. Our agents run
in pods with no person and with no credential. The direction is one mechanism for remote MCP
servers, not one integration for each system.

The words of the issues do not all match the code. This document uses these terms:

- **The credential broker** is the process that the issues call the credential proxy
  (`agents/platform/scripts/credential_proxy.py`). It is not a general HTTP proxy.
- **The MCP relay** is the broker route `POST /v1/mcp/<server>`.
- **The server table** is `MCP_SERVERS` in `agents/platform/scripts/mcp_policy.py`.
- **An external server** is a remote MCP server whose host is not a Google API host. It can be on
  the internet or in a private network.
- **The admin** is the person who writes the PlatformAgent of the install.

The other direction is not a part of this document: an MCP host that connects to kube-agents
([#1720](https://github.com/gke-labs/kube-agents/issues/1720), the page `install/mcp.md`, the
broker role `mcp`). [#5037](https://github.com/gke-labs/kube-agents/issues/5037) is the installer
switch for that server.

## What exists today

The broker already relays MCP calls. An external server is an extension of this relay, not a new
part. Each name in the column "Where" is a name in `mcp_policy.py` or `credential_proxy.py`, if
the cell names no other file.

| Fact               | Today                                                                                                                                                                                                  | Where                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Route              | `POST /v1/mcp/<server>` forwards one JSON-RPC message on the broker's own credential                                                                                                                   | `MCP_RELAY_PREFIX`, `_handle_mcp_relay`                                                                                                                        |
| Server table       | 2 servers, both Google: `gke` (15 read tools) and `developer_knowledge` (3 read tools). "The table is the enforcement." It is "widened only by pull request"                                           | `MCP_SERVERS`, the module text of `mcp_policy.py`                                                                                                              |
| Host rule          | A host that does not end with `.googleapis.com` stops the import. Today this check is what keeps the Google token away from each other host                                                            | `_validate_servers`                                                                                                                                            |
| Methods            | 5: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Each other method is refused                                                                                         | `RELAYED_METHODS`                                                                                                                                              |
| Credential         | The broker's Google token as `Authorization: Bearer`, and the header `X-Goog-User-Project`. Both go to each server of the table                                                                        | `GoogleApiRelay`, `MCP_RELAY_USER_PROJECT`                                                                                                                     |
| Headers            | The broker sends `Accept: application/json`. It drops each caller header but two that pass with a shape check: `User-Agent` and `MCP-Protocol-Version`. `Mcp-Session-Id` is dropped                    | `API_RELAY_ACCEPT`, the comment "The remote MCP relay"                                                                                                         |
| Bounds             | Connect 10 s, deadline 120 s, reply 8 MiB, port 443, no redirect. A reply whose content type is not JSON is refused; the body is parsed only for `tools/list`. A 401 starts no sign-in                 | `API_RELAY_CONNECT_TIMEOUT_S`, `API_RELAY_DEADLINE_S`, `API_RELAY_MAX_RESPONSE_BYTES`, `API_RELAY_UPSTREAM_PORT`, `UPSTREAM_NOT_JSON`, `UPSTREAM_UNAUTHORIZED` |
| Connection         | A direct TLS connection for each call. The relay reads no proxy variable                                                                                                                               | `GoogleApiRelay.connection`                                                                                                                                    |
| Callers            | 2 of the 11 caller roles: `chat` (the token of the Hermes container) and `profile` (an AgentProfile worker). Session pods and the sandbox are not admitted                                             | `CALLER_ROLES`, `ROUTE_ROLES`                                                                                                                                  |
| The `profile` role | Needs a task credential. Gets only `developer_knowledge` with two tools; this set is also "Widened only by pull request". `gke` stays out: the relay has no check of the task's target                 | `PROFILE_SERVER_TOOLS`, `mcp_relay_decision`                                                                                                                   |
| The offer          | A Hermes profile names a relay server in its image config with a `tools.include` list. A persona names it in `mcp.yaml` with `broker:`. The persona format refuses `url:`                              | `deploy/shared/defaults/config.yaml`, `a2a/lib/persona/README.md`                                                                                              |
| The operator       | It renders no `mcp_servers` key in the managed scope: that scope is one file that all Hermes profiles of the pod share, and its merge replaces lists. A second path merges an overlay into one profile | `agents/chat/config.yaml`, `deploy/shared/profile_overlay.py`                                                                                                  |
| A forge            | The nearest model: the admin writes the host name of a forge in the PlatformAgent (no port, no path), with a Secret reference and an optional CA reference. Only the broker mounts both                | `ForgeSpec` in `k8s-operator/api/v1alpha1/common_types.go`                                                                                                     |

Four more facts decide the shape of the proposal.

**Who investigates today.** Each install runs the A2A stack
([spec-mode-switch.md](spec-mode-switch.md)). A chat message goes to the A2A gateway, then to the
Hermes bridge, then to the Planning Agent, which files a board card (the part "How a chat question
is answered" of `docs/site/src/content/docs/concepts/chatops.md`). A Hermes worker of the platform
profile, or of a Cluster Agent profile for one named cluster, does the investigation. Its MCP
calls carry the `chat` role. The three AgentProfiles of the chart are off by default, and a
session pod is not on the relay. The same `chat` token also runs the scheduled audits and the
alert triage, and a scheduled job gets the toolsets of its profile if its entry does not replace
them. So under that role the broker cannot tell a person's question from a run with no person.
The epic [#6526](https://github.com/gke-labs/kube-agents/issues/6526) plans to move this work to
AgentProfiles; the `profile` role is the one that stays.

**Egress.** No allowlist by host exists. The broker's egress is open, to the internet and to the
cluster network. A NetworkPolicy matches addresses, not names. So the server table is the only
thing that limits which host the broker calls for MCP.

**Redaction.** The gateway redaction is off by default (`litellm.redaction` in
`charts/kube-agents/values.yaml`), covers only the request to the model, and stops the request on
an error (`charts/kube-agents/files/litellm_redaction_callback.py`). The relay redacts nothing.
[#6820](https://github.com/gke-labs/kube-agents/pull/6820) proposes a redaction of credential
shapes in what the broker returns to the `profile` and `session` roles, not to `chat`.

**Approval.** No human-approval path exists in the broker or in the relay.
[#1724](https://github.com/gke-labs/kube-agents/issues/1724) is that work.

## What must hold

1. **The credential stays in the broker.** In the manifests that the operator renders, no pod of
   an agent, of a worker or of the sandbox holds the credential of an external server. No route
   returns it, also not in a reply of the server.
2. **A table is the enforcement.** Each server has an exact list of tool names. A name that is not
   in the list is refused, so a tool that the server adds later stays refused until someone names
   it.
3. **A persona and an agent config name relay servers only.** No config holds the URL of a remote
   server (the [profile spec](spec-subagent-profiles.md) says it for a persona: "MCP servers are
   named on the broker's relay only"; the conformance row B1 holds it for the configs of the
   platform, Cluster Agent and tse profiles).
4. **A grant uses a fact that the broker verifies.** It does not use a request field
   ([profile-broker-surface.md](profile-broker-surface.md#rules-for-a-grant)). A URL, a host and a
   tool list come from the code or from the PlatformAgent, never from a caller.
5. **A read is still a grant.** One credential reads a whole account of the other system, not one
   cluster.
6. **No per-user sign-in.** The decision that #1720 names D12 stays: we hold no refresh token for
   a person. The full text of D12 is not in this repository. #1720 has three sentences on it and
   calls it "the decision most likely to be re-examined here"; this design does not need that.
7. **No credential in a URL or in the PlatformAgent.** The agent can read the PlatformAgent. No
   Google credential and no project id go to an external host.
8. **A tool result is data.** One vendor says of its own results: "This content can include text
   that your AI agent might interpret as instructions."

## Options

The mechanism:

| Option | Result                                                                                                                                                                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A      | A new kind of entry on the MCP relay for an external server. The relay, its tool lists, its refusals and its tests stay. Recommended.                                                                                                            |
| B      | An MCP server process in the broker's pod that holds the token ([version-control-support.md](version-control-support.md) names this form in its part on MCP). More code that runs beside the credential. Only for a server that needs it.        |
| C      | A server URL in a Hermes config or in a persona. The persona format, the profile spec and the conformance row B1 refuse it. The agent's pod would hold the credential.                                                                           |
| D      | A general forward proxy that adds a credential. It reads no tool name. [gcp-api-relay.md](gcp-api-relay.md#rejected-alternatives) rejected the same form for Google APIs: a proxy that signs each request of a caller gives the credential away. |

Where an entry comes from:

| Option | Result                                                                                                                                                                                                   |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1      | Code entries only. Each server needs a pull request and a release of ours. A server that an owner hosts itself cannot get one: its URL and its tools are not ours to review.                             |
| 2      | Admin-set entries only. One mechanism. But each install then writes the tool list of a hosted vendor by hand, and no test of ours holds that list to read tools.                                         |
| 3      | Both. A code entry for a vendor's hosted server that many installs share. An admin-set entry for each other server. One relay code path; the two forms differ only in who writes the entry. Recommended. |

Our recommendation is A with 3. Option B stays possible later for a server whose requests need a
signing process.

## The two forms of an entry

|                        | A code entry (a vendor's hosted server)                                                                       | An admin-set entry (each other server)                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Who writes the host    | The code: a closed list of hosts. The admin selects one (a site)                                              | The admin, in the PlatformAgent, as the host of a forge today. Here the value is a URL with a path, which a forge does not have  |
| Who writes the tools   | The code: exact names from a dated read of the live server, reviewed in a pull request                        | The admin: exact names in the PlatformAgent. No pattern, no "all"                                                                |
| Read-only              | Held by our list, by a conformance row with a mutation, and by an eval case                                   | Not held by us. The admin's list can name a tool that writes. The rights of the credential on that server are the limit (row 14) |
| A change of the server | A new tool stays refused. We read the server again and review the change                                      | A new tool stays refused. A tool can change what it does behind the same name, and nobody of ours sees it                        |
| Transport              | Checked by us against the relay before the entry merges                                                       | Not checked. A server that needs more than the relay's subset does not work                                                      |
| What differs more      | A fixed header, port 443, no CA, a fixed query and tool descriptions that we reviewed (row 36), a token shape | A header name, a port and a CA that the admin gives (rows 8, 9 and 11); no token shape (row 22); no eval of ours                 |
| What is the same       | The record of a call: row 24                                                                                  | The same. An admin-set entry gets no caller in the first release (rows 15 and 29)                                                |

What the admin accepts with an admin-set entry: the admin answers for the URL, for each tool name
and for the rights of the credential. The broker calls the host that the admin names. We test the
mechanism and not that server. The guide says this in these words.

## Decisions

"Where" names what changes when the row is built. A row with "Proposed" needs a yes or a no. A row
with "To decide" gives the choice, and the first choice is our recommendation. A row with "Decided"
holds a decision of the security approver; in such a row the words after "Ours:" are our reason,
consequence or detail, and not a part of the decision.

| Id  | Decision                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Where                                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| 1   | Proposed: an external server is a new kind of entry on the relay (option A). An entry holds the name, the host, the port, the path with a fixed query, the tool names and the credential mode; an admin-set entry also holds a header name and a CA reference, and a code entry the profile name of row 17, if that row takes its first choice. The import check stays for each entry of the Google credential mode. A code entry of another mode whose host is a Google API host also stops the import; for an admin-set entry rows 5 and 30 apply. The credential mode of a code entry is a part of the code and never a field of the admin.                                                                                                                                                                                                                                                                                                                                                                                                                                             | `MCP_SERVERS`, `_validate_servers`                    |
| 2   | To decide: both forms of an entry (proposed; option 3); or code entries only; or admin-set entries only. Rows 7, 15 and 36 are decided and are written for the first choice.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `mcp_policy.py`, the CRD                              |
| 3   | To decide: for a code entry, the admin gives only a switch, a site from the closed list of that entry, a Secret reference and, if row 17 takes its first choice, a profile name (proposed); or the admin can also shorten the tool list of a code entry. Row 5 keeps an admin-set entry away from the hosts of a code entry, so that it is no second way to the same vendor.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | The CRD                                               |
| 4   | Proposed, if row 2 has both forms: the rule "widened only by pull request" stays for each code entry, and a change of the PlatformAgent widens an admin-set entry. The person is the same who sets the images, the forges and the repositories of the install. On an install that keeps its PlatformAgent in a repository where the agent opens pull requests, the merge by a person is the control. Row 17 has the same question for the profile set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | The module text of `mcp_policy.py`                    |
| 5   | Proposed: the form of an admin-set entry, held by the CRD and again by the broker at start. The URL is `https`. The host is a DNS name with two or more labels: no IP literal, no user part, no fragment. The fixed query has no key that is a credential by its name (the list `API_RELAY_STRIPPED_QUERY_KEYS` is the start); a server that takes its key only in the query is not supported. The host is not a Google API host, not a host of the install's universe domain, not the name of the metadata server, and not a host of a code entry, also when that code entry is off. The name fits the name rule of the worker adapter and of the persona loader (`relayServerName`, `serverRE`), starts with a fixed prefix that no code entry uses, has no two `_` in a row, and is not a key of `mcp_servers` in an image config. The tool list has exact names. At most 8 entries, and at most 64 tools for each. `http` is refused.                                                                                                                                                  | The CRD, `_validate_servers`                          |
| 6   | Proposed: at each call the broker refuses a connection to a loopback, link-local or metadata address, and to the addresses of row 7. The check is on the address of the connection, not on the name, because the name is resolved again at each call. Behind a proxy (row 21) the proxy resolves the name, and the broker checks no address then; the rules of the proxy are the only check.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `GoogleApiRelay.connection`                           |
| 7   | Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7414#issuecomment-7207673924)): an admin-set entry can name a host that resolves to a private address, for a server that the owner runs. At connect time the broker refuses the Service and Pod ranges of the cluster and the address of its API server. Ours: so an MCP server at a Service or Pod address of the same cluster is not served; point 3 of #7242 names that case, and this design declines it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `GoogleApiRelay.connection`                           |
| 8   | To decide: an admin-set URL can name a port other than 443 (proposed, for a server that the owner runs in a private network); or port 443 only. A code entry stays on port 443.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `API_RELAY_UPSTREAM_PORT`                             |
| 9   | Proposed: the credential mode of the first release is "static header": the broker reads one key of a Secret on each call and sends it in one header. For a code entry the header is fixed. For an admin-set entry it is `Authorization` with the scheme `Bearer`, or one header name that the admin gives; that name has only letters, digits and `-`, and, compared without case, it does not start with `X-Goog-` and is not `Host`, `Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `Expect`, `TE`, `Trailer`, `Content-Type`, `Accept`, `Accept-Encoding`, `User-Agent`, `MCP-Protocol-Version`, `Mcp-Session-Id`, `Cookie` or `Proxy-Authorization`. The broker removes one line break at the end of the value; a value that is empty or has a control character is "no credential"; no answer and no log line holds the value. The Google token and `X-Goog-User-Project` go to Google hosts only. To an external host the broker sends its own `User-Agent`, not the caller's; each other caller header is dropped but `MCP-Protocol-Version` with its shape check. | `GoogleApiRelay`, `MCP_RELAY_USER_PROJECT`            |
| 10  | Proposed: the Secret is an optional volume with named keys only, in the stateful broker Deployment only, as a forge credential is. A change of the Secret needs no restart. The exec tier mounts no Secret and does not serve the relay. The volume name prefix is reserved (`ReservedVolumePrefixes`), so that the case of [#3698](https://github.com/gke-labs/kube-agents/issues/3698) does not grow. The prefix guards a name collision only. Today the operator also refuses a volume or an env reference that an admin writes for the credential Secret of a forge (`ForgeCredentialSecretNames`, `ForgeCredentialRoutes`); the Secret of an external server gets the same check. A CA Secret is left out of that check, as for a forge.                                                                                                                                                                                                                                                                                                                                              | The operator, `common_types.go`                       |
| 11  | Proposed: an admin-set entry can name a CA in a Secret, as a forge can. The broker trusts it for that one host only. A code entry has no CA field.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | The operator, `GoogleApiRelay.connection`             |
| 12  | Proposed, if the probe of [The open fact](#the-open-fact) permits it: the relay keeps its transport subset for an external server: one POST, a reply with the JSON content type, the five methods, the bounds of today but the port of row 8.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | `_handle_mcp_relay`                                   |
| 13  | Proposed, for a code entry: read tools only. `tools/list` is served from the list of the entry, with the reviewed text of row 36. A write tool has no path until #1724 gives an approval path; the switch of #1989 for write tools is not built.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `mcp_policy.py`                                       |
| 14  | Proposed: an admin-set entry is for read tools, but the broker cannot know what a tool does. The CRD text and the guide say that the list names read tools and that the credential has read rights only, and the admin answers for it. By rows 15 and 29 an admin-set entry gets no caller before #1724 gives an approval path.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | The CRD, the guide                                    |
| 15  | Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7414#issuecomment-7207673924)): in the first release the `chat` role reaches code entries only. The `chat` role reaches an admin-set entry only after #1724 gives an approval path for its tools. Ours: until then the broker refuses an admin-set entry to each caller role, and with rows 17 and 29 it has no caller in the first release. The reason: an admin-set tool can write, and the `chat` token also runs the scheduled audits and the alert triage, so the text of an alert could drive a write in another system through a run with no person. The `chat` role is the role of the actor that investigates for a person on a default install of today. [Runs with no person](#runs-with-no-person) says why this row and row 18 differ.                                                                                                                                                                                                                                                     | `mcp_relay_decision`; `ROUTE_ROLES` does not change   |
| 16  | Proposed: the offer of a code entry is in the configs of the platform profile and of the Cluster Agent profile, not of the Planning Agent. Each shipped model job gets an `enabled_toolsets` list with the toolsets that it uses today, `no_mcp`, and the names of the MCP servers that it keeps; a list that only leaves a server out does not remove it. No external server is in such a list, and a test holds the shipped configs and jobs to this. A job that the agent makes at run time gets the toolsets of its profile; that is a residual. This row is not broker enforcement.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | The Hermes configs, `agents/platform/cron/jobs.json`  |
| 17  | To decide: this design asks the broker surface design for one Routes row: an external server goes to one named profile, with each tool of the entry (proposed; the profile name is a field of the entry; question 4 of that design, a named profile from an operator list, has no answer on record); or no profile in the first step. Until the broker surface design has that row, no profile reaches an external server. The named profile is one whose shipped persona names the server (row 28), and it does not list the `unattended` tier, so that a child task of an unattended run, which has the tier `read` and no trigger, cannot be on it. In the first step this is for a code entry only (row 29). For an admin-set entry it would later mean a grant by a change of the PlatformAgent, with no pull request and no conformance row for that one server, which rule 3 of that design asks for.                                                                                                                                                                               | `PROFILE_SERVER_TOOLS`, `profile-broker-surface.md`   |
| 18  | To decide, if row 17 takes its first choice, one of: (a) the refusal in the first release, and a later row of this design can admit one named alert sender within the bounds of [Runs with no person](#runs-with-no-person); (b) the refusal in the first release, and no later opening; (c) that opening is a part of the first release. The refusal in the first release is proposed; between (a) and (b) this document gives no recommendation yet. The refusal means: for a profile, the call needs a task credential that has a target, whose tier name is not `unattended`, and that has no trigger, and a report credential is refused. Its cost is in that part: the own run of the alert triage of #2002 then reads no external server. The relay does not read the tier today, so this is a new check. A child task of an unattended run has the tier `read` and no trigger; the profile rule of row 17 keeps it out. The broker does not know the mode of a tier that an install adds; that is a residual.                                                                      | `mcp_relay_decision`                                  |
| 19  | To decide: a read of an external server is held to the profile's name and to the rights of the one credential, and not to the task's cluster (proposed, as a stated residual); or no profile grant until a tool call can be held to a target. The tools of these servers take no cluster argument that the broker can check.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | -                                                     |
| 20  | Proposed: no new NetworkPolicy rule. The entries are the allowlist, and this document states the gap. One conformance row: no agent config and no persona points at an external host. This declines the NetworkPolicy part of point 3 of #7242.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `tests/conformance/`                                  |
| 21  | To decide: behind a corporate proxy, the relay uses the `HTTPS_PROXY` value that `platformAgent.deployment.env` already gives to the broker, for an external host only, and it obeys `NO_PROXY` (proposed; it adds no field, as the decision on [#6227](https://github.com/gke-labs/kube-agents/issues/6227) asks); or the first release states "not supported behind a proxy". A proxy that opens TLS is not supported for a code entry in the first release. First-class proxy fields and an install-wide CA bundle stay with points 1 and 2 of #7242.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | `GoogleApiRelay.connection`                           |
| 22  | To decide: for both forms and for each status, the relay replaces in a reply the exact value of the key that it sent on this call, and the credential shapes of `credential_shapes.py`, with the token shapes of each code entry added, for each caller role, `chat` also (proposed; on the mechanism of #6820 if that is on `main` then, and if not, this change builds it for the relay route only); or the exact value only. A key of an admin-set entry has no shape, so only the exact value covers it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `_handle_mcp_relay`, `credential_shapes.py`           |
| 23  | Proposed: personal data in a tool result is covered only by the gateway redaction of the next model request. The guide tells the admin to turn it on and names what leaves the cluster: the arguments of a tool call go to the external server as the model wrote them, and the result goes to the model provider. #1989 asks for redaction of requests and of results; this design does not give it for the request, because no redaction rule sees this path.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | The guide                                             |
| 24  | Proposed: the log line of a forwarded call, which has the server and the method today, also gets the tool name. A structured audit record waits for question 3 of [profile-pod-telemetry.md](profile-pod-telemetry.md), which has no answer.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | `_handle_mcp_relay`                                   |
| 25  | To decide: an external entry gets rule ids with a prefix that is not `gcp.` and its own refusal text (proposed; the text of today says "Changes to cloud resources go through a pull request"); or it reuses `gcp.mcp.*`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | The `RULE_` names of `mcp_policy.py`                  |
| 26  | Proposed: the configuration is one list under `spec.integration` of the PlatformAgent. An entry names a code entry with its site, or it is admin-set with its URL, its tools, its header name and its CA reference. Each has a Secret reference; a code entry also has the profile name of row 17, if that row takes its first choice. An item in the list is the switch. The chart has the same value and the guard for a CRD that lacks the field. The AgentProfile gets no field, which declines that part of #7242.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | The CRD, the chart                                    |
| 27  | To decide: the first release has the chart value and the CRD field only (proposed); or also the installer key and the Terraform variable. Each added layer must be complete.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | The installer                                         |
| 28  | Proposed: the agent-side entry of a code entry is always in the image config or in the persona. When the switch is off, the broker opens no connection to the server: it answers `initialize`, `ping` and `tools/list` itself, with no tool, and refuses each call with a rule id. So one image serves each install, and an install that did not choose a vendor sends it nothing.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | `_handle_mcp_relay`, the Hermes configs, the personas |
| 29  | Proposed, for the step after #1724 (row 15): the agent-side entry of an admin-set entry, whose name no image knows, comes from the operator through the overlay of one Hermes profile, the platform profile, never through the managed scope. A profile worker gets no admin-set entry in that step, because that needs a grant outside the persona, whose `tools` list is the allowlist. In the first release an admin-set entry has no agent-side entry and no caller. The merge of an `mcp_servers` key through the overlay is read in the code and is not tested.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | `deploy/shared/profile_overlay.py`, `a2a/lib/persona` |
| 30  | Proposed: a configuration file that the broker cannot read stops it at start, as for forges. One entry that fails a check, or a code entry that an older broker image does not know, only makes that one server absent, with a log line. A newer broker with no configuration behaves as today.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `credential_proxy.py`                                 |
| 31  | Proposed: each failure of a tool call is a tool result with `isError` on HTTP 200, with the cause and the action in plain text: no credential, the key value cannot be sent, the server refused the credential (401 or 403), the server's rate limit, the broker's limit on calls in flight (row 33), the server is not reachable. A value that the CRD does not permit is refused at admission.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `mcp_policy.py`, the CRD                              |
| 32  | To decide: a fault of the credential at run time shows only in the broker's answer and log, as for a forge today (the guide `install/gitlab.md` says: "The PlatformAgent status does not report these"), with one check command in the guide (proposed); or a new status signal, which needs a way for the operator to learn it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | The guide                                             |
| 33  | Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7414#issuecomment-7207673924)): a small limit on the calls in flight for each entry in the first release. Ours: a call over the broker's limit gets a tool error and does not wait, and the server's own limit answer still goes to the model as a tool error. The reason: each caller shares the one limit of the server, so a scheduled run or a profile can use the calls that a person's question needs. The profile set keeps one tool out today for this reason.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `_handle_mcp_relay`                                   |
| 34  | To decide: the eval of a code entry uses a real vendor test account with planted, inert data on a lane that does not block a merge, as the GitLab lane does (proposed; the owner of the lane owns the account and its token); or a mock MCP server in the eval install, as #1989 asks. No mock path exists today: the host of a code entry is in the code, and rows 6 and 7 refuse a Service or Pod address of the cluster of the eval install for each entry, so the second choice needs an exception to them. An admin-set entry gives no mock path in the first release: it also has no caller (rows 15 and 29).                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | `bench/`, the lane                                    |
| 35  | Proposed: under a custom universe domain, an external entry is served as elsewhere. The admin turns it on by choice, and the guide says that the data leaves the universe.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | `tests/conformance/`                                  |
| 36  | Decided on 2026-11-27 ([the decision](https://github.com/gke-labs/kube-agents/pull/7414#issuecomment-7207673924)): for a code entry, `tools/list` serves the reviewed description and input schema of each tool from the entry, and the broker answers `initialize` without the free text of the server. Ours: so the text of a vendor reaches the model only as a tool result. An admin-set entry keeps the residual. The cost: the entry holds each schema, so a change on the vendor's side can break a call until a pull request updates the entry.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `mcp_policy.py`, `_handle_mcp_relay`                  |

Rows that also need the view of a second owner, by the areas of `docs/ownership.md`: each row
that changes the CRD, the chart or what the operator renders (rows 2, 3, 5, 10, 11, 14, 26, 27,
29 and 31, and row 32 with its second choice), the owner of "Operator and install"; each row
that changes the broker or its policy (rows 1, 2, 4 to 13, 15, 17, 18, 21, 22, 24, 25, 28, 30,
31, 33 and 36), the owner of "Credential proxy".

## Runs with no person

Rows 15 and 18 give different answers for a run with no person. This part says why, and what
each answer costs.

Under the `chat` role the broker has no fact that tells a run with no person from a person's
question: the scheduled audits, the alert triage of today and a person's investigation use one
token. So row 15 cannot refuse such a run at the broker. A scheduled run can reach a code entry,
and row 16 keeps the shipped jobs away with their tool lists, which is config and not broker
enforcement. Row 16 does not cover the alert triage of today: it is a
board card, and its worker has the toolsets of its profile. So an alert of today can start a run
that reads a code entry, with the first two risks of the list below. It cannot reach an
admin-set entry: row 15 keeps those away from the `chat` role, because such a tool can write.

Under the `profile` role the broker has the fact: the task credential names the tier and the
trigger. So row 18 can refuse, and it proposes to refuse in the first release. A child task of
such a run has no trigger in its credential; the profile rule of row 17 covers it.

The cost of that refusal: the alert triage of #2002 starts from an alert, so its run is
unattended and has a trigger by construction. With the refusal its own run reads cluster state
only, and a read of the alert's source is for a person who follows up in chat. Alert triage and
the Datadog reads are the first two requests of this work, so row 18 decides how they meet.

A later opening, if the decider wants it, has these bounds:

- An opt-in of the admin that admits one named alert sender, by its trigger name, to one named
  external server.
- Read tools only, from a list that can be shorter than the list of the entry.
- A target: one cluster from the sender's own list. No change request, so no pull request.
- A bound on the calls of one run.
- The same redaction and the same record as each other call.

The risks that stay with it:

- The text of an alert is not trusted. It can steer which reads the run makes inside the tool
  list, and what goes into a tool argument.
- The report of the run can show data of the source in a chat channel to persons with no access to
  the source.
- A party with the sender's secret can start such runs.

Our recommendation for the first release is the refusal. Between a later opening and no later
opening we give no recommendation yet. What would change the refusal: the decision on the
inbound alert design, a measure of what the triage loses with no data of the source, and a bound
on tool arguments, which this design does not have.

## The open fact

The five vendor pages that we read do not say how the first vendor server answers on the wire, and
no reader of this design sent it a request. The pages name the transport "Streamable HTTP" and
speak of a session initialization. Four things are not known:

1. Does it answer a POST that accepts only JSON with one JSON message, or with an event stream?
2. Does it issue a session id and want it on each later call?
3. Does its `tools/list` have pages? The relay drops the cursor today. Row 36 serves the list of a
   code entry from the entry, so this answer shows only what an admin-set entry can need.
4. Which status does it give for a bad token and at the rate limit?

A probe with a token of a test account answers them. It is one read-only step before the code.

- **If the answers are "one JSON message, no session":** row 12 holds as written. The relay change
  is small.
- **If not:** the relay adds, for an external entry only, a bounded read of one event-stream reply
  into one message, a session id with a shape check that the broker keeps for the caller, and, for
  an admin-set entry, the cursor of `tools/list` (row 36 serves the list of a code entry from the
  entry). Each is one more row for the decider, because the relay leaves these out
  on purpose today (`UPSTREAM_NOT_JSON`, the comment "The remote MCP relay", `filter_tools_list`).

The same four things are not known for a server that an owner hosts. The guide names the subset
that the relay supports.

## What a caller gains

By row 15, the `chat` role, which has two Google servers today, gets each tool of each enabled
code entry. With the first choice of row 17, the named profile of a code entry, which has one
Google server with two tools today, gets the tools of that entry. Each gets reads of a whole
account of another system, under the one identity of the entry. Neither gets a write: a code
entry has read tools only, and an admin-set entry has no caller in the first release (rows 15 and
29). Text that comes back in a result adds no write, but it can drive each read and write that the
caller has, the new reads also.

## What a prompt injection or a wrong configuration can try

| Attempt                                                                                        | Stopped by                                                                                                                                                     |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Call a host that no entry names                                                                | The host comes from the entry. The caller gives a server name only                                                                                             |
| Follow a redirect to another host                                                              | The relay follows no redirect                                                                                                                                  |
| Send its own credential                                                                        | Caller headers are dropped, but the one of row 9                                                                                                               |
| Read the credential of the server from a reply that repeats it                                 | Row 22, the exact value                                                                                                                                        |
| Call a tool that is not in the list                                                            | The tool rule of the entry                                                                                                                                     |
| Change the query of the server URL                                                             | The query is a part of the entry. The caller cannot set it                                                                                                     |
| Use a method outside the five                                                                  | The method rule of today                                                                                                                                       |
| Start a browser sign-in after a 401                                                            | The relay turns the 401 into its own answer                                                                                                                    |
| A session pod or the sandbox calls the server                                                  | The role list of the route does not change. This holds where the broker gives each caller a role                                                               |
| A run that a trigger started, or a child task of it, calls the server under the `profile` role | Rows 17 and 18                                                                                                                                                 |
| A very large or very slow reply, or many calls at one time                                     | The bounds of today for each call, and the limit on calls in flight of row 33                                                                                  |
| An admin-set URL with an IP literal, a user part, or a key in the query                        | Row 5                                                                                                                                                          |
| An admin-set name that resolves to a loopback, link-local or metadata address                  | Row 6. Behind a proxy (row 21) the broker checks no address, and only the rules of the proxy stop it                                                           |
| An admin-set entry that takes the name of a Google server or of a server of the image          | Row 5                                                                                                                                                          |
| An admin-set header name that changes the request                                              | Row 9                                                                                                                                                          |
| An admin-set entry for the Kubernetes API server or for a Service or a Pod of the cluster      | Rows 6 and 7. Behind a proxy (row 21) the broker checks no address, and only the rules of the proxy stop it                                                    |
| An admin-set tool that writes, under a read name or under its own name                         | No caller in the first release (rows 15 and 29). After that, the approval path of #1724 is the control; the broker still cannot know what a tool does (row 14) |
| The text of an alert drives a write of an admin-set tool in a run with no person               | Rows 15 and 29: no caller in the first release                                                                                                                 |
| A pull request of the agent that changes an entry, where the PlatformAgent is in a repository  | Not stopped by the broker. The merge by a person is the control                                                                                                |
| Cluster data, for example a Secret value, in a tool argument                                   | Not stopped. No rule redacts an argument, and no record holds it                                                                                               |
| Text in a tool result that reads as an instruction                                             | Not stopped by the relay. See [What a caller gains](#what-a-caller-gains)                                                                                      |
| Text in a tool description of `tools/list`, or in the result of `initialize`                   | Stopped for a code entry (row 36). Not stopped for an admin-set entry                                                                                          |

Residuals, stated:

- The broker can open a connection to each host on the internet and to each private address
  outside the addresses of rows 6 and 7. Behind a proxy (row 21) the broker checks no address.
  Beyond that, only the entries limit the relay.
- Under the `chat` role, a run with no person can call a code entry (rows 15 and 16): a job that
  the agent makes at run time, and the alert triage of today.
- One credential reads the whole account. A log read can also return lines that this install sent
  to that system. The admin limits both on the side of the server.
- The arguments of each call leave the cluster as the model wrote them. Tool results go to the
  model provider, with no redaction unless the admin turns it on.
- The server records each call under the one service identity. Only the broker's log has the
  caller.
- The agent can read each entry of the PlatformAgent, but not the Secret.
- A run at a tier that an install adds in unattended mode passes the tier check of row 18.
- For an admin-set entry: no read-only control of ours; no query that we reviewed; no token shape;
  a header name, a port and a CA that the admin gives; no eval; no check of the transport; no
  tool description that we reviewed.

## The worked example: Datadog

Vendor facts are from the vendor's pages, read on 2026-11-26
([overview](https://docs.datadoghq.com/mcp_server/),
[setup](https://docs.datadoghq.com/mcp_server/setup/),
[tools](https://docs.datadoghq.com/mcp_server/tools/),
[MCP access](https://docs.datadoghq.com/account_management/rbac/mcp_access/),
[service access tokens](https://docs.datadoghq.com/account_management/service-access-tokens/)).
The vendor says that the server is "under significant development".

| Piece         | For Datadog                                                                                                                                                                                                                                                                                                                                                                       |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Form          | A code entry with the name `datadog`                                                                                                                                                                                                                                                                                                                                              |
| Sites         | A closed list of the seven sites that have a server: US1, US3, US5, EU1, AP1, AP2, UK1. The value is the site domain, for example `datadoghq.eu`. The two government sites have no server                                                                                                                                                                                         |
| Host and path | `mcp.<site>`, path `/v1/mcp`. The path of #1989 (`/api/unstable/...`) is not on the vendor's page                                                                                                                                                                                                                                                                                 |
| Credential    | One Service Access Token of a Datadog service account, sent as `Authorization: Bearer`. The vendor's page says: "Pass the token as a bearer token in the Authorization header. No API key is required." This answers the first open question of #1989 for the kind of credential                                                                                                  |
| Fixed query   | An explicit `toolsets` list and an `omit_tools` list. The default toolset is not read-only: it holds tools that write notebooks and run code                                                                                                                                                                                                                                      |
| Read tools    | The first list is for the acceptance criteria of #1989: search monitors, search and analyze logs, search and get incidents, and the metric, event, host and trace reads that an investigation needs. The code change reads a live `tools/list`, writes the date in a comment, as the Google lists do, and names each tool with its reviewed description and input schema (row 36) |
| Vendor-side   | Steps of the guide: the organization setting "MCP Write Access" stays off (its default); the service account's role has the read permission of the MCP server and not the write permission; the token has the fewest scopes                                                                                                                                                       |
| Rate limit    | "a fair-use burst limit of 50 tool calls per 10 seconds". The page does not say what the server answers at the limit                                                                                                                                                                                                                                                              |
| Rotation      | A token's expiry cannot change. To rotate: make a new token, update the Secret, revoke the old token                                                                                                                                                                                                                                                                              |

The acceptance criteria of #1989 against this design: (1) the site setting and the Secret in the
broker only are rows 3, 10 and 26; (2) a read of a monitor and a log query with no key in the
agent's container are rows 9, 13 and 15; (3) "write tools are unavailable" is row 13. The words
"unless explicitly enabled" wait for #1724.

Where this design differs from #1989: the issue asks for a mock or recorded endpoint and for an
eval check that the agent redacts fields. Row 34 proposes a real test account first, and row 23
gives no redaction on a default install.

The second open question of #1989 asks what the agent does when Datadog and Cloud Monitoring
disagree. That is text for the agent, not for the broker. It goes to the skill work with one
direction: report both signals with their sources, and do not choose one in silence.

## What the other servers need

| Server                           | Recorded in                                                  | Form and what it needs                                                                                                                                                                                                                                                                                                                                                                                                                                                               | State of the facts                                                      |
| -------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| Wiz                              | [#6340](https://github.com/gke-labs/kube-agents/issues/6340) | A code entry. Probably a second credential mode: a token that the broker gets with a client id and a client secret. The entry form of row 1 has the place for it; the mode itself is later work                                                                                                                                                                                                                                                                                      | Not confirmed. The vendor's setup pages need a sign-in of a Wiz account |
| A server for clusters on AWS     | No issue yet                                                 | If the owner hosts the server itself: an admin-set entry with a static header, if that server takes one; it has no caller in the first release (rows 15 and 29). If it is the server that AWS hosts: each request is signed, which is a third credential mode and its own design                                                                                                                                                                                                     | The tools and the sign-in are to confirm with the owner                 |
| A server for cost data (CAST AI) | No issue yet                                                 | The vendor's newer hosted server ([its page](https://docs.cast.ai/docs/castpilot-mcp)) takes a bearer key and says that it is read-only: the static header mode fits. Its page shows a session, so the transport is the second branch of [The open fact](#the-open-fact) until a probe says no. The vendor's older server needs two headers and is not read-only: it does not fit. A server that the owner runs: an admin-set entry (no caller in the first release, rows 15 and 29) | Vendor pages read on 2026-11-26; which server is meant is to confirm    |

So the mechanism is proven on paper for the mode "static header" only. Point 3 of #7242 names
"External / in-cluster MCP server URLs with clean credential-proxy secret injection and
NetworkPolicy egress rules". The admin-set form answers the URL and the Secret part, for an
external URL and for a private address that is not a Service or Pod address of the cluster, with
`https`. Row 7 refuses a server at a Service or Pod address of the same cluster. Rows 20 and 26
decline the NetworkPolicy rule and the AgentProfile field. A server with plain `http` stays out
(row 5).

With rows 15 and 29, a server that the owner of an install hosts has no caller in the first
release. It
waits for the approval path of #1724. A code entry does not wait.

## The other pieces of the integrations epic

[#1994](https://github.com/gke-labs/kube-agents/issues/1994) lists six shared pieces. #1989 lists
four of them and adds the docs and a smoke test. The two others are the inbound path, which is the
design of #2002, and the image: the epic asks for the `mcp` extra of Hermes. With option A the
broker holds the connection, and Hermes sees the same local bridge as for the Google servers. So
the image needs no new package.

## Proof

- **Unit tests** of the relay over a real socket with a fake upstream, in the form of
  `agents/platform/scripts/test_mcp_relay.py`: a listed tool passes; each other tool is refused;
  the key of an entry goes to the host of that entry only; the Google token goes to Google hosts
  only; the fixed query cannot change; the broker answers the handshake itself when the switch is
  off; a reply that repeats the key comes back without it; each check of rows 5, 6, 7 and 9; the
  limit of row 33; the reviewed text of row 36; the refusal of each caller role on an admin-set
  entry (row 15).
- **Conformance rows**, each with a mutation that must go red: a code entry offers read tools
  only; the broker refuses each other tool; in the manifests that the operator renders, only the
  broker's pod mounts the Secret; no config points at an external host; an admin-set entry cannot
  take the name or the host of a code entry; the shipped jobs keep no external server; the operator
  refuses a volume or an env reference for the Secret (row 10); the rows of the `profile` role and
  the refusals of row 18, if row 17 takes its first choice.
- **An eval case**, `mcp-datadog-query-monitors-and-logs` (the name is from #1989), that passes
  three times, on the lane of row 34.
- **One live check** on a test bed with a vendor test account. When an admin-set entry has a
  caller, one more with such an entry that points at a test server that is not at a Service or
  Pod address of the cluster of the test bed (row 7).

## What stays out

- First-class proxy fields for each pod and an install-wide CA bundle: points 1 and 2 of #7242.
  The proxy variables for the A2A gateway are #6227 and its pull request
  [#7386](https://github.com/gke-labs/kube-agents/pull/7386).
- Write tools and the approval path (#1724).
- A sign-in for each person (D12 stays).
- The inbound alert endpoint (#2002), and the install's own MCP server (#5037).
- A server with plain `http`, and a server that takes its key only in the query.
- An MCP server at a Service or Pod address of the install's cluster (row 7).
- A caller for an admin-set entry before #1724 (rows 15 and 29).
- The credential modes "signed request" and "token from a client secret".
- Session pods on the relay, and a host allowlist for the broker.
- The text of a skill for one system.

## Order of the work

1. This design, and the probe of [The open fact](#the-open-fact).
2. The relay change in Python: the entry form, the host rule, the credential mode, the limit
   of row 33, the served text of row 36 and the Datadog entry, with its unit tests and conformance
   rows. It takes its turn on `credential_proxy.py`
   after the open pull requests of #6526 on that file, and it keeps its part in that file small.
3. In parallel, the operator and chart change in Go: the field, the Secret mount, the
   configuration key.
4. The agent-side entry, the job lists of row 16, the skill text, the eval case, the guide and the
   live check for Datadog. This step closes #1989.
5. The admin-set form: the CRD part, the checks of rows 5 to 9, its conformance rows and its
   guide. It gets a caller, and the path of row 29, after #1724 gives an approval path (row 15).
   When it has a caller, this step answers the URL and the Secret part of point 3 of #7242, but
   not the case of a server at a Service or Pod address of the same cluster (row 7).
6. The row of the `profile` role, as its own pull request, after the answer on row 17.

## Done when

- The decider answered each row of [Decisions](#decisions), and this document records each answer
  with its date and link.
- The probe of [The open fact](#the-open-fact) ran, and row 12 names the branch.
- Each code change that follows has the proof of [Proof](#proof) for its part.
- The site page `reference/credential-isolation.md`, the table "What else leaves the cluster" of
  `reference/security-and-iam.md` and a guide under `install/` name the external servers, the
  Secret, what leaves the cluster, and what the admin accepts with an admin-set entry.
- The part "Remote MCP servers" of
  [`credential-isolation-design.md`](../credential-isolation-design.md) states what is built.

## Checked, and not checked

Checked in the code on `main` at `a6b4bcbe7`: each line of [What exists today](#what-exists-today)
and each name in a "Where" cell; the counts (2 servers, 15 and 3 read tools, 5 methods, 11 caller
roles, 2 roles on the relay); that the relay reads no proxy variable; that the persona format
refuses `url:`; that the overlay of one Hermes profile merges a mapping key; that a scheduled job
with no `enabled_toolsets` list gets the toolsets of its profile.

Checked on GitHub on 2026-11-26: the issues #1989, #7242, #6227, #5037, #1720, #1724, #1994,
#2002, #6340, #3698 and the epic #6526; that the pull requests #6820, #7386 and #7406 are open;
that question 4 of the broker surface design, row 13 of the hooks design and question 3 of the
telemetry design have no answer on record.

Not checked:

- How the Datadog server answers on the wire ([The open fact](#the-open-fact)).
- The fewest scopes of a Datadog Service Access Token for these reads, and if the `toolsets` and
  `omit_tools` query applies to a connection with a token.
- The exact read tool names against a live `tools/list`.
- Each Wiz fact. The tools and the sign-in of a server that an owner hosts.
- That an `mcp_servers` entry from the overlay of one profile starts a working bridge (row 29).
- That a job whose list has `no_mcp` and a server name keeps that server (row 16); no shipped job
  has this form.
- What Hermes does at connect with a server entry whose handshake the broker answers itself (row
  28), and what its bridge shows the model for an HTTP 502 or 503 of the relay.
- The full text of D12.
- How the broker learns the Service and Pod ranges of the cluster and the address of its API
  server (row 7).
