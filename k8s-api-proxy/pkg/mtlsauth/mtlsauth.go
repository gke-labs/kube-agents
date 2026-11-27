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
// it was good. This check is in addition to the TLS handshake, not in place of it: the
// listener must verify a client certificate when the connection opens, with
// tls.VerifyClientCertIfGiven or tls.RequireAndVerifyClientCert and ClientCAs holding the
// union of the bundles (certreload.NewCAPoolUnion). A listener that only requests a
// certificate lets a peer no CA vouches for make every request pay for a chain search, so
// do not use tls.RequestClientCert or tls.RequireAnyClientCert. As a bound of its own, a
// peer that presented more than eight certificates is refused before any verification.
// Revocation (CRL, OCSP) is not checked. A certificate without an extended key usage
// extension is accepted as crypto/x509 and the TLS handshake accept it; one that lists
// usages without client authentication is refused.
//
// The name is read from one certificate field, chosen in the configuration: the URI SAN,
// the DNS SAN or the subject common name. No other field is tried when it is missing, and
// a certificate carrying more than one value of the chosen field is refused, since which
// one names the peer would be a guess. Names are compared exactly, with no case folding
// or pattern. A URI name is the certificate's own text with the scheme in lower case and
// nothing else changed: a URI that net/url would write back differently (a space it would
// escape, for example), or that has userinfo, a query, a fragment, even an empty one, or
// a "." or ".." path segment, plain or percent-encoded, is refused. X.509 name constraints do not apply
// to the subject common name: with SourceCN, a name-constrained CA in the bundle can
// still issue any name.
//
// # Errors
//
// The handler answers 401 for every error below; they differ in what a chain of
// authenticators does next.
//
//   - No client certificate, more certificates than the bound, a certificate that does
//     not verify against this authenticator's bundle, and a certificate without exactly
//     one usable name in the configured field: a plain error. The certificate was not
//     shown to be this authenticator's to judge, so a chain offers the request to the
//     next authenticator. A certificate issued for the other authenticator of this
//     package, under its own CA, falls through this way.
//   - A certificate that verified against this bundle and whose name AllowedIdentities,
//     RequiredURIPrefix or AllowedNames does not admit: a plain error as well. This one is
//     the authenticator's to judge, and it returns a plain error by choice, so that a
//     chain can admit the caller another way, as the filters of pkg/jwtauth do.
//   - A front proxy that verified and is in AllowedNames, whose identity headers are
//     missing, empty, repeated, or name a partition that is not allowed: an error wrapping
//     k8sapiproxy.ErrCallerRefused. The request is the proxy's, and a proxy that names no
//     caller must not have its request admitted by another authenticator on a credential
//     the proxy merely forwarded. A chain stops there, which protects only the
//     authenticators after this one: put FrontProxyAuthenticator first. An authenticator
//     ahead of it that reads a bearer token would admit a front proxy's request under the
//     token's identity before the headers are looked at.
//
// Only the header failures of a front proxy that verifies now are final. A proxy whose
// certificate expired, or whose CA was rotated out of the bundle, is a peer like any
// other to this authenticator, and its requests fall through. In a chain with a
// bearer-token authenticator behind it, a request such a proxy forwards with the caller's
// own Authorization header is then admitted under the token's identity and that
// authenticator's partition. Do not chain a bearer-token authenticator behind
// FrontProxyAuthenticator on a listener where the proxy forwards callers' Authorization,
// or have the proxy strip it.
//
// Neither authenticator returns k8sapiproxy.ErrAuthenticationUnavailable: verification
// needs nothing but the bundle in memory. A bundle that cannot be read or parsed after a
// change is reported through the reload options and the last good one keeps verifying;
// only the constructor fails on a bundle that does not load.
package mtlsauth

