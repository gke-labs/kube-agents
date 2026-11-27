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
	frontProxyConfigName  = "FrontProxyConfig"
	nameSourceField       = "NameSource"
	allowedNamesName      = frontProxyConfigName + ".AllowedNames"
	allowedPartitionsName = frontProxyConfigName + ".AllowedPartitions"
	// The FrontProxyConfig fields that hold header names, for the constructor's errors.
	usernameHeadersField  = "UsernameHeaders"
	uidHeadersField       = "UIDHeaders"
	groupHeadersField     = "GroupHeaders"
	partitionHeadersField = "PartitionHeaders"
)

var (
	errProxyNotAllowed     = errors.New("the client certificate's name is not an allowed front proxy")
	errHeaderAbsent        = errors.New("the front proxy sent none of the configured headers")
	errHeaderEmpty         = errors.New("the front proxy sent an empty value")
	errHeaderRepeated      = errors.New("the front proxy sent more than one value")
	errPartitionNotAllowed = errors.New("the front proxy named a partition that is not allowed")
)

// FrontProxyConfig configures NewFrontProxyAuthenticator.
type FrontProxyConfig struct {
	// ClientCAFile is the PEM bundle of the CAs a front proxy's certificate must chain to.
	// Required, and it must load when the constructor is called. It is re-read when it
	// changes; a changed bundle that does not load is reported through Reload and the
	// previous one kept. Keep it apart from any CA that signs callers' own certificates.
	ClientCAFile string
	// NameSource is the certificate field the front proxy's name is read from. Required.
	// A certificate without exactly one value there is not a front proxy.
	NameSource IdentitySource
	// AllowedNames lists the front proxies whose headers are believed, compared exactly
	// with the certificate's name. Entries are trimmed and empty ones dropped; at
	// least one is required. Every peer named here can act as any caller.
	AllowedNames []string
	// UsernameHeaders names, in order of preference, the headers holding the caller's
	// username; the first one present in a request is read. At least one is required, and
	// a request from the front proxy without exactly one non-empty value there is refused.
	UsernameHeaders []string
	// UIDHeaders names the headers holding the caller's UID, read as UsernameHeaders is.
	// Optional; when set, a request without exactly one non-empty value there is refused.
	UIDHeaders []string
	// GroupHeaders names the headers holding the caller's groups. Optional. Every value of
	// every one of them present in the request is a group, in order; a value is not split
	// at commas, and an empty one is dropped. A request with none has no groups.
	GroupHeaders []string
	// PartitionHeaders names the headers holding the value stored in
	// CallerIdentity.Partition, read as UsernameHeaders is. Optional; when set, a request
	// without exactly one non-empty value there is refused: a caller never falls back to
	// the "" partition.
	PartitionHeaders []string
	// AllowedPartitions, when not empty, lists the partitions admitted; a request naming
	// another is refused. Entries are trimmed and empty ones dropped; a list left with
	// none is an error. It requires PartitionHeaders.
	AllowedPartitions []string
	// Reload tunes how ClientCAFile is checked for changes and where a failed reload is
	// reported.
	Reload certreload.Options
	// Now returns the time certificates are verified at; time.Now when nil. For tests.
	Now func() time.Time
}

// FrontProxyAuthenticator is a k8sapiproxy.CallerAuthenticator that believes the identity
// headers of a request whose TLS peer is an allowed front proxy. It fills Username, UID,
// Groups and Partition from the headers and records nothing about the proxy itself.
//
// The headers are the caller's credential here, so they must not travel upstream: pass
// HeaderNames to k8sapiproxy.Config.ExtraCallerHeaders. In a chain of authenticators put
// this one first; see the package documentation.
type FrontProxyAuthenticator struct {
	peer              *peerVerifier
	allowedNames      map[string]struct{}
	usernameHeaders   []string
	uidHeaders        []string
	groupHeaders      []string
	partitionHeaders  []string
	allowedPartitions map[string]struct{}
}

