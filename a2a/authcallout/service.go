package authcallout

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"strings"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

const (
	// AuthRequestSubject is where the server asks. The callout subscribes to
	// it inside the dedicated callout account.
	AuthRequestSubject = "$SYS.REQ.USER.AUTH"

	// AuthQueueGroup is the queue group every replica joins, and it is not
	// optional above one replica.
	//
	// With a plain subscription every replica receives every request and
	// every replica answers; the server takes whichever response arrives
	// first and discards the rest without logging anything. Measured: two
	// replicas disagreeing about one identity, with a 300ms delay on one of
	// them, produced whichever verdict was faster — every time. That makes
	// authorization a latency race during any rollout that has two policy
	// versions live at once. A queue group makes it one authoritative answer
	// per request.
	AuthQueueGroup = "auth-callout"

	// ServerXKeyHeader carries the server's curve public key when the server
	// is configured to encrypt authorization requests.
	//
	// Read the header, never the server_id.xkey claim. 2.10 sets both and
	// 2.14 sets only the header, so a callout keyed on the claim silently
	// stops encrypting its responses when the server is upgraded — and the
	// server accepts a plaintext response even with xkey configured, so that
	// regression fails open and logs nothing on either side.
	ServerXKeyHeader = "Nats-Server-Xkey"

	// defaultGrantTTL bounds how long an issued connection keeps the grants
	// it was given. See Config.GrantTTL.
	defaultGrantTTL = time.Hour

	// grantTTLJitter is the fraction of GrantTTL randomly subtracted from
	// each grant, so a fleet that connected together does not re-authenticate
	// together. Same reasoning as NR-6's jittered backoff, one layer up: the
	// herd this spreads is the one the callout would otherwise create for
	// itself, every TTL, forever.
	grantTTLJitter = 0.2

	// authDecisionBudget is the wall clock the whole decision gets,
	// TokenReview round trip included. It is not a number we chose freely:
	// the server starts a first-ping timer on the not-yet-authenticated
	// connection at roughly two seconds, and the Go client fails the connect
	// outright on a PING where it expected a PONG. Overrun does not surface
	// as an authorization failure, it surfaces as "expected 'PONG', got
	// 'PING'" — which names nothing about authorization at all. See handle.
	authDecisionBudget = 1500 * time.Millisecond

	// taskCredentialSkew is the clock skew a task credential's expiry and
	// issue time are allowed against this callout's clock: the dispatcher
	// that signed it runs on another node.
	taskCredentialSkew = 30 * time.Second

	// defaultTaskCredentialMaxLifetime bounds how far out a task
	// credential's expiry may be. See Config.TaskCredentialMaxLifetime.
	defaultTaskCredentialMaxLifetime = 6 * time.Hour

	// maxTaskCredentialMaxLifetime is the most TaskCredentialMaxLifetime may
	// be set to; see ParseTaskCredentialMaxLifetime.
	maxTaskCredentialMaxLifetime = 7 * 24 * time.Hour
)

