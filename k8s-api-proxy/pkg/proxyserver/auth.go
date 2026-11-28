package proxyserver

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/jwtauth"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	authModeJWT  = "jwt"
	authModeFlag = "auth-mode"
	// listSeparator separates the entries of a list-valued flag or environment variable.
	listSeparator = ","
	// reasonSeparator joins the reasons of a chain's authenticators into one line.
	reasonSeparator = "; "
	// reasonPrefixSeparator follows the name or the sentinel an error text starts with.
	reasonPrefixSeparator = ": "

	jwtIssuerFlag            = "jwt-issuer"
	jwtIssuerEnv             = "KUBE_PROXY_JWT_ISSUER"
	jwtKeySetURLFlag         = "jwt-jwks-url"
	jwtKeySetURLEnv          = "KUBE_PROXY_JWT_JWKS_URL"
	jwtAudiencesFlag         = "jwt-audiences"
	jwtAudiencesEnv          = "KUBE_PROXY_JWT_AUDIENCES"
	jwtUsernameClaimFlag     = "jwt-username-claim"
	jwtUsernameClaimEnv      = "KUBE_PROXY_JWT_USERNAME_CLAIM"
	jwtUIDClaimFlag          = "jwt-uid-claim"
	jwtUIDClaimEnv           = "KUBE_PROXY_JWT_UID_CLAIM"
	jwtGroupsClaimFlag       = "jwt-groups-claim"
	jwtGroupsClaimEnv        = "KUBE_PROXY_JWT_GROUPS_CLAIM"
	jwtPartitionClaimFlag    = "jwt-partition-claim"
	jwtPartitionClaimEnv     = "KUBE_PROXY_JWT_PARTITION_CLAIM"
	jwtRequiredSubPrefixFlag = "jwt-required-sub-prefix"
	jwtRequiredSubPrefixEnv  = "KUBE_PROXY_JWT_REQUIRED_SUB_PREFIX"
	jwtAllowedPartitionsFlag = "jwt-allowed-partitions"
	jwtAllowedPartitionsEnv  = "KUBE_PROXY_JWT_ALLOWED_PARTITIONS"
	jwtCAFileFlag            = "jwt-ca-file"
	jwtCAFileEnv             = "KUBE_PROXY_JWT_CA_FILE"

	partitionByCallerFlag = "partition-by-caller"
	partitionByCallerEnv  = "KUBE_PROXY_PARTITION_BY_CALLER"
	// The --partition-by-caller values: one partition for every caller, one per
	// CallerIdentity.Partition, or one per CallerIdentity.Username.
	partitionByNone      = "none"
	partitionByPartition = "partition"
	partitionByUsername  = "username"

	// The attributes of the startup record that says JWT authentication is on.
	logKeyIssuer           = "issuer"
	logKeyAudiences        = "audiences"
	logKeyKeySetDiscovered = "key_set_discovered"
)

// errNoAuthenticatorAccepted is the refusal of a chain whose authenticators gave no reason.
var errNoAuthenticatorAccepted = errors.New("no caller authenticator accepted the request")

// splitList returns the trimmed, non-empty entries of a comma-separated value; nil when
// there are none, so an unset flag is no list rather than a list of one empty entry.
func splitList(raw string) []string {
	var entries []string
	for _, entry := range strings.Split(raw, listSeparator) {
		if entry = strings.TrimSpace(entry); entry != "" {
			entries = append(entries, entry)
		}
	}
	return entries
}

// parseAuthModes reads --auth-mode: none alone, or token-review and jwt, either or both, in
// the order their authenticators are tried.
func parseAuthModes(raw string) ([]string, error) {
	modes := splitList(raw)
	if len(modes) == 0 {
		return nil, invalidAuthModeError(raw)
	}
	for i, mode := range modes {
		switch mode {
		case authModeTokenReview, authModeJWT:
		case authModeNone:
			if len(modes) > 1 {
				return nil, fmt.Errorf("invalid --%s %q: %q cannot be combined with another mode", authModeFlag, raw, authModeNone)
			}
		default:
			return nil, invalidAuthModeError(raw)
		}
		if slices.Contains(modes[:i], mode) {
			return nil, fmt.Errorf("invalid --%s %q: %q is listed twice", authModeFlag, raw, mode)
		}
	}
	return modes, nil
}

func invalidAuthModeError(raw string) error {
	return fmt.Errorf("invalid --%s %q: expected %q, or a comma-separated list of %q and %q",
		authModeFlag, raw, authModeNone, authModeTokenReview, authModeJWT)
}

// trimAuthFlags drops the whitespace around the values --jwt-* and --partition-by-caller
// carry, such as the newline of a value read from a file, so a value is checked and used in
// one form and a whitespace-only one is unset.
func trimAuthFlags(cfg *proxyConfig) {
	for _, value := range []*string{
		&cfg.jwtIssuer, &cfg.jwtKeySetURL, &cfg.jwtAudiences, &cfg.jwtUsernameClaim, &cfg.jwtUIDClaim,
		&cfg.jwtGroupsClaim, &cfg.jwtPartitionClaim, &cfg.jwtRequiredSubPrefix, &cfg.jwtAllowedPartitions,
		&cfg.jwtCAFile, &cfg.partitionByCaller,
	} {
		*value = strings.TrimSpace(*value)
	}
}

