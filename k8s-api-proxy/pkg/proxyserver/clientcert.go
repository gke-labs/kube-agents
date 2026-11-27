package proxyserver

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/mtlsauth"
)

const (
	// The --auth-mode values that read the client certificate of the request's connection.
	authModeMTLS          = "mtls"
	authModeRequestHeader = "requestheader"

	mtlsClientCAFileFlag      = "mtls-client-ca-file"
	mtlsClientCAFileEnv       = "KUBE_PROXY_MTLS_CLIENT_CA_FILE"
	mtlsIdentitySourceFlag    = "mtls-identity-source"
	mtlsIdentitySourceEnv     = "KUBE_PROXY_MTLS_IDENTITY_SOURCE"
	mtlsAllowedIdentitiesFlag = "mtls-allowed-identities"
	mtlsAllowedIdentitiesEnv  = "KUBE_PROXY_MTLS_ALLOWED_IDENTITIES"
	mtlsRequiredURIPrefixFlag = "mtls-required-uri-prefix"
	mtlsRequiredURIPrefixEnv  = "KUBE_PROXY_MTLS_REQUIRED_URI_PREFIX"

	headerClientCAFileFlag      = "requestheader-client-ca-file"
	headerClientCAFileEnv       = "KUBE_PROXY_REQUESTHEADER_CLIENT_CA_FILE"
	headerNameSourceFlag        = "requestheader-name-source"
	headerNameSourceEnv         = "KUBE_PROXY_REQUESTHEADER_NAME_SOURCE"
	headerAllowedNamesFlag      = "requestheader-allowed-names"
	headerAllowedNamesEnv       = "KUBE_PROXY_REQUESTHEADER_ALLOWED_NAMES"
	headerUsernameHeadersFlag   = "requestheader-username-headers"
	headerUsernameHeadersEnv    = "KUBE_PROXY_REQUESTHEADER_USERNAME_HEADERS"
	headerUIDHeadersFlag        = "requestheader-uid-headers"
	headerUIDHeadersEnv         = "KUBE_PROXY_REQUESTHEADER_UID_HEADERS"
	headerGroupHeadersFlag      = "requestheader-group-headers"
	headerGroupHeadersEnv       = "KUBE_PROXY_REQUESTHEADER_GROUP_HEADERS"
	headerPartitionHeadersFlag  = "requestheader-partition-headers"
	headerPartitionHeadersEnv   = "KUBE_PROXY_REQUESTHEADER_PARTITION_HEADERS"
	headerAllowedPartitionsFlag = "requestheader-allowed-partitions"
	headerAllowedPartitionsEnv  = "KUBE_PROXY_REQUESTHEADER_ALLOWED_PARTITIONS"

	// certificateSources is how a usage text and an error name the three certificate fields.
	certificateSources = "uri, dns or cn"
	// libraryErrorPrefix starts every error of pkg/mtlsauth's constructors.
	libraryErrorPrefix = "mtlsauth: "
	// flagListSeparator joins flag names in an error that names several.
	flagListSeparator = ", "

	// The protocols the listener offers once it asks for client certificates. net/http adds
	// them to the server's own TLS configuration, and the per-handshake configuration that
	// replaces it has to name them itself, or the listener would stop speaking HTTP/2.
	nextProtoHTTP2 = "h2"
	nextProtoHTTP1 = "http/1.1"

	// The attributes of the startup records that say a certificate mode is on.
	logKeyClientCAFile     = "client_ca_file"
	logKeyClientCAFiles    = "client_ca_files"
	logKeyCertificateField = "certificate_field"
	logKeyAllowedNames     = "allowed_names"
	logKeyCallerHeaders    = "caller_headers"
	logKeyOrder            = "order"
)

// certificateAuthModes are the modes that read the connection's client certificate, in the
// order the binary tries them: requestheader first, whatever --auth-mode's own order.
var certificateAuthModes = []string{authModeRequestHeader, authModeMTLS}

