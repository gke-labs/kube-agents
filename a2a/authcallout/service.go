package authcallout

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
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

	// Now is injectable for tests.
	Now func() time.Time
}

// Service answers the server's authorization requests.
type Service struct {
	store     *Store
	validator *TokenValidator
	issuer    nkeys.KeyPair
	xkey      nkeys.KeyPair
	grantTTL  time.Duration
	now       func() time.Time
	log       *slog.Logger
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

	svc := &Service{
		store:     store,
		validator: validator,
		issuer:    issuer,
		grantTTL:  cfg.GrantTTL,
		now:       cfg.Now,
		log:       log,
	}
	if svc.grantTTL <= 0 {
		svc.grantTTL = defaultGrantTTL
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

	account, perms, user, err := s.authorize(ctx, req)
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
		ujwt, jerr := s.mint(req, account, user, perms)
		if jerr != nil {
			s.log.Error("could not mint a user JWT", "user", user, "error", jerr)
			resp.Error = "internal error"
		} else {
			resp.Jwt = ujwt
			s.log.Info("authorized a connection",
				"user", user,
				"account", account,
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

// authorize resolves the presented token to a mapped identity.
func (s *Service) authorize(ctx context.Context, req *jwt.AuthorizationRequestClaims) (account string, perms *jwt.Permissions, user string, err error) {
	m := s.store.Current()
	if m == nil {
		return "", nil, "", fmt.Errorf("no identity map is being served")
	}

	// The token may arrive as the connection token or as the password. Both
	// deliver it byte-identical; accepting either means a client library can
	// use whichever fits it without the callout caring.
	token := req.ConnectOptions.Token
	if token == "" {
		token = req.ConnectOptions.Password
	}
	if token == "" {
		return "", nil, "", fmt.Errorf("no token presented")
	}

	serviceAccount, err := s.validator.Validate(ctx, token)
	if err != nil {
		return "", nil, "", fmt.Errorf("token rejected: %w", err)
	}

	id, ok := m.Lookup(serviceAccount)
	if !ok {
		// The cluster vouches for this identity and this deployment has no
		// entry for it. Named in the log because it is the single most
		// likely thing to be wrong after a rename.
		return "", nil, "", fmt.Errorf("%s is not in the identity map (version %s)", serviceAccount, m.Version)
	}

	p := &jwt.Permissions{}
	p.Pub.Allow.Add(id.Grants.Publish...)
	p.Sub.Allow.Add(id.Grants.Subscribe...)
	return id.Account, p, id.User, nil
}

// mint builds the user JWT the server will enforce.
func (s *Service) mint(req *jwt.AuthorizationRequestClaims, account, user string, perms *jwt.Permissions) (string, error) {
	// Every one of these three was proven load-bearing by violating it:
	// a subject that is not the server's ephemeral user nkey, an audience
	// that is not an account the server knows by name, or a signature from
	// anything but the configured issuer account key, each produce an
	// Authorization Violation the client cannot tell from a bad token.
	uc := jwt.NewUserClaims(req.UserNkey)
	uc.Audience = account
	uc.Name = user
	uc.Expires = s.grantExpiry().Unix()
	uc.Permissions = *perms
	return uc.Encode(s.issuer)
}

func (s *Service) grantExpiry() time.Time {
	jitter := time.Duration(rand.Float64() * grantTTLJitter * float64(s.grantTTL))
	return s.now().Add(s.grantTTL - jitter)
}
