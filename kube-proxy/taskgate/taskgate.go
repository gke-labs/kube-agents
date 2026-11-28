// Package taskgate is the kube-proxy's task credential gate: a
// k8sapiproxy.Config.Authorize hook that admits an AgentProfile worker's request only when
// it carries the credential of the task the worker is running, and only to the cluster
// that credential names.
//
// The credential broker holds a worker's kubectl to the same rule
// (agents/platform/scripts/credential_proxy.py, ProfileTaskGate, and task_credential.py's
// parse_identity_map and bind), and this package is that rule in Go, read from the same
// four environment variables in the same formats. Three configure the gate; the fourth,
// the profile audience, admits rather than gates. It differs from the broker in one
// place on purpose: a worker that presents no credential is refused here, where the
// broker admits it to commands that reach no cluster. Every request at the kube-proxy
// reaches a cluster.
//
// The gate is off unless the identity map is set. A caller the map does not name is never
// asked for a credential and passes untouched.
//
// The map also says who the proxy admits as a worker: the binary accepts the profile
// audience (ProfileAudienceEnv) from exactly the map's usernames (Config.WorkerUsernames),
// so a caller admitted for that audience is always one the gate checks.
//
// And it says whose requests the proxy limits: Config.CallerLimits holds each pod of the
// map's usernames, and all of them together, to WorkerLimits, so workers can neither fill
// the in-flight cap every caller shares nor open watches without end.
package taskgate

