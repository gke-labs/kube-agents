package gateway

import (
	"crypto/hkdf"
	"crypto/sha256"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

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

// defaultGchatTokenPath is where the operator projects the gateway's
// relay-audience ServiceAccount token when the gchat backend is armed.
const defaultGchatTokenPath = "/var/run/secrets/a2a-chat-relay/token"

// defaultInjectPrincipalMapPath is where the operator mounts the inject
// door's own principal map when the door is armed. A file of "id principal"
// lines rather than a directory of one file per id, because every key carries
// the inject: prefix and a colon is not a legal ConfigMap key.
const defaultInjectPrincipalMapPath = "/etc/a2a/inject-principal-map/principals"

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

	// PrincipalMapPath is the mounted principal-map ConfigMap.
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

	// IdleTTL is the reap threshold since the last user message (decided
	// 8/24: 30 minutes, config-backed).
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

	// AskTTL bounds the active task's `ask` copy in session-state
	// (A2A_ASK_TTL). The copy's stated justification — the same text rides
	// the W-bounded stream and the copy dies at the terminal event — holds
	// only where a terminal event is guaranteed, and the spec names the
	// case where it is not (a wedged adapter, until every pod carries its
	// deadline; fixed-route executors have no janitor until stage 3). So
	// the record gets an independent bound: the reap scan clears an ask
	// older than this, leaving the task record itself intact. Unset means
	// 24h — far above any legitimate task's runtime, well under the
	// stream's 72h retention, so the KV copy always has the shorter
	// horizon the content posture claims. Raising it toward the stream
	// retention erodes exactly that claim; lowering it only trims how long
	// a status card can echo the ask.
	AskTTL time.Duration

	// SessionTTL bounds the lifetime of idle session records in session-state
	// (A2A_SESSION_TTL). The session record holds contextId across pod
	// incarnations. Once a session has no active pod and no active task, and
	// has seen no activity for longer than SessionTTL, its record is deleted
	// from KV so session-state does not grow unbounded. Unset means 7 days
	// (168h), sitting comfortably past TASKS' 72h retention horizon.
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

	// StrictEventsWriter makes the `…events` writer-class agreement check a
	// refusal instead of a counted advisory (A2A_STRICT_EVENTS_WRITER=true).
	// It ships false: for one TASKS retention window after an install takes
	// the supervisor subject split, the stream still holds supervisor
	// terminals written on `…events` before it, and refusing those folds
	// every recent task non-terminal. Flip it no earlier than one retention
	// window (72h at the dev default) after the split reaches the install.
	StrictEventsWriter bool

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
}

