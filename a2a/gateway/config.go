package gateway

import (
	"crypto/hkdf"
	"crypto/sha256"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// attributionSaltInfo is the HKDF info string that binds the derived
// fallback salt to this one use of the bus password, so the same password
// expanded for any other purpose yields unrelated bytes. It is a wire
// constant in the sense that changing it re-salts every pseudonym on an
// install running the fallback; do not edit it to tidy the string.
const attributionSaltInfo = "a2a-attribution-salt"

// attributionSaltLen is how many bytes the derived fallback salt gets: one
// SHA-256 output, the length the digest it replaces produced, so the HMAC
// keying in principal.go sees the same shape it always did.
const attributionSaltLen = 32

// defaultMaxSessions is what MaxSessions means when unset; the field's
// comment carries the sizing rationale.
const defaultMaxSessions = 10

// defaultDelegationDepthMax is what DelegationDepthMax means when unset; the
// field's comment carries the rationale.
const defaultDelegationDepthMax = 3

// defaultGchatTokenPath is where the operator projects the gateway's
// relay-audience ServiceAccount token when the gchat backend is armed.
const defaultGchatTokenPath = "/var/run/secrets/a2a-chat-relay/token"

// envGchatHomeChannel carries the Chat home space the chat.notify route posts
// to (notify.go); the operator renders it from googleChat.homeChannel.
const envGchatHomeChannel = "A2A_GCHAT_HOME_CHANNEL"

// envSlackHomeChannel carries the Slack home channel's id the chat.notify
// route posts to; the operator renders it from slack.homeChannel.
const envSlackHomeChannel = "A2A_SLACK_HOME_CHANNEL"

// defaultInjectPrincipalMapPath is where the operator mounts the inject
// door's own principal map when the door is armed. A file of "id principal"
// lines rather than a directory of one file per id, because every key carries
// the inject: prefix and a colon is not a legal ConfigMap key.
const defaultInjectPrincipalMapPath = "/etc/a2a/inject-principal-map/principals"

// The session-pod image when A2A_WORKER_IMAGE is unset, which is only a
// gateway run outside the operator: the operator renders that env from its
// own resolution (a2aWorkerImage in platformagent_a2a_manifests.go), and
// hack/check-image-inventory.sh holds this repository to images.json's
// a2a-worker entry.
const (
	defaultWorkerRepository = "ghcr.io/gke-labs/kube-agents/a2a-worker"
	defaultWorkerTag        = "latest"
)

// defaultA2ADoorPrincipalMapPath is the same for the A2A door's map: its own
// file, keys prefixed a2a:, for the reason the inject door's is.
const defaultA2ADoorPrincipalMapPath = "/etc/a2a/a2a-door-principal-map/principals"

// metricsPortEnv names the metrics listener's port (see MetricsPath in
// metrics.go). Unset or empty means no listener, the broker's rule for
// CREDENTIAL_PROXY_METRICS_PORT; metricsPortMin and metricsPortMax are the
// range a set value has to fall in.
const (
	metricsPortEnv = "A2A_METRICS_PORT"
	metricsPortMin = 1
	metricsPortMax = 65535
)

// The display-mode values, matching the GoogleChatSpec.Mode enum.
const (
	displayModeDefault = "default"
	displayModeDebug   = "debug"
)

// defaultTaskDeadline is what TaskDeadline means when unset — the worker
// adapter's own default (a2a/cmd/worker-adapter: A2A_TASK_DEADLINE_SECONDS,
// 1800s), restated here because the two halves of one contract must agree.
const defaultTaskDeadline = 30 * time.Minute

// defaultAskTTL is what AskTTL means when unset; the field's comment carries
// the horizon rationale.
const defaultAskTTL = 24 * time.Hour

// defaultSessionTTL is what SessionTTL means when unset (7 days, sitting
// comfortably beyond TASKS stream's 72h retention).
const defaultSessionTTL = 7 * 24 * time.Hour

// defaultFirstEventGrace is what FirstEventGrace means when unset; the
// field's comment carries the sizing rationale.
const defaultFirstEventGrace = 10 * time.Minute

// Config is the gateway's runtime configuration. The env contract matches
// what the W6 operator renders onto the a2a-gateway Deployment; everything
// else has playground defaults.
type Config struct {
	NATSURL      string
	NATSUser     string
	NATSPassword string
	DiscordToken string

	// SlackBotToken and SlackAppToken arm the Slack backend: Socket Mode
	// needs both (xoxb- drives the Web API, xapp- the outbound websocket —
	// no inbound endpoint, nothing to expose). Exactly one backend may be
	// configured per gateway process: two gateways bound to one relay
	// durable split event deliveries (Options.RelayDurable), so a second
	// backend is a second Deployment with its own durable, not a second
	// adapter here.
	SlackBotToken string
	SlackAppToken string

	// PrincipalMapPath is the mounted principal map — Discord's test
	// ConfigMap or Slack's admin-owned Secret; same on-disk shape either way.
	PrincipalMapPath string

	// GchatRelayURL is the credential proxy's relay base URL — the gchat
	// backend's transport. Setting it selects the Google Chat adapter.
	GchatRelayURL string
	// GchatTokenPath is the projected ServiceAccount token (a2a-chat audience)
	// the adapter authenticates to the relay with.
	GchatTokenPath string
	// GchatAllowedUsers is the ingress allowlist for the gchat backend —
	// the same gate the legacy path enforces as GOOGLE_CHAT_ALLOWED_USERS.
	// gchat has no mapping table (the Google-asserted email IS the
	// principal), so the allowlist is the whole verification config.
	GchatAllowedUsers []string
	// GchatAllowAllUsers disables the allowlist, stated explicitly —
	// mirroring the legacy GOOGLE_CHAT_ALLOW_ALL_USERS posture.
	GchatAllowAllUsers bool
	// GchatHomeChannel is the install's Chat home space ("spaces/AAA"), where
	// a chat.notify proactive post lands (notify.go). Empty leaves the route
	// serving conversation requests only.
	GchatHomeChannel string
	// SlackHomeChannel is the install's Slack home channel id ("C0123"),
	// the one place a Slack chat.notify post may land (notify.go). Empty
	// leaves the Slack notify route unarmed.
	SlackHomeChannel string
	// SlackAllowedUsers is the Slack backend's ingress allowlist, carried
	// from spec.integration.slack.allowedUsers the way GchatAllowedUsers is
	// from Chat's: the gate the legacy path enforces as SLACK_ALLOWED_USERS.
	// It is the only admission gate (beside the gateway's refusal of another
	// workspace's member); Slack's mapping table (PrincipalMapPath) is an
	// optional override that attributes a listed sender by an IdP identity.
	// Member ids compare exactly.
	SlackAllowedUsers []string
	// SlackAllowAllUsers disables the Slack allowlist, stated explicitly -
	// mirroring the legacy SLACK_ALLOW_ALL_USERS posture. Another
	// workspace's member is still refused.
	SlackAllowAllUsers bool

	// TargetAllowedUsers is the per-target, per-backend trusted-human
	// allowlist a session's delegation is checked against: target ->
	// backend -> ids in that backend's vocabulary. Only "platform" is
	// populated from env today (EnvTargetAllowedUsersGchat/Slack); an absent
	// pair means all authenticated users, and a present pair whose list is
	// empty means nobody. The A2A door's backend ("a2a") is the exception:
	// no pair for it means nobody (doorUnlisted). See allowlist.go.
	TargetAllowedUsers map[string]map[string][]string

	// InjectListen is the inject side door's HTTP listen address, and setting
	// it arms the door. DEV AND EVAL ONLY. The door is not a backend in the
	// one-backend guard's sense (see FromEnv): it may sit beside exactly one
	// real backend, because a local HTTP door has no silent-stop failure mode
	// of the kind that guard exists for. The operator renders it only under
	// its eval flag.
	InjectListen string

	// InjectToken is the bearer token the door requires on every request
	// (A2A_INJECT_TOKEN), and there is no unauthenticated mode: FromEnv
	// refuses a listen address without one.
	//
	// The NetworkPolicy edge is not a substitute, and that is why this
	// exists. The fence governs pod-network traffic; the eval runner reaches
	// the Service through `kubectl port-forward`, which enters from the node
	// and is exempt -- so without a token the population that can drive the
	// platform persona widens from the holders of the agent's own API key to
	// anyone holding pods/portforward in the namespace.
	InjectToken string

	// InjectPrincipalMapPath is the door's OWN principal map
	// (A2A_INJECT_PRINCIPAL_MAP), separate from PrincipalMapPath and never a
	// fallback to it.
	//
	// Separate because the door takes its principal from a request body. A
	// map of its own, whose every key carries the inject: prefix and whose
	// every value is an eval-only identity (injectPrincipalPrefix and
	// injectEvalPrincipalPrefix in inject.go), is what keeps the door
	// structurally incapable of asserting a principal a real backend's
	// sender could hold -- the property the Discord mapping table has and
	// the reason the gateway spec calls that table a feature.
	InjectPrincipalMapPath string

	// A2ADoorListen is the A2A door's HTTP listen address (A2A_DOOR_LISTEN),
	// and setting it arms the door. Like the inject door it is a side door,
	// not a backend: it can be armed beside a real backend or alone, and it
	// is not counted by the one-real-backend guard because it consumes
	// nothing and competes for nothing. See a2a/gateway/a2adoor.go.
	A2ADoorListen string

	// A2ADoorToken is the bearer token the door requires on every RPC
	// request (A2A_DOOR_TOKEN); the agent card is the one unauthenticated
	// route. No unauthenticated mode for the RPCs, for the inject door's
	// reason: the port-forward path is served from inside the pod, past
	// the NetworkPolicy.
	A2ADoorToken string

	// A2ADoorPrincipalMapPath is the door's OWN principal map
	// (A2A_DOOR_PRINCIPAL_MAP): keys prefixed a2a:, values eval-only
	// identities, never a fallback to the chat map or the inject map. The
	// developer identity class (ID tokens) arrives as a second resolver
	// beside this file, not as an entry in it.
	A2ADoorPrincipalMapPath string

	// A2ADoorPublicURL is the URL the agent card advertises for the door's
	// JSON-RPC endpoint (A2A_DOOR_PUBLIC_URL): what a client reaches it at,
	// which behind a port-forward or an ingress is not the listen address.
	// Empty makes the card advertise the address it was fetched from.
	A2ADoorPublicURL string

	// MetricsPort is the metrics-only listener's port (A2A_METRICS_PORT);
	// zero means no listener. It binds every interface, because its caller
	// is the managed-Prometheus collector on the pod network, and serves
	// MetricsPath and nothing else. It may not be either door's port: a door
	// that lost its port to this listener would fail to bind, and a door
	// that won it would be what the collector's NetworkPolicy rule admits.
	MetricsPort int

	// DisplayMode is the existing Chat integration's default-vs-debug split
	// (GoogleChatSpec.Mode), honoured by this relay rather than reinvented:
	// under "default" the rolling line carries the state but never the
	// turn-by-turn narration; "debug" is the gateway's historical verbose
	// behaviour and the value an unset env resolves to, so installs that
	// predate the knob render exactly as before.
	DisplayMode string

	// DefaultAddressee is where every conversation's tasks route until a
	// per-conversation override says otherwise. Retarget 8/26: the first
	// shipped configuration routes everything to "platform" (the W7 bridge
	// executes) and spawns no session pods — the W4 switch is this setting,
	// not surgery.
	DefaultAddressee string

	// SpawnSessions arms the session-pod path (spawn/rehydrate/sweep with
	// client-go). The gateway pod now always mounts a service-account token
	// (it needs one to create pods at all), so this is a rollout switch
	// rather than a capability one: off, the gateway routes every task to
	// DefaultAddressee and creates nothing. The k8s client is still built
	// lazily so that an install with it off never depends on the RBAC.
	SpawnSessions bool

	// IdleTTL is the reap threshold since the session's last activity: a
	// verified turn, or an executor ending a live task (decided 8/24: 30
	// minutes, config-backed; the task's end counts since 2026-09-30).
	IdleTTL time.Duration

	// AttributionSalt keys the HMAC pseudonyms in authority blocks. The
	// salt is SESSION_KV_SALT, the one the install already provisions into
	// platform-agent-secrets (settled 8/31, spec-chatops-gateway.md): the
	// shipped attribution path hashes session metadata with it, so hashing
	// with anything else silently breaks the cross-surface audit join this
	// pseudonym exists to preserve — one human, one value, on the bus and
	// in session metadata. The env-var fallbacks below are playground
	// posture for installs without that Secret, and the derived one
	// (HKDF-SHA-256 over the bus password) is a recorded deviation on two
	// counts: the broken join, and a de-anonymization key handed to whoever
	// holds the bus password. HKDF is the construction a credential is
	// permitted to pass through, and that is all it is: it answers neither
	// count, and it is not a password hash — no work factor, so it does not
	// make a weak hand-set NATS_PASSWORD any harder to guess from a leaked
	// salt. Provisioning the Secret is what fixes that.
	AttributionSalt []byte

	// TaskDeadline mirrors the worker adapter's task deadline — the SAME
	// env the adapter reads (A2A_TASK_DEADLINE_SECONDS, integer seconds),
	// because the spawner renders it onto the worker pod alongside the
	// pod-level activeDeadlineSeconds it sizes above it, and two knobs for
	// one contract would drift. Unset means 1800s, the adapter's own
	// default. The adapter kills the harness and publishes the terminal at
	// this deadline; the pod deadline (this plus a fixed grace) is the
	// backstop that hands a wedged ADAPTER to Sweep instead of letting it
	// hold its bus credential indefinitely. Raising it buys longer tasks at
	// the price of how long a wedged worker can hold a cap slot; lowering
	// it turns long-running asks into failed tasks sooner.
	TaskDeadline time.Duration

	// AskTTL bounds the copies of a turn that session-state keeps past the
	// bus (A2A_ASK_TTL): the active task's `ask` copy, and each task history
	// entry's requester (backend and pseudonymized subject) and attribution,
	// aged by the entry's StartedAt. The ask copy's stated justification —
	// the same text rides the W-bounded stream and the copy dies at the
	// terminal event — holds only where a terminal event is guaranteed, and
	// the spec names the case where it is not (a wedged adapter, until every
	// pod carries its deadline; fixed-route executors have no janitor until
	// stage 3). So the record gets an independent bound: the reap scan
	// clears an ask, and a history entry's requester and attribution, once
	// they are this old (exactly this old included), leaving the task record
	// and the history entry themselves intact. Unset means 24h — far above
	// any legitimate task's runtime, well under the stream's 72h retention,
	// so the KV copies always have the shorter horizon the content posture
	// claims. Raising it toward the stream retention erodes exactly that
	// claim; lowering it trims how long a status card can echo the ask, and
	// how long after a turn a child task can still be minted on its behalf:
	// past the TTL the entry has no requester to check, so a delegation from
	// it is refused.
	AskTTL time.Duration

	// SessionTTL bounds the lifetime of idle session records in session-state
	// (A2A_SESSION_TTL). The session record holds contextId across pod
	// incarnations. Once a session has no active pod and has seen no activity
	// for longer than SessionTTL, its record is deleted from KV (leaving a
	// ~100-byte tombstone marker under the bucket's --history=1 limit),
	// bounding session-state growth to a marker per conversation rather than
	// accumulating multi-KB session records, rosters, and task histories
	// (including sessions with stale or abandoned active tasks whose executors
	// never completed, while preserving tasks actively running within
	// TaskDeadline). Unset means 7 days (168h), sitting comfortably past
	// TASKS' 72h retention horizon.
	SessionTTL time.Duration

	// FirstEventGrace bounds how long an active task with NOTHING on its
	// events subject may hold a conversation's serialization
	// (A2A_FIRST_EVENT_GRACE). Every other bound assumes a pod: the adapter's
	// deadline runs from task start inside the worker, the pod deadline from
	// pod start, and Sweep watches pod phases — so a task whose executor
	// never came up (a spawn that never happened, a bus that dropped between
	// the two publishes, a gateway restart mid-turn) has no events for the
	// heal in handleInbound to see a terminal in, and the record steers every
	// later message into it. Past this grace the heal treats "no events" as
	// "never started" and releases the serialization; it publishes no
	// terminal for the task, because age alone is not evidence. Unset
	// means 10 minutes: the spec's cold start is 5-10s and the pod deadline's
	// pre-start budget (podDeadlineGrace, the image pull before the process
	// starts) is 10 minutes, so a task still legitimately pre-first-event at
	// this age is a pod that will not be coming up. Lowering it risks
	// releasing a slow-starting worker's task out from under it — the next
	// turn then starts a second task while the first may still emit;
	// raising it is how long a user waits before the conversation answers
	// again. Values under 1m are refused at boot.
	FirstEventGrace time.Duration

	// OwnerDeployment names the gateway's own Deployment
	// (A2A_OWNER_DEPLOYMENT; the operator renders its own render's name).
	// When set, every spawned session pod carries an ownerReference to it,
	// so Kubernetes GC reaps sessions when the Deployment goes — cleanupA2A
	// deleting the gateway, or any other deletion — with no operator
	// exception to its IsControlledBy refusal. Empty (playground) spawns
	// unowned pods, the pre-S9 posture.
	OwnerDeployment string

	// Namespace and WorkerImage configure the dark spawn path.
	Namespace   string
	WorkerImage string

	// SessionServiceAccount is the ServiceAccount every session pod runs
	// as, rendered by the operator as <agent>-a2a-session and passed here
	// so the two cannot disagree. It carries no RBAC; its only purpose is
	// to be the identity the kubelet mints the pod-bound bus token against,
	// and the identity the callout's map is keyed on.
	//
	// There is no default. A wrong or absent name spawns pods whose token
	// the callout has no entry for, which fails as every session refused at
	// connect — a boot-time refusal here is the same information, hours
	// earlier and in one place.
	SessionServiceAccount string

	// SessionClusterView gives spawned session pods the temporary read-only
	// cluster view: a projected token for the credential broker's session
	// audience, the broker's URL, and Bash in the worker. Rendered by the
	// operator under its A2A_SESSION_CLUSTER_VIEW flag; off, the pod is
	// exactly the inert one. CredentialProxyURL is where the shim dials;
	// New refuses the view without it.
	SessionClusterView bool
	CredentialProxyURL string

	// StrictEventsWriter makes the `…events` writer-class agreement check a
	// refusal instead of a counted advisory (A2A_STRICT_EVENTS_WRITER=true).
	// It ships false: for one TASKS retention window after an install takes
	// the supervisor subject split, the stream still holds supervisor
	// terminals written on `…events` before it, and refusing those folds
	// every recent task non-terminal. Flip it no earlier than one retention
	// window (72h at the dev default) after the split reaches the install.
	StrictEventsWriter bool

	// AuthorityTier and AuthorityScope are what the gateway mints a task's
	// root capability at: the ceiling for this agent, from which every hop
	// can only narrow.
	//
	// They come from the environment because there is nowhere better yet.
	// 02-agent-personas §9 puts `tier` and `scope` on the Agent CRD, and
	// that CRD does not exist — PlatformAgent carries neither, and inventing
	// them on it is an API change this card is not. The operator renders
	// neither — so the defaults below are what every rendered install runs
	// on, not a local-run convenience, and they are deliberately the
	// narrowest thing that could be true: the ceiling an operator has not
	// chosen must not be a generous one.
	AuthorityTier  capability.Tier
	AuthorityScope capability.Scope

	// CapabilityOptional relaxes exactly one thing: what happens when this
	// gateway cannot mint. Zero value — the safe one — refuses the turn.
	// Set, a mint failure logs and the envelope goes out with `grants: null`,
	// which is the pre-A3b shape, for an install whose bus has no `cap`
	// bucket yet. The executor has the matching knob and the operator renders
	// both from A2A_CAPABILITY_REQUIRED, so the two halves cannot drift.
	//
	// It is NOT a switch for enforcement. A capability that exists is always
	// checked, and no configuration makes a refused verb run.
	CapabilityOptional bool

	// MaxSessions caps how many session pods run concurrently, gateway-wide
	// (A2A_MAX_SESSIONS). "Delegate:" makes pod creation user-triggerable and
	// threads are free, so the principal map bounds WHO can spawn and this
	// bounds HOW MANY - without it, one mapped user's afternoon can fill the
	// namespace. At the cap a new delegation (or a session-routed first ask)
	// is refused with a chat reply naming the numbers; nothing queues,
	// nothing drops silently.
	//
	// Zero means 10, the harness spike's "busy day": at the worker shape's
	// requests (250m/512Mi) ten concurrent sessions hold 2.5 CPU / 5Gi, which
	// a small dev cluster absorbs without preemption. Raising it buys more
	// concurrent delegations at that per-pod price plus model-quota
	// contention; lowering it turns busy-hour delegations into refusals
	// sooner - a UX decision, not a safety one, because this cap is the
	// usability half. The enforcement half is the namespace ResourceQuota
	// the operator renders above this number (a compromised or buggy gateway
	// ignores its own cap and cannot ignore that one), which is also what
	// bounds the count-then-create race between concurrent conversations.
	MaxSessions int

	// DelegationDepthMax bounds how deep a delegation chain may run
	// (A2A_DELEGATION_DEPTH_MAX). A human turn is depth 0, a child its
	// parent's depth plus one, and a turn already at the bound may not
	// delegate again. One child at a time means the chain is a line, and
	// this bounds its length: a harness that delegates in a loop stops at
	// the bound instead of walking the session cap.
	//
	// Zero means 3. FromEnv refuses a value under 1 rather than clamping
	// it: 0 would be "delegation off", which is a different switch
	// (A2A_DELEGATE_TOOL on the worker side), not a typo to paper over.
	DelegationDepthMax int
}

// Backend names the REAL chat backend this config arms: "gchat", "slack",
// "discord", or "" when a side door (inject, A2A, or both) is the only way
// in. FromEnv
// refuses more than one real backend, so the order here only decides what a
// hand-built Config means.
//
// The doors are deliberately not among the answers. Either can be armed
// beside any one backend, so "which backend is this gateway" and "is a door
// open" are two questions, and collapsing them is what would make a door
// exclusive again.
func (c *Config) Backend() string {
	switch {
	case c.GchatRelayURL != "":
		return gchatBackend
	case c.SlackBotToken != "":
		return slackBackend
	case c.DiscordToken != "":
		return discordBackend
	case c.InjectListen != "" || c.A2ADoorListen != "":
		// No real backend: a door is the whole of the ingress, and the
		// attribution on a message that comes through it is the door's own.
		return ""
	default:
		// A hand-built Config with nothing armed at all. FromEnv refuses
		// this; a test that builds a Config directly gets the historical
		// default rather than an empty backend string in its authority
		// blocks.
		return discordBackend
	}
}

// InjectArmed reports whether the side door is open.
func (c *Config) InjectArmed() bool { return c.InjectListen != "" }

// A2ADoorArmed reports whether the A2A door is open.
func (c *Config) A2ADoorArmed() bool { return c.A2ADoorListen != "" }

// FromEnv loads the config from the environment.
func FromEnv() (*Config, error) {
	cfg := &Config{
		NATSURL:          os.Getenv("NATS_URL"),
		NATSUser:         os.Getenv("NATS_USER"),
		NATSPassword:     os.Getenv("NATS_PASSWORD"),
		DiscordToken:     os.Getenv("DISCORD_TOKEN"),
		SlackBotToken:    pyStrip(os.Getenv("SLACK_BOT_TOKEN")),
		SlackAppToken:    pyStrip(os.Getenv("SLACK_APP_TOKEN")),
		PrincipalMapPath: envOr("A2A_PRINCIPAL_MAP", "/etc/a2a/principal-map"),
		DefaultAddressee: envOr("A2A_DEFAULT_ADDRESSEE", "platform"),
		SpawnSessions:    os.Getenv("A2A_SPAWN_SESSIONS") == "true",
		Namespace:        envOr("POD_NAMESPACE", "kubeagents-system"),
		WorkerImage:      envOr("A2A_WORKER_IMAGE", defaultWorkerRepository+":"+defaultWorkerTag),

		SessionServiceAccount: os.Getenv("A2A_SESSION_SERVICE_ACCOUNT"),
		StrictEventsWriter:    os.Getenv("A2A_STRICT_EVENTS_WRITER") == "true",
		SessionClusterView:    os.Getenv("A2A_SESSION_CLUSTER_VIEW") == "true",
		CredentialProxyURL:    os.Getenv("A2A_CREDENTIAL_PROXY_URL"),
		AuthorityTier:         capability.Tier(envOr("A2A_AUTHORITY_TIER", string(capability.TierDeveloperTeam))),
		AuthorityScope:        capability.Scope(os.Getenv("A2A_AUTHORITY_SCOPE")),
		CapabilityOptional:    capability.OptionalFromEnv(),
	}
	cfg.GchatRelayURL = os.Getenv("A2A_GCHAT_RELAY_URL")
	cfg.GchatTokenPath = envOr("A2A_GCHAT_TOKEN_PATH", defaultGchatTokenPath)
	for _, u := range strings.Split(os.Getenv("A2A_GCHAT_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.GchatAllowedUsers = append(cfg.GchatAllowedUsers, u)
		}
	}
	cfg.GchatAllowAllUsers = os.Getenv("A2A_GCHAT_ALLOW_ALL_USERS") == "true"
	cfg.GchatHomeChannel = strings.TrimSpace(os.Getenv(envGchatHomeChannel))
	cfg.SlackHomeChannel = strings.TrimSpace(os.Getenv(envSlackHomeChannel))
	cfg.TargetAllowedUsers = map[string]map[string][]string{}
	platformLists := map[string][]string{}
	// Set is a list, even set empty: the operator renders the var empty for
	// a CR list of blanks, which admits nobody. Unset is no list.
	if raw, ok := os.LookupEnv(EnvTargetAllowedUsersGchat); ok {
		platformLists[gchatBackend] = append([]string{}, splitList(raw)...)
	}
	if raw, ok := os.LookupEnv(EnvTargetAllowedUsersSlack); ok {
		platformLists[slackBackend] = append([]string{}, splitList(raw)...)
	}
	if len(platformLists) > 0 {
		cfg.TargetAllowedUsers[targetPlatform] = platformLists
	}
	for _, u := range strings.Split(os.Getenv("A2A_SLACK_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.SlackAllowedUsers = append(cfg.SlackAllowedUsers, u)
		}
	}
	cfg.SlackAllowAllUsers = os.Getenv("A2A_SLACK_ALLOW_ALL_USERS") == "true"
	cfg.InjectListen = strings.TrimSpace(os.Getenv("A2A_INJECT_LISTEN"))
	cfg.InjectToken = strings.TrimSpace(os.Getenv("A2A_INJECT_TOKEN"))
	cfg.InjectPrincipalMapPath = envOr("A2A_INJECT_PRINCIPAL_MAP", defaultInjectPrincipalMapPath)
	cfg.A2ADoorListen = strings.TrimSpace(os.Getenv("A2A_DOOR_LISTEN"))
	cfg.A2ADoorToken = strings.TrimSpace(os.Getenv("A2A_DOOR_TOKEN"))
	cfg.A2ADoorPrincipalMapPath = envOr("A2A_DOOR_PRINCIPAL_MAP", defaultA2ADoorPrincipalMapPath)
	cfg.A2ADoorPublicURL = strings.TrimSpace(os.Getenv("A2A_DOOR_PUBLIC_URL"))
	metricsPort, err := metricsPortFromEnv(cfg)
	if err != nil {
		return nil, err
	}
	cfg.MetricsPort = metricsPort
	cfg.DisplayMode = envOr("A2A_CHAT_DISPLAY_MODE", displayModeDebug)
	if cfg.DisplayMode != displayModeDefault && cfg.DisplayMode != displayModeDebug {
		return nil, fmt.Errorf("A2A_CHAT_DISPLAY_MODE %q: want %q or %q", cfg.DisplayMode, displayModeDefault, displayModeDebug)
	}
	if cfg.NATSURL == "" {
		return nil, fmt.Errorf("NATS_URL is required")
	}
	// Socket Mode needs the whole Slack pair; half a pair is a typo, not a
	// choice, so it refuses rather than silently running another backend.
	if (cfg.SlackBotToken != "") != (cfg.SlackAppToken != "") {
		return nil, fmt.Errorf("SLACK_BOT_TOKEN and SLACK_APP_TOKEN arm Slack together; only one is set")
	}
	// One REAL backend per gateway process, chosen by which credential is
	// set. A silent default here would make a two-backend misconfiguration a
	// working Discord gateway that quietly never consumes Chat — refuse both
	// directions instead. Counted rather than enumerated pairwise: with three
	// backends the pairs are the easy thing to leave a hole in, and a fourth
	// must not be addable with a combination nobody checked. Adding a backend
	// is one entry in this list.
	var armed []string
	if cfg.GchatRelayURL != "" {
		armed = append(armed, "A2A_GCHAT_RELAY_URL")
	}
	if cfg.SlackBotToken != "" {
		armed = append(armed, "the SLACK_BOT_TOKEN+SLACK_APP_TOKEN pair")
	}
	if cfg.DiscordToken != "" {
		armed = append(armed, "DISCORD_TOKEN")
	}
	// The doors (inject, A2A) are NOT in that list, decided 2026-09-17 on
	// the design doc's review for the inject door and holding for its
	// sibling. The guard exists so that arming two backends cannot leave
	// one of them silently unconsumed: two processes on one Chat relay
	// durable split its event deliveries, and the symptom is a gateway that
	// looks healthy and answers half the messages. A local HTTP door has no
	// such failure mode — it consumes nothing and competes for nothing — so
	// counting it would buy no safety and would cost the thing stage 2 needs,
	// which is the eval door and the Chat relay on one install so the two
	// transports can be compared against it.
	switch len(armed) {
	case 1:
	case 0:
		// The door alone is enough to start. Decided, not assumed: the A2A
		// owner's decision of 2026-09-17 on the eval transport's design doc
		// (eval-next-transport.md) is that an eval install with neither a
		// Discord token nor a Chat relay starts its gateway on the door
		// instead of crash-looping, so a `mode: next` install reads Ready
		// and a rollout can gate on it. The gateway logs that it is
		// inject-only when it does, and says so on the door's read route,
		// because a next install whose relay URL failed to render looks the
		// same. The spec's test-backend section states the same decision.
		if cfg.InjectListen == "" && cfg.A2ADoorListen == "" {
			return nil, fmt.Errorf("no chat backend: set DISCORD_TOKEN (W0's discord-bot Secret), A2A_GCHAT_RELAY_URL (the credential proxy's chat relay), the SLACK_BOT_TOKEN+SLACK_APP_TOKEN pair (Socket Mode), A2A_INJECT_LISTEN (the dev-only inject side door), or A2A_DOOR_LISTEN (the A2A door for agent callers)")
		}
	default:
		return nil, fmt.Errorf("more than one chat backend is configured (%s): one backend per gateway process — two gateways on one relay durable split event deliveries; run a second Deployment for a second backend. The side doors (A2A_INJECT_LISTEN, A2A_DOOR_LISTEN) are not backends in this sense and may sit beside any one of them", strings.Join(armed, ", "))
	}
	// Fail closed: a door with no token would be reachable by anything that
	// reaches the listener, and the port-forward path the runner uses is
	// served from inside the pod, past both the loopback bind and the
	// NetworkPolicy in front of it. There is deliberately no opt-out — see
	// Config.InjectToken.
	if cfg.InjectListen != "" && cfg.InjectToken == "" {
		return nil, fmt.Errorf("A2A_INJECT_TOKEN is required when A2A_INJECT_LISTEN is set: the inject door authenticates every request with a bearer token, because neither its loopback bind nor the NetworkPolicy in front of it governs the port-forward path its caller uses")
	}
	if cfg.A2ADoorListen != "" && cfg.A2ADoorToken == "" {
		return nil, fmt.Errorf("A2A_DOOR_TOKEN is required when A2A_DOOR_LISTEN is set: the A2A door authenticates every RPC request with a bearer token (the agent card alone is open), for the reason the inject door does; see Config.A2ADoorToken")
	}
	// Only when the spawn path is armed: a gateway that spawns nothing has
	// no session identity to name, and demanding one would break every
	// bridge-only install.
	if cfg.SpawnSessions && cfg.SessionServiceAccount == "" {
		return nil, fmt.Errorf("A2A_SESSION_SERVICE_ACCOUNT is required when A2A_SPAWN_SESSIONS is true; session pods authenticate to the bus as it, and there is no safe default")
	}
	cfg.defaultCapabilityCeiling()
	// Validated at boot rather than at mint: a bad tier or scope would
	// otherwise surface as every task failing to start, one refusal at a
	// time, with the cause in the gateway's logs and not the operator's.
	if err := cfg.validateCapabilityCeiling(); err != nil {
		return nil, err
	}
	// The addressee is a subject token; validate at boot, not per-message.
	// The "session" sentinel passes by construction; whether a spawner backs
	// it is checked where the spawner is built (gateway.New).
	if !lib.ValidSubjectToken(cfg.DefaultAddressee) {
		return nil, fmt.Errorf("A2A_DEFAULT_ADDRESSEE %q is not a dot-free DNS-1123 label", cfg.DefaultAddressee)
	}
	// The session cap: absent means the documented default; a value the cap
	// cannot honestly enforce (zero, negative, junk) refuses at boot rather
	// than surprising at spawn time. Keep the default in step with the
	// operator's (resolveA2AMaxSessions in the k8s-operator module), which
	// renders it explicitly onto this env var.
	maxSessions := envOr("A2A_MAX_SESSIONS", strconv.Itoa(defaultMaxSessions))
	n, err := strconv.Atoi(maxSessions)
	if err != nil || n < 1 {
		return nil, fmt.Errorf("A2A_MAX_SESSIONS %q: need an integer >= 1", maxSessions)
	}
	cfg.MaxSessions = n
	depthMax := envOr("A2A_DELEGATION_DEPTH_MAX", strconv.Itoa(defaultDelegationDepthMax))
	dm, err := strconv.Atoi(depthMax)
	if err != nil || dm < 1 {
		return nil, fmt.Errorf("A2A_DELEGATION_DEPTH_MAX %q: need an integer >= 1", depthMax)
	}
	cfg.DelegationDepthMax = dm
	ttl := envOr("A2A_IDLE_TTL", "30m")
	d, err := time.ParseDuration(ttl)
	if err != nil {
		return nil, fmt.Errorf("A2A_IDLE_TTL %q: %w", ttl, err)
	}
	if d < time.Minute {
		return nil, fmt.Errorf("A2A_IDLE_TTL %q is under the 1m floor; an instant reap deletes pods mid-conversation", ttl)
	}
	cfg.IdleTTL = d

	// The adapter's deadline, in the adapter's own units (integer seconds) —
	// see the field comment for why the env name is shared.
	deadlineSecs := envOr("A2A_TASK_DEADLINE_SECONDS", strconv.Itoa(int(defaultTaskDeadline/time.Second)))
	secs, err := strconv.Atoi(deadlineSecs)
	if err != nil || secs < 60 {
		return nil, fmt.Errorf("A2A_TASK_DEADLINE_SECONDS %q: need an integer >= 60; a sub-minute deadline kills pods mid-cold-start", deadlineSecs)
	}
	cfg.TaskDeadline = time.Duration(secs) * time.Second

	askTTL := envOr("A2A_ASK_TTL", defaultAskTTL.String())
	at, err := time.ParseDuration(askTTL)
	if err != nil {
		return nil, fmt.Errorf("A2A_ASK_TTL %q: %w", askTTL, err)
	}
	if at < time.Minute {
		return nil, fmt.Errorf("A2A_ASK_TTL %q is under the 1m floor; it would erase the ask from status cards while the task runs", askTTL)
	}
	cfg.AskTTL = at

	sessionTTL := envOr("A2A_SESSION_TTL", defaultSessionTTL.String())
	st, err := time.ParseDuration(sessionTTL)
	if err != nil {
		return nil, fmt.Errorf("A2A_SESSION_TTL %q: %w", sessionTTL, err)
	}
	if st < 72*time.Hour {
		return nil, fmt.Errorf("A2A_SESSION_TTL %q is under the 72h floor; it must sit beyond TASKS stream retention (72h)", sessionTTL)
	}
	if st <= cfg.TaskDeadline {
		return nil, fmt.Errorf("A2A_SESSION_TTL %q must exceed A2A_TASK_DEADLINE_SECONDS (%v)", sessionTTL, cfg.TaskDeadline)
	}
	cfg.SessionTTL = st

	grace := envOr("A2A_FIRST_EVENT_GRACE", defaultFirstEventGrace.String())
	fg, err := time.ParseDuration(grace)
	if err != nil {
		return nil, fmt.Errorf("A2A_FIRST_EVENT_GRACE %q: %w", grace, err)
	}
	if fg < time.Minute {
		return nil, fmt.Errorf("A2A_FIRST_EVENT_GRACE %q is under the 1m floor; it would release a task still cold-starting", grace)
	}
	cfg.FirstEventGrace = fg

	cfg.OwnerDeployment = os.Getenv("A2A_OWNER_DEPLOYMENT")

	// Salt precedence: the install's provisioned SESSION_KV_SALT is the
	// salt (the spec's settled answer); the explicit override and the
	// derived fallback are playground posture, in that order. The trim is
	// load-bearing: the shipped redactor does `.strip()` on this same env,
	// and two readers of one Secret must agree byte-for-byte or a trailing
	// newline in a hand-made Secret silently unjoins every pseudonym.
	switch {
	case strings.TrimSpace(os.Getenv("SESSION_KV_SALT")) != "":
		cfg.AttributionSalt = []byte(strings.TrimSpace(os.Getenv("SESSION_KV_SALT")))
	case os.Getenv("A2A_ATTRIBUTION_SALT") != "":
		cfg.AttributionSalt = []byte(os.Getenv("A2A_ATTRIBUTION_SALT"))
	default:
		// Derived fallback while the install has no provisioned salt Secret.
		// An empty password would make this a public constant and the
		// pseudonyms an offline dictionary away from plaintext — refuse.
		if cfg.NATSPassword == "" {
			return nil, fmt.Errorf("SESSION_KV_SALT or A2A_ATTRIBUTION_SALT is required when NATS_PASSWORD is empty: the derived fallback would be a public constant")
		}
		// HKDF, not a bare digest of the password: a credential reaching a
		// plain hash is what CodeQL's go/weak-sensitive-data-hashing
		// refuses, and extract-and-expand under a fixed info string is the
		// construction one is allowed to go through. It buys no resistance
		// to offline guessing — HKDF has no work factor, and at a nil salt
		// the cost per candidate password is a handful of SHA-256
		// compressions either way. What keeps this fallback from being a
		// de-anonymization key is the password's own entropy (the operator
		// mints 128 bits of it) and, properly, the provisioned Secret; see
		// the AttributionSalt field comment.
		derived, err := hkdf.Key(sha256.New, []byte(cfg.NATSPassword), nil, attributionSaltInfo, attributionSaltLen)
		if err != nil {
			return nil, fmt.Errorf("deriving the attribution salt from NATS_PASSWORD: %w", err)
		}
		cfg.AttributionSalt = derived
	}
	return cfg, nil
}

// metricsPortFromEnv reads A2A_METRICS_PORT: zero when unset or empty, else
// a port in range that neither door's listen address already names. A bad
// value refuses the boot, like every other setting FromEnv reads; the
// operator renders a valid one, so only a hand edit reaches these refusals.
func metricsPortFromEnv(cfg *Config) (int, error) {
	raw := strings.TrimSpace(os.Getenv(metricsPortEnv))
	if raw == "" {
		return 0, nil
	}
	port, err := strconv.Atoi(raw)
	if err != nil || port < metricsPortMin || port > metricsPortMax {
		return 0, fmt.Errorf("%s %q: need a port in %d-%d", metricsPortEnv, raw, metricsPortMin, metricsPortMax)
	}
	for _, door := range []struct{ name, listen string }{
		{"the inject door", cfg.InjectListen},
		{"the A2A door", cfg.A2ADoorListen},
	} {
		if door.listen == "" {
			continue
		}
		// Compared as the number net.Listen will bind, not as the string:
		// net.LookupPort is the parse net.Listen runs on the port, so a
		// zero-padded, signed or named spelling of this port is caught here
		// rather than as a door that loses the bind to this listener. An
		// address or port it cannot read is skipped, because the door's own
		// net.Listen refuses it the same way when it binds; that failure is
		// the door's, and not a collision with this listener.
		_, doorPortRaw, err := net.SplitHostPort(door.listen)
		if err != nil {
			continue
		}
		if doorPort, err := net.LookupPort("tcp", doorPortRaw); err == nil && doorPort == port {
			return 0, fmt.Errorf("%s %d is the port %s listens on (%q); the metrics listener needs a port of its own", metricsPortEnv, port, door.name, door.listen)
		}
	}
	return port, nil
}

// pyStrip trims what Python's str.strip() trims and nothing more. The Slack
// pair is the one credential the gateway shares with the broker, which reads
// the same Secret keys with .strip() (credential_proxy.py); reading them the
// same way keeps a value that worked under `today` (a trailing newline from
// --from-file or an `echo` without -n) working under `next`. Python's
// whitespace is Go's unicode.IsSpace plus the four separators U+001C-U+001F.
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	})
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// defaultCapabilityCeiling fills the tier and scope the gateway mints under.
// Tests and embedders build Config directly, bypassing FromEnv, and the
// ceiling is inherited by every hop of every task, so the unset value has to
// be the narrowest thing that is certainly true rather than the widest thing
// that would work.
func (c *Config) defaultCapabilityCeiling() {
	if c.AuthorityTier == "" {
		c.AuthorityTier = capability.TierDeveloperTeam
	}
	if c.AuthorityScope == "" {
		c.AuthorityScope = capability.NamespaceScope(c.Namespace)
	}
}

// validateCapabilityCeiling runs the ceiling through the same validation a
// minted entry gets, so a bad tier or scope is a boot failure the operator
// sees rather than a per-task refusal in the gateway's log.
func (c *Config) validateCapabilityCeiling() error {
	// The namespace first, because it is the rung the default ceiling is
	// built from and Entry.Validate cannot speak for it: a namespace with an
	// even number of separators makes a scope that validates cleanly as a
	// DIFFERENT ceiling, so running the Entry check alone would pass the
	// gateway out of boot minting against something nobody chose. Checked
	// even when A2A_AUTHORITY_SCOPE is set, because an explicit scope does
	// not make a malformed namespace correct -- it only hides it here.
	if err := capability.ValidateNamespace(c.Namespace); err != nil {
		return fmt.Errorf("POD_NAMESPACE: %w", err)
	}
	e := capability.Entry{Tier: c.AuthorityTier, Scope: c.AuthorityScope, Delegate: "boot-check"}
	if err := e.Validate(); err != nil {
		return fmt.Errorf("A2A_AUTHORITY_TIER/A2A_AUTHORITY_SCOPE: %w", err)
	}
	return nil
}