// Config is what the callout needs to answer.
type Config struct {
	// IssuerSeed is the account seed (SA...) whose public half is the
	// server's auth_callout.issuer. It signs both the user JWT and the
	// response envelope, and it is the most powerful secret in the
	// deployment: whoever holds it can mint a bus user with any grants,
	// including read across the capability bucket. It wants gateway-grade
	// custody, a rotation story, and a compromise runbook.
	IssuerSeed string

	// XKeySeed is the curve seed (SX...) whose public half is the server's
	// auth_callout.xkey. Optional, and worth having: the authorization
	// request carries the client's raw ServiceAccount token, so without it
	// that token crosses the bus in plaintext.
	XKeySeed string

	// GrantTTL bounds an issued connection's life.
	//
	// This is the deployment's revocation window, and it is the only one it
	// has. Permissions are fixed when a connection authenticates and the
	// callout is never consulted again, so a narrowed grant or a removed
	// identity does not reach a connection that is already established —
	// until it expires and the client reconnects, re-presenting a token the
	// cluster gets to refuse. Zero means the default; a grant that never
	// expires means a map change never reaches anything already connected.
	GrantTTL time.Duration

	// ReservedPrincipals are the static nats.conf users a narrowed pod may
	// not be named after (see reserved.go). Required: NewService refuses an
	// empty list rather than serving a callout that reserves nothing. The
	// identity map's own users are reserved too, but come from the map being
	// served rather than from here.
	ReservedPrincipals []string

	// ReservedAddressees are the fixed-name addressees a narrowed pod may
	// not be named after (see addressees.go). Required: NewService refuses
	// an empty list rather than serving a callout that reserves none. They
	// join ReservedPrincipals in the one set reservedAs checks.
	ReservedAddressees []string

	// TaskSigningKeys are the public task-signing keys (C...) whose task
	// credentials this callout honours on a profile worker's task
	// connection (#3889, a2a/taskcred): the current key, and during a
	// rotation the one it replaces. Empty means no task connection is
	// honoured; a worker's pod connection still is. NewService refuses a
	// key that is not a task-signing public key, or more than
	// taskcred.MaxTrustedKeys of them.
	TaskSigningKeys []string

	// TaskCredentialMaxLifetime is the furthest a task credential's expiry
	// may be, both from now (exp-now) and from its issue time (exp-iat), each
	// plus the skew; one further out either way is refused, not shortened.
	// It is
	// decoupled from GrantTTL on purpose: a task runs to its profile's
	// deadline (spec.lifecycle.activeDeadlineSeconds, which has no CRD
	// maximum), and the credential has to last that long. Revocation stays
	// at GrantTTL anyway, because the connection JWT is still capped by it
	// (see mint); a worker whose connection expires reconnects with the
	// same credential until the credential's own expiry.
	//
	// Zero means the default, 6h, which matches #4121's. The operator
	// doesn't render it in this PR; #4121's operator will render it from the
	// PlatformAgent's spec.harness.tuning.profiles.maxTaskDeadlineSeconds,
	// the same value it clamps profile deadlines to, so the dispatcher never
	// signs an expiry the callout refuses.
	TaskCredentialMaxLifetime time.Duration

	// TaskCredentialMaxLifetimeInvalid, when set, says the configured
	// lifetime could not be used (ParseTaskCredentialMaxLifetime's error).
	// The callout then refuses every task credential, naming it, and serves
	// every other connection as usual. It never falls back to the default:
	// a cap someone set and got wrong is not a cap of 6h. A negative
	// TaskCredentialMaxLifetime is treated the same way.
	TaskCredentialMaxLifetimeInvalid error

	// Now is injectable for tests.
	Now func() time.Time
}

// envTaskCredentialMaxLifetimeName is the variable ParseTaskCredentialMaxLifetime
// reads, named in its errors so a refusal says which setting to fix.
const envTaskCredentialMaxLifetimeName = "A2A_TASK_CREDENTIAL_MAX_LIFETIME"

// ParseTaskCredentialMaxLifetime reads the task credential lifetime #4121's
// operator will render (time.Duration.String, e.g. "6h0m0s"). Empty is
// (0, nil): unset, the default. Anything that isn't a positive Go duration is
// an error, which the caller passes on as Config.TaskCredentialMaxLifetimeInvalid
// rather than stopping: see that field.
func ParseTaskCredentialMaxLifetime(raw string) (time.Duration, error) {
	// Trimmed like the other rendered settings: a value written through a
	// ConfigMap or by hand may carry a newline, and whitespace alone is unset.
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not a Go duration: %w", envTaskCredentialMaxLifetimeName, raw, err)
	}
	if d <= 0 {
		return 0, fmt.Errorf("%s=%q is not positive", envTaskCredentialMaxLifetimeName, raw)
	}
	// A ceiling, so a typo can't make credentials good for years, and so the
	// exp arithmetic in taskcred.Verify (maxLifetime plus skew) stays far
	// from time.Duration's ~292-year overflow. 7 days is well above #4121's
	// own maximum (21600s), so it bounds mistakes, not configurations.
	if d > maxTaskCredentialMaxLifetime {
		return 0, fmt.Errorf("%s=%q is above the %s ceiling", envTaskCredentialMaxLifetimeName, raw, maxTaskCredentialMaxLifetime)
	}
	return d, nil
}