// Backend names the REAL chat backend this config arms: "gchat", "discord",
// or "" when the inject side door is the only way in. FromEnv refuses more
// than one real backend, so the order here only decides what a hand-built
// Config means.
//
// The door is deliberately not one of the answers. It can be armed beside
// either backend, so "which backend is this gateway" and "is the door open"
// are two questions, and collapsing them is what would make the door
// exclusive again.
func (c *Config) Backend() string {
	switch {
	case c.GchatRelayURL != "":
		return gchatBackend
	case c.DiscordToken != "":
		return discordBackend
	case c.InjectListen != "":
		// No real backend: the door is the whole of the ingress, and the
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

// FromEnv loads the config from the environment.
func FromEnv() (*Config, error) {
	cfg := &Config{
		NATSURL:          os.Getenv("NATS_URL"),
		NATSUser:         os.Getenv("NATS_USER"),
		NATSPassword:     os.Getenv("NATS_PASSWORD"),
		DiscordToken:     os.Getenv("DISCORD_TOKEN"),
		PrincipalMapPath: envOr("A2A_PRINCIPAL_MAP", "/etc/a2a/principal-map"),
		DefaultAddressee: envOr("A2A_DEFAULT_ADDRESSEE", "platform"),
		SpawnSessions:    os.Getenv("A2A_SPAWN_SESSIONS") == "true",
		Namespace:        envOr("POD_NAMESPACE", "kubeagents-system"),
		WorkerImage:      envOr("A2A_WORKER_IMAGE", "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/worker-next:latest"),

		SessionServiceAccount: os.Getenv("A2A_SESSION_SERVICE_ACCOUNT"),
		StrictEventsWriter:    os.Getenv("A2A_STRICT_EVENTS_WRITER") == "true",
	}
	cfg.GchatRelayURL = os.Getenv("A2A_GCHAT_RELAY_URL")
	cfg.GchatTokenPath = envOr("A2A_GCHAT_TOKEN_PATH", defaultGchatTokenPath)
	for _, u := range strings.Split(os.Getenv("A2A_GCHAT_ALLOWED_USERS"), ",") {
		if u = strings.TrimSpace(u); u != "" {
			cfg.GchatAllowedUsers = append(cfg.GchatAllowedUsers, u)
		}
	}
	cfg.GchatAllowAllUsers = os.Getenv("A2A_GCHAT_ALLOW_ALL_USERS") == "true"
	cfg.InjectListen = strings.TrimSpace(os.Getenv("A2A_INJECT_LISTEN"))
	cfg.InjectToken = strings.TrimSpace(os.Getenv("A2A_INJECT_TOKEN"))
	cfg.InjectPrincipalMapPath = envOr("A2A_INJECT_PRINCIPAL_MAP", defaultInjectPrincipalMapPath)
	cfg.DisplayMode = envOr("A2A_CHAT_DISPLAY_MODE", displayModeDebug)
	if cfg.DisplayMode != displayModeDefault && cfg.DisplayMode != displayModeDebug {
		return nil, fmt.Errorf("A2A_CHAT_DISPLAY_MODE %q: want %q or %q", cfg.DisplayMode, displayModeDefault, displayModeDebug)
	}
	if cfg.NATSURL == "" {
		return nil, fmt.Errorf("NATS_URL is required")
	}
	// One REAL backend per gateway process, chosen by which variable is set.
	// A silent default here would make a two-backend misconfiguration a
	// working Discord gateway that quietly never consumes Chat — refuse both
	// directions instead. Counted rather than enumerated pairwise: with a
	// third backend the pairs are the easy thing to leave a hole in, and the
	// fourth (Slack, #1248) must not be addable with a combination nobody
	// checked. Adding a backend is one entry in this list.
	var armed []string
	if cfg.GchatRelayURL != "" {
		armed = append(armed, "A2A_GCHAT_RELAY_URL")
	}
	if cfg.DiscordToken != "" {
		armed = append(armed, "DISCORD_TOKEN")
	}
	// The inject door is NOT in that list, decided 2026-09-17 on the design
	// doc's review. The guard exists so that arming two backends cannot leave
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
		if cfg.InjectListen == "" {
			return nil, fmt.Errorf("no chat backend: set DISCORD_TOKEN (W0's discord-bot Secret), A2A_GCHAT_RELAY_URL (the credential proxy's chat relay), or A2A_INJECT_LISTEN (the dev-only inject side door)")
		}
	default:
		return nil, fmt.Errorf("more than one chat backend is configured (%s): one backend per gateway process — two gateways on one relay durable split event deliveries; run a second Deployment for a second backend. The inject side door (A2A_INJECT_LISTEN) is not a backend in this sense and may sit beside either", strings.Join(armed, ", "))
	}
	// Fail closed: a door with no token would be reachable by anything that
	// reaches the listener, and the port-forward path the runner uses is
	// served from inside the pod, past both the loopback bind and the
	// NetworkPolicy in front of it. There is deliberately no opt-out — see
	// Config.InjectToken.
	if cfg.InjectListen != "" && cfg.InjectToken == "" {
		return nil, fmt.Errorf("A2A_INJECT_TOKEN is required when A2A_INJECT_LISTEN is set: the inject door authenticates every request with a bearer token, because neither its loopback bind nor the NetworkPolicy in front of it governs the port-forward path its caller uses")
	}
	// Only when the spawn path is armed: a gateway that spawns nothing has
	// no session identity to name, and demanding one would break every
	// bridge-only install.
	if cfg.SpawnSessions && cfg.SessionServiceAccount == "" {
		return nil, fmt.Errorf("A2A_SESSION_SERVICE_ACCOUNT is required when A2A_SPAWN_SESSIONS is true; session pods authenticate to the bus as it, and there is no safe default")
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
	if st < time.Hour {
		return nil, fmt.Errorf("A2A_SESSION_TTL %q is under the 1h floor", sessionTTL)
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

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