import (
	"crypto/x509"
	"encoding/asn1"
	"errors"
	"fmt"
	"net/http"
	"net/url"
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

	// maxPeerCertificates bounds the certificates a peer may have presented, the leaf
	// included. Every request searches for a chain through the intermediates the peer
	// sent, so their number is the peer's lever on the cost of a request; a leaf under a
	// root through six intermediates is deeper than any PKI in use.
	maxPeerCertificates = 8
	// uriDelimiters start a query or a fragment, which a URI used as a name cannot have,
	// even an empty one (the SPIFFE ID rule).
	uriDelimiters = "?#"
	// uriSchemeEnd ends a URI's scheme, the one part net/url writes back in lower case.
	uriSchemeEnd     = ":"
	uriPathSeparator = "/"
	// uriDotSegment and uriParentSegment are the path segments a consumer may resolve
	// away, so that two paths name one place.
	uriDotSegment    = "."
	uriParentSegment = ".."
	// sanURITag is the tag of a uniformResourceIdentifier in a subject alternative name
	// (RFC 5280 section 4.2.1.6).
	sanURITag = 6
)

var (
	// oidCommonName is the X.520 common name attribute of a subject.
	oidCommonName = asn1.ObjectIdentifier{2, 5, 4, 3}

	// oidSubjectAltName is the subject alternative name extension.
	oidSubjectAltName = asn1.ObjectIdentifier{2, 5, 29, 17}

	errNoClientCertificate = errors.New("the request carries no client certificate")
	errTooManyCertificates = errors.New("the peer presented more certificates than are verified")
	errURINotPlain         = errors.New("the client certificate's URI is not written in the form a name is read from")

	// dots writes a percent-encoded dot as the dot a path segment is compared with.
	dots            = strings.NewReplacer("%2e", ".", "%2E", ".")
	errNotVerified  = errors.New("the client certificate does not verify against the configured CA bundle")
	errNoName       = errors.New("the client certificate has no value in the configured field")
	errSeveralNames = errors.New("the client certificate has more than one value in the configured field")
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
	if len(r.TLS.PeerCertificates) > maxPeerCertificates {
		return "", errTooManyCertificates
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
	if source == SourceURI && !plainURI(cert, values[0]) {
		return "", errURINotPlain
	}
	return values[0], nil
}

// plainURI reports whether the one URI SAN of cert is in the form a name is read from:
// what uriNameProblem allows, once the scheme is in lower case. cert.URIs cannot say:
// net/url drops an empty fragment and re-escapes what it parsed, so the text is taken
// from the bytes the certificate carries and must be the name itself.
func plainURI(cert *x509.Certificate, name string) bool {
	raw, ok := rawURINames(cert)
	if !ok || len(raw) != 1 {
		return false
	}
	written := raw[0]
	if i := strings.Index(written, uriSchemeEnd); i >= 0 {
		written = strings.ToLower(written[:i]) + written[i:]
	}
	return written == name && uriNameProblem(written) == ""
}

// uriNameProblem says why raw cannot be a URI name, or "" when it can. A name is a URI
// that net/url writes back exactly as it was written, so that two spellings never stand
// for one name, and that has no userinfo, query, fragment (not even an empty one) or dot
// segment in its path.
func uriNameProblem(raw string) string {
	uri, err := url.Parse(raw)
	switch {
	case err != nil:
		return "is not a URI"
	case uri.String() != raw:
		return fmt.Sprintf("is not in the form it is compared in, %q", uri.String())
	case uri.User != nil:
		return "has userinfo"
	case strings.ContainsAny(raw, uriDelimiters):
		return "has a query or a fragment"
	}
	for _, segment := range strings.Split(uri.EscapedPath(), uriPathSeparator) {
		switch dots.Replace(segment) {
		case uriDotSegment, uriParentSegment:
			return "has a dot segment in its path"
		}
	}
	return ""
}

// rawURINames returns the URI subject alternative names of cert as they are encoded; ok
// is false when the extension cannot be read.
func rawURINames(cert *x509.Certificate) (names []string, ok bool) {
	for _, ext := range cert.Extensions {
		if !ext.Id.Equal(oidSubjectAltName) {
			continue
		}
		var san asn1.RawValue
		if rest, err := asn1.Unmarshal(ext.Value, &san); err != nil || len(rest) > 0 {
			return nil, false
		}
		for rest := san.Bytes; len(rest) > 0; {
			var name asn1.RawValue
			var err error
			if rest, err = asn1.Unmarshal(rest, &name); err != nil {
				return nil, false
			}
			if name.Class == asn1.ClassContextSpecific && name.Tag == sanURITag {
				names = append(names, string(name.Bytes))
			}
		}
	}
	return names, true
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