import (
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// CredentialHeader is the request header a worker's task credential arrives in, the
	// broker's TASK_CREDENTIAL_HEADER. Its value is never logged and never put in an error.
	CredentialHeader = "X-Kube-Agents-Task-Credential" // #nosec G101 -- a header name, not a credential

	// IdentitiesEnv names the profile identity map: a JSON object of ServiceAccount
	// username to {"profile": ..., "tier": ...}. Unset or blank turns the gate off.
	IdentitiesEnv = "CREDENTIAL_PROXY_PROFILE_IDENTITIES" // #nosec G101 -- an environment variable name
	// SigningKeysEnv names the task-signing public keys the gate accepts a credential
	// from: comma separated, at most taskcred.MaxTrustedKeys.
	SigningKeysEnv = "A2A_TASK_SIGNING_PUBLIC_KEYS" // #nosec G101 -- an environment variable name
	// MaxLifetimeEnv names the ceiling on a credential's lifetime, a Go duration.
	MaxLifetimeEnv = "A2A_TASK_CREDENTIAL_MAX_LIFETIME" // #nosec G101 -- an environment variable name
	// ProfileAudienceEnv names the audience a worker's ServiceAccount token is issued for,
	// the one the broker gives the profile role by. Required when the identity map names a
	// worker, and not read otherwise.
	ProfileAudienceEnv = "CREDENTIAL_PROXY_PROFILE_AUDIENCE" // #nosec G101 -- an environment variable name

	// WorkerMaxInFlightEnv names the most requests one worker pod may have in flight at a
	// replica, and WorkerMaxLongRunningEnv the most watches, log requests and other
	// long-running requests it may have open there. WorkerGroupMaxInFlightEnv and
	// WorkerGroupMaxLongRunningEnv name the same two for every worker pod together. Each
	// is a non-negative integer. Unset or blank is the limit's default, the constant
	// below; zero sets no limit. Read only when the identity map names a worker. The
	// broker's CREDENTIAL_PROXY_PROFILE_SLOTS is not reused: it counts whole kubectl
	// commands, and these count HTTP requests, of which one command has several.
	WorkerMaxInFlightEnv         = "KUBE_PROXY_PROFILE_MAX_IN_FLIGHT"
	WorkerMaxLongRunningEnv      = "KUBE_PROXY_PROFILE_MAX_LONG_RUNNING"
	WorkerGroupMaxInFlightEnv    = "KUBE_PROXY_PROFILE_GROUP_MAX_IN_FLIGHT"
	WorkerGroupMaxLongRunningEnv = "KUBE_PROXY_PROFILE_GROUP_MAX_LONG_RUNNING"

	// DefaultWorkerGroupMaxInFlight is what every worker pod together may have in flight:
	// two thirds of the 24 requests a replica forwards at once (the server's
	// --max-in-flight default), so 8 shared slots are left to the shell and to session pods
	// however many workers are busy. The broker's limit this stands in for is also one
	// count for all profile callers (four commands, DEFAULT_PROFILE_MAX_CONCURRENT_COMMANDS
	// in credential_proxy.py).
	DefaultWorkerGroupMaxInFlight = 16
	// DefaultWorkerMaxInFlight is one pod's share: half of the group's, so one worker
	// cannot shut the others out. It is below what a cold kubectl sends at once against an
	// API server without aggregated discovery, which is a request per group version, some
	// thirty; that discovery completes through client-go's retries of the 429s. Aggregated
	// discovery is two requests, and the proxy's discovery cache answers repeats quickly, so
	// the retried case is rare.
	DefaultWorkerMaxInFlight = 8
	// DefaultWorkerMaxLongRunning leaves a pod room for one `kubectl logs -f` over a
	// selector, which follows up to five pods at once, beside a few watches, and
	// DefaultWorkerGroupMaxLongRunning is four pods at that limit. Streams take none of the
	// shared in-flight slots; the bound is on the connections and upstream watches workers
	// hold open.
	DefaultWorkerMaxLongRunning      = 8
	DefaultWorkerGroupMaxLongRunning = 32

	// workerGroup is the k8sapiproxy.CallerLimits.Group every worker is counted in.
	workerGroup = "profile-workers"
	// workerKeySeparator joins a worker's username and its pod's name into the key its own
	// limits are counted under. Neither a ServiceAccount username nor a pod name holds it.
	workerKeySeparator = "/"
	// noWorkerLimit is how a limit set to zero is handed to the proxy: a negative field is
	// no limit for the caller whatever the proxy's per-caller flags say, where a zero one
	// would take the flag's value.
	noWorkerLimit = -1

	// ReasonTaskCredential is the audit reason of a worker request with no credential,
	// more than one, or one that does not verify, does not bind to the caller, or names
	// no cluster. ReasonTaskTarget is the reason of a verified, bound credential that
	// names another cluster than the request's. Both are the broker's rule ids.
	ReasonTaskCredential k8sapiproxy.RejectionReason = "caller.task-credential" // #nosec G101 -- a rule id
	ReasonTaskTarget     k8sapiproxy.RejectionReason = "caller.task-target"

	// messageTaskCredential and messageTaskTarget are what the caller reads. They are
	// fixed: why a credential was refused describes the verifier's setup and goes to the
	// audit record alone.
	messageTaskCredential = "kube-proxy: this caller reaches a cluster only with the task credential of the task it is running, sent once in the " + CredentialHeader + " header."
	messageTaskTarget     = "kube-proxy: the task credential names another cluster than the one this request addresses."

	// podNameExtra and podUIDExtra are the TokenReview user.extra keys under which the
	// API server names the pod a projected ServiceAccount token is bound to.
	podNameExtra = "authentication.kubernetes.io/pod-name"
	podUIDExtra  = "authentication.kubernetes.io/pod-uid"

	// maxLifetimeCeiling is the most MaxLifetimeEnv may ask for, the auth callout's
	// maxTaskCredentialMaxLifetime and the broker's MAX_MAX_LIFETIME_SECONDS.
	maxLifetimeCeiling = 7 * 24 * time.Hour

	// maxCredentialBytes bounds what is decoded at all, the broker's MAX_TOKEN_BYTES. A
	// minted credential is well under 1 KiB.
	maxCredentialBytes = 8192

	// detailLimit bounds the refusal text that reaches the audit record, the length the
	// broker logs a refusal's reason at.
	detailLimit = 256

	// clusterIDFormat renders a credential's target as the cluster ID the proxy routes
	// by: gke_<project>_<location>_<cluster>.
	clusterIDFormat = "gke_%s_%s_%s"

	// admittedMessage is the log line of an admitted worker request, and callerFormat the
	// broker's rendering of the task and the credential's jti in it.
	admittedMessage = "task credential admitted"
	callerFormat    = "%s (caller task %s jti %s)"

	logKeyPrincipal = "principal"
	logKeyTask      = "task"
	logKeyJTI       = "jti"
	logKeyPod       = "pod"
	logKeyCluster   = "cluster"
	logKeyEnv       = "env"

	// noKeysWarning is logged once at startup when the identity map names workers and no
	// signing key is configured.
	noKeysWarning = "the profile identity map names workers but no task-signing key is configured; every worker request is refused"
	// maxASCII is the highest byte a credential may hold.
	maxASCII = 0x7f
)

// targetComponent matches one component of a GKE cluster ID as the broker reads a context
// name (credential_proxy_client.py's _GKE_CONTEXT_COMPONENT). No component may hold the
// separator, so a target renders to exactly one ID.
var targetComponent = regexp.MustCompile(`\A[a-z0-9][a-z0-9-]*\z`)