// jwtFlagsSet names the --jwt-* flags that carry a value, for the error that refuses them
// without jwt in --auth-mode.
func jwtFlagsSet(cfg proxyConfig) []string {
	var set []string
	for _, f := range []struct{ name, value string }{
		{jwtIssuerFlag, cfg.jwtIssuer},
		{jwtKeySetURLFlag, cfg.jwtKeySetURL},
		{jwtAudiencesFlag, cfg.jwtAudiences},
		{jwtUsernameClaimFlag, cfg.jwtUsernameClaim},
		{jwtUIDClaimFlag, cfg.jwtUIDClaim},
		{jwtGroupsClaimFlag, cfg.jwtGroupsClaim},
		{jwtPartitionClaimFlag, cfg.jwtPartitionClaim},
		{jwtRequiredSubPrefixFlag, cfg.jwtRequiredSubPrefix},
		{jwtAllowedPartitionsFlag, cfg.jwtAllowedPartitions},
		{jwtCAFileFlag, cfg.jwtCAFile},
	} {
		if f.value != "" {
			set = append(set, "--"+f.name)
		}
	}
	return set
}

// validateAuthFlags refuses flags that belong to an authenticator --auth-mode does not
// enable, and an embedder's Options.AudienceCallers without token-review: left alone they
// would be ignored, and the operator who set an allowlist or an issuer would believe it
// applied. The none mode makes its own checks.
func validateAuthFlags(cfg proxyConfig, modes []string) error {
	if !slices.Contains(modes, authModeJWT) {
		if set := jwtFlagsSet(cfg); len(set) > 0 {
			return fmt.Errorf("%s requires %q in --%s", strings.Join(set, ", "), authModeJWT, authModeFlag)
		}
	}
	if slices.Contains(modes, authModeTokenReview) || slices.Contains(modes, authModeNone) {
		return nil
	}
	if strings.TrimSpace(cfg.allowedCallers) != "" {
		return fmt.Errorf("--allowed-callers (or %s) requires %q in --%s", allowedCallersEnv, authModeTokenReview, authModeFlag)
	}
	if strings.TrimSpace(cfg.sessionAudience) != "" || strings.TrimSpace(cfg.sessionCallers) != "" {
		return fmt.Errorf("%s and %s require %q in --%s", sessionAudienceEnv, sessionCallersEnv, authModeTokenReview, authModeFlag)
	}
	if len(cfg.embedder.AudienceCallers) > 0 {
		return audienceCallersErrorf("Options.AudienceCallers requires %q in --%s", authModeTokenReview, authModeFlag)
	}
	return nil
}

