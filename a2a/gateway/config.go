package gateway

import (
	"crypto/hkdf"
	"crypto/sha256"
	"fmt"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
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

// The mux keys and conversation prefixes, exported for cmd/gateway, which
// builds one adapter per backend: Backends returns the first four, and the
// console is the essential backend beside them.
const (
	BackendGchat   = gchatBackend
	BackendSlack   = slackBackend
	BackendTeams   = teamsBackend
	BackendDiscord = discordBackend
	BackendConsole = consoleBackend
)

// backendArm is one REAL chat backend and the config field that arms it.
type backendArm struct {
	name  string
	armed func(*Config) bool
}

// backendArms lists the real chat backends in Backends' order. Backends and
// the mux's direct-message refusal (knownBackendName) both read it; the
// adapter switch in cmd/gateway, the metrics platform labels and the people
// reading each name their backends on their own.
var backendArms = []backendArm{
	{name: gchatBackend, armed: func(c *Config) bool { return c.GchatRelayURL != "" }},
	{name: slackBackend, armed: func(c *Config) bool { return c.SlackBotToken != "" }},
	{name: teamsBackend, armed: func(c *Config) bool { return c.TeamsRelayURL != "" }},
	{name: discordBackend, armed: func(c *Config) bool { return c.DiscordToken != "" }},
}

// defaultMaxSessions is what MaxSessions means when unset; the field's
// comment carries the sizing rationale.
const defaultMaxSessions = 10

// defaultDelegationDepthMax is what DelegationDepthMax means when unset; the
// field's comment carries the rationale.
const defaultDelegationDepthMax = 3

// defaultDelegationWidthMax is what DelegationWidthMax means when unset; the
// field's comment carries the rationale.
const defaultDelegationWidthMax = 10

// delegationWidthBuiltMax is the widest A2A_DELEGATION_WIDTH_MAX FromEnv
// accepts: ten, the fleet question's width (#5682). The children queue to
// their profile's concurrency, which is what binds at once, not this.
const delegationWidthBuiltMax = 10

// defaultScopeFile is where the operator projects the install's rendered
// spec.scope into the gateway pod: the same scope.json, at the same path, the
// platform agent reads.
const defaultScopeFile = "/etc/kube-agents/scope.json"

// defaultGchatTokenPath is where the operator projects the gateway's
// relay-audience ServiceAccount token when the gchat backend is armed.
const defaultGchatTokenPath = "/var/run/secrets/a2a-chat-relay/token" // #nosec G101 -- a path, not a credential

// envGchatHomeChannel carries the Chat home space the chat.notify route posts
// to (notify.go); the operator renders it from googleChat.homeChannel.
const envGchatHomeChannel = "A2A_GCHAT_HOME_CHANNEL"

// The Teams backend's environment: the broker relay URL that arms it, the
// projected token it presents (the a2a-chat audience, the Google Chat
// backend's token by default), the allowlist and allow-all gates, the home
// channel the chat.notify route posts to, the tenant it serves, and whether
// replies render as Adaptive Cards. The operator renders them from
// spec.integration.teams when it runs with A2A_TEAMS_BACKEND=true.
const (
	envTeamsRelayURL      = "A2A_TEAMS_RELAY_URL"
	envTeamsTokenPath     = "A2A_TEAMS_TOKEN_PATH" // #nosec G101 -- an environment variable name, not a credential
	envTeamsAllowedUsers  = "A2A_TEAMS_ALLOWED_USERS"
	envTeamsAllowAllUsers = "A2A_TEAMS_ALLOW_ALL_USERS"
	envTeamsHomeChannel   = "A2A_TEAMS_HOME_CHANNEL"
	envTeamsTenantID      = "A2A_TEAMS_TENANT_ID"
	envTeamsAdaptiveCards = "A2A_TEAMS_ADAPTIVE_CARDS"
)

// envSlackHomeChannel carries the Slack home channel's id the chat.notify
// route posts to; the operator renders it from slack.homeChannel.
const envSlackHomeChannel = "A2A_SLACK_HOME_CHANNEL"

// EnvSlackTestUsers carries Config.SlackTestUsers, comma-separated; the
// operator renders it from its own environment of the same name, never from
// the CR, so only an install whose operator was deployed with it (CI, eval)
// has it.
const EnvSlackTestUsers = "A2A_SLACK_TEST_USERS"

// The learned-home contract (home_channel.go): the operator renders each
// backend's policy from its homeChannelPolicy, the record ConfigMap's name, the
// gateway's own image for the record's writer and reader keys, and the one
// switch that arms the learn, which only an operator that also admits the
// record renders.
const (
	envSlackHomePolicy    = "A2A_SLACK_HOME_POLICY"
	envGchatHomePolicy    = "A2A_GCHAT_HOME_POLICY"
	envHomeChannelRecord  = "A2A_HOME_CHANNEL_RECORD"
	envSelfImage          = "A2A_SELF_IMAGE"
	envHomeChannelLearn   = "A2A_HOME_CHANNEL_LEARN"
	slackHomePolicyManual = "manual"
	// gchatHomePolicyManual is googleChat.homeChannelPolicy's "manual", the
	// same word as Slack's.
	gchatHomePolicyManual = slackHomePolicyManual
	envTrue               = "true"
)

// The election (election.go): the Lease the gateway campaigns for, and its
// identity there, the pod's own name. The operator renders both together.
// With no Lease the gateway runs unelected at term 0, as it did before the
// election, which is what every install runs until the operator renders
// the Lease and its Role.
const (
	envGatewayLease = "A2A_GATEWAY_LEASE"
	envPodName      = "POD_NAME"
)

// envSlackAnswerFeedback and envGchatAnswerFeedback turn answer feedback
// off when "false" (feedback.go); the operator renders each only from its
// backend's answerFeedback=false. The gateway keeps one switch for every
// backend it holds, so either name turns feedback off on Chat and Slack
// alike.
const (
	envSlackAnswerFeedback = "A2A_SLACK_ANSWER_FEEDBACK"
	envGchatAnswerFeedback = "A2A_GOOGLE_CHAT_ANSWER_FEEDBACK"
)

// envPeopleSeen turns the people bucket's records off when "false"
// (people.go); the operator renders it only from
// spec.telemetry.peopleSeen=false.
const envPeopleSeen = "A2A_PEOPLE_SEEN"
const envGoogleCloudUniverseDomain = "GOOGLE_CLOUD_UNIVERSE_DOMAIN"

// envKubectlCostTier is the variable the kubectl admission
// (deploy/sandbox/kubectl-admit.py) reads its sizing tier from when no tier
// file names one: the shell sandbox's name for it, on the session pod.
const envKubectlCostTier = "KUBEAGENTS_KUBECTL_COST_TIER"

// defaultKubectlCostTier is the tier the admission charges when none is
// named; spawn names it rather than leave the variable unset.
const defaultKubectlCostTier = "default"

// The A2A door's Microsoft Entra sign-in class (a2adoor_entra.go), set by
// hand on the gateway as the Google class's variables are.
const (
	envA2ADoorEntraTenantID     = "A2A_DOOR_ENTRA_TENANT_ID"
	envA2ADoorEntraClientID     = "A2A_DOOR_ENTRA_CLIENT_ID"
	envA2ADoorEntraAllowedUsers = "A2A_DOOR_ENTRA_ALLOWED_USERS"
)
const (
	envImagePullRefs = "A2A_IMAGE_PULL_REFS"
	// envTriggerEventKinds lists the event triggers the trigger ingress
	// admits (Config.TriggerEventKinds).
	envTriggerEventKinds = "A2A_TRIGGER_EVENT_KINDS"
	imagePullRefSep      = ","
)

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

// busyNoticeAtEnv names the busy notice's threshold (Config.BusyNoticeAt).
const busyNoticeAtEnv = "A2A_BUSY_NOTICE_AT"

// EnvNATSCAConfigMap names the ConfigMap the gateway's CA bundle comes from
// (Config.NATSCAConfigMap), which the operator renders beside NATS_CA_FILE.
const EnvNATSCAConfigMap = "NATS_CA_CONFIGMAP"

// The display-mode values, matching the GoogleChatSpec.Mode enum.
const (
	displayModeDefault = "default"
	displayModeDebug   = "debug"
)

// defaultTaskDeadline is what TaskDeadline means when unset — the worker
// adapter's own default (a2a/cmd/worker-adapter: A2A_TASK_DEADLINE_SECONDS,
// 1800s), restated here because the two halves of one contract must agree.
const defaultTaskDeadline = 30 * time.Minute

// defaultSessionMaxLifetime is what SessionMaxLifetime means when unset; the
// field's comment carries the rationale.
const defaultSessionMaxLifetime = 4 * time.Hour

// defaultAskTTL is what AskTTL means when unset; the field's comment carries
// the horizon rationale.
const defaultAskTTL = 24 * time.Hour

// defaultSessionTTL is what SessionTTL means when unset (7 days, sitting
// comfortably beyond TASKS stream's 72h retention).
const defaultSessionTTL = 7 * 24 * time.Hour

// defaultFirstEventGrace is what FirstEventGrace means when unset; the
// field's comment carries the sizing rationale.
const defaultFirstEventGrace = 10 * time.Minute

// defaultCardStopTimeout is what CardStopTimeout means when unset: above the
// bridge's worst case for a card-stop control, about 60 s (its capability
// check's 5 s, its 20 s wait for the session's turn and its 35 s POST to
// session-kv; a2a/hermes-bridge/cardstop.go), with room for the bus.
const defaultCardStopTimeout = 75 * time.Second

// cardStopTimeoutFloor is the least A2A_CARD_STOP_TIMEOUT FromEnv takes: a
// wait shorter than the bridge's own turn wait gives up on every stop that
// has to wait for the turn it just cancelled.
const cardStopTimeoutFloor = 30 * time.Second

// Config is the gateway's runtime configuration. The env contract matches
// what the W6 operator renders onto the a2a-gateway Deployment; everything
// else has playground defaults.
type Config struct {
	NATSURL      string
	NATSUser     string
	NATSPassword string
	DiscordToken string

	// BusAuth is how the gateway authenticates to the bus
	// (A2A_GATEWAY_BUS_AUTH, #5301): BusAuthPassword, the default, as the
	// static user nats.conf carries (NATSUser, NATSPassword); or
	// BusAuthToken, with its projected ServiceAccount token (BusTokenFile),
	// which the auth callout mints from the operator's map entry for the
	// gateway's ServiceAccount. Both are rendered while the flip is
	// pending, so rolling back across it is this setting alone.
	BusAuth string

	// BusTokenFile is the token BusAuthToken presents: lib.EnvBusTokenFile,
	// or lib.BusTokenPath. Read on every connect and reconnect.
	BusTokenFile string

	// NATSCAFile is the CA bundle this gateway trusts the bus by
	// (lib.EnvNATSCAFile), and NATSCAConfigMap the ConfigMap it was mounted
	// from. The spawner hands both to every session pod: the pod mounts the
	// same ConfigMap at the same path, so it trusts what the gateway trusts.
	NATSCAFile      string
	NATSCAConfigMap string

	// SlackBotToken and SlackAppToken arm the Slack backend: Socket Mode
	// needs both (xoxb- drives the Web API, xapp- the outbound websocket —
	// no inbound endpoint, nothing to expose). Slack may run beside the
	// other backends in one gateway process. Each backend is its own
	// transport (Chat a Pub/Sub pull through the broker, Slack a Socket
	// Mode connection, Discord its gateway websocket) and none shares a
	// durable with another. The task-event relay durable
	// (Options.RelayDurable) is the process's own, bound once per process,
	// and the election keeps one leader per Deployment.
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
	// TeamsRelayURL is the credential proxy's relay base URL for the Teams
	// backend; setting it arms the Teams adapter, which pulls Bot Framework
	// activities from the broker's Teams queue and replies through its
	// passthrough. TeamsTokenPath is the projected ServiceAccount token it
	// presents (the a2a-chat audience, GchatTokenPath's default).
	TeamsRelayURL  string
	TeamsTokenPath string
	// TeamsAllowedUsers is the Teams backend's ingress allowlist: Microsoft
	// Entra object ids (from.aadObjectId), the immutable identity, or Bot
	// Framework user ids, compared case-insensitively; the legacy path's
	// TEAMS_ALLOWED_USERS gate. TeamsAllowAllUsers disables it, stated
	// explicitly.
	TeamsAllowedUsers  []string
	TeamsAllowAllUsers bool
	// TeamsHomeChannel is the install's Teams home conversation id, where a
	// chat.notify proactive post lands; empty leaves the route serving
	// conversation requests only.
	TeamsHomeChannel string
	// TeamsTenantID, when set, is the one Entra tenant the adapter serves; an
	// activity from another tenant is acked away.
	TeamsTenantID string
	// TeamsAdaptiveCards renders Teams replies as Adaptive Cards; off, they
	// post as plain text. On unless the env says false.
	TeamsAdaptiveCards bool
	// SlackHomeChannel is the install's Slack home channel id ("C0123"),
	// the one place a Slack chat.notify home post may land (notify.go). Empty
	// leaves the route serving conversation requests only.
	SlackHomeChannel string
	// SlackHomePolicy is slack.homeChannelPolicy as the operator renders it:
	// "manual" keeps the home channel the CR's alone; anything else is auto.
	SlackHomePolicy string
	// GchatHomePolicy is googleChat.homeChannelPolicy as the operator renders
	// it: "auto" (also absent) or "manual".
	GchatHomePolicy string
	// HomeChannelRecord names the ConfigMap a learned home channel is
	// recorded in, and SelfImage the gateway's own image reference, which it
	// stamps into that record. HomeChannelLearn is the switch that arms the
	// learn at all (HomeLearnArmed).
	HomeChannelRecord string
	SelfImage         string
	HomeChannelLearn  bool
	// GatewayLease names the Lease this gateway campaigns for (Elected), and
	// PodName is its identity there. Empty runs the gateway unelected.
	GatewayLease string
	PodName      string
	// AnswerFeedbackOff is the install's slack.answerFeedback=false or
	// googleChat.answerFeedback=false: no rating controls are drawn on any
	// backend and nothing is kept, whatever the feedback bucket holds.
	AnswerFeedbackOff bool
	// PeopleSeenOff is the install's spec.telemetry.peopleSeen=false: the
	// gateway records no one it sees in the people bucket.
	PeopleSeenOff bool
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
	// SlackTestUsers are the Slack member ids of end-to-end test users
	// (EnvSlackTestUsers), meant for CI and eval installs; the allowlist
	// (A2A_SLACK_ALLOWED_USERS) is still the gate on who is served. A message such a
	// user posts with a user token through an app carries the app's bot_id,
	// which the Slack adapter otherwise drops as bot traffic; from a listed
	// member it is read as theirs (SlackAdapter.SetTestUsers). Every other
	// gate still applies, and the allowlist must still admit them.
	SlackTestUsers []string
	// RelayPrecreated binds the event relay to the durable the provision Job
	// created rather than creating it (#4173): the operator sets
	// A2A_GATEWAY_RELAY_PRECREATED=true beside a grant that denies this
	// principal the create on that name. Off, as in tests and a hand-run
	// gateway, the relay creates its own durable as before.
	RelayPrecreated bool

	// TargetAllowedUsers is the per-target, per-backend trusted-human
	// allowlist a session's delegation is checked against: target ->
	// backend -> ids in that backend's vocabulary. Only "platform" is
	// populated from env today (EnvTargetAllowedUsersGchat/Slack); an absent
	// pair means all authenticated users, and a present pair whose list is
	// empty means nobody. The A2A door's backend ("a2a") is the exception:
	// no pair for it means nobody (doorUnlisted). See allowlist.go.
	TargetAllowedUsers map[string]map[string][]string
	// TierMembership maps a person, by backend and plaintext user id, to the
	// tier their AgentProfile tasks run at (#3659, Tier 2c): the operator
	// renders the PlatformAgent's spec.tiers.membership into
	// A2A_TIER_MEMBERSHIP, and the gateway HMACs each id as it does the
	// allowlists. Anyone unlisted runs at read; the console's shared principal
	// always does (#5557).
	TierMembership []TierMember

	// InjectListen is the inject side door's HTTP listen address, and setting
	// it arms the door. DEV AND EVAL ONLY. The door is not a backend (see
	// FromEnv): it may sit beside any real backends, because a local HTTP
	// door consumes nothing and competes for nothing. The operator renders it
	// only under its eval flag.
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

	// InjectConfirm (A2A_INJECT_CONFIRM, "true" and nothing else) lets the
	// inject door take intent "confirm", which submits the turn with a
	// change requested (TaskParameters.ChangeRequested). Off, the door
	// refuses that intent with a 400. The door's caller is the eval
	// harness, a program, so a confirm over it is automation by design and
	// belongs only on an install armed for evals: the operator renders this
	// beside A2A_INJECT_LISTEN under the same flag (A2A_INJECT_BACKEND on
	// the operator), and a door armed by hand without it cannot confirm.
	InjectConfirm bool

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

	// ConsoleDoor (A2A_CONSOLE_DOOR=true) says the console door is wanted
	// on its own: a gateway with no chat backend and no other door starts
	// for it rather than refusing. The console adapter runs whenever the
	// gateway runs; this only lets it be the gateway's whole ingress. The
	// operator sets it on every next gateway, so an install with no chat
	// backend still runs one for the console page and the MCP server. Like
	// the doors it is not a backend in the one-real-backend sense: it
	// consumes nothing and competes for nothing.
	ConsoleDoor bool

	// A2ADoorToken is the bearer token the door requires on every RPC
	// request (A2A_DOOR_TOKEN); the agent card is the one unauthenticated
	// route. No unauthenticated mode for the RPCs, for the inject door's
	// reason: the port-forward path is served from inside the pod, past
	// the NetworkPolicy.
	A2ADoorToken string

	// A2ADoorPrincipalMapPath is the door's OWN principal map
	// (A2A_DOOR_PRINCIPAL_MAP): keys prefixed a2a:, values eval-only
	// identities, never a fallback to the chat map or the inject map. The
	// Google and Entra sign-in classes resolve through verifiers beside
	// this file, never as entries in it.
	A2ADoorPrincipalMapPath string

	// A2ADoorPublicURL is the URL the agent card advertises for the door's
	// JSON-RPC endpoint (A2A_DOOR_PUBLIC_URL): what a client reaches it at,
	// which behind a port-forward or an ingress is not the listen address.
	// Empty makes the card advertise the address it was fetched from.
	A2ADoorPublicURL string

	// A2ADoorGoogleClientID arms the door's developer identity class
	// (A2A_DOOR_GOOGLE_CLIENT_ID): a bearer that is not A2ADoorToken is
	// checked with Google as an access token issued for this OAuth client,
	// the install's one pre-registered client. Empty leaves the class off.
	// It needs the door: set without A2A_DOOR_LISTEN it is refused.
	A2ADoorGoogleClientID string
	// A2ADoorAllowedUsers is the developer class's allowlist
	// (A2A_DOOR_ALLOWED_USERS, comma-separated emails): the door refuses a
	// verified email off it (403) before it holds any state for the caller,
	// and the gateway checks the same list again. Empty admits nobody. There is no allow-all, unlike Chat's legacy
	// posture: a door any Google account can sign in to is not a default
	// anyone should get by leaving a list blank.
	A2ADoorAllowedUsers []string
	// A2ADoorEntraTenantID and A2ADoorEntraClientID arm the door's
	// Microsoft Entra class (A2A_DOOR_ENTRA_TENANT_ID,
	// A2A_DOOR_ENTRA_CLIENT_ID): a bearer without Google's ya29. prefix is checked,
	// against the tenant's published signing keys, as a token the tenant
	// issued for this application registration. Both empty leaves the
	// class off; one without the other is refused, as is either without
	// A2A_DOOR_LISTEN. Set by hand on the gateway, as the Google class's
	// are; nothing renders them yet.
	A2ADoorEntraTenantID string
	A2ADoorEntraClientID string
	// A2ADoorEntraAllowedUsers is the Entra class's allowlist
	// (A2A_DOOR_ENTRA_ALLOWED_USERS, comma-separated object ids), held to
	// the posture of A2ADoorAllowedUsers: the door refuses an object id off
	// it (403) before it holds any state for the caller, the gateway checks
	// the same list again, empty admits nobody, and there is no allow-all.
	A2ADoorEntraAllowedUsers []string
	// MetricsPort is the metrics-only listener's port (A2A_METRICS_PORT);
	// zero means no listener. It binds every interface, because its caller
	// is the managed-Prometheus collector on the pod network, and serves
	// MetricsPath and ReadyzPath and nothing else. It may not be either door's port: a door
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

	// IdleTTL is the reap threshold since the session's last user message
	// (decided 8/24: 30 minutes, config-backed; spec-chatops-gateway.md,
	// Session lifecycle). The answer to that message does not move it: with
	// one pod serving every turn, the pod is the conversation's, and a
	// conversation nobody has spoken in for the TTL is idle however recently
	// its last answer landed. A running task still exempts its pod.
	IdleTTL time.Duration

	// DMIdleTTL replaces IdleTTL for a single-turn DM: a "dm" conversation
	// whose current pod has taken at most one human turn (#2371,
	// A2A_DM_IDLE_TTL, default 5m). Most DMs are one question, and under
	// sessions by default each holds a session pod, and a slot of the session
	// cap, for the full IdleTTL after its answer. A second turn for the pod
	// puts the conversation back on IdleTTL. A reply after the reap still
	// works: the next turn gets a new pod, rehydrated from the context's
	// history. Never longer than IdleTTL (Config.idleTTLFor), and counted from
	// the answer as well as the message (Config.idleExpired).
	DMIdleTTL time.Duration

	// SessionReuse keeps one pod per conversation across turns
	// (A2A_SESSION_REUSE, on unless "false"): a turn whose pod is live and
	// idle goes to it, and only a conversation with no live pod gets a new
	// one. Off, every turn is a fresh pod, as before 10/9, and the pods
	// spawned are never handed a second task. Either way the gateway only
	// reuses pods spawned with the flag, so turning it on never reaches a
	// pod an older gateway spawned.
	SessionReuse bool

	// SessionMaxLifetime bounds a reused session pod's whole life
	// (A2A_SESSION_MAX_LIFETIME, 4h by default), as its activeDeadlineSeconds.
	// A one-task pod's deadline is TaskDeadline plus a grace, which a pod
	// serving many turns cannot have; this is the backstop in its place, and
	// a per-task overdue check in the reap scan bounds a wedged task inside
	// it (Gateway.taskOverdue). A pod near the end of its lifetime is not
	// handed another task. Never below TaskDeadline plus twice the grace, so
	// the pod deadline and the overdue check cannot fire together.
	SessionMaxLifetime time.Duration

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
	// this deadline; for a pod that serves one task, the pod deadline (this
	// plus a fixed grace) is the backstop that hands a wedged ADAPTER to
	// Sweep instead of letting it hold its bus credential indefinitely. A
	// reused pod's backstop is SessionMaxLifetime plus the reap scan's
	// overdue check at the same bound (Gateway.taskOverdue). Raising it buys longer tasks at
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

	// CardStop arms the card stop (A2A_CARD_STOP, off unless "true";
	// gke-labs#3263): a `stop` in a conversation on the default addressee
	// also sends that addressee a card-stop control, a task whose message is
	// the cardStop data part alone, and the reply names the kanban cards the
	// executor stopped (cardstop.go). Off, a stop cancels the active task
	// only and says a card keeps working, as before. A rollout switch: the
	// control needs a hermes-bridge and an agent image that answer it, and an
	// older bridge rejects it, which answers cardStopUnconfirmedNotice.
	CardStop bool

	// CardStopTimeout bounds the wait for a card-stop control's terminal
	// (A2A_CARD_STOP_TIMEOUT, a Go duration, 75s by default and at least
	// 30s). Past it the gateway cancels the control and answers that it
	// could not confirm the stop: the cards may have stopped, and the
	// executor's answer is what was lost. The operator renders it at the
	// 75s default and offers no override, as the bridge's own bound
	// (BRIDGE_CARD_STOP_TIMEOUT_SECONDS, 35s by default) has none; the env
	// is read so a hand-built Deployment can set it. A change to either
	// bound is a code change made to both together.
	CardStopTimeout time.Duration

	// OwnerDeployment names the gateway's own Deployment
	// (A2A_OWNER_DEPLOYMENT; the operator renders its own render's name).
	// When set, every spawned session pod carries an ownerReference to it,
	// so Kubernetes GC reaps sessions when the Deployment goes — the CR's
	// deletion, or any other — with no operator
	// exception to its IsControlledBy refusal. Empty (playground) spawns
	// unowned pods, the pre-S9 posture.
	OwnerDeployment string

	// Namespace and WorkerImage configure the dark spawn path.
	Namespace   string
	WorkerImage string
	// SessionHarness is the harness every session pod runs, passed to it as
	// A2A_HARNESS (A2A_SESSION_HARNESS, from the PlatformAgent's
	// spec.harness.sessionWorker.type). Empty is the worker's default,
	// Claude Code, and adds nothing to the pod.
	SessionHarness string
	// SessionHarnessRefused is an A2A_SESSION_HARNESS value outside
	// sessionHarnesses (CRD or image skew). The gateway is shared by every
	// conversation, so it keeps serving and spawns sessions on the default
	// harness, and main logs this at error level, rather than refusing to
	// start and taking the whole chat surface down.
	SessionHarnessRefused string

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
	// New refuses the view without it. CredentialProxyExecURL is the
	// broker's exec tier, empty while the operator renders none; the shim
	// sends its commands there and falls back to CredentialProxyURL.
	// KubectlCostTier is the sizing tier whose column of the kubectl cost
	// table (deploy/sandbox/kubectl-costs.json) the session pod's kubectl
	// admission charges; spawn passes it on as KUBEAGENTS_KUBECTL_COST_TIER
	// beside KUBE_PROXY_URL. The operator forwards it normalised, as for the
	// shell sandbox; anything but a plain lowercase name is dropped here too,
	// and empty passes the admission's default tier by name.
	SessionClusterView     bool
	CredentialProxyURL     string
	CredentialProxyExecURL string
	KubeProxyURL           string
	KubeProxyCAConfigMap   string
	KubeProxyHostContext   string
	// KubeProxyMode is the policy mode the operator gives kube-proxy
	// (A2A_KUBE_PROXY_MODE). Session pods get it as KUBE_PROXY_MODE, and
	// their kubectl wrapper records a failed write attempt only under a
	// read-only mode. Empty records nothing.
	KubeProxyMode   string
	KubectlCostTier string
	UniverseDomain  string
	ImagePullRefs   []string

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
	// TriggerRatePerHour is the per-trigger cap on submissions the trigger
	// ingress accepts (trigger_ingress.go): a token bucket that holds this
	// many and refills at this rate. Zero takes DefaultTriggerRatePerHour.
	TriggerRatePerHour int
	// TriggerEventKinds are the event triggers (#6572) whose submissions the
	// trigger ingress admits, each <source>/<kind>: a source the callout
	// binds to its own identity, and one kind of event it reports
	// (A2A_TRIGGER_EVENT_KINDS, comma-separated). Empty, the default, admits
	// none: only the schedule source submits. Listing kinds, not sources,
	// bounds the rate buckets a source can open to the kinds named here.
	TriggerEventKinds []string
	// TriggerEventRatePerHour is the cap on each admitted event kind's
	// submissions, a bucket per <source>/<kind>. Zero takes
	// DefaultTriggerEventRatePerHour.
	TriggerEventRatePerHour int

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
	// namespace. At the cap the session pod idle longest is evicted to make
	// room, and its conversation told; only with none idle is the new
	// delegation (or session-routed turn) refused, with a chat reply naming
	// the numbers. Nothing queues, nothing drops silently.
	//
	// Zero means 10, the harness spike's "busy day": at the worker shape's
	// default requests (150m/320Mi, SessionResources) ten concurrent sessions
	// hold 1.5 CPU / about 3.1Gi, which a small dev cluster absorbs without
	// preemption. Raising it buys more
	// concurrent delegations at that per-pod price plus model-quota
	// contention; lowering it turns busy-hour delegations into refusals
	// sooner - a UX decision, not a safety one, because this cap is the
	// usability half. The enforcement half is the namespace ResourceQuota
	// the operator renders above this number (a compromised or buggy gateway
	// ignores its own cap and cannot ignore that one). The gateway serializes
	// the check and counts slots granted but not yet spawned, so concurrent
	// conversations don't race past the cap (#6033); a replacing turn or a
	// leader handover can still overshoot it briefly, which the quota's
	// margin absorbs.
	MaxSessions int

	// SessionResources are the session pod's CPU and memory requests and
	// limits (#2371), from A2A_SESSION_{CPU,MEMORY}_{REQUEST,LIMIT}, which
	// the operator renders resolved from spec.harness.tuning.sessionResources
	// (always all four). An unset variable takes spawn.go's default (the
	// operator module's DefaultSession* constants, held equal by a test). A
	// request above its limit is refused at start: the operator never
	// renders one, so it's a hand-set value the API server would refuse at
	// every spawn.
	SessionResources corev1.ResourceRequirements

	// DelegationDepthMax bounds how deep a delegation chain may run
	// (A2A_DELEGATION_DEPTH_MAX). A human turn is depth 0, a child its
	// parent's depth plus one, and a turn already at the bound may not
	// delegate again. One child at a time means the chain is a line, and
	// this bounds its length: a harness that delegates in a loop stops at
	// the bound instead of walking the session cap. Since a wake's pod has
	// no delegate tool (delegateToolFor), a chain from a stock worker is a
	// human turn and its child, so the bound is the backstop for a worker
	// that delegates without the tool, not the everyday limit.
	//
	// Zero means 3. FromEnv refuses a value under 1 rather than clamping
	// it: 0 would be "delegation off", which is a different switch
	// (A2A_DELEGATE_TOOL on the worker side), not a typo to paper over.
	DelegationDepthMax int

	// DelegationWidthMax bounds how many children one delegation may ask
	// for (A2A_DELEGATION_WIDTH_MAX): a session's batch of more is refused
	// whole, with a notice naming the bound. Each turn's delegateTargets
	// part advertises it when over one, and the session's tool offers a
	// batch only then. Width bounds what one conversation asks for, not
	// what runs at once: the dispatcher queues children to each profile's
	// concurrency.
	//
	// Zero means 1, a single child. FromEnv defaults it to 10 and accepts 1
	// to 10 (delegationWidthBuiltMax): a batch's results come back in one
	// wake, and one stop cancels every child of the tree. At the `cluster`
	// profile's default concurrency of 2, ten children run two at a time,
	// about five rounds. Tests and embedders that build Config directly may
	// set up to lib.DelegateBatchMax. FromEnv refuses a value under 1, as it
	// does the depth bound, and one over 10.
	DelegationWidthMax int

	// ScopeFile is the rendered spec.scope a profile task's target is
	// checked against (A2A_SCOPE_FILE; checkTargetScope). It is read per
	// routed task, so an edit reaches the gateway without a restart. A
	// missing or unreadable file refuses every target.
	ScopeFile string

	// ManagementProject is the install's own project
	// (A2A_MANAGEMENT_PROJECT, spec.harness.projectId). checkTargetScope
	// admits it whatever the scope lists, and ignores an exclude.projects
	// entry that matches it, as the reconcile does. Empty admits nothing
	// extra.
	ManagementProject string

	// ResolvedScopeConfigMap names the ConfigMap the scope publisher writes
	// the resolved scope into (A2A_RESOLVED_SCOPE_CONFIGMAP), which
	// Gateway.checkScope reads for a target a folder, organization or selector
	// reaches. Empty leaves such a target unconfirmed.
	ResolvedScopeConfigMap string

	// BusyNoticeAt is how many tasks have to be ahead of a new fixed-route
	// turn before the gateway marks the turn's status line queued
	// (A2A_BUSY_NOTICE_AT). The count is the fixed addressee's outstanding
	// work, read from session-state (fixedRouteBacklog in busy.go); at or
	// above this number the turn's task is still submitted, and its status
	// line is edited to a queued state that says how many are ahead
	// (showBusy). Nothing is refused or dropped.
	//
	// Zero means 10, the rendered bridge's default BRIDGE_CONCURRENCY. The
	// operator renders the bridge's worker count here, so a turn is told it
	// is waiting exactly when every worker is taken; raising it past the
	// worker count makes the notice rarer and later, lowering it under the
	// count tells people about a wait that is not there. FromEnv refuses a
	// value under 1.
	BusyNoticeAt int
}

// Backends lists every REAL chat backend this config arms, in backendArms'
// order (gchat, slack, teams, discord: BackendGchat, BackendSlack, BackendTeams, BackendDiscord),
// or nil when none is armed. The doors are not among the answers, for the
// reason Backend gives.
func (c *Config) Backends() []string {
	var backends []string
	for _, arm := range backendArms {
		if arm.armed(c) {
			backends = append(backends, arm.name)
		}
	}
	return backends
}

// Backend names the first REAL chat backend of Backends: "gchat", "slack",
// "teams", "discord", or "" when a side door (inject, A2A, or both) or the console
// door alone is the only way in. It is the authority default; the door
// default (inject, a2a or console) is chosen in New, not here.
//
// The doors are deliberately not among the answers. Either can be armed
// beside any backend, so "which backend is this gateway" and "is a door
// open" are two questions, and collapsing them is what would make a door
// exclusive again.
func (c *Config) Backend() string {
	if backends := c.Backends(); len(backends) > 0 {
		return backends[0]
	}
	if c.InjectListen != "" || c.A2ADoorListen != "" || c.ConsoleDoor {
		// No real backend: a door is the whole of the ingress, and the
		// attribution on a message that comes through it is the door's own.
		return ""
	}
	// A hand-built Config with nothing armed at all. FromEnv refuses this;
	// a test that builds a Config directly gets the historical default
	// rather than an empty backend string in its authority blocks.
	return discordBackend
}

// InjectArmed reports whether the side door is open.
func (c *Config) InjectArmed() bool { return c.InjectListen != "" }

// HomeLearnArmed reports whether the gateway may learn the Slack home channel
// from the first admitted channel mention and record it: the Slack backend
// is armed (beside any other), the CR names no home channel (a set one is
// the CR's, malformed or not), the policy is auto, a record is named, and the
// operator has armed the learn. Every gate holds, or nothing is read, written or posted.
func (c *Config) HomeLearnArmed() bool {
	return c.SlackBotToken != "" && c.SlackHomeChannel == "" &&
		c.SlackHomePolicy != slackHomePolicyManual && c.HomeChannelRecord != "" && c.HomeChannelLearn
}

// GchatHomeLearnArmed is HomeLearnArmed for the Google Chat home space: the
// Chat backend is armed (beside any other), the CR names no home channel,
// the policy is auto, a record is named, and the operator has armed the
// learn. The switch is the one Slack's learn reads: an operator that renders
// it without projecting Chat's file leaves the file absent, and an absent
// file learns nothing (homeLearner.learn).
func (c *Config) GchatHomeLearnArmed() bool {
	return c.GchatRelayURL != "" && c.GchatHomeChannel == "" &&
		c.GchatHomePolicy != gchatHomePolicyManual && c.HomeChannelRecord != "" && c.HomeChannelLearn
}

// HomeLearnArmedFor is the learn's gate for one backend by name
// (BackendSlack, BackendGchat); no other backend learns a home.
func (c *Config) HomeLearnArmedFor(backend string) bool {
	switch backend {
	case BackendSlack:
		return c.HomeLearnArmed()
	case BackendGchat:
		return c.GchatHomeLearnArmed()
	}
	return false
}

// Elected reports whether this gateway runs under the election (election.go).
func (c *Config) Elected() bool { return c.GatewayLease != "" }

// A2ADoorArmed reports whether the A2A door is open.
func (c *Config) A2ADoorArmed() bool { return c.A2ADoorListen != "" }

// The values A2A_GATEWAY_BUS_AUTH takes.
const (
	BusAuthPassword = "password"
	BusAuthToken    = "token"

	envGatewayBusAuth = "A2A_GATEWAY_BUS_AUTH"
)

// FromEnv loads the config from the environment.
func FromEnv() (*Config, error) {
	cfg := &Config{
		NATSURL:          os.Getenv("NATS_URL"),
		NATSCAFile:       os.Getenv(lib.EnvNATSCAFile),
		NATSCAConfigMap:  os.Getenv(EnvNATSCAConfigMap),
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
		SessionHarness:   os.Getenv(envSessionHarness),

		SessionServiceAccount:  os.Getenv("A2A_SESSION_SERVICE_ACCOUNT"),
		StrictEventsWriter:     os.Getenv("A2A_STRICT_EVENTS_WRITER") == "true",
		SessionClusterView:     os.Getenv("A2A_SESSION_CLUSTER_VIEW") == "true",
		CredentialProxyURL:     os.Getenv("A2A_CREDENTIAL_PROXY_URL"),
		CredentialProxyExecURL: os.Getenv("A2A_CREDENTIAL_PROXY_EXEC_URL"),
		KubeProxyURL:           os.Getenv("A2A_KUBE_PROXY_URL"),
		KubeProxyCAConfigMap:   os.Getenv("A2A_KUBE_PROXY_CA_CONFIGMAP"),
		KubeProxyHostContext:   os.Getenv("A2A_KUBE_PROXY_HOST_CONTEXT"),
		KubeProxyMode:          os.Getenv("A2A_KUBE_PROXY_MODE"),
		KubectlCostTier:        kubectlCostTier(os.Getenv("A2A_KUBECTL_COST_TIER")),
		UniverseDomain:         strings.TrimSpace(os.Getenv(envGoogleCloudUniverseDomain)),
		AuthorityTier:          capability.Tier(envOr("A2A_AUTHORITY_TIER", string(capability.TierDeveloperTeam))),
		AuthorityScope:         capability.Scope(os.Getenv("A2A_AUTHORITY_SCOPE")),
		CapabilityOptional:     capability.OptionalFromEnv(),
		ScopeFile:              envOr("A2A_SCOPE_FILE", defaultScopeFile),
		ManagementProject:      os.Getenv("A2A_MANAGEMENT_PROJECT"),
		ResolvedScopeConfigMap: os.Getenv("A2A_RESOLVED_SCOPE_CONFIGMAP"),
	}
	cfg.GchatRelayURL = os.Getenv("A2A_GCHAT_RELAY_URL")
	cfg.GchatTokenPath = envOr("A2A_GCHAT_TOKEN_PATH", defaultGchatTokenPath)
	for _, u := range strings.Split(os.Getenv("A2A_GCHAT_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.GchatAllowedUsers = append(cfg.GchatAllowedUsers, u)
		}
	}
	cfg.GchatAllowAllUsers = os.Getenv("A2A_GCHAT_ALLOW_ALL_USERS") == "true"
	for _, kind := range strings.Split(os.Getenv(envTriggerEventKinds), ",") {
		if kind = strings.TrimSpace(kind); kind != "" {
			cfg.TriggerEventKinds = append(cfg.TriggerEventKinds, kind)
		}
	}
	seenPullRefs := make(map[string]struct{})
	for _, refName := range strings.Split(os.Getenv(envImagePullRefs), imagePullRefSep) {
		refName = strings.TrimSpace(refName)
		if refName == "" {
			continue
		}
		if _, dup := seenPullRefs[refName]; dup {
			continue
		}
		seenPullRefs[refName] = struct{}{}
		cfg.ImagePullRefs = append(cfg.ImagePullRefs, refName)
	}
	cfg.GchatHomeChannel = strings.TrimSpace(os.Getenv(envGchatHomeChannel))
	cfg.TeamsRelayURL = strings.TrimSpace(os.Getenv(envTeamsRelayURL))
	cfg.TeamsTokenPath = envOr(envTeamsTokenPath, cfg.GchatTokenPath)
	for _, u := range strings.Split(os.Getenv(envTeamsAllowedUsers), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.TeamsAllowedUsers = append(cfg.TeamsAllowedUsers, u)
		}
	}
	cfg.TeamsAllowAllUsers = os.Getenv(envTeamsAllowAllUsers) == envTrue
	cfg.TeamsHomeChannel = strings.TrimSpace(os.Getenv(envTeamsHomeChannel))
	cfg.TeamsTenantID = strings.TrimSpace(os.Getenv(envTeamsTenantID))
	cfg.TeamsAdaptiveCards = strings.TrimSpace(os.Getenv(envTeamsAdaptiveCards)) != strconv.FormatBool(false)
	cfg.SlackHomeChannel = strings.TrimSpace(os.Getenv(envSlackHomeChannel))
	cfg.SlackHomePolicy = strings.TrimSpace(os.Getenv(envSlackHomePolicy))
	cfg.GchatHomePolicy = strings.TrimSpace(os.Getenv(envGchatHomePolicy))
	cfg.HomeChannelRecord = strings.TrimSpace(os.Getenv(envHomeChannelRecord))
	cfg.SelfImage = strings.TrimSpace(os.Getenv(envSelfImage))
	cfg.HomeChannelLearn = os.Getenv(envHomeChannelLearn) == envTrue
	cfg.GatewayLease = strings.TrimSpace(os.Getenv(envGatewayLease))
	cfg.PodName = strings.TrimSpace(os.Getenv(envPodName))
	if cfg.GatewayLease != "" && cfg.PodName == "" {
		// The pod name is the election's identity; without it every replica
		// would campaign as the same holder.
		return nil, fmt.Errorf("%s is set but %s is empty: the election needs the pod's name as its identity", envGatewayLease, envPodName)
	}
	cfg.AnswerFeedbackOff = strings.TrimSpace(os.Getenv(envSlackAnswerFeedback)) == strconv.FormatBool(false) ||
		strings.TrimSpace(os.Getenv(envGchatAnswerFeedback)) == strconv.FormatBool(false)
	cfg.PeopleSeenOff = strings.TrimSpace(os.Getenv(envPeopleSeen)) == strconv.FormatBool(false)
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
	if raw, ok := os.LookupEnv(EnvTargetAllowedUsersTeams); ok {
		platformLists[teamsBackend] = append([]string{}, splitList(raw)...)
	}
	if len(platformLists) > 0 {
		cfg.TargetAllowedUsers[targetPlatform] = platformLists
	}
	members, err := parseTierMembership(os.Getenv(EnvTierMembership))
	if err != nil {
		return nil, err
	}
	cfg.TierMembership = members
	for _, u := range strings.Split(os.Getenv("A2A_SLACK_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.SlackAllowedUsers = append(cfg.SlackAllowedUsers, u)
		}
	}
	cfg.SlackAllowAllUsers = os.Getenv("A2A_SLACK_ALLOW_ALL_USERS") == "true"
	cfg.SlackTestUsers = splitList(os.Getenv(EnvSlackTestUsers))
	cfg.RelayPrecreated = os.Getenv("A2A_GATEWAY_RELAY_PRECREATED") == "true"
	cfg.InjectListen = strings.TrimSpace(os.Getenv("A2A_INJECT_LISTEN"))
	cfg.InjectToken = strings.TrimSpace(os.Getenv("A2A_INJECT_TOKEN"))
	cfg.InjectPrincipalMapPath = envOr("A2A_INJECT_PRINCIPAL_MAP", defaultInjectPrincipalMapPath)
	cfg.InjectConfirm = os.Getenv("A2A_INJECT_CONFIRM") == "true"
	if cfg.InjectConfirm && cfg.InjectListen == "" {
		return nil, fmt.Errorf("A2A_INJECT_CONFIRM is set but the inject door is not armed (A2A_INJECT_LISTEN)")
	}
	cfg.A2ADoorListen = strings.TrimSpace(os.Getenv("A2A_DOOR_LISTEN"))
	cfg.ConsoleDoor = os.Getenv("A2A_CONSOLE_DOOR") == "true"
	cfg.A2ADoorToken = strings.TrimSpace(os.Getenv("A2A_DOOR_TOKEN"))
	cfg.A2ADoorPrincipalMapPath = envOr("A2A_DOOR_PRINCIPAL_MAP", defaultA2ADoorPrincipalMapPath)
	cfg.A2ADoorPublicURL = strings.TrimSpace(os.Getenv("A2A_DOOR_PUBLIC_URL"))
	cfg.A2ADoorGoogleClientID = strings.TrimSpace(os.Getenv("A2A_DOOR_GOOGLE_CLIENT_ID"))
	for _, u := range strings.Split(os.Getenv("A2A_DOOR_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.A2ADoorAllowedUsers = append(cfg.A2ADoorAllowedUsers, u)
		}
	}
	cfg.A2ADoorEntraTenantID = strings.TrimSpace(os.Getenv(envA2ADoorEntraTenantID))
	cfg.A2ADoorEntraClientID = strings.TrimSpace(os.Getenv(envA2ADoorEntraClientID))
	cfg.A2ADoorEntraAllowedUsers = splitList(os.Getenv(envA2ADoorEntraAllowedUsers))
	metricsPort, err := metricsPortFromEnv(cfg)
	if err != nil {
		return nil, err
	}
	cfg.MetricsPort = metricsPort
	cfg.DisplayMode = envOr("A2A_CHAT_DISPLAY_MODE", displayModeDebug)
	if cfg.DisplayMode != displayModeDefault && cfg.DisplayMode != displayModeDebug {
		return nil, fmt.Errorf("A2A_CHAT_DISPLAY_MODE %q: want %q or %q", cfg.DisplayMode, displayModeDefault, displayModeDebug)
	}
	if cfg.SessionHarness != "" && !sessionHarnesses[cfg.SessionHarness] {
		cfg.SessionHarnessRefused, cfg.SessionHarness = cfg.SessionHarness, ""
	}
	if cfg.NATSURL == "" {
		return nil, fmt.Errorf("NATS_URL is required")
	}
	// The CA and the ConfigMap it came from arm the session pods' trust
	// together. With the file alone the gateway itself connects, while every
	// session pod it spawns gets the tls:// URL and no CA, and fails
	// verification on its first connect.
	if (cfg.NATSCAFile != "") != (cfg.NATSCAConfigMap != "") {
		return nil, fmt.Errorf("%s and %s arm the session pods' bus trust together; only one is set", lib.EnvNATSCAFile, EnvNATSCAConfigMap)
	}
	// Socket Mode needs the whole Slack pair; half a pair is a typo, not a
	// choice, so it refuses rather than silently running another backend.
	if (cfg.SlackBotToken != "") != (cfg.SlackAppToken != "") {
		return nil, fmt.Errorf("SLACK_BOT_TOKEN and SLACK_APP_TOKEN arm Slack together; only one is set")
	}
	// The gateway runs every REAL backend whose credential is set. Several
	// in one process split nothing: each backend is its own transport (Chat
	// a Pub/Sub pull through the broker, Slack a Socket Mode connection,
	// Discord its gateway websocket) and none shares a durable with another.
	// The task-event relay durable is the gateway process's own, bound once
	// per process, and the election keeps one leader per Deployment. What
	// the guard still refuses is half a Slack pair (above) and no ingress at
	// all (below).
	//
	// The doors (inject, A2A) are NOT among Backends, decided 2026-09-17 on
	// the design doc's review for the inject door and holding for its
	// sibling. A local HTTP door consumes nothing and competes for nothing,
	// so it is a way in beside the backends rather than one of them.
	if len(cfg.Backends()) == 0 {
		// The door alone is enough to start. Decided, not assumed: the A2A
		// owner's decision of 2026-09-17 on the eval transport's design doc
		// (eval-next-transport.md) is that an eval install with neither a
		// Discord token nor a Chat relay starts its gateway on the door
		// instead of crash-looping, so a `mode: next` install reads Ready
		// and a rollout can gate on it. The gateway logs that it is
		// inject-only when it does, and says so on the door's read route,
		// because a next install whose relay URL failed to render looks the
		// same. The spec's test-backend section states the same decision.
		// The console door alone is enough too (A2A_CONSOLE_DOOR), for the
		// same reason: a console client such as the MCP server needs a
		// gateway on an install that configured no chat backend.
		if cfg.InjectListen == "" && cfg.A2ADoorListen == "" && !cfg.ConsoleDoor {
			return nil, fmt.Errorf("no chat backend: set DISCORD_TOKEN (W0's discord-bot Secret), A2A_GCHAT_RELAY_URL (the credential proxy's chat relay), A2A_TEAMS_RELAY_URL (its Teams relay), the SLACK_BOT_TOKEN+SLACK_APP_TOKEN pair (Socket Mode), A2A_INJECT_LISTEN (the dev-only inject side door), A2A_DOOR_LISTEN (the A2A door for agent callers), or A2A_CONSOLE_DOOR=true (the console door alone)")
		}
	}
	// Fail closed: a door with no token would be reachable by anything that
	// reaches the listener, and the port-forward path the runner uses is
	// served from inside the pod, past both the loopback bind and the
	// NetworkPolicy in front of it. There is deliberately no opt-out — see
	// Config.InjectToken.
	if cfg.InjectListen != "" && cfg.InjectToken == "" {
		return nil, fmt.Errorf("A2A_INJECT_TOKEN is required when A2A_INJECT_LISTEN is set: the inject door authenticates every request with a bearer token, because neither its loopback bind nor the NetworkPolicy in front of it governs the port-forward path its caller uses")
	}
	if cfg.A2ADoorGoogleClientID != "" && cfg.A2ADoorListen == "" {
		return nil, fmt.Errorf("A2A_DOOR_GOOGLE_CLIENT_ID is set but A2A_DOOR_LISTEN is not: the Google sign-in class is part of the A2A door and has nothing to arm without it")
	}
	if (cfg.A2ADoorEntraTenantID == "") != (cfg.A2ADoorEntraClientID == "") {
		return nil, fmt.Errorf("%s and %s are set together or not at all: the Microsoft Entra sign-in class checks a token against one tenant's keys for one application", envA2ADoorEntraTenantID, envA2ADoorEntraClientID)
	}
	// The token's tid claim is always the directory's GUID, and the door
	// compares it to this value, so the tenant's domain name (which the
	// Entra authority also accepts in a URL) would fetch the keys and then
	// refuse every token as another tenant's. The client id is a GUID too.
	for name, value := range map[string]string{envA2ADoorEntraTenantID: cfg.A2ADoorEntraTenantID, envA2ADoorEntraClientID: cfg.A2ADoorEntraClientID} {
		if value != "" && !a2aEntraObjectIDPattern.MatchString(value) {
			return nil, fmt.Errorf("%s is %q, not a GUID: use the portal's \"Directory (tenant) ID\" and \"Application (client) ID\" values, not a domain name", name, value)
		}
	}
	// The allowlist names object ids; a UPN or an email can never match the
	// GUID a token carries, so a list of them would admit nobody with no
	// sign of why.
	for _, u := range cfg.A2ADoorEntraAllowedUsers {
		if !a2aEntraObjectIDPattern.MatchString(u) {
			return nil, fmt.Errorf("%s entry %q is not an object id (a GUID): the Entra class lists people by object id, not by UPN or email", envA2ADoorEntraAllowedUsers, u)
		}
	}
	if cfg.A2ADoorEntraTenantID != "" && cfg.A2ADoorListen == "" {
		return nil, fmt.Errorf("%s is set but A2A_DOOR_LISTEN is not: the Microsoft Entra sign-in class is part of the A2A door and has nothing to arm without it", envA2ADoorEntraTenantID)
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
	for _, kind := range cfg.TriggerEventKinds {
		// Neither "schedule" nor "run-now" as a source: each opens a
		// schedule's idempotency key, and an event source's keys must not
		// spell one.
		src, k, ok := strings.Cut(kind, "/")
		if !ok || !lib.ValidSubjectToken(src) || !lib.ValidSubjectToken(k) || src == lib.TriggerSourceSchedule || src == lib.TriggerRunNowKeyPrefix {
			return nil, fmt.Errorf(envTriggerEventKinds+" entry %q is not <source>/<kind> of dot-free DNS-1123 labels for an event source", kind)
		}
	}
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
	sessionResources, err := sessionResourcesFromEnv()
	if err != nil {
		return nil, err
	}
	cfg.SessionResources = sessionResources
	depthMax := envOr("A2A_DELEGATION_DEPTH_MAX", strconv.Itoa(defaultDelegationDepthMax))
	dm, err := strconv.Atoi(depthMax)
	if err != nil || dm < 1 {
		return nil, fmt.Errorf("A2A_DELEGATION_DEPTH_MAX %q: need an integer >= 1", depthMax)
	}
	cfg.DelegationDepthMax = dm
	widthMax := envOr("A2A_DELEGATION_WIDTH_MAX", strconv.Itoa(defaultDelegationWidthMax))
	wm, err := strconv.Atoi(widthMax)
	if err != nil || wm < 1 || wm > lib.DelegateBatchMax {
		return nil, fmt.Errorf("A2A_DELEGATION_WIDTH_MAX %q: need an integer from 1 to %d", widthMax, lib.DelegateBatchMax)
	}
	if wm > delegationWidthBuiltMax {
		return nil, fmt.Errorf("A2A_DELEGATION_WIDTH_MAX %q: at most %d", widthMax, delegationWidthBuiltMax)
	}
	cfg.DelegationWidthMax = wm
	busyAt := envOr(busyNoticeAtEnv, strconv.Itoa(defaultBusyNoticeAt))
	ba, err := strconv.Atoi(busyAt)
	if err != nil || ba < 1 {
		return nil, fmt.Errorf("%s %q: need an integer >= 1", busyNoticeAtEnv, busyAt)
	}
	cfg.BusyNoticeAt = ba
	ttl := envOr("A2A_IDLE_TTL", "30m")
	d, err := time.ParseDuration(ttl)
	if err != nil {
		return nil, fmt.Errorf("A2A_IDLE_TTL %q: %w", ttl, err)
	}
	if d < time.Minute {
		return nil, fmt.Errorf("A2A_IDLE_TTL %q is under the 1m floor; an instant reap deletes pods mid-conversation", ttl)
	}
	cfg.IdleTTL = d
	dmTTL := envOr("A2A_DM_IDLE_TTL", defaultDMIdleTTL.String())
	dmIdle, err := time.ParseDuration(dmTTL)
	if err != nil {
		return nil, fmt.Errorf("A2A_DM_IDLE_TTL %q: %w", dmTTL, err)
	}
	if dmIdle < time.Minute {
		return nil, fmt.Errorf("A2A_DM_IDLE_TTL %q is under the 1m floor; an instant reap deletes pods mid-conversation", dmTTL)
	}
	cfg.DMIdleTTL = dmIdle

	// The adapter's deadline, in the adapter's own units (integer seconds) —
	// see the field comment for why the env name is shared.
	deadlineSecs := envOr("A2A_TASK_DEADLINE_SECONDS", strconv.Itoa(int(defaultTaskDeadline/time.Second)))
	secs, err := strconv.Atoi(deadlineSecs)
	if err != nil || secs < 60 {
		return nil, fmt.Errorf("A2A_TASK_DEADLINE_SECONDS %q: need an integer >= 60; a sub-minute deadline kills pods mid-cold-start", deadlineSecs)
	}
	cfg.TaskDeadline = time.Duration(secs) * time.Second

	cfg.SessionReuse = os.Getenv("A2A_SESSION_REUSE") != "false"
	lifetime := envOr("A2A_SESSION_MAX_LIFETIME", defaultSessionMaxLifetime.String())
	lt, err := time.ParseDuration(lifetime)
	if err != nil {
		return nil, fmt.Errorf("A2A_SESSION_MAX_LIFETIME %q: %w", lifetime, err)
	}
	if floor := cfg.TaskDeadline + 2*podDeadlineGrace; lt < floor {
		return nil, fmt.Errorf("A2A_SESSION_MAX_LIFETIME %q is under %v, A2A_TASK_DEADLINE_SECONDS plus twice the pod deadline grace; a pod that cannot outlive one task cannot serve a conversation", lifetime, floor)
	}
	cfg.SessionMaxLifetime = lt

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

	cfg.CardStop = os.Getenv("A2A_CARD_STOP") == "true"
	cardStopTimeout := envOr("A2A_CARD_STOP_TIMEOUT", defaultCardStopTimeout.String())
	cst, err := time.ParseDuration(cardStopTimeout)
	if err != nil {
		return nil, fmt.Errorf("A2A_CARD_STOP_TIMEOUT %q: %w", cardStopTimeout, err)
	}
	if cst < cardStopTimeoutFloor {
		return nil, fmt.Errorf("A2A_CARD_STOP_TIMEOUT %q is under the %v floor; the bridge alone may wait 20s for the turn the stop cancelled", cardStopTimeout, cardStopTimeoutFloor)
	}
	cfg.CardStopTimeout = cst

	cfg.OwnerDeployment = os.Getenv("A2A_OWNER_DEPLOYMENT")

	switch cfg.BusAuth = envOr(envGatewayBusAuth, BusAuthPassword); cfg.BusAuth {
	case BusAuthPassword:
	case BusAuthToken:
		cfg.BusTokenFile = envOr(lib.EnvBusTokenFile, lib.BusTokenPath)
	default:
		return nil, fmt.Errorf("%s=%q: want %q or %q", envGatewayBusAuth, cfg.BusAuth, BusAuthPassword, BusAuthToken)
	}

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
	case cfg.BusAuth == BusAuthToken:
		// The derived fallback keys on the bus password, which a gateway
		// on its token no longer presents and which goes from nats.conf a
		// release after the flip. Deriving from it here would work until
		// then and re-key every pseudonym the day it went, silently
		// unjoining the audit trail; refusing the boot names the gap now.
		return nil, fmt.Errorf("%s=%s needs SESSION_KV_SALT or A2A_ATTRIBUTION_SALT: the derived fallback keys on NATS_PASSWORD, which token auth retires", envGatewayBusAuth, BusAuthToken)
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

// kubectlCostTierPattern is the tier name the operator forwards (its own
// kubectlCostTierPattern) and the sandbox entrypoint accepts.
var kubectlCostTierPattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// kubectlCostTier is a forwarded tier name if it is a plain lowercase one,
// else empty: the session pod's admission then charges its default tier.
func kubectlCostTier(raw string) string {
	if tier := strings.TrimSpace(raw); kubectlCostTierPattern.MatchString(tier) {
		return tier
	}
	return ""
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

// defaultDMIdleTTL is A2A_DM_IDLE_TTL's default (#2371).
const defaultDMIdleTTL = 5 * time.Minute

// singleTurnDM reports whether rec is a DM whose current pod has taken at
// most one human turn and no steer (SteeredSession). Two things are left out
// of the count:
//   - a wake (Role set): a session pod without cluster access delegates every
//     cluster read, and the child's result comes back as a wake addressed to
//     the pod, so counting wakes would put nearly every one-question DM on
//     IdleTTL (seen live on ka-fleet-dev-1). A delegation still running holds
//     the conversation's active slot (the child, or a fan-out's parent), which
//     already exempts the pod from the reap, so leaving it out of the count
//     never reaps a pod with a result still to come;
//   - a turn addressed to an earlier pod: a DM keyed by its channel or space
//     (Discord, an unthreaded Google Chat DM) keeps one record across every
//     question the user asks, and each pod mints a fresh bus session.
func (rec *SessionRecord) singleTurnDM() bool {
	if rec.Kind != sessionKindDM {
		return false
	}
	// A follow-up steered into the running task is a second message for this
	// pod even though it started no task.
	if rec.SteeredSession != "" && rec.SteeredSession == rec.BusSession {
		return false
	}
	n := 0
	for _, t := range rec.Tasks {
		if t.Role == "" && t.Addressee == rec.BusSession {
			n++
		}
	}
	return n <= 1
}

// idleTTLFor is the reap threshold for rec: DMIdleTTL for a single-turn DM,
// IdleTTL otherwise. DMIdleTTL is capped at IdleTTL, so setting it higher
// never keeps a DM's pod longer than any other conversation's, and zero (a
// Config built without it) reads as IdleTTL.
func (c *Config) idleTTLFor(rec *SessionRecord) time.Duration {
	if c.DMIdleTTL > 0 && c.DMIdleTTL < c.IdleTTL && rec.singleTurnDM() {
		return c.DMIdleTTL
	}
	return c.IdleTTL
}

// idleExpired reports whether rec's pod has been idle past its TTL at now.
// The clock is the last user message (idleSince). Under the short DM TTL it
// is the answer too, whichever is later (LastTaskActivity, which an
// executor's terminal moves): a DM task that ran longer than DMIdleTTL would
// otherwise lose its pod the moment the answer posts, leaving no time to
// follow up. Under IdleTTL the answer does not move it, as before.
func (c *Config) idleExpired(rec *SessionRecord, now time.Time) bool {
	ttl := c.idleTTLFor(rec)
	since := rec.idleSince()
	if ttl < c.IdleTTL && rec.LastTaskActivity.After(since) {
		since = rec.LastTaskActivity
	}
	return now.Sub(since) >= ttl
}

// sessionResourcesFromEnv reads the session pod's requests and limits from
// A2A_SESSION_{CPU,MEMORY}_{REQUEST,LIMIT}, each defaulting to spawn.go's
// value, and refuses an unparsable quantity or a request above its limit.
func sessionResourcesFromEnv() (corev1.ResourceRequirements, error) {
	read := func(env, def string) (resource.Quantity, error) {
		raw := strings.TrimSpace(envOr(env, def))
		q, err := resource.ParseQuantity(raw)
		if err != nil || q.Sign() <= 0 {
			return resource.Quantity{}, fmt.Errorf("%s %q: need a positive Kubernetes quantity", env, raw)
		}
		return q, nil
	}
	out := corev1.ResourceRequirements{Requests: corev1.ResourceList{}, Limits: corev1.ResourceList{}}
	for _, f := range []struct {
		set  corev1.ResourceList
		name corev1.ResourceName
		env  string
		def  string
	}{
		{out.Requests, corev1.ResourceCPU, "A2A_SESSION_CPU_REQUEST", workerCPURequest},
		{out.Requests, corev1.ResourceMemory, "A2A_SESSION_MEMORY_REQUEST", workerMemoryRequest},
		{out.Limits, corev1.ResourceCPU, "A2A_SESSION_CPU_LIMIT", workerCPULimit},
		{out.Limits, corev1.ResourceMemory, "A2A_SESSION_MEMORY_LIMIT", workerMemoryLimit},
	} {
		q, err := read(f.env, f.def)
		if err != nil {
			return corev1.ResourceRequirements{}, err
		}
		f.set[f.name] = q
	}
	for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
		if req, lim := out.Requests[name], out.Limits[name]; req.Cmp(lim) > 0 {
			return corev1.ResourceRequirements{}, fmt.Errorf("the session %s request %s is above its limit %s", name, req.String(), lim.String())
		}
	}
	return out, nil
}

// sessionResources is c's session pod requests and limits, each unset one
// at spawn.go's default (a Config built without LoadConfig, as tests do).
func (c *Config) sessionResources() corev1.ResourceRequirements {
	pick := func(set corev1.ResourceList, name corev1.ResourceName, def string) resource.Quantity {
		if q, ok := set[name]; ok && !q.IsZero() {
			return q.DeepCopy()
		}
		return resource.MustParse(def)
	}
	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{
			corev1.ResourceCPU:    pick(c.SessionResources.Requests, corev1.ResourceCPU, workerCPURequest),
			corev1.ResourceMemory: pick(c.SessionResources.Requests, corev1.ResourceMemory, workerMemoryRequest),
		},
		Limits: corev1.ResourceList{
			corev1.ResourceCPU:    pick(c.SessionResources.Limits, corev1.ResourceCPU, workerCPULimit),
			corev1.ResourceMemory: pick(c.SessionResources.Limits, corev1.ResourceMemory, workerMemoryLimit),
		},
	}
}

// envSessionHarness names the session worker's harness (Config.SessionHarness).
const envSessionHarness = "A2A_SESSION_HARNESS"

// sessionHarnesses are the harnesses the gateway spawns session pods for.
// Antigravity joins when its worker isolation lands.
var sessionHarnesses = map[string]bool{string(persona.ClaudeCode): true}