// NewFrontProxyAuthenticator returns a FrontProxyAuthenticator for cfg, or an error when
// cfg is incomplete, names a header k8sapiproxy.ValidateCallerHeaderNames refuses, names
// one header twice, or ClientCAFile does not load.
func NewFrontProxyAuthenticator(cfg FrontProxyConfig) (*FrontProxyAuthenticator, error) {
	allowedNames, err := newAllowlist(allowedNamesName, cfg.AllowedNames)
	if err != nil {
		return nil, err
	}
	if len(allowedNames) == 0 {
		return nil, fmt.Errorf("mtlsauth: %s is required", allowedNamesName)
	}
	if len(cfg.UsernameHeaders) == 0 {
		return nil, fmt.Errorf("mtlsauth: %s.%s is required", frontProxyConfigName, usernameHeadersField)
	}
	// One header read as two fields would let one value be, say, both the caller and its
	// partition.
	seen := map[string]string{}
	var lists [][]string
	for _, field := range []struct {
		name  string
		names []string
	}{
		{usernameHeadersField, cfg.UsernameHeaders},
		{uidHeadersField, cfg.UIDHeaders},
		{groupHeadersField, cfg.GroupHeaders},
		{partitionHeadersField, cfg.PartitionHeaders},
	} {
		// The handler's own check, so every name can go into its ExtraCallerHeaders.
		if err := k8sapiproxy.ValidateCallerHeaderNames(field.names); err != nil {
			return nil, fmt.Errorf("mtlsauth: %s.%s %w", frontProxyConfigName, field.name, err)
		}
		canonical := make([]string, 0, len(field.names))
		for _, name := range field.names {
			key := http.CanonicalHeaderKey(name)
			if first, dup := seen[key]; dup {
				return nil, fmt.Errorf("mtlsauth: %s.%s entry %q is already listed in %s", frontProxyConfigName, field.name, name, first)
			}
			seen[key] = field.name
			canonical = append(canonical, key)
		}
		lists = append(lists, canonical)
	}
	allowedPartitions, err := newAllowlist(allowedPartitionsName, cfg.AllowedPartitions)
	if err != nil {
		return nil, err
	}
	if len(allowedPartitions) > 0 && len(cfg.PartitionHeaders) == 0 {
		return nil, fmt.Errorf("mtlsauth: %s requires %s.%s", allowedPartitionsName, frontProxyConfigName, partitionHeadersField)
	}
	peer, err := newPeerVerifier(frontProxyConfigName, nameSourceField, cfg.ClientCAFile, cfg.NameSource, cfg.Reload, cfg.Now)
	if err != nil {
		return nil, err
	}
	return &FrontProxyAuthenticator{
		peer:              peer,
		allowedNames:      allowedNames,
		usernameHeaders:   lists[0],
		uidHeaders:        lists[1],
		groupHeaders:      lists[2],
		partitionHeaders:  lists[3],
		allowedPartitions: allowedPartitions,
	}, nil
}

// HeaderNames returns every header the authenticator reads a caller's identity from, in
// canonical form: the names to add to k8sapiproxy.Config.ExtraCallerHeaders so that none
// of them reaches the API server.
func (a *FrontProxyAuthenticator) HeaderNames() []string {
	var names []string
	for _, list := range [][]string{a.usernameHeaders, a.uidHeaders, a.groupHeaders, a.partitionHeaders} {
		names = append(names, list...)
	}
	return names
}

// AuthenticateRequest admits the caller r's headers name when r's connection belongs to
// an allowed front proxy. A peer that is not one is a plain refusal, which a chain follows
// with its next authenticator; a front proxy whose headers do not name one caller is a
// refusal wrapping k8sapiproxy.ErrCallerRefused, which ends a chain. See the package
// documentation.
func (a *FrontProxyAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	proxy, err := a.peer.name(r)
	if err != nil {
		return nil, err
	}
	if _, ok := a.allowedNames[proxy]; !ok {
		return nil, fmt.Errorf("%w: %q", errProxyNotAllowed, proxy)
	}
	id, err := a.identity(r.Header)
	if err != nil {
		return nil, fmt.Errorf("%w: front proxy %q: %w", k8sapiproxy.ErrCallerRefused, proxy, err)
	}
	return id, nil
}

// identity reads the caller from the headers of an allowed front proxy's request.
func (a *FrontProxyAuthenticator) identity(h http.Header) (*k8sapiproxy.CallerIdentity, error) {
	username, err := singleValue(h, a.usernameHeaders)
	if err != nil {
		return nil, fmt.Errorf("username: %w", err)
	}
	id := &k8sapiproxy.CallerIdentity{Username: username}
	if len(a.uidHeaders) > 0 {
		if id.UID, err = singleValue(h, a.uidHeaders); err != nil {
			return nil, fmt.Errorf("UID: %w", err)
		}
	}
	for _, name := range a.groupHeaders {
		for _, group := range h.Values(name) {
			if group = strings.TrimSpace(group); group != "" {
				id.Groups = append(id.Groups, group)
			}
		}
	}
	if len(a.partitionHeaders) > 0 {
		if id.Partition, err = singleValue(h, a.partitionHeaders); err != nil {
			return nil, fmt.Errorf("partition: %w", err)
		}
		if _, ok := a.allowedPartitions[id.Partition]; len(a.allowedPartitions) > 0 && !ok {
			return nil, fmt.Errorf("%w: %q", errPartitionNotAllowed, id.Partition)
		}
	}
	return id, nil
}

// singleValue returns the value of the first of names present in h. That header must hold
// exactly one non-empty value: a later name is not tried in its place.
func singleValue(h http.Header, names []string) (string, error) {
	for _, name := range names {
		values := h.Values(name)
		if len(values) == 0 {
			continue
		}
		if len(values) > 1 {
			return "", fmt.Errorf("%w in %s", errHeaderRepeated, name)
		}
		value := strings.TrimSpace(values[0])
		if value == "" {
			return "", fmt.Errorf("%w in %s", errHeaderEmpty, name)
		}
		return value, nil
	}
	return "", errHeaderAbsent
}