// taskSigningKeySeparator separates the keys in ParseTaskSigningKeys' input.
const taskSigningKeySeparator = ","

// ParseTaskSigningKeys reads the task-signing public keys the operator renders
// for the callout, comma separated. Empty is allowed and means none (no task
// connection is honoured); an empty element, a key that is not a task-signing
// public key, or more than taskcred.MaxTrustedKeys is refused.
func ParseTaskSigningKeys(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var keys []string
	for _, field := range strings.Split(raw, taskSigningKeySeparator) {
		key := strings.TrimSpace(field)
		if key == "" {
			return nil, fmt.Errorf("the task-signing key list %q has an empty element", raw)
		}
		if !taskcred.ValidPublicKey(key) {
			return nil, fmt.Errorf("%q is not a task-signing public key (C...)", key)
		}
		keys = append(keys, key)
	}
	if len(keys) > taskcred.MaxTrustedKeys {
		return nil, fmt.Errorf("%d task-signing keys; at most %d (the current one and the one it replaces)", len(keys), taskcred.MaxTrustedKeys)
	}
	return keys, nil
}

// Service answers the server's authorization requests.
type Service struct {
	store     *Store
	validator *TokenValidator
	issuer    nkeys.KeyPair
	xkey      nkeys.KeyPair
	grantTTL  time.Duration
	taskKeys  []string
	// taskMaxLifetime is Config.TaskCredentialMaxLifetime, defaulted.
	taskMaxLifetime time.Duration
	// taskLifetimeErr, when set, refuses every task credential: the
	// configured lifetime was unusable. See
	// Config.TaskCredentialMaxLifetimeInvalid.
	taskLifetimeErr error
	reserved        map[string]reservedKind
	now             func() time.Time
	log             *slog.Logger
}

// NewService builds the callout.
func NewService(store *Store, validator *TokenValidator, cfg Config, log *slog.Logger) (*Service, error) {
	if store == nil {
		return nil, fmt.Errorf("callout needs an identity map store")
	}
	if validator == nil {
		return nil, fmt.Errorf("callout needs a token validator")
	}
	if log == nil {
		log = slog.Default()
	}

	// Checked at construction rather than discovered at the first connection
	// attempt. The server refuses a response whose inner JWT is signed by
	// anything but the configured issuer ACCOUNT key, and that refusal
	// reaches the client as a bare "Authorization Violation" — the same
	// thing a bad token produces. A user or curve seed handed to this field
	// would therefore present as every workload in the deployment having
	// wrong credentials, with nothing naming the real cause.
	prefix, _, err := nkeys.DecodeSeed([]byte(cfg.IssuerSeed))
	if err != nil {
		return nil, fmt.Errorf("issuer seed: %w", err)
	}
	if prefix != nkeys.PrefixByteAccount {
		return nil, fmt.Errorf("issuer seed is not an account seed (SA...); the server refuses every response signed with any other key type")
	}
	issuer, err := nkeys.FromSeed([]byte(cfg.IssuerSeed))
	if err != nil {
		return nil, fmt.Errorf("issuer seed: %w", err)
	}

	reserved, err := reservedSet(cfg.ReservedPrincipals, cfg.ReservedAddressees)
	if err != nil {
		return nil, err
	}

	if len(cfg.TaskSigningKeys) > taskcred.MaxTrustedKeys {
		return nil, fmt.Errorf("%d task-signing keys configured; at most %d (the current one and the one it replaces)", len(cfg.TaskSigningKeys), taskcred.MaxTrustedKeys)
	}
	for _, k := range cfg.TaskSigningKeys {
		if !taskcred.ValidPublicKey(k) {
			return nil, fmt.Errorf("task-signing key %q is not a task-signing public key (C...)", k)
		}
	}

	svc := &Service{
		store:     store,
		validator: validator,
		issuer:    issuer,
		grantTTL:  cfg.GrantTTL,
		taskKeys:  append([]string(nil), cfg.TaskSigningKeys...),
		reserved:  reserved,
		now:       cfg.Now,
		log:       log,
	}
	if svc.grantTTL <= 0 {
		svc.grantTTL = defaultGrantTTL
	}
	// Three explicit cases. An unusable setting refuses task credentials and
	// nothing else; it is not a reason to take the callout down, which would
	// refuse every connection on the bus, and it is not a reason to apply the
	// default either. Unset is the default. Anything else is the setting.
	switch {
	case cfg.TaskCredentialMaxLifetimeInvalid != nil:
		svc.taskLifetimeErr = cfg.TaskCredentialMaxLifetimeInvalid
	case cfg.TaskCredentialMaxLifetime < 0:
		svc.taskLifetimeErr = fmt.Errorf("the task credential max lifetime %s is negative", cfg.TaskCredentialMaxLifetime)
	case cfg.TaskCredentialMaxLifetime == 0:
		svc.taskMaxLifetime = defaultTaskCredentialMaxLifetime
	default:
		svc.taskMaxLifetime = cfg.TaskCredentialMaxLifetime
	}
	if svc.taskLifetimeErr != nil {
		log.Error("every task credential will be refused; other connections are served as usual", "reason", svc.taskLifetimeErr)
	}
	if svc.now == nil {
		svc.now = time.Now
	}
	if cfg.XKeySeed != "" {
		if svc.xkey, err = nkeys.FromSeed([]byte(cfg.XKeySeed)); err != nil {
			return nil, fmt.Errorf("xkey seed: %w", err)
		}
	}
	return svc, nil
}

