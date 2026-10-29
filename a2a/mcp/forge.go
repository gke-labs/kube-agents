package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// The credential broker's version-control read this server makes. The MCP pod
// holds no forge token and calls no forge: it asks the broker
// (agents/platform/scripts/credential_proxy.py, `POST /v1/vcs/<verb>`), which
// checks the repository against the install's managed list and spends its own
// credential. The broker confers this pod's token the "mcp" role, which
// reaches proposal-view and no other verb (ROLE_VCS_VERBS there).
const (
	proposalViewPath = "/v1/vcs/proposal-view"

	// DefaultBrokerTimeout bounds one broker request. A view with the diff
	// and comments is up to five forge calls on the broker's side; the whole
	// tool call has to fit inside the server's write timeout.
	DefaultBrokerTimeout = 25 * time.Second
	// DefaultBrokerMaxResponseBytes bounds one broker answer. It is sized
	// to the pod, not to the broker: the MCP Deployment's memory limit is
	// 128Mi, and one answer costs about three times its size while it is
	// read and decoded (the raw bytes, the decoder's copy and the strings it
	// decodes), so a 4 MiB answer is about 12 MiB per call, and
	// maxInspectInFlight calls, doubled for the Go collector's headroom, are
	// well inside the limit. The answer shows at most 64 KiB
	// of diff, so a bigger cap would buy only the change counts of a diff
	// past 4 MiB, and GitHub refuses a diff past 20,000 lines anyway (about
	// 1 MiB at typical line lengths). A larger answer is refused, and
	// inspect_pr asks again without the diff. The broker's own cap on one
	// forge answer is 8 MiB (DEFAULT_HTTP_MAX_BYTES).
	DefaultBrokerMaxResponseBytes = 4 << 20

	// brokerCommentLimit is the comment limit asked of the broker: as many
	// as inspect_pr shows. GitHub's conversation is three endpoints and the
	// limit applies to each, so the broker may still answer up to three
	// times this many; asking less of each would not give the oldest
	// maxComments across the three, which is what the answer shows. When
	// that is more than an answer can carry, inspect_pr asks again without
	// the comments.
	brokerCommentLimit = maxComments
	// maxBrokerErrorBytes bounds how much of a refusal is read.
	maxBrokerErrorBytes = 8 << 10

	schemeHTTP  = "http"
	schemeHTTPS = "https"

	brokerCodeTooLarge = "FORGE_RESPONSE_TOO_LARGE"
	brokerCodeFailed   = "FORGE_CALL_FAILED"
)