// Config is what the gate decides with.
type Config struct {
	// Identities maps a worker ServiceAccount's TokenReview username to its profile and
	// tier. A caller outside it is not a worker.
	Identities map[string]Identity
	// SigningKeys are the task-signing public keys a credential may be signed by. With
	// none, every worker is refused.
	SigningKeys []string
	// MaxLifetime is the ceiling on a credential's lifetime.
	MaxLifetime time.Duration
	// ProfileAudience is the audience a worker's ServiceAccount token is issued for. The
	// hook does not read it: the binary admits WorkerUsernames for it.
	ProfileAudience string
	// WorkerLimits is what a worker pod may have open at once at a replica (MaxInFlight,
	// MaxLongRunning) and what all worker pods together may (GroupMaxInFlight,
	// GroupMaxLongRunning). A zero field is no limit. The hook does not read it, and its
	// Key and Group are not read at all: CallerLimits sets those.
	WorkerLimits k8sapiproxy.CallerLimits
}

// CallerLimits is the k8sapiproxy.Config.CallerLimits of a proxy with this gate. A caller
// the identity map names gets WorkerLimits: its own two limits counted per pod, under its
// username and the pod name its TokenReview carries, and the group's two counted across
// every worker. A worker whose TokenReview names no pod, which the proxy's pod check does
// not admit, is counted under its username alone. A limit of zero is handed on as no limit,
// so the proxy's per-caller flags do not apply to a worker in its place. Any other caller
// gets no limits from here.
func (c Config) CallerLimits() func(*k8sapiproxy.CallerIdentity) k8sapiproxy.CallerLimits {
	limits := k8sapiproxy.CallerLimits{
		MaxInFlight:         orNoWorkerLimit(c.WorkerLimits.MaxInFlight),
		MaxLongRunning:      orNoWorkerLimit(c.WorkerLimits.MaxLongRunning),
		Group:               workerGroup,
		GroupMaxInFlight:    c.WorkerLimits.GroupMaxInFlight,
		GroupMaxLongRunning: c.WorkerLimits.GroupMaxLongRunning,
	}
	return func(caller *k8sapiproxy.CallerIdentity) k8sapiproxy.CallerLimits {
		if caller == nil {
			return k8sapiproxy.CallerLimits{}
		}
		if _, worker := c.Identities[caller.Username]; !worker {
			return k8sapiproxy.CallerLimits{}
		}
		own := limits
		if pod := single(caller.Extra[podNameExtra]); pod != "" {
			own.Key = caller.Username + workerKeySeparator + pod
		}
		return own
	}
}

// orNoWorkerLimit is limit, or noWorkerLimit for a limit that is not positive.
func orNoWorkerLimit(limit int) int {
	if limit <= 0 {
		return noWorkerLimit
	}
	return limit
}

// WorkerUsernames are the ServiceAccount usernames the identity map names, sorted: the
// callers the proxy admits for ProfileAudience. The slice is the caller's own.
func (c Config) WorkerUsernames() []string {
	return slices.Sorted(maps.Keys(c.Identities))
}

// CallerHeaders are the request headers the proxy must not forward: the credential's.
// They are stripped whether the gate is on or off, so a credential sent to a proxy that
// does not check it still reaches no upstream.
func CallerHeaders() []string {
	return []string{CredentialHeader}
}

// LoadConfig reads the gate's configuration from the environment. It returns nil when the
// identity map is unset, blank or empty: the gate is off, and no other variable is read. A
// malformed map, key list, lifetime or worker limit is an error, for the binary to stop on,
// and so is a map that names workers with no profile audience: the proxy would admit none of them as
// one. The broker starts in that state, since there a worker's role comes from the audience
// and a broker without it has no workers to gate; here the map is what turns the gate on.
func LoadConfig(getenv func(string) string) (*Config, error) {
	identities, err := ParseIdentityMap(getenv(IdentitiesEnv))
	if err != nil {
		return nil, fmt.Errorf("%s is malformed: %w", IdentitiesEnv, err)
	}
	if len(identities) == 0 {
		return nil, nil
	}
	keys, err := ParseSigningKeys(getenv(SigningKeysEnv))
	if err != nil {
		return nil, fmt.Errorf("%s is malformed: %w", SigningKeysEnv, err)
	}
	lifetime, err := ParseMaxLifetime(getenv(MaxLifetimeEnv))
	if err != nil {
		return nil, err
	}
	audience := strings.TrimSpace(getenv(ProfileAudienceEnv))
	if audience == "" {
		return nil, fmt.Errorf("%s is required when %s names a worker: it is the audience the proxy admits them for", ProfileAudienceEnv, IdentitiesEnv)
	}
	limits := k8sapiproxy.CallerLimits{}
	for _, limit := range []struct {
		env      string
		fallback int
		into     *int
	}{
		{WorkerMaxInFlightEnv, DefaultWorkerMaxInFlight, &limits.MaxInFlight},
		{WorkerMaxLongRunningEnv, DefaultWorkerMaxLongRunning, &limits.MaxLongRunning},
		{WorkerGroupMaxInFlightEnv, DefaultWorkerGroupMaxInFlight, &limits.GroupMaxInFlight},
		{WorkerGroupMaxLongRunningEnv, DefaultWorkerGroupMaxLongRunning, &limits.GroupMaxLongRunning},
	} {
		if *limit.into, err = ParseWorkerLimit(limit.env, getenv(limit.env), limit.fallback); err != nil {
			return nil, err
		}
	}
	return &Config{
		Identities:      identities,
		SigningKeys:     keys,
		MaxLifetime:     lifetime,
		ProfileAudience: audience,
		WorkerLimits:    limits,
	}, nil
}