// errBearerNotConsulted is added to the refusal of a request whose connection presented a
// client certificate no certificate mode admitted, when --auth-mode also lists modes that
// read a bearer token: they were not asked.
var errBearerNotConsulted = errors.New("the connection presented a client certificate, so no bearer-token authenticator was consulted")

// clientCertConfig holds the values of the --mtls-* and --requestheader-* flags.
type clientCertConfig struct {
	mtlsClientCAFile      string
	mtlsIdentitySource    string
	mtlsAllowedIdentities string
	mtlsRequiredURIPrefix string

	headerClientCAFile      string
	headerNameSource        string
	headerAllowedNames      string
	headerUsernameHeaders   string
	headerUIDHeaders        string
	headerGroupHeaders      string
	headerPartitionHeaders  string
	headerAllowedPartitions string

	// reload and now are not read from a flag or the environment and are zero in the
	// binary: tests set the clock the client CA bundles are reloaded on and the time
	// certificates are verified at.
	reload certreload.Options
	now    func() time.Time
}

// clientCertFlag is one --mtls-* or --requestheader-* flag: its name, its environment
// variable, its usage text and where its value is kept.
type clientCertFlag struct {
	name, env, usage string
	value            *string
}

func (c *clientCertConfig) mtlsFlags() []clientCertFlag {
	return []clientCertFlag{
		{mtlsClientCAFileFlag, mtlsClientCAFileEnv, "PEM bundle of the CAs a caller's client certificate must chain to, re-read when it changes; required with --" + authModeFlag + "=" + authModeMTLS, &c.mtlsClientCAFile},
		{mtlsIdentitySourceFlag, mtlsIdentitySourceEnv, "Certificate field a caller's username is read from: " + certificateSources + "; required with --" + authModeFlag + "=" + authModeMTLS + ", and uri when " + authModeMTLS + " is listed with another mode", &c.mtlsIdentitySource},
		{mtlsAllowedIdentitiesFlag, mtlsAllowedIdentitiesEnv, "Comma-separated certificate identities admitted, compared exactly (empty admits every certificate the CA issues, narrowed only by --" + mtlsRequiredURIPrefixFlag + ")", &c.mtlsAllowedIdentities},
		{mtlsRequiredURIPrefixFlag, mtlsRequiredURIPrefixEnv, "Prefix a caller certificate's URI must start with, ending in /; needs --" + mtlsIdentitySourceFlag + "=uri, and is required when " + authModeMTLS + " is listed with another mode", &c.mtlsRequiredURIPrefix},
	}
}

func (c *clientCertConfig) requestHeaderFlags() []clientCertFlag {
	return []clientCertFlag{
		{headerClientCAFileFlag, headerClientCAFileEnv, "PEM bundle of the CAs a front proxy's client certificate must chain to, re-read when it changes; required with --" + authModeFlag + "=" + authModeRequestHeader, &c.headerClientCAFile},
		{headerNameSourceFlag, headerNameSourceEnv, "Certificate field a front proxy's name is read from: " + certificateSources + "; required with --" + authModeFlag + "=" + authModeRequestHeader, &c.headerNameSource},
		{headerAllowedNamesFlag, headerAllowedNamesEnv, "Comma-separated names of the front proxies whose identity headers are believed, compared exactly; at least one is required with --" + authModeFlag + "=" + authModeRequestHeader, &c.headerAllowedNames},
		{headerUsernameHeadersFlag, headerUsernameHeadersEnv, "Comma-separated request headers holding the caller's username, the first one present being read; at least one is required with --" + authModeFlag + "=" + authModeRequestHeader, &c.headerUsernameHeaders},
		{headerUIDHeadersFlag, headerUIDHeadersEnv, "Comma-separated request headers holding the caller's UID; when set, a front proxy's request without one is refused", &c.headerUIDHeaders},
		{headerGroupHeadersFlag, headerGroupHeadersEnv, "Comma-separated request headers whose every value is one of the caller's groups (optional)", &c.headerGroupHeaders},
		{headerPartitionHeadersFlag, headerPartitionHeadersEnv, "Comma-separated request headers holding the caller's tenant partition; when set, a front proxy's request without one is refused", &c.headerPartitionHeaders},
		{headerAllowedPartitionsFlag, headerAllowedPartitionsEnv, "Comma-separated --" + headerPartitionHeadersFlag + " values admitted (empty admits any)", &c.headerAllowedPartitions},
	}
}