// Subscribe joins the callout queue group on an established connection.
func (s *Service) Subscribe(nc *nats.Conn) (*nats.Subscription, error) {
	sub, err := nc.QueueSubscribe(AuthRequestSubject, AuthQueueGroup, s.handle)
	if err != nil {
		return nil, fmt.Errorf("subscribing to %s: %w", AuthRequestSubject, err)
	}
	// Deliberately no Flush.
	//
	// The connection this is handed may legitimately be RECONNECTING: the
	// operator applies the NATS StatefulSet and the callout Deployment
	// milliseconds apart, so on a fresh install the callout reliably starts
	// before the bus is listening, and nats.Connect with RetryOnFailedConnect
	// returns a usable client in that state. The subscription buffers and is
	// replayed when the connection establishes.
	//
	// A Flush here waits for a PONG that cannot arrive until then, times out
	// after ten seconds, and returns an error that exits the process — turning
	// "retry forever" into CrashLoopBackOff, with a log line naming the flush
	// rather than the bus. Because the callout gates every non-exempt
	// connection, that is the whole fabric dark to new work for the length of
	// the backoff, on exactly the path every install takes.
	s.log.Info("serving authorization requests", "subject", AuthRequestSubject, "queue", AuthQueueGroup)
	return sub, nil
}

func (s *Service) handle(m *nats.Msg) {
	// The server's own first-ping timer, not authorization.timeout, is what
	// actually bounds everything below. See authDecisionBudget.
	ctx, cancel := context.WithTimeout(context.Background(), authDecisionBudget)
	defer cancel()

	req, serverXKey, err := s.decode(m)
	if err != nil {
		// Nothing to answer to: without a decoded request there is no user
		// nkey to address a response to, and an unanswered request fails
		// the client at the server's own timeout.
		s.log.Error("could not decode an authorization request", "error", err)
		return
	}

	resp := jwt.NewAuthorizationResponseClaims(req.UserNkey)
	resp.Audience = req.Server.ID

	d, err := s.authorize(ctx, req)
	if err != nil {
		// This string never reaches the client. The client sees a bare
		// "Authorization Violation" whether its token was bad, the callout
		// was down, or the response was malformed — so this log line and
		// the $SYS disconnect advisory are the only places the reason
		// exists. Log the identity, never the token.
		s.log.Warn("refusing a connection",
			"reason", err,
			"host", req.ClientInformation.Host,
			"map_version", s.store.Version())
		resp.Error = err.Error()
	} else {
		ujwt, jerr := s.mint(req, d)
		if jerr != nil {
			s.log.Error("could not mint a user JWT", "user", d.user, "error", jerr)
			resp.Error = "internal error"
		} else {
			resp.Jwt = ujwt
			s.log.Info("authorized a connection",
				"user", d.user,
				"account", d.account,
				"task", d.task,
				"host", req.ClientInformation.Host,
				"map_version", s.store.Version())
		}
	}

	out, err := resp.Encode(s.issuer)
	if err != nil {
		s.log.Error("could not encode the authorization response", "error", err)
		return
	}
	payload := []byte(out)
	if serverXKey != "" {
		sealed, serr := s.xkey.Seal(payload, serverXKey)
		if serr != nil {
			s.log.Error("could not seal the authorization response", "error", serr)
			return
		}
		payload = sealed
	}
	if err := m.Respond(payload); err != nil {
		s.log.Error("could not answer an authorization request", "error", err)
	}
}