// ParseWorkerLimit reads one worker limit from the variable named env: blank is fallback,
// and anything else must be a base-10 integer that is not negative. Zero is no limit. The
// broker reads its slot count leniently, with a warning and the default; a limit that
// protects other callers is not replaced by a guess.
func ParseWorkerLimit(env, raw string, fallback int) (int, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return fallback, nil
	}
	limit, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not an integer", env, clip(raw))
	}
	if limit < 0 {
		return 0, fmt.Errorf("%s=%q is negative; 0 sets no limit", env, clip(raw))
	}
	return limit, nil
}

// ParseSigningKeys reads the task-signing public key list as the broker does
// (task_credential.py's parse_trusted_keys): taskcred.ParseTrustedKeys, and no key twice.
func ParseSigningKeys(raw string) ([]string, error) {
	keys, err := taskcred.ParseTrustedKeys(raw)
	if err != nil {
		return nil, err
	}
	for i, key := range keys {
		if slices.Contains(keys[:i], key) {
			return nil, errors.New("the task-signing key list names one key twice")
		}
	}
	return keys, nil
}

// ParseMaxLifetime reads the lifetime ceiling as the auth callout and the broker do:
// blank is taskcred.DefaultMaxLifetime, and anything else must be a positive Go duration
// no greater than seven days.
func ParseMaxLifetime(raw string) (time.Duration, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return taskcred.DefaultMaxLifetime, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not a Go duration: %w", MaxLifetimeEnv, raw, err)
	}
	if d <= 0 {
		return 0, fmt.Errorf("%s=%q is not positive", MaxLifetimeEnv, raw)
	}
	if d > maxLifetimeCeiling {
		return 0, fmt.Errorf("%s=%q is above the %s ceiling", MaxLifetimeEnv, raw, maxLifetimeCeiling)
	}
	return d, nil
}

// FromEnv builds the Authorize hook the environment asks for: nil when the gate is off.
func FromEnv(getenv func(string) string, logger *slog.Logger) (func(k8sapiproxy.AuthorizeAttributes) error, error) {
	cfg, err := LoadConfig(getenv)
	if err != nil || cfg == nil {
		return nil, err
	}
	return FromConfig(*cfg, logger), nil
}

// FromConfig is the Authorize hook of a binary that starts with cfg, as LoadConfig returned
// it: New on the wall clock, after the one warning a configuration with no signing key gets.
func FromConfig(cfg Config, logger *slog.Logger) func(k8sapiproxy.AuthorizeAttributes) error {
	if logger == nil {
		logger = slog.Default()
	}
	if len(cfg.SigningKeys) == 0 {
		logger.Warn(noKeysWarning, logKeyEnv, SigningKeysEnv)
	}
	return New(cfg, logger, time.Now)
}

// New returns the Authorize hook for cfg. now is the verifier's clock.
func New(cfg Config, logger *slog.Logger, now func() time.Time) func(k8sapiproxy.AuthorizeAttributes) error {
	if logger == nil {
		logger = slog.Default()
	}
	if now == nil {
		now = time.Now
	}
	g := &gate{cfg: cfg, logger: logger, now: now}
	return g.authorize
}

type gate struct {
	cfg    Config
	logger *slog.Logger
	now    func() time.Time
}

// refusal is a refused worker request: detail goes to the audit record, and the wrapped
// AuthorizeError carries the reason and the caller's message.
type refusal struct {
	authz  *k8sapiproxy.AuthorizeError
	detail string
}

func (r *refusal) Error() string { return r.detail }

func (r *refusal) Unwrap() error { return r.authz }

