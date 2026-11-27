// Package mtlsauth holds two k8sapiproxy.CallerAuthenticators that identify a caller by
// the client certificate of the TLS connection its request arrived on.
//
// ClientCertAuthenticator names the caller after the certificate itself, such as a
// workload's SPIFFE X.509-SVID. FrontProxyAuthenticator admits an authenticating front
// proxy by its certificate and reads the caller the proxy vouches for from request
// headers.
//
// Both verify the certificate on every request, not once per connection: the leaf and the
// intermediates the peer sent are checked against the authenticator's own CA bundle, for
// client authentication, at the current time. A certificate that expired, or whose CA was
// rotated out of the bundle, is refused on the next request of a connection opened while
// it was good. The TLS handshake proves the peer holds the certificate's key and nothing
// more is asked of it, so the listener may use tls.VerifyClientCertIfGiven with the union
// of the bundles (certreload.NewCAPoolUnion) or tls.RequestClientCert. Revocation (CRL,
// OCSP) is not checked. A certificate without an extended key usage extension is accepted
// as crypto/x509 and the TLS handshake accept it; one that lists usages without client
// authentication is refused.
//
// The name is read from one certificate field, chosen in the configuration: the URI SAN,
// the DNS SAN or the subject common name. No other field is tried when it is missing, and
// a certificate carrying more than one value of the chosen field is refused, since which
// one names the peer would be a guess. Names are compared byte for byte.
//
// # Errors
//
// The handler answers 401 for every error below; they differ in what a chain of
// authenticators does next.
//
//   - No client certificate, a certificate that does not verify against this
//     authenticator's bundle, a certificate without exactly one name in the configured
//     field, and a name the allowlist or the prefix does not admit: a plain error. The
//     certificate was not shown to be this authenticator's to judge, so a chain offers the
//     request to the next authenticator. A certificate issued for the other authenticator
//     of this package, under its own CA, ends here.
//   - A front proxy that verified and is in AllowedNames, whose identity headers are
//     missing, empty, repeated, or name a partition that is not allowed: an error wrapping
//     k8sapiproxy.ErrCallerRefused. The request is the proxy's, and a proxy that names no
//     caller must not have its request admitted by another authenticator on a credential
//     the proxy merely forwarded. A chain stops there.
//   - Neither authenticator returns k8sapiproxy.ErrAuthenticationUnavailable: verification
//     needs nothing but the bundle in memory. A bundle that cannot be read or parsed after
//     a change is reported through the reload options and the last good one keeps
//     verifying; only the constructor fails on a bundle that does not load.
package mtlsauth

import (
	"crypto/x509"
	"encoding/asn1"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
)

// IdentitySource names the certificate field a peer's name is read from.
type IdentitySource string

const (
	// SourceURI reads the certificate's URI subject alternative name, such as a SPIFFE ID.
	SourceURI IdentitySource = "uri"
	// SourceDNS reads the certificate's DNS subject alternative name.
	SourceDNS IdentitySource = "dns"
	// SourceCN reads the common name of the certificate's subject.
	SourceCN IdentitySource = "cn"

	// headerNameSymbols are the characters, besides letters and digits, of an HTTP field
	// name (RFC 9110 section 5.6.2, tchar).
	headerNameSymbols = "!#$%&'*+-.^_`|~"
)

var (
	// oidCommonName is the X.520 common name attribute of a subject.
	oidCommonName = asn1.ObjectIdentifier{2, 5, 4, 3}

	errNoClientCertificate = errors.New("the request carries no client certificate")
	errNotVerified         = errors.New("the client certificate does not verify against the configured CA bundle")
	errNoName              = errors.New("the client certificate has no value in the configured field")
	errSeveralNames        = errors.New("the client certificate has more than one value in the configured field")
)

// peerVerifier verifies a request's client certificate against one CA bundle and reads
// the peer's name from one field of it.
type peerVerifier struct {
	roots  *certreload.CAPool
	source IdentitySource
	now    func() time.Time
}