// decode unwraps the request, decrypting it when the server encrypted it.
func (s *Service) decode(m *nats.Msg) (*jwt.AuthorizationRequestClaims, string, error) {
	serverXKey := ""
	if m.Header != nil {
		serverXKey = m.Header.Get(ServerXKeyHeader)
	}

	data := m.Data
	if serverXKey != "" {
		if s.xkey == nil {
			return nil, "", fmt.Errorf("server encrypted the request but this callout has no xkey configured")
		}
		opened, err := s.xkey.Open(m.Data, serverXKey)
		if err != nil {
			// Indistinguishable client-side from the callout being down,
			// so it has to be loud here.
			return nil, "", fmt.Errorf("decrypting the request (xkey mismatch with the server?): %w", err)
		}
		data = opened
	}

	req, err := jwt.DecodeAuthorizationRequestClaims(string(data))
	if err != nil {
		return nil, "", err
	}
	return req, serverXKey, nil
}

// decision is what authorize resolved a connection to.
type decision struct {
	account string
	perms   *jwt.Permissions
	user    string

	// task and notAfter are set on a profile worker's task connection
	// alone: the task its grants are scoped to, and its credential's
	// expiry, which caps the minted JWT's (see mint).
	task     string
	notAfter time.Time
}

// authorize resolves the presented token to a mapped identity.
func (s *Service) authorize(ctx context.Context, req *jwt.AuthorizationRequestClaims) (decision, error) {
	m := s.store.Current()
	if m == nil {
		return decision{}, fmt.Errorf("no identity map is being served")
	}

	// The ServiceAccount token may arrive as the connection token or as the
	// password. Both deliver it byte-identical; accepting either means a
	// client library can use whichever fits it without the callout caring.
	//
	// A profile worker's task connection presents a second credential, the
	// task credential, and it rides the password beside a token. So: a token
	// is the ServiceAccount token and the password, if any, the task
	// credential; with no token, the password is the ServiceAccount token
	// and there is no task credential.
	//
	// Why the password and not CONNECT's jwt field, which nats-server
	// v2.15.1 also forwards to the callout in this (non-operator) mode:
	// fillConnectOpts (server/auth_callout.go) copies the client's jwt into
	// ConnectOptions.JWT, and processClientOrLeafAuthentication
	// (server/auth.go) only decodes it as a user JWT when trusted keys are
	// configured, which they aren't here. But the server also copies
	// c.opts.JWT into the $SYS connect, disconnect and auth-error
	// advisories (server/events.go, accountConnectEvent and
	// sendAccountAuthErrorEvent among them) and into connz's jwt field
	// (server/monitor.go, redacted only for a bearer USER JWT). The callout
	// clears it on success (processClientOrLeafCallout, "the JWT is
	// cleared"), so a refused connection is exactly when the task
	// credential would be published in cleartext. The password appears in
	// none of those, crosses the bus only inside the authorization request
	// (sealed when the xkey is configured, as the operator renders it), and
	// nats.go sends it beside a token with no conflict (connectProto).
	token, taskToken := req.ConnectOptions.Token, ""
	if token != "" {
		taskToken = req.ConnectOptions.Password
	} else {
		token = req.ConnectOptions.Password
	}
	if token == "" {
		return decision{}, fmt.Errorf("no token presented")
	}

	att, err := s.validator.Validate(ctx, token)
	if err != nil {
		return decision{}, fmt.Errorf("token rejected: %w", err)
	}

	id, ok := m.Lookup(att.ServiceAccount)
	if !ok {
		// The cluster vouches for this identity and this deployment has no
		// entry for it. Named in the log because it is the single most
		// likely thing to be wrong after a rename.
		return decision{}, fmt.Errorf("%s is not in the identity map (version %s)", att.ServiceAccount, m.Version)
	}

	// A task credential means something only on a profile worker's entry.
	// Anywhere else it is refused, not ignored: a client presenting one
	// believes it is getting a task's scope.
	if taskToken != "" && id.Narrowing != NarrowingProfile {
		return decision{}, fmt.Errorf("%s presented a task credential, but its entry is not a profile worker's", att.ServiceAccount)
	}

	grants, user := id.Grants, id.User
	var task string
	var notAfter time.Time
	if id.Narrowing != "" {
		// A narrowed entry holds no grants; they are built here from what
		// the cluster attested about this connection specifically. The
		// default arm cannot be reached through a parsed map — validate
		// refuses an unknown narrowing — so it exists for the case where
		// this switch and the map's validation drift apart, and it refuses
		// rather than falling through to the entry's empty grants, which
		// would connect a client that is UNRESTRICTED rather than one that
		// can do nothing. See the mint below for why.
		switch id.Narrowing {
		case NarrowingPod:
			if err := validSessionName(att.PodName); err != nil {
				return decision{}, fmt.Errorf("%s narrows on the pod, but %w", att.ServiceAccount, err)
			}
			if att.PodUID == "" {
				return decision{}, fmt.Errorf("%s narrows on the pod, but the token attests no pod UID", att.ServiceAccount)
			}
			// The user is named for the pod, so `connz`, the $SYS
			// advisories and the line below all say which session —
			// otherwise every session on the bus is called "session".
			g, err := sessionGrants(att.PodName)
			if err != nil {
				return decision{}, fmt.Errorf("%s narrows on the pod, but %w", att.ServiceAccount, err)
			}
			grants, user = g, att.PodName
		case NarrowingProfile:
			// The same pod attestation a session needs, for the same
			// reason: the pod name becomes the consumer names and the
			// inbox, so it has to be one the API server vouched for.
			if err := validSessionName(att.PodName); err != nil {
				return decision{}, fmt.Errorf("%s narrows on its profile's pod, but %w", att.ServiceAccount, err)
			}
			if att.PodUID == "" {
				return decision{}, fmt.Errorf("%s narrows on its profile's pod, but the token attests no pod UID", att.ServiceAccount)
			}
			if taskToken == "" {
				// The pod connection: hand-off, lease and topics, and,
				// until #3889's worker PR, the profile-wide task plane
				// (see the TODO on profileWorkerGrants).
				var topics TopicGrants
				if id.Topics != nil {
					topics = *id.Topics
				}
				g, err := profileGrants(id.Profile, id.Tier, att.PodName, topics)
				if err != nil {
					return decision{}, fmt.Errorf("%s narrows on profile %q, but %w", att.ServiceAccount, id.Profile, err)
				}
				grants, user = g, att.PodName
				break
			}
			// The task connection. Every failure below is a refusal, never
			// a fall back to the pod connection's grants: the client asked
			// for one task's scope and gets that or nothing.
			c, err := s.verifyTaskCredential(taskToken, id, att)
			if err != nil {
				return decision{}, fmt.Errorf("%s presented a task credential that was refused: %w", att.ServiceAccount, err)
			}
			grants, user = profileTaskGrants(id.Profile, c.Task, att.PodName), att.PodName
			task, notAfter = c.Task, c.Expires
		default:
			return decision{}, fmt.Errorf("%s names narrowing %q, which this callout does not implement", att.ServiceAccount, id.Narrowing)
		}
		// A narrowed user is named for its pod, and that name is both its
		// inbox prefix and the addressee its task subjects are keyed on. A
		// pod named after a static principal (the gateway, the bridge, web,
		// console, seed) or after a user this map mints (the verifier, the
		// agent, the provisioner) would be granted that principal's inbox,
		// and could read or forge the JetStream replies delivered there. A
		// pod named after a fixed-name addressee (the bridge's `platform`)
		// would be handed that addressee's task events, its `.in` consumers
		// and its verify subject. Checked after the switch so every narrowing
		// is covered by one check, whatever derived the user, and against m,
		// the snapshot the identity was resolved from.
		if kind, ok := s.reservedAs(m, user); ok {
			return decision{}, fmt.Errorf("%s narrows on pod %q, which is the name of %s; %s", att.ServiceAccount, user, kind, kind.copied())
		}
	}

	// Deny-by-default is a property of a grant set with entries in it. An
	// EMPTY list means the opposite, and the two sides are independent, so
	// both halves of every resolved grant set must be non-empty however it
	// was resolved — from the map or from a narrowing. ParseIdentityMap
	// enforces this for mapped entries; the callout is the enforcement point
	// and enforces it for all of them.
	if len(grants.Publish) == 0 || len(grants.Subscribe) == 0 {
		return decision{}, fmt.Errorf("%s resolved to %d publish and %d subscribe grants; a side with none would mint a client unrestricted on that side",
			att.ServiceAccount, len(grants.Publish), len(grants.Subscribe))
	}

	return decision{account: id.Account, perms: permissionsFor(grants), user: user, task: task, notAfter: notAfter}, nil
}