func (c *clientCertConfig) flags() []clientCertFlag {
	return append(c.mtlsFlags(), c.requestHeaderFlags()...)
}

// registerClientCertFlags registers the --mtls-* and --requestheader-* flags, each
// defaulting to its environment variable.
func registerClientCertFlags(fs *flag.FlagSet, c *clientCertConfig) {
	for _, f := range c.flags() {
		fs.StringVar(f.value, f.name, envOrDefault(f.env, ""), f.usage)
	}
}

// flagsSet names the flags among flags that carry a value.
func flagsSet(flags []clientCertFlag) []string {
	var set []string
	for _, f := range flags {
		if *f.value != "" {
			set = append(set, flagArg(f.name))
		}
	}
	return set
}

// flagArg writes a flag's name as an operator types it.
func flagArg(name string) string { return "--" + name }

// requiredFlagError is the refusal of a mode whose required flag is empty.
func requiredFlagError(name, env, mode string) error {
	return fmt.Errorf("--%s (or %s) is required when --%s includes %q", name, env, authModeFlag, mode)
}

// certificateSource reads a --mtls-identity-source or --requestheader-name-source value.
func certificateSource(name, env, mode, value string) (mtlsauth.IdentitySource, error) {
	switch source := mtlsauth.IdentitySource(value); source {
	case mtlsauth.SourceURI, mtlsauth.SourceDNS, mtlsauth.SourceCN:
		return source, nil
	case "":
		return "", fmt.Errorf("%w: %s", requiredFlagError(name, env, mode), certificateSources)
	default:
		return "", fmt.Errorf("invalid --%s %q: expected %s", name, value, certificateSources)
	}
}

// listFlag splits a list-valued flag. A value of only separators is an error, so a list
// that names nothing never comes to mean "admit anything" or "read no header".
func listFlag(name, env, value string) ([]string, error) {
	entries := splitList(value)
	if value != "" && len(entries) == 0 {
		return nil, fmt.Errorf("--%s (or %s) names nothing: it holds only separators", name, env)
	}
	return entries, nil
}

// usesClientCertificates reports whether modes holds a mode that reads the connection's
// client certificate.
func usesClientCertificates(modes []string) bool {
	return slices.ContainsFunc(certificateAuthModes, func(mode string) bool { return slices.Contains(modes, mode) })
}

// orderedAuthModes returns modes in the order their authenticators are built and tried:
// requestheader, then mtls, then the others as --auth-mode lists them. The front proxy
// authenticator's final refusal binds only what comes after it, so its place is not left
// to the order an operator happened to write.
func orderedAuthModes(modes []string) []string {
	ordered := make([]string, 0, len(modes))
	for _, mode := range certificateAuthModes {
		if slices.Contains(modes, mode) {
			ordered = append(ordered, mode)
		}
	}
	for _, mode := range modes {
		if !slices.Contains(certificateAuthModes, mode) {
			ordered = append(ordered, mode)
		}
	}
	return ordered
}

// validateClientCertFlags refuses a --mtls-* or --requestheader-* flag without its mode,
// either mode on a listener without TLS, and a mode whose flags are incomplete or
// contradict each other. What is left for the constructors of pkg/mtlsauth to refuse is the
// form of a name, a prefix or a header, and a CA bundle that does not load.
func validateClientCertFlags(cfg proxyConfig, modes []string) error {
	c := cfg.clientCert
	for _, mode := range []struct {
		name  string
		flags []clientCertFlag
	}{
		{authModeMTLS, c.mtlsFlags()},
		{authModeRequestHeader, c.requestHeaderFlags()},
	} {
		if slices.Contains(modes, mode.name) {
			continue
		}
		if set := flagsSet(mode.flags); len(set) > 0 {
			return fmt.Errorf("%s requires %q in --%s", strings.Join(set, flagListSeparator), mode.name, authModeFlag)
		}
	}
	if !usesClientCertificates(modes) {
		return nil
	}
	if strings.TrimSpace(cfg.tlsCertFile) == "" || strings.TrimSpace(cfg.tlsKeyFile) == "" {
		return fmt.Errorf("--%s with %q or %q requires --%s and --%s: a client certificate is read from the TLS connection, and the listener would otherwise serve plain HTTP",
			authModeFlag, authModeMTLS, authModeRequestHeader, tlsCertFileFlag, tlsKeyFileFlag)
	}
	if slices.Contains(modes, authModeMTLS) {
		if err := validateMTLSFlags(c, len(modes) > 1); err != nil {
			return err
		}
	}
	if slices.Contains(modes, authModeRequestHeader) {
		return validateRequestHeaderFlags(c)
	}
	return nil
}

