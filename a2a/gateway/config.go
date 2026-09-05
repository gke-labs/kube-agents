package gateway

import (
	"crypto/sha256"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// defaultMaxSessions is what MaxSessions means when unset; the field's
// comment carries the sizing rationale.
const defaultMaxSessions = 10

// defaultTaskDeadline is what TaskDeadline means when unset — the worker
// adapter's own default (a2a/cmd/worker-adapter: A2A_TASK_DEADLINE_SECONDS,
// 1800s), restated here because the two halves of one contract must agree.
const defaultTaskDeadline = 30 * time.Minute

// defaultAskTTL is what AskTTL means when unset; the field's comment carries
// the horizon rationale.
const defaultAskTTL = 24 * time.Hour

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

	// DefaultAddressee is where every conversation's tasks route until a
	// per-conversation override says otherwise. Retarget 8/26: the first
	// shipped configuration routes everything to "platform" (the W7 bridge
	// executes) and spawns no session pods — the W4 switch is this setting,
	// not surgery.
	DefaultAddressee string

	// SpawnSessions arms the session-pod path (spawn/rehydrate/sweep with
	// client-go). Off until W4's worker image exists; the gateway pod has no
	// service-account token until this arms, so the k8s client is built
	// lazily.
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
	// posture for installs without that Secret, and the derived one is a
	// recorded deviation on two counts: the broken join, and a
	// de-anonymization key handed to whoever holds the bus password.
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

	// OwnerDeployment names the gateway's own Deployment
	// (A2A_OWNER_DEPLOYMENT; the operator renders its own render's name).
	// When set, every spawned session pod carries an ownerReference to it,
	// so Kubernetes GC reaps sessions when the Deployment goes — cleanupA2A
	// deleting the gateway, or any other deletion — with no operator
	// exception to its IsControlledBy refusal. Empty (playground) spawns
	// unowned pods, the pre-S9 posture.
	OwnerDeployment string

	// Namespace, WorkerImage, and NATSCredsSecret configure the dark spawn
	// path; the secret holds the worker user's password for spawned pods.
	Namespace       string
	WorkerImage     string
	NATSCredsSecret string

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
		NATSCredsSecret:  envOr("A2A_NATS_CREDS_SECRET", "platform-agent-a2a-nats-creds"),
	}
	if cfg.NATSURL == "" {
		return nil, fmt.Errorf("NATS_URL is required")
	}
	if cfg.DiscordToken == "" {
		return nil, fmt.Errorf("DISCORD_TOKEN is required (W0's discord-bot Secret)")
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
		derived := sha256.Sum256([]byte("a2a-attribution-salt:" + cfg.NATSPassword))
		cfg.AttributionSalt = derived[:]
	}
	return cfg, nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