// verifyTaskCredential checks a task credential against the connection that
// presented it. taskcred.Verify checks the credential itself: the signature,
// a trusted task-signing key, the nats audience, an expiry within
// TaskCredentialMaxLifetime both from now and from the credential's issue time
// (refused, not shortened), and well-formed claims. What only the callout can
// check is that it is this connection's:
//
//   - the pod and its UID are the pod the cluster attested for the
//     ServiceAccount token on this same CONNECT, so a copied credential is
//     useless without that pod's own token, and a credential minted for a
//     pod that has since been replaced under the same name is refused too;
//   - the profile and tier are the attested identity's map entry's. The
//     subjects are built from the entry's profile, not the credential's, so
//     this check is what refuses a credential for another profile rather
//     than quietly granting the entry's own; an entry naming no tier (an
//     operator before #3655) matches no credential.
func (s *Service) verifyTaskCredential(raw string, id Identity, att Attested) (taskcred.Claims, error) {
	if s.taskLifetimeErr != nil {
		return taskcred.Claims{}, fmt.Errorf("the callout's task credential lifetime setting is unusable, so every task credential is refused: %w", s.taskLifetimeErr)
	}
	if len(s.taskKeys) == 0 {
		return taskcred.Claims{}, fmt.Errorf("this callout trusts no task-signing key")
	}
	now := s.now()
	c, err := taskcred.Verify(raw, s.taskKeys, taskcred.AudienceNATS, now, s.taskMaxLifetime, taskCredentialSkew)
	if err != nil {
		return taskcred.Claims{}, err
	}
	if c.Pod != att.PodName {
		return taskcred.Claims{}, fmt.Errorf("it is for pod %q, and the token is bound to pod %q", c.Pod, att.PodName)
	}
	if c.PodUID != att.PodUID {
		return taskcred.Claims{}, fmt.Errorf("it is for pod UID %q, and the token is bound to pod UID %q", c.PodUID, att.PodUID)
	}
	if c.Profile != id.Profile {
		return taskcred.Claims{}, fmt.Errorf("it is for profile %q, and the entry is profile %q's", c.Profile, id.Profile)
	}
	if c.Tier != id.Tier {
		return taskcred.Claims{}, fmt.Errorf("it is for tier %q, and the entry is tier %q's", c.Tier, id.Tier)
	}
	// Inside the skew Verify allows, but a JWT minted to expire now would
	// be refused by the server with nothing naming why.
	if !c.Expires.After(now) {
		return taskcred.Claims{}, fmt.Errorf("it expired at %s", c.Expires.UTC().Format(time.RFC3339))
	}
	return c, nil
}