// validateMTLSFlags checks the --mtls-* flags of a listed mtls mode; chained says another
// mode is listed with it.
func validateMTLSFlags(c clientCertConfig, chained bool) error {
	if c.mtlsClientCAFile == "" {
		return requiredFlagError(mtlsClientCAFileFlag, mtlsClientCAFileEnv, authModeMTLS)
	}
	source, err := certificateSource(mtlsIdentitySourceFlag, mtlsIdentitySourceEnv, authModeMTLS, c.mtlsIdentitySource)
	if err != nil {
		return err
	}
	if c.mtlsRequiredURIPrefix != "" && source != mtlsauth.SourceURI {
		return fmt.Errorf("--%s requires --%s=%s", mtlsRequiredURIPrefixFlag, mtlsIdentitySourceFlag, mtlsauth.SourceURI)
	}
	// Another authenticator names its callers in a namespace of its own, such as
	// system:serviceaccount:<namespace>:<name>. A URI under a required prefix cannot be one
	// of those names; a common name or a DNS name can.
	if chained && (source != mtlsauth.SourceURI || c.mtlsRequiredURIPrefix == "") {
		return fmt.Errorf("%q listed with another mode in --%s requires --%s=%s and --%s, so a certificate cannot carry a username another authenticator issues",
			authModeMTLS, authModeFlag, mtlsIdentitySourceFlag, mtlsauth.SourceURI, mtlsRequiredURIPrefixFlag)
	}
	_, err = listFlag(mtlsAllowedIdentitiesFlag, mtlsAllowedIdentitiesEnv, c.mtlsAllowedIdentities)
	return err
}

// validateRequestHeaderFlags checks the --requestheader-* flags of a listed requestheader
// mode.
func validateRequestHeaderFlags(c clientCertConfig) error {
	if c.headerClientCAFile == "" {
		return requiredFlagError(headerClientCAFileFlag, headerClientCAFileEnv, authModeRequestHeader)
	}
	if _, err := certificateSource(headerNameSourceFlag, headerNameSourceEnv, authModeRequestHeader, c.headerNameSource); err != nil {
		return err
	}
	for _, list := range []struct {
		name, env, value string
		required         bool
	}{
		{headerAllowedNamesFlag, headerAllowedNamesEnv, c.headerAllowedNames, true},
		{headerUsernameHeadersFlag, headerUsernameHeadersEnv, c.headerUsernameHeaders, true},
		{headerUIDHeadersFlag, headerUIDHeadersEnv, c.headerUIDHeaders, false},
		{headerGroupHeadersFlag, headerGroupHeadersEnv, c.headerGroupHeaders, false},
		{headerPartitionHeadersFlag, headerPartitionHeadersEnv, c.headerPartitionHeaders, false},
		{headerAllowedPartitionsFlag, headerAllowedPartitionsEnv, c.headerAllowedPartitions, false},
	} {
		if list.required && list.value == "" {
			return requiredFlagError(list.name, list.env, authModeRequestHeader)
		}
		if _, err := listFlag(list.name, list.env, list.value); err != nil {
			return err
		}
	}
	if c.headerAllowedPartitions != "" && c.headerPartitionHeaders == "" {
		return fmt.Errorf("--%s requires --%s", headerAllowedPartitionsFlag, headerPartitionHeadersFlag)
	}
	return nil
}

