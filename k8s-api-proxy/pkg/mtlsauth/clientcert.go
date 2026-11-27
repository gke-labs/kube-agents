package mtlsauth

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	clientCertConfigName  = "ClientCertConfig"
	identitySourceField   = "IdentitySource"
	allowedIdentitiesName = clientCertConfigName + ".AllowedIdentities"
)

var errIdentityNotAdmitted = errors.New("the client certificate's identity is not admitted")

// ClientCertConfig configures NewClientCertAuthenticator.
type ClientCertConfig struct {
	// ClientCAFile is the PEM bundle of the CAs a caller's certificate must chain to.
	// Required, and it must load when the constructor is called. It is re-read when it
	// changes; a changed bundle that does not load is reported through Reload and the
	// previous one kept. Give it CAs that sign nothing but callers of this authenticator:
	// every certificate they issue for client authentication is a caller, unless
	// AllowedIdentities or RequiredURIPrefix narrow it.
	ClientCAFile string
	// IdentitySource is the certificate field the caller's username is read from. Required.
	// A certificate without exactly one value there is refused.
	IdentitySource IdentitySource
	// AllowedIdentities, when not empty, lists the identities admitted, compared byte for
	// byte. Entries are trimmed and empty ones dropped; a list left with none is an error.
	AllowedIdentities []string
	// RequiredURIPrefix, when set, refuses a certificate whose URI does not start with it.
	// It is valid only with SourceURI. The comparison is on the text alone, so end the
	// prefix at a separator ("spiffe://example.org/", not "spiffe://example.org") to keep
	// it from matching a longer trust domain or path segment.
	RequiredURIPrefix string
	// Reload tunes how ClientCAFile is checked for changes and where a failed reload is
	// reported.
	Reload certreload.Options
	// Now returns the time certificates are verified at; time.Now when nil. For tests.
	Now func() time.Time
}

// ClientCertAuthenticator is a k8sapiproxy.CallerAuthenticator that admits the peer of a
// TLS connection by its client certificate. The caller's Username is the certificate's
// name in the configured field; UID, Groups and Partition are left empty.
type ClientCertAuthenticator struct {
	peer              *peerVerifier
	allowed           map[string]struct{}
	requiredURIPrefix string
}

// NewClientCertAuthenticator returns a ClientCertAuthenticator for cfg, or an error when
// cfg is incomplete or ClientCAFile does not load.
func NewClientCertAuthenticator(cfg ClientCertConfig) (*ClientCertAuthenticator, error) {
	if cfg.RequiredURIPrefix != "" && cfg.IdentitySource != "" && cfg.IdentitySource != SourceURI {
		return nil, fmt.Errorf("mtlsauth: %s.RequiredURIPrefix requires %s %q", clientCertConfigName, identitySourceField, SourceURI)
	}
	allowed, err := newAllowlist(allowedIdentitiesName, cfg.AllowedIdentities)
	if err != nil {
		return nil, err
	}
	peer, err := newPeerVerifier(clientCertConfigName, identitySourceField, cfg.ClientCAFile, cfg.IdentitySource, cfg.Reload, cfg.Now)
	if err != nil {
		return nil, err
	}
	return &ClientCertAuthenticator{peer: peer, allowed: allowed, requiredURIPrefix: cfg.RequiredURIPrefix}, nil
}

// AuthenticateRequest admits r when its connection's client certificate verifies and its
// identity passes the configured filters. Every error is a plain refusal, which a chain
// follows with its next authenticator; see the package documentation.
func (a *ClientCertAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	identity, err := a.peer.name(r)
	if err != nil {
		return nil, err
	}
	if !strings.HasPrefix(identity, a.requiredURIPrefix) {
		return nil, fmt.Errorf("%w: %q lacks the required prefix", errIdentityNotAdmitted, identity)
	}
	if _, ok := a.allowed[identity]; len(a.allowed) > 0 && !ok {
		return nil, fmt.Errorf("%w: %q is not in the allowlist", errIdentityNotAdmitted, identity)
	}
	return &k8sapiproxy.CallerIdentity{Username: identity}, nil
}