// permissionsFor turns a grant set into the permissions the user JWT carries.
//
// An empty allow list is not a closed one, and this is where that is made safe
// rather than merely checked.
//
// nats-server reads an ABSENT list as "unrestricted" rather than as "nothing":
// buildPermissionsFromJwt builds Permissions.Publish only when the publish
// allow or deny list is non-empty, setPermissions then leaves perms.pub.allow
// nil, and pubAllowedFullCheck returns true for every subject. Because the
// sides are independent, a grant set carrying subscribes and no publishes
// mints a client that may publish anywhere — including
// $JS.API.STREAM.DELETE.TASKS — while its subscribes are still enforced, which
// is the shape least likely to be noticed.
//
// Which disjunct fires is worth being exact about, because the two empty cases
// are not the same mechanism. With BOTH sides empty, buildPermissionsFromJwt
// returns nil and buildInternalNkeyUser falls back to the account's
// default_permissions — unrestricted here only because no account in this
// deployment defines any. With ONE side empty the permission object exists, so
// c.perms is NOT nil and it is pubAllowedFullCheck's second disjunct
// (allow == nil && deny == nil) that returns true. The deny below is what
// makes both cases impossible, and it is the one-sided case it is really for.
//
// A deny of ">" is how the server itself spells "nothing":
// processUserPermissionsTemplate appends exactly this when template expansion
// turns a non-empty allow list into an empty one. (That path runs only for a
// scoped signing key, so it is cited as the server's own spelling, not as code
// that executes on this mint.) Doing it here rather than at the callers means
// it holds for every path into the mint, including paths added later.
//
// This is defence in depth, not the enforcement: authorize refuses an empty
// side before reaching here, and that refusal is what
// TestAnEmptySideIsRefusedBeforeItCanBeMinted measures against a real server.
// What this function guarantees is the shape of the credential if that refusal
// is ever wrong, which TestPermissionsNeverLeaveASideEmpty pins directly.
func permissionsFor(grants Grants) *jwt.Permissions {
	p := &jwt.Permissions{}
	p.Pub.Allow.Add(grants.Publish...)
	p.Sub.Allow.Add(grants.Subscribe...)
	if len(p.Pub.Allow) == 0 {
		p.Pub.Deny.Add(">")
	}
	if len(p.Sub.Allow) == 0 {
		p.Sub.Deny.Add(">")
	}
	return p
}