// refuseCredential and refuseTarget build the two refusals. detail must not hold the
// credential; it may name a claim the verifier decoded.
func refuseCredential(format string, args ...any) error {
	return &refusal{
		authz:  &k8sapiproxy.AuthorizeError{Reason: ReasonTaskCredential, Message: messageTaskCredential},
		detail: bound(fmt.Sprintf(format, args...)),
	}
}

func refuseTarget(format string, args ...any) error {
	return &refusal{
		authz:  &k8sapiproxy.AuthorizeError{Reason: ReasonTaskTarget, Message: messageTaskTarget},
		detail: bound(fmt.Sprintf(format, args...)),
	}
}

// bound cuts detail to detailLimit bytes of valid UTF-8.
func bound(detail string) string {
	if len(detail) <= detailLimit {
		return detail
	}
	return strings.ToValidUTF8(detail[:detailLimit], "")
}

func (g *gate) authorize(attrs k8sapiproxy.AuthorizeAttributes) error {
	if attrs.Caller == nil {
		return refuseCredential("the request has no authenticated caller")
	}
	identity, worker := g.cfg.Identities[attrs.Caller.Username]
	if !worker {
		return nil
	}

	var values []string
	if attrs.Request != nil {
		values = attrs.Request.Header.Values(CredentialHeader)
	}
	if len(values) > 1 {
		return refuseCredential("more than one task credential header")
	}
	credential := ""
	if len(values) == 1 {
		credential = strings.TrimSpace(values[0])
	}
	if credential == "" {
		return refuseCredential("no task credential; a profile worker reaches a cluster only with one")
	}
	if len(g.cfg.SigningKeys) == 0 {
		return refuseCredential("this proxy trusts no task-signing key, so it verifies no task credential")
	}
	if len(credential) > maxCredentialBytes || !isASCII(credential) {
		return refuseCredential("task credential is too long or not ASCII")
	}

	claims, err := taskcred.Verify(credential, g.cfg.SigningKeys, taskcred.AudienceCredentialProxy, g.now(), g.cfg.MaxLifetime, taskcred.Skew)
	if err != nil {
		return refuseCredential("%v", err)
	}
	pod, podUID := single(attrs.Caller.Extra[podNameExtra]), single(attrs.Caller.Extra[podUIDExtra])
	if err := bind(claims, identity, pod, podUID); err != nil {
		return refuseCredential("%v (jti %s)", err, claims.ID)
	}
	if claims.Target == nil {
		return refuseCredential("a report credential names no cluster and reaches none (jti %s)", claims.ID)
	}
	target, ok := targetClusterID(*claims.Target)
	if !ok {
		return refuseCredential("the task credential's target is not a GKE cluster name (jti %s)", claims.ID)
	}
	if target != attrs.ClusterID {
		return refuseTarget("the task credential confines task %s to %s; the request addresses %q (jti %s)", claims.Task, target, attrs.ClusterID, claims.ID)
	}

	g.logger.Info(admittedMessage,
		logKeyPrincipal, fmt.Sprintf(callerFormat, attrs.Caller.Username, claims.Task, claims.ID),
		logKeyTask, claims.Task,
		logKeyJTI, claims.ID,
		logKeyPod, pod,
		logKeyCluster, target,
	)
	return nil
}

// bind refuses a verified credential that is not this caller's: task_credential.py's
// bind, in its order. pod and podUID come from the TokenReview, never from the request, so
// a copied credential is useless without the pod's own ServiceAccount token, and one
// minted for another profile or tier does not match this ServiceAccount's entry.
func bind(claims taskcred.Claims, identity Identity, pod, podUID string) error {
	if pod == "" || claims.Pod != pod {
		return errors.New("task credential is for another pod")
	}
	if podUID == "" || claims.PodUID != podUID {
		return errors.New("task credential is for another pod UID")
	}
	if claims.Profile != identity.Profile {
		return errors.New("task credential is for another profile")
	}
	if claims.Tier != identity.Tier {
		return errors.New("task credential is for another tier")
	}
	return nil
}

// targetClusterID renders a credential's target as a cluster ID, or reports that it is
// not one: the broker's target_from_claims.
func targetClusterID(t taskcred.Target) (string, bool) {
	for _, component := range []string{t.Project, t.Location, t.Cluster} {
		if !targetComponent.MatchString(component) {
			return "", false
		}
	}
	return fmt.Sprintf(clusterIDFormat, t.Project, t.Location, t.Cluster), true
}

// single returns the one value of a TokenReview extra, and "" when the API server named
// none or more than one: a pod-bound token names exactly one pod.
func single(values []string) string {
	if len(values) != 1 {
		return ""
	}
	return values[0]
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] > maxASCII {
			return false
		}
	}
	return true
}
