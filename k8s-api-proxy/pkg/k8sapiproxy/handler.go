package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	authFailedMessage   = messagePrefix + "caller authentication failed"
	unclassifiedMessage = messagePrefix + "the request path could not be classified"
	upstreamFailed      = "kube-proxy could not reach cluster %q"

	// flushImmediately streams each upstream write to the caller, which watch and log
	// follow need.
	flushImmediately = -1
)

// Config configures NewHandler.
type Config struct {
	// Authenticator identifies the caller. Required.
	Authenticator CallerAuthenticator
	// Upstream forwards an admitted request to its cluster. Required.
	Upstream UpstreamTransport
	// DefaultClusterID is the cluster a path without a /clusters/{id} prefix targets;
	// InClusterID when empty.
	DefaultClusterID string
	// Policy decides which requests are forwarded. Its Mode must be set.
	Policy Policy
}

type clusterIDKey struct{}

type handler struct {
	authenticator    CallerAuthenticator
	defaultClusterID string
	policy           Policy
	factory          *request.RequestInfoFactory
	proxy            *httputil.ReverseProxy
}

// NewHandler returns the proxy's HTTP handler. Each request is authenticated, routed to a
// cluster by SplitClusterPath, classified, evaluated by cfg.Policy, stripped of the caller's
// credentials and identity headers, and forwarded through cfg.Upstream. With
// cfg.Policy.RedactSecrets, a Secret response carries no data or stringData values and no
// annotation holding a copy of them. Every refusal is a
// metav1.Status.
func NewHandler(cfg Config) (http.Handler, error) {
	if cfg.Authenticator == nil {
		return nil, errors.New("k8sapiproxy: Config.Authenticator is required")
	}
	if cfg.Upstream == nil {
		return nil, errors.New("k8sapiproxy: Config.Upstream is required")
	}
	if !cfg.Policy.Mode.valid() {
		return nil, fmt.Errorf("k8sapiproxy: invalid Config.Policy.Mode %q", cfg.Policy.Mode)
	}
	if cfg.DefaultClusterID != "" && !validClusterID.MatchString(cfg.DefaultClusterID) {
		return nil, fmt.Errorf("k8sapiproxy: invalid Config.DefaultClusterID %q", cfg.DefaultClusterID)
	}

	upstream := cfg.Upstream
	return &handler{
		authenticator:    cfg.Authenticator,
		defaultClusterID: cfg.DefaultClusterID,
		policy:           cfg.Policy,
		factory:          NewRequestInfoFactory(),
		proxy: &httputil.ReverseProxy{
			// Rewrite rather than Director: the reverse proxy then also drops the caller's
			// Forwarded, X-Forwarded-For, X-Forwarded-Host and X-Forwarded-Proto headers. The
			// outbound URL keeps only the path and query, so a scheme, host or userinfo from an
			// absolute-form request URI never reaches the transport.
			Rewrite: func(pr *httputil.ProxyRequest) {
				pr.Out.Host = ""
				pr.Out.URL = &url.URL{Path: pr.In.URL.Path, RawQuery: pr.In.URL.RawQuery}
			},
			Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
				clusterID, _ := req.Context().Value(clusterIDKey{}).(string)
				resp, err := upstream.RoundTripCluster(req.Context(), clusterID, req)
				// Redacting here rather than in ModifyResponse keeps the plan off resp.Request,
				// which a transport may replace with a request of its own.
				if plan, ok := req.Context().Value(secretRedactionKey{}).(*secretRedaction); ok && resp != nil {
					redactSecretResponse(resp, plan)
				}
				return resp, err
			}),
			FlushInterval: flushImmediately,
			// The transport's error is not echoed: it can name the upstream endpoint.
			ErrorHandler: func(w http.ResponseWriter, req *http.Request, _ error) {
				clusterID, _ := req.Context().Value(clusterIDKey{}).(string)
				writeStatus(w, http.StatusBadGateway, metav1.StatusReasonInternalError,
					fmt.Sprintf(upstreamFailed, clusterID))
			},
		},
	}, nil
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// The authenticator's error is not echoed, as the transport's is not: it can describe the
	// proxy's own authentication setup.
	if caller, err := h.authenticator.AuthenticateRequest(r); err != nil || caller == nil {
		writeStatus(w, http.StatusUnauthorized, metav1.StatusReasonUnauthorized, authFailedMessage)
		return
	}

	clusterID, apiPath, perr := SplitClusterPath(r.URL, h.defaultClusterID)
	if perr != nil {
		writePolicyError(w, perr)
		return
	}

	out := r.Clone(r.Context())
	out.URL.Path = apiPath
	out.URL.RawPath = ""

	info, err := h.factory.NewRequestInfo(out)
	if err != nil {
		writeStatus(w, http.StatusBadRequest, metav1.StatusReasonBadRequest, unclassifiedMessage)
		return
	}
	if perr := h.policy.Evaluate(out, info); perr != nil {
		writePolicyError(w, perr)
		return
	}

	sanitizeRequestHeaders(out.Header, h.policy.permitsUpgrade(info))
	ctx := context.WithValue(out.Context(), clusterIDKey{}, clusterID)
	if h.policy.RedactSecrets && isCoreV1Secret(info) {
		ctx = context.WithValue(ctx, secretRedactionKey{}, rewriteSecretRequest(out.Header, info))
	}
	out = out.WithContext(ctx)
	h.proxy.ServeHTTP(w, out)
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