// flagNamedError is an error of a pkg/mtlsauth constructor, worded with the flags an
// operator set in place of the Go fields the library names.
type flagNamedError struct {
	text string
	err  error
}

func (e *flagNamedError) Error() string { return e.text }
func (e *flagNamedError) Unwrap() error { return e.err }

// The Go names in the errors of pkg/mtlsauth's constructors and the flags that set them, a
// qualified name ahead of the bare one it ends in.
var (
	mtlsFieldFlags = strings.NewReplacer(
		libraryErrorPrefix, "",
		"ClientCertConfig.ClientCAFile", flagArg(mtlsClientCAFileFlag),
		"ClientCertConfig.IdentitySource", flagArg(mtlsIdentitySourceFlag),
		"ClientCertConfig.AllowedIdentities", flagArg(mtlsAllowedIdentitiesFlag),
		"ClientCertConfig.RequiredURIPrefix", flagArg(mtlsRequiredURIPrefixFlag),
		"IdentitySource", flagArg(mtlsIdentitySourceFlag),
	)
	headerFieldFlags = strings.NewReplacer(
		libraryErrorPrefix, "",
		"FrontProxyConfig.ClientCAFile", flagArg(headerClientCAFileFlag),
		"FrontProxyConfig.NameSource", flagArg(headerNameSourceFlag),
		"FrontProxyConfig.AllowedNames", flagArg(headerAllowedNamesFlag),
		"FrontProxyConfig.AllowedPartitions", flagArg(headerAllowedPartitionsFlag),
		"FrontProxyConfig.UsernameHeaders", flagArg(headerUsernameHeadersFlag),
		"FrontProxyConfig.UIDHeaders", flagArg(headerUIDHeadersFlag),
		"FrontProxyConfig.GroupHeaders", flagArg(headerGroupHeadersFlag),
		"FrontProxyConfig.PartitionHeaders", flagArg(headerPartitionHeadersFlag),
		"UsernameHeaders", flagArg(headerUsernameHeadersFlag),
		"UIDHeaders", flagArg(headerUIDHeadersFlag),
		"GroupHeaders", flagArg(headerGroupHeadersFlag),
		"PartitionHeaders", flagArg(headerPartitionHeadersFlag),
	)
)

// clientCAReload is how a certificate mode's CA bundle is reloaded: on the test's clock
// when one is set, and with a changed bundle that does not load reported the way a changed
// server certificate is, as a warning and in the reload failure counter.
func clientCAReload(c clientCertConfig, logger *slog.Logger, metrics *k8sapiproxy.Metrics, what, flagName string) certreload.Options {
	opts := c.reload
	opts.OnReloadError = func(err error) {
		logger.Warn(what+" against the previous CA bundle: the changed --"+flagName+" could not be loaded", "err", err)
		metrics.RecordCertificateReloadFailure()
	}
	return opts
}

// newMTLSAuthenticator builds the mtls mode's authenticator from the --mtls-* flags
// validateClientCertFlags accepted.
func newMTLSAuthenticator(c clientCertConfig, logger *slog.Logger, metrics *k8sapiproxy.Metrics) (k8sapiproxy.CallerAuthenticator, error) {
	authn, err := mtlsauth.NewClientCertAuthenticator(mtlsauth.ClientCertConfig{
		ClientCAFile:      c.mtlsClientCAFile,
		IdentitySource:    mtlsauth.IdentitySource(c.mtlsIdentitySource),
		AllowedIdentities: splitList(c.mtlsAllowedIdentities),
		RequiredURIPrefix: c.mtlsRequiredURIPrefix,
		Reload:            clientCAReload(c, logger, metrics, "verifying callers' certificates", mtlsClientCAFileFlag),
		Now:               c.now,
	})
	if err != nil {
		return nil, &flagNamedError{text: "failed to initialize the " + authModeMTLS + " authenticator: " + mtlsFieldFlags.Replace(err.Error()), err: err}
	}
	logger.Info(
		"client certificate caller authentication enabled",
		logKeyClientCAFile, c.mtlsClientCAFile,
		logKeyCertificateField, c.mtlsIdentitySource,
	)
	return authn, nil
}