var (
	// ErrBrokerResponseTooLarge is an answer past the client's byte cap.
	ErrBrokerResponseTooLarge = errors.New("the credential broker's answer is larger than this server accepts")

	// brokerCodePattern is the shape of the broker's stable refusal symbols.
	// Anything else in the code field is not repeated.
	brokerCodePattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]{0,63}$`)
)

// BrokerConfig says where the broker is and which token to present.
type BrokerConfig struct {
	// URL is the broker's base URL, e.g. http://platform-agent-credential-proxy.ns.svc:8080.
	URL string
	// TokenFile is the projected ServiceAccount token presented as the
	// bearer. It is read on every request: the kubelet rewrites a projected
	// token in place before it expires.
	TokenFile string
	// Timeout bounds one request; zero is DefaultBrokerTimeout.
	Timeout time.Duration
	// MaxResponseBytes bounds one answer; zero is DefaultBrokerMaxResponseBytes.
	MaxResponseBytes int64
}

// BrokerClient calls the broker's proposal-view verb.
type BrokerClient struct {
	endpoint  string
	tokenFile string
	maxBytes  int64
	http      *http.Client
}

// NewBrokerClient validates cfg. The URL must be a bare http(s) base: no
// credentials, query or fragment (not even an empty `?` or `#`), and no path
// but "/", since the token is the only credential and the path is this
// client's.
func NewBrokerClient(cfg BrokerConfig) (*BrokerClient, error) {
	u, err := url.Parse(strings.TrimSpace(cfg.URL))
	if err != nil {
		return nil, fmt.Errorf("broker URL: %w", err)
	}
	if (u.Scheme != schemeHTTP && u.Scheme != schemeHTTPS) || u.Hostname() == "" {
		return nil, fmt.Errorf("broker URL must be an http or https URL with a host, got %q", cfg.URL)
	}
	// ForceQuery is a bare trailing "?", and a bare "#" leaves Fragment
	// empty: either would end up in front of this client's path.
	if u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || strings.Contains(cfg.URL, "#") {
		return nil, errors.New("broker URL must not carry credentials, a query or a fragment")
	}
	if u.Path != "" && u.Path != "/" {
		return nil, fmt.Errorf("broker URL must not carry a path, got %q", u.Path)
	}
	if strings.TrimSpace(cfg.TokenFile) == "" {
		return nil, errors.New("broker token file is required")
	}
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = DefaultBrokerTimeout
	}
	maxBytes := cfg.MaxResponseBytes
	if maxBytes <= 0 {
		maxBytes = DefaultBrokerMaxResponseBytes
	}
	// The broker is in-cluster: an HTTP_PROXY in the pod's environment must
	// not route the request, and the bearer token on it, out through the
	// proxy (the console's bus transport and the operator's usage scrape
	// make the same choice for theirs).
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &BrokerClient{
		endpoint:  u.Scheme + "://" + u.Host + proposalViewPath,
		tokenFile: cfg.TokenFile,
		maxBytes:  maxBytes,
		http: &http.Client{
			Timeout:   timeout,
			Transport: transport,
			// A redirect is never followed: the next hop would get the
			// bearer token, and the broker has no reason to send one.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

// ProposalViewRequest is the proposal-view payload.
type ProposalViewRequest struct {
	// Repository is owner/name or host/path; the broker resolves it.
	Repository string `json:"repository"`
	Number     int    `json:"number"`
	Diff       bool   `json:"diff"`
	Comments   bool   `json:"comments"`
	Limit      int    `json:"limit,omitempty"`
}

// Proposal is the broker's forge-neutral pull or merge request
// (providers/{github,gitlab}/translate.py proposal()).
type Proposal struct {
	Number     int      `json:"number"`
	Title      string   `json:"title"`
	State      string   `json:"state"`
	Draft      bool     `json:"draft"`
	Author     string   `json:"author"`
	Labels     []string `json:"labels"`
	Source     string   `json:"source"`
	SourceRepo string   `json:"sourceRepo"`
	Target     string   `json:"target"`
	URL        string   `json:"url"`
	Created    string   `json:"created"`
	Updated    string   `json:"updated"`
	Closed     string   `json:"closed"`
	Body       string   `json:"body"`
}

// ProposalComment is one comment (translate.py comment()). Kind is "issue"
// (the conversation), "review_comment" (on a diff line) or "review" (a review
// summary).
type ProposalComment struct {
	Kind    string `json:"kind"`
	Author  string `json:"author"`
	Bot     bool   `json:"bot"`
	Created string `json:"created"`
	Body    string `json:"body"`
	URL     string `json:"url"`
	Path    string `json:"path"`
	Line    *int   `json:"line"`
}

// ProposalView is the proposal-view answer: the proposal, the comments and
// the unified diff when asked for, and the forge and repository the broker
// resolved the request to (Binding.stamp in vcs_broker.py).
type ProposalView struct {
	Proposal          Proposal          `json:"proposal"`
	Comments          []ProposalComment `json:"comments"`
	CommentCount      int               `json:"commentCount"`
	CommentsTruncated bool              `json:"commentsTruncated"`
	Diff              string            `json:"diff"`
	Forge             string            `json:"forge"`
	Repo              string            `json:"repo"`
}

// BrokerError is a refusal or failure the broker answered with.
type BrokerError struct {
	Status int
	// Code is the broker's stable symbol (REPOSITORY_NOT_MANAGED,
	// FORGE_NOT_FOUND, ...), or "" when it sent none of that shape.
	Code string
	// Detail is the broker's own words, kept only for the statuses where
	// they tell the caller what to fix (400, 422, 501).
	Detail string
}

func (e *BrokerError) Error() string {
	msg := brokerStatusMessage(e.Status, e.Code)
	if e.Code != "" {
		msg += " (" + e.Code + ")"
	}
	if e.Detail != "" {
		msg += ": " + cell(e.Detail)
	}
	return msg
}

// brokerStatusMessage is what a refusal means to the person who asked. These
// are this server's words, not the broker's: the broker's text names
// ConfigMaps and audiences that are not the caller's to fix.
func brokerStatusMessage(status int, code string) string {
	switch code {
	case "REPOSITORY_NOT_MANAGED":
		return "the credential broker refused: this install does not manage that repository"
	case "CALLER_ROLE_FORBIDDEN", "CALLER_VERB_FORBIDDEN":
		return "the credential broker refused: this MCP server's identity may not read proposals there"
	case "MANAGED_REPOSITORIES_UNAVAILABLE":
		return "the credential broker could not read the install's managed-repository list; try again later"
	case "VCS_UNAVAILABLE":
		return "the credential broker has no version-control routes"
	case "FORGE_NOT_FOUND":
		return "no such pull or merge request (or the install's credential cannot see it)"
	case "FORGE_FORBIDDEN":
		return "the forge refused the install's credential for that read"
	case "FORGE_UNAUTHENTICATED":
		return "the forge rejected the install's credential; it has expired or been revoked"
	case "FORGE_RATE_LIMITED":
		return "the forge is rate-limiting this install; try again later"
	case brokerCodeTooLarge:
		return "the forge's answer is larger than the credential broker accepts"
	case "CREDENTIAL_PROXY_BUSY":
		return "the credential broker is busy with other requests; try again shortly"
	}
	switch {
	case status == http.StatusUnauthorized:
		return "the credential broker did not accept this MCP server's token (expired, or an audience the broker does not know)"
	case status == http.StatusForbidden:
		return "the credential broker refused the request"
	case status == http.StatusNotFound:
		return "the credential broker found no such route, repository or proposal"
	case status == http.StatusBadRequest, status == http.StatusUnprocessableEntity:
		return "the credential broker rejected the request as malformed"
	case status == http.StatusTooManyRequests, status == http.StatusServiceUnavailable:
		return "the credential broker or the forge is busy; try again later"
	case status == http.StatusNotImplemented:
		return "the credential broker serves no forge for that repository's host"
	case status >= 300 && status < 400:
		return fmt.Sprintf("the credential broker answered with a redirect (HTTP %d), which is not followed", status)
	case status >= 500:
		return fmt.Sprintf("the credential broker or the forge failed (HTTP %d)", status)
	default:
		return fmt.Sprintf("the credential broker answered HTTP %d", status)
	}
}

// ProposalView asks the broker for one proposal.
func (c *BrokerClient) ProposalView(ctx context.Context, req ProposalViewRequest) (*ProposalView, error) {
	token, err := ReadToken(c.tokenFile)
	if err != nil {
		// The path is the pod's, not a secret; the token never reaches here.
		return nil, fmt.Errorf("the credential broker token is unavailable: %w", err)
	}
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set(headerContentType, contentTypeJSON)
	httpReq.Header.Set(headerAuthorization, bearerPrefix+token)
	resp, err := c.http.Do(httpReq)
	if err != nil {
		// *url.Error names the endpoint and the cause, never a header.
		return nil, fmt.Errorf("the credential broker did not answer: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return nil, brokerError(resp, token)
	}
	tooLarge := fmt.Errorf("%w (%d bytes)", ErrBrokerResponseTooLarge, c.maxBytes)
	if resp.ContentLength > c.maxBytes {
		// Refused before a byte of it is read.
		return nil, tooLarge
	}
	// One buffer sized to the declared length (plus the read slack
	// bytes.Buffer wants), rather than io.ReadAll's doubling, which can
	// hold up to twice the answer while it grows.
	var raw bytes.Buffer
	if resp.ContentLength > 0 {
		raw.Grow(int(resp.ContentLength) + bytes.MinRead)
	}
	if _, err := raw.ReadFrom(io.LimitReader(resp.Body, c.maxBytes+1)); err != nil {
		return nil, fmt.Errorf("reading the credential broker's answer: %w", err)
	}
	if int64(raw.Len()) > c.maxBytes {
		return nil, tooLarge
	}
	var view ProposalView
	if err := json.Unmarshal(raw.Bytes(), &view); err != nil {
		return nil, fmt.Errorf("the credential broker's answer is not a proposal view: %w", err)
	}
	return &view, nil
}

func brokerError(resp *http.Response, token string) error {
	e := &BrokerError{Status: resp.StatusCode}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxBrokerErrorBytes))
	var payload struct {
		Error string `json:"error"`
		Code  string `json:"code"`
	}
	if json.Unmarshal(raw, &payload) == nil {
		if brokerCodePattern.MatchString(payload.Code) {
			e.Code = payload.Code
		}
		switch resp.StatusCode {
		case http.StatusBadRequest, http.StatusUnprocessableEntity, http.StatusNotImplemented:
			// The broker redacts credentials at its boundary; this
			// server's own token is taken out again here regardless.
			e.Detail = strings.TrimSpace(strings.ReplaceAll(payload.Error, token, "[redacted]"))
		}
	}
	return e
}

// diffMayBeRefused reports whether err is the kind of failure a diff too big
// for the forge or the broker produces, so the view is worth asking for again
// without it. GitHub answers 406 to a diff past 20,000 lines or 300 files,
// which the broker relays as FORGE_CALL_FAILED; a diff past the broker's 8 MiB
// is FORGE_RESPONSE_TOO_LARGE; one past this client's cap is
// ErrBrokerResponseTooLarge. A timeout or a cancellation is never one of
// these, whatever else the error carries: asking again would spend a second
// request on a broker or forge that is already slow, or on a caller who has
// gone.
func diffMayBeRefused(err error) bool {
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) ||
		(errors.As(err, &ne) && ne.Timeout()) {
		return false
	}
	if errors.Is(err, ErrBrokerResponseTooLarge) {
		return true
	}
	var be *BrokerError
	if errors.As(err, &be) {
		return be.Code == brokerCodeTooLarge || (be.Status == http.StatusBadGateway && (be.Code == brokerCodeFailed || be.Code == ""))
	}
	return false
}

// answerTooLarge reports whether err is a refusal that is known to be about
// size: the broker's FORGE_RESPONSE_TOO_LARGE, or an answer past this
// client's cap. diffMayBeRefused is wider, since a bare 502 may be one too.
func answerTooLarge(err error) bool {
	if errors.Is(err, ErrBrokerResponseTooLarge) {
		return true
	}
	var be *BrokerError
	return errors.As(err, &be) && be.Code == brokerCodeTooLarge
}