// newJWTAuthenticator builds the jwt mode's authenticator from the --jwt-* flags. It has no
// per-caller allowlist: every token the issuer signs for one of the audiences is admitted,
// narrowed only by --jwt-required-sub-prefix and --jwt-allowed-partitions.
func newJWTAuthenticator(cfg proxyConfig, logger *slog.Logger) (k8sapiproxy.CallerAuthenticator, error) {
	if cfg.jwtIssuer == "" {
		return nil, fmt.Errorf("--%s (or %s) is required when --%s includes %q", jwtIssuerFlag, jwtIssuerEnv, authModeFlag, authModeJWT)
	}
	audiences := splitList(cfg.jwtAudiences)
	if len(audiences) == 0 {
		return nil, fmt.Errorf("--%s (or %s) is required when --%s includes %q and must name at least one audience", jwtAudiencesFlag, jwtAudiencesEnv, authModeFlag, authModeJWT)
	}
	allowedPartitions := splitList(cfg.jwtAllowedPartitions)
	// Only separators: a list that names nothing must not come to mean "admit any partition".
	if cfg.jwtAllowedPartitions != "" && len(allowedPartitions) == 0 {
		return nil, fmt.Errorf("--%s (or %s) names no partition", jwtAllowedPartitionsFlag, jwtAllowedPartitionsEnv)
	}
	authn, err := jwtauth.NewAuthenticator(jwtauth.Config{
		Issuer:            cfg.jwtIssuer,
		KeySetURL:         cfg.jwtKeySetURL,
		Audiences:         audiences,
		UsernameClaim:     cfg.jwtUsernameClaim,
		UIDClaim:          cfg.jwtUIDClaim,
		GroupsClaim:       cfg.jwtGroupsClaim,
		PartitionClaim:    cfg.jwtPartitionClaim,
		AllowedPartitions: allowedPartitions,
		RequiredSubPrefix: cfg.jwtRequiredSubPrefix,
		CAFile:            cfg.jwtCAFile,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to initialize JWT authenticator: %w", err)
	}
	logger.Info(
		"JWT caller authentication enabled",
		logKeyIssuer, cfg.jwtIssuer,
		logKeyAudiences, audiences,
		logKeyKeySetDiscovered, cfg.jwtKeySetURL == "",
	)
	return authn, nil
}

// callerPartitionFunc is the k8sapiproxy.Config.CallerPartition --partition-by-caller asks
// for; nil for none, which keeps every caller in one partition.
func callerPartitionFunc(cfg proxyConfig, modes []string) (func(*k8sapiproxy.CallerIdentity) string, error) {
	switch cfg.partitionByCaller {
	case partitionByNone:
		return nil, nil
	case "":
		// Passed empty on the command line: not a way to ask for the default.
		if cfg.partitionByCallerSet {
			return nil, fmt.Errorf("invalid --%s %q: expected %q, %q or %q", partitionByCallerFlag, cfg.partitionByCaller, partitionByNone, partitionByPartition, partitionByUsername)
		}
		return nil, nil
	case partitionByPartition:
		// Without the claim every caller's Partition is "", and the flag would separate nobody.
		if !slices.Contains(modes, authModeJWT) || cfg.jwtPartitionClaim == "" {
			return nil, fmt.Errorf("--%s=%s requires %q in --%s and --%s", partitionByCallerFlag, partitionByPartition, authModeJWT, authModeFlag, jwtPartitionClaimFlag)
		}
		return func(caller *k8sapiproxy.CallerIdentity) string { return caller.Partition }, nil
	case partitionByUsername:
		// Two authenticators can name different callers alike, and a bare username would put
		// them in one partition.
		if len(modes) > 1 {
			return nil, fmt.Errorf("--%s=%s cannot be combined with more than one mode in --%s", partitionByCallerFlag, partitionByUsername, authModeFlag)
		}
		return func(caller *k8sapiproxy.CallerIdentity) string { return caller.Username }, nil
	default:
		return nil, fmt.Errorf("invalid --%s %q: expected %q, %q or %q", partitionByCallerFlag, cfg.partitionByCaller, partitionByNone, partitionByPartition, partitionByUsername)
	}
}

// namedAuthenticator records its mode's name in the caller it admits, so the audit record
// says which authenticator admitted a caller that another could have named alike.
type namedAuthenticator struct {
	name string
	next k8sapiproxy.CallerAuthenticator
}

func (n namedAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	caller, err := n.next.AuthenticateRequest(r)
	if err != nil || caller == nil {
		return caller, err
	}
	named := *caller
	named.Authenticator = n.name
	return &named, nil
}

// chainAuthenticator tries its authenticators in order and admits the caller the first one
// accepts, and stops at one whose refusal wraps k8sapiproxy.ErrCallerRefused. When none does, the request is unavailable (503) if any authenticator could not
// decide, since the one that failed might have accepted it, and unauthenticated (401) only
// when every one of them refused. Each authenticator gets its own copy of the request, so
// one that consumes the Authorization header leaves it for the next.
type chainAuthenticator []k8sapiproxy.CallerAuthenticator

// newChainAuthenticator returns the one authenticator as it is, and a chain for several.
func newChainAuthenticator(authenticators []k8sapiproxy.CallerAuthenticator) k8sapiproxy.CallerAuthenticator {
	if len(authenticators) == 1 {
		return authenticators[0]
	}
	return chainAuthenticator(authenticators)
}

func (c chainAuthenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	var reasons []string
	unavailable := false
	for _, authn := range c {
		caller, err := authn.AuthenticateRequest(r.Clone(r.Context()))
		if err == nil && caller != nil {
			return caller, nil
		}
		if err == nil {
			err = errNoAuthenticatorAccepted
		}
		// An authenticator that recognized the credential and refused it is the last word:
		// the next one must not admit what this one turned away.
		if errors.Is(err, k8sapiproxy.ErrCallerRefused) {
			if named, ok := authn.(namedAuthenticator); ok {
				return nil, fmt.Errorf("%s%s%w", named.name, reasonPrefixSeparator, err)
			}
			return nil, err
		}
		if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
			unavailable = true
		}
		// The sentinel's text is said once, in front of the whole line.
		reason := strings.ReplaceAll(err.Error(), k8sapiproxy.ErrAuthenticationUnavailable.Error()+reasonPrefixSeparator, "")
		if named, ok := authn.(namedAuthenticator); ok {
			reason = named.name + reasonPrefixSeparator + reason
		}
		reasons = append(reasons, reason)
	}
	if len(reasons) == 0 {
		return nil, errNoAuthenticatorAccepted
	}
	// One line: the audit record carries it as a single attribute.
	reason := strings.Join(reasons, reasonSeparator)
	if unavailable {
		return nil, fmt.Errorf("%w%s%s", k8sapiproxy.ErrAuthenticationUnavailable, reasonPrefixSeparator, reason)
	}
	return nil, errors.New(reason)
}