// newRequestHeaderAuthenticator builds the requestheader mode's authenticator from the
// --requestheader-* flags validateClientCertFlags accepted.
func newRequestHeaderAuthenticator(c clientCertConfig, logger *slog.Logger, metrics *k8sapiproxy.Metrics) (*mtlsauth.FrontProxyAuthenticator, error) {
	authn, err := mtlsauth.NewFrontProxyAuthenticator(mtlsauth.FrontProxyConfig{
		ClientCAFile:      c.headerClientCAFile,
		NameSource:        mtlsauth.IdentitySource(c.headerNameSource),
		AllowedNames:      splitList(c.headerAllowedNames),
		UsernameHeaders:   splitList(c.headerUsernameHeaders),
		UIDHeaders:        splitList(c.headerUIDHeaders),
		GroupHeaders:      splitList(c.headerGroupHeaders),
		PartitionHeaders:  splitList(c.headerPartitionHeaders),
		AllowedPartitions: splitList(c.headerAllowedPartitions),
		Reload:            clientCAReload(c, logger, metrics, "verifying front proxies' certificates", headerClientCAFileFlag),
		Now:               c.now,
	})
	if err != nil {
		return nil, &flagNamedError{text: "failed to initialize the " + authModeRequestHeader + " authenticator: " + headerFieldFlags.Replace(err.Error()), err: err}
	}
	logger.Info(
		"front proxy caller authentication enabled: every allowed front proxy can name any caller",
		logKeyClientCAFile, c.headerClientCAFile,
		logKeyCertificateField, c.headerNameSource,
		logKeyAllowedNames, splitList(c.headerAllowedNames),
		logKeyCallerHeaders, authn.HeaderNames(),
	)
	return authn, nil
}

// mergeCallerHeaders returns the embedder's Options.ExtraCallerHeaders followed by the
// headers the front proxy authenticator reads that are not among them already, so every
// identity header stops at the proxy.
func mergeCallerHeaders(embedder, frontProxy []string) []string {
	if len(frontProxy) == 0 {
		return embedder
	}
	merged := slices.Clone(embedder)
	for _, name := range frontProxy {
		if !slices.ContainsFunc(merged, func(have string) bool { return strings.EqualFold(have, name) }) {
			merged = append(merged, name)
		}
	}
	return merged
}

// presentedClientCertificate reports whether r arrived on a TLS connection whose peer
// presented a client certificate. The listener verifies one that is given, so a connection
// that has one opened with a certificate a configured client CA vouched for at the time.
func presentedClientCertificate(r *http.Request) bool {
	return r.TLS != nil && len(r.TLS.PeerCertificates) > 0
}

// certificateFirstAuthenticator is the binary's authenticator once a certificate mode is
// listed. A request on a connection that presented a client certificate is the
// certificate modes' alone: when none of them admits it, it is refused, and the modes that
// read a bearer token are never asked. A request on a connection without one goes to the
// bearer modes.
//
// The certificate modes verify the certificate again on every request, so a front proxy
// whose certificate expired, or whose CA was rotated out of the bundle, on a connection
// that is still open looks to them like any peer they do not know. Offered to the bearer
// modes, a request it forwards with a caller's own Authorization header would be admitted
// under that token's identity, with the proxy's identity headers ignored. The connection
// still carries the certificate it opened with, which is what this authenticator decides on.
type certificateFirstAuthenticator struct {
	// certificates are the certificate modes, requestheader first.
	certificates k8sapiproxy.CallerAuthenticator
	// bearer are the other listed modes; nil when --auth-mode lists none.
	bearer k8sapiproxy.CallerAuthenticator
}

// newModeAuthenticator returns the authenticator of the listed modes: the bearer modes'
// chain as it always was when no certificate mode is listed.
func newModeAuthenticator(certificates, bearer []k8sapiproxy.CallerAuthenticator) k8sapiproxy.CallerAuthenticator {
	if len(certificates) == 0 {
		return newChainAuthenticator(bearer)
	}
	authn := certificateFirstAuthenticator{certificates: newChainAuthenticator(certificates)}
	if len(bearer) > 0 {
		authn.bearer = newChainAuthenticator(bearer)
	}
	return authn
}