// newPeerVerifier loads caFile; owner names the configuration type in its errors, and
// sourceField that type's field holding source.
func newPeerVerifier(owner, sourceField, caFile string, source IdentitySource, reload certreload.Options, now func() time.Time) (*peerVerifier, error) {
	if caFile == "" {
		return nil, fmt.Errorf("mtlsauth: %s.ClientCAFile is required", owner)
	}
	switch source {
	case SourceURI, SourceDNS, SourceCN:
	case "":
		return nil, fmt.Errorf("mtlsauth: %s.%s is required: %q, %q or %q", owner, sourceField, SourceURI, SourceDNS, SourceCN)
	default:
		return nil, fmt.Errorf("mtlsauth: %s.%s %q is not %q, %q or %q", owner, sourceField, source, SourceURI, SourceDNS, SourceCN)
	}
	roots, err := certreload.NewCAPool(caFile, reload)
	if err != nil {
		return nil, fmt.Errorf("mtlsauth: %s.ClientCAFile: %w", owner, err)
	}
	if now == nil {
		now = time.Now
	}
	return &peerVerifier{roots: roots, source: source, now: now}, nil
}

// name returns the name of the peer that sent r, once its certificate verifies against
// the current bundle at the current time. Its errors are plain: the certificate is not
// one this verifier vouches for.
func (p *peerVerifier) name(r *http.Request) (string, error) {
	if r.TLS == nil || len(r.TLS.PeerCertificates) == 0 {
		return "", errNoClientCertificate
	}
	leaf := r.TLS.PeerCertificates[0]
	opts := x509.VerifyOptions{
		Roots:       p.roots.Pool(),
		CurrentTime: p.now(),
		KeyUsages:   []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	if rest := r.TLS.PeerCertificates[1:]; len(rest) > 0 {
		opts.Intermediates = x509.NewCertPool()
		for _, cert := range rest {
			opts.Intermediates.AddCert(cert)
		}
	}
	if _, err := leaf.Verify(opts); err != nil {
		return "", fmt.Errorf("%w: %w", errNotVerified, err)
	}
	return certificateName(leaf, p.source)
}

// certificateName reads the one value cert carries in source's field.
func certificateName(cert *x509.Certificate, source IdentitySource) (string, error) {
	var values []string
	switch source {
	case SourceURI:
		for _, uri := range cert.URIs {
			values = append(values, uri.String())
		}
	case SourceDNS:
		values = cert.DNSNames
	case SourceCN:
		// Subject.CommonName is the last of several; every one is counted instead.
		for _, attr := range cert.Subject.Names {
			if attr.Type.Equal(oidCommonName) {
				value, _ := attr.Value.(string)
				values = append(values, value)
			}
		}
	}
	switch {
	case len(values) == 0:
		return "", errNoName
	case len(values) > 1:
		return "", errSeveralNames
	case values[0] == "":
		return "", errNoName
	}
	return values[0], nil
}

// newAllowlist returns the trimmed, non-empty entries of list as a set; nil for an empty
// list. A list whose entries are all empty is an error, so a list that names nothing
// never comes to mean "admit anything".
func newAllowlist(field string, list []string) (map[string]struct{}, error) {
	var set map[string]struct{}
	for _, entry := range list {
		if entry = strings.TrimSpace(entry); entry != "" {
			if set == nil {
				set = map[string]struct{}{}
			}
			set[entry] = struct{}{}
		}
	}
	if len(list) > 0 && len(set) == 0 {
		return nil, fmt.Errorf("mtlsauth: %s has only empty entries", field)
	}
	return set, nil
}

// validHeaderName reports whether name is a non-empty HTTP field name: letters, digits and
// headerNameSymbols, so no spaces or colons.
func validHeaderName(name string) bool {
	if name == "" {
		return false
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		case strings.IndexByte(headerNameSymbols, c) >= 0:
		default:
			return false
		}
	}
	return true
}