// mint builds the user JWT the server will enforce.
func (s *Service) mint(req *jwt.AuthorizationRequestClaims, d decision) (string, error) {
	// Every one of these three was proven load-bearing by violating it:
	// a subject that is not the server's ephemeral user nkey, an audience
	// that is not an account the server knows by name, or a signature from
	// anything but the configured issuer account key, each produce an
	// Authorization Violation the client cannot tell from a bad token.
	uc := jwt.NewUserClaims(req.UserNkey)
	uc.Audience = d.account
	uc.Name = d.user
	// A task connection ends with its task: the server closes a connection
	// when its JWT expires, so the credential's expiry caps the grant's.
	// GrantTTL still caps it too, so a credential that outlives GrantTTL
	// (TaskCredentialMaxLifetime) means a reconnect, re-checked here, not a
	// longer connection.
	expiry := s.grantExpiry()
	if !d.notAfter.IsZero() && d.notAfter.Before(expiry) {
		expiry = d.notAfter
	}
	uc.Expires = expiry.Unix()
	uc.Permissions = *d.perms
	return uc.Encode(s.issuer)
}

func (s *Service) grantExpiry() time.Time {
	jitter := time.Duration(rand.Float64() * grantTTLJitter * float64(s.grantTTL))
	return s.now().Add(s.grantTTL - jitter)
}