func (a certificateFirstAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	if a.bearer != nil && !presentedClientCertificate(r) {
		return a.bearer.AuthenticateRequest(r)
	}
	caller, err := a.certificates.AuthenticateRequest(r)
	if err == nil && caller == nil {
		err = errNoAuthenticatorAccepted
	}
	if err != nil && a.bearer != nil {
		return nil, fmt.Errorf("%w%s%w", err, reasonSeparator, errBearerNotConsulted)
	}
	return caller, err
}

// clientCAFiles returns the CA bundles of the certificate modes --auth-mode lists, the
// front proxies' first; none when it lists neither mode. It reads a configuration
// buildHandler has accepted.
func clientCAFiles(cfg proxyConfig) []string {
	trimAuthFlags(&cfg)
	modes, err := parseAuthModes(cfg.authMode)
	if err != nil {
		return nil
	}
	var files []string
	if slices.Contains(modes, authModeRequestHeader) {
		files = append(files, cfg.clientCert.headerClientCAFile)
	}
	if slices.Contains(modes, authModeMTLS) {
		files = append(files, cfg.clientCert.mtlsClientCAFile)
	}
	return files
}

// newClientCAPool pools the CA bundles of the listed certificate modes for the listener's
// handshakes; nil when --auth-mode lists neither mode. Each bundle reloads on its own, and
// one that stops loading keeps its last good certificates in the pool, reported as the
// authenticators' own bundles are.
func newClientCAPool(cfg proxyConfig, logger *slog.Logger, metrics *k8sapiproxy.Metrics) (*certreload.CAPool, error) {
	files := clientCAFiles(cfg)
	if len(files) == 0 {
		return nil, nil
	}
	opts := cfg.clientCert.reload
	opts.OnReloadError = func(err error) {
		logger.Warn("accepting client certificates in TLS handshakes against the previous CA bundle: a changed client CA file could not be loaded", "err", err)
		metrics.RecordCertificateReloadFailure()
	}
	pool, err := certreload.NewCAPoolUnion(files, opts)
	if err != nil {
		return nil, fmt.Errorf("failed to load the client CA bundles (--%s, --%s): %w", mtlsClientCAFileFlag, headerClientCAFileFlag, err)
	}
	logger.Info("the listener verifies a client certificate when one is presented", logKeyClientCAFiles, files)
	return pool, nil
}

// handshakeConfig is the TLS configuration of the handshakes made while pool is the
// current set of client CAs.
type handshakeConfig struct {
	pool   *x509.CertPool
	config *tls.Config
}

// verifyClientCertificates returns a tls.Config.GetConfigForClient for a listener whose
// TLS configuration is base. Each handshake asks for a client certificate and verifies one
// that is given against the current clientCAs, so a rotated bundle applies to the next
// handshake; a peer that presents none still connects, which is what a health probe and a
// bearer-token caller do. The configuration is built once per version of the pool.
func verifyClientCertificates(base *tls.Config, clientCAs *certreload.CAPool) func(*tls.ClientHelloInfo) (*tls.Config, error) {
	// Read now: net/http adds protocols to base once the server starts.
	minVersion, getCertificate := base.MinVersion, base.GetCertificate
	var current atomic.Pointer[handshakeConfig]
	return func(*tls.ClientHelloInfo) (*tls.Config, error) {
		pool := clientCAs.Pool()
		if cached := current.Load(); cached != nil && cached.pool == pool {
			return cached.config, nil
		}
		config := &tls.Config{
			MinVersion:     minVersion,
			GetCertificate: getCertificate,
			NextProtos:     []string{nextProtoHTTP2, nextProtoHTTP1},
			ClientAuth:     tls.VerifyClientCertIfGiven,
			ClientCAs:      pool,
		}
		current.Store(&handshakeConfig{pool: pool, config: config})
		return config, nil
	}
}
