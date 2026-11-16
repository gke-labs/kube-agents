package workeradapter

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"net/url"
	"os"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// The broker's MCP relay as a profile worker reaches it (#5495). A persona's
// MCP servers are rendered as `worker-adapter mcp-relay <url>`, a stdio MCP
// server the harness starts per server, which POSTs each JSON-RPC message to
// the credential broker's `/v1/mcp/<server>` with the pod's credentials. The
// broker's mcp_policy decides what a profile worker may call
// (PROFILE_SERVER_TOOLS); this only carries the messages.
//
// It attaches the pod's projected ServiceAccount token and the task's
// credential to every request, so where it sends them is held here, not left
// to the URL it was given: a model that rewrites its MCP config must not be
// able to point it anywhere else. The URL has to be the broker's own
// (CREDENTIAL_PROXY_URL) scheme and host, under its /v1/mcp/ path, and no
// redirect is followed.
const (
	// MCPRelayCommand is the subcommand that runs the bridge.
	MCPRelayCommand = "mcp-relay"
	// MCPRelayPath is the broker's MCP relay route.
	MCPRelayPath = "/v1/mcp"

	// The broker's address and the pod's two credential files, as the
	// kubectl and gcloud shims read them (credential_proxy_client.py).
	EnvCredentialProxyURL       = "CREDENTIAL_PROXY_URL"             // #nosec G101 -- an env var name, not a credential
	EnvCredentialProxyTokenFile = "CREDENTIAL_PROXY_TOKEN_FILE"      // #nosec G101 -- an env var name, not a credential
	EnvCredentialProxyTaskToken = "CREDENTIAL_PROXY_TASK_TOKEN_FILE" // #nosec G101 -- an env var name, not a credential
	taskCredentialHeader        = "X-Kube-Agents-Task-Credential"    // #nosec G101 -- a header name, not a credential
	// The broker's ceiling on a task credential (task_credential.MAX_TOKEN_BYTES).
	maxTaskCredentialBytes = 8192

	mcpRelayTimeout = 120 * time.Second
	// The most of a reply read from the broker, which caps its own upstream
	// replies well below this.
	maxRelayReplyBytes = 16 << 20
	// The largest message read from the harness.
	maxRelayMessageBytes = 4 << 20
	// jsonRPCServerError is the JSON-RPC code for an error the bridge
	// answers with itself, never the server.
	jsonRPCServerError = -32000
)

var (
	taskCredentialShape = regexp.MustCompile(`\A[A-Za-z0-9_.-]+\z`)
	relayServerName     = regexp.MustCompile(`\A[a-z0-9_]+\z`)
	errRelayRedirect    = errors.New("the broker answered with a redirect, which the relay never follows")
)

// ProfileRelay is the relay a profile worker renders its persona's MCP
// servers with: this binary as `mcp-relay <broker>/v1/mcp/<server>`, given
// the broker's address and the pod's credential files explicitly rather than
// relying on the harness to pass its environment through. None of the three
// is a secret; the bridge reads the files on each request. Empty when the pod
// has no broker (CREDENTIAL_PROXY_URL unset), which drops the servers.
func ProfileRelay(self string, getenv func(string) string) persona.Relay {
	base := strings.TrimRight(strings.TrimSpace(getenv(EnvCredentialProxyURL)), "/")
	if base == "" || self == "" {
		return persona.Relay{}
	}
	env := map[string]string{EnvCredentialProxyURL: base}
	for _, k := range []string{EnvCredentialProxyTokenFile, EnvCredentialProxyTaskToken} {
		if v := strings.TrimSpace(getenv(k)); v != "" {
			env[k] = v
		}
	}
	return persona.Relay{URL: base + MCPRelayPath, Command: self, Args: []string{MCPRelayCommand, "{url}"}, Env: env}
}

// RelayTarget checks that raw is a server under the broker's MCP relay, base
// being CREDENTIAL_PROXY_URL, and returns it parsed. Scheme and host must be
// the broker's, the path the relay route plus one server name, with no
// userinfo, query or fragment, and nothing a path clean would move.
func RelayTarget(base, raw string) (*url.URL, error) {
	b, err := url.Parse(strings.TrimSpace(base))
	if err != nil || b.Scheme == "" || b.Host == "" {
		return nil, fmt.Errorf("%s is not a broker URL", EnvCredentialProxyURL)
	}
	if b.Scheme != "http" && b.Scheme != "https" {
		return nil, fmt.Errorf("%s has scheme %q", EnvCredentialProxyURL, b.Scheme)
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("the relay URL doesn't parse: %w", err)
	}
	if u.Scheme != b.Scheme || u.Host != b.Host || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return nil, errors.New("the relay URL is not the broker's: credentials go only to " + EnvCredentialProxyURL)
	}
	prefix := strings.TrimRight(b.Path, "/") + MCPRelayPath + "/"
	if u.RawPath != "" || path.Clean(u.Path) != u.Path || !strings.HasPrefix(u.Path, prefix) {
		return nil, errors.New("the relay URL is not under the broker's " + MCPRelayPath + "/ route")
	}
	if !relayServerName.MatchString(strings.TrimPrefix(u.Path, prefix)) {
		return nil, errors.New("the relay URL names no single MCP server")
	}
	return u, nil
}

// ServeMCPRelay bridges newline-delimited JSON-RPC on in to the broker at
// target and writes the replies to out, one message per line, until in ends.
// A request the broker can't answer gets a JSON-RPC error with its id, so
// the harness sees why rather than waiting; a notification gets nothing.
func ServeMCPRelay(ctx context.Context, target string, in io.Reader, out io.Writer, log *slog.Logger) error {
	u, err := RelayTarget(os.Getenv(EnvCredentialProxyURL), target)
	if err != nil {
		return err
	}
	client := newRelayClient()
	r := bufio.NewReaderSize(in, 64<<10)
	w := bufio.NewWriter(out)
	for {
		line, err := readRelayLine(r)
		if errors.Is(err, errRelayLineTooLong) {
			// No id can be read from a message this size, so it gets no
			// reply; the bridge stays up for the rest of the task.
			log.Warn("mcp-relay: dropped a message over the size limit", "limit", maxRelayMessageBytes)
			continue
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var probe struct {
			ID json.RawMessage `json:"id"`
		}
		if err := json.Unmarshal(line, &probe); err != nil {
			// A batch array lands here too: MCP has no batching since
			// 2025-06-18, and no harness this runs under sends one.
			log.Warn("mcp-relay: dropped a message that isn't one JSON object", "err", err)
			continue
		}
		replies, err := relayOne(ctx, client, u, line)
		if err == nil && len(replies) == 0 && len(probe.ID) > 0 && string(probe.ID) != "null" {
			err = errors.New("the broker sent no reply to a request")
		}
		if err != nil {
			log.Warn("mcp-relay: request failed", "server", path.Base(u.Path), "err", err)
			if len(probe.ID) > 0 && string(probe.ID) != "null" {
				replies = [][]byte{relayError(probe.ID, err)}
			}
		}
		for _, r := range replies {
			if _, err := w.Write(append(r, '\n')); err != nil {
				return err
			}
		}
		if err := w.Flush(); err != nil {
			return err
		}
	}
}

var errRelayLineTooLong = errors.New("message over the size limit")

// readRelayLine reads one newline-terminated message, discarding the rest of
// one over maxRelayMessageBytes and saying so. The last line needs no
// newline.
func readRelayLine(r *bufio.Reader) ([]byte, error) {
	var line []byte
	for {
		chunk, err := r.ReadSlice('\n')
		if len(line)+len(chunk) > maxRelayMessageBytes {
			for errors.Is(err, bufio.ErrBufferFull) {
				_, err = r.ReadSlice('\n')
			}
			if err != nil && !errors.Is(err, io.EOF) {
				return nil, err
			}
			return nil, errRelayLineTooLong
		}
		line = append(line, chunk...)
		switch {
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case errors.Is(err, io.EOF) && len(line) > 0:
			return line, nil
		default:
			return line, err
		}
	}
}

// newRelayClient is the bridge's HTTP client: no proxy from the environment
// (an in-cluster Service, and a proxy between the pod and the broker would see
// both credentials), and no redirect followed.
func newRelayClient() *http.Client {
	return &http.Client{
		Timeout:   mcpRelayTimeout,
		Transport: &http.Transport{Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errRelayRedirect
		},
	}
}

// relayOne POSTs one message with the pod's credentials and returns the
// broker's replies: none for an accepted notification, one or more from a
// JSON or event-stream body.
func relayOne(ctx context.Context, client *http.Client, u *url.URL, message []byte) ([][]byte, error) {
	headers, err := relayCredentials()
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), bytes.NewReader(message))
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("User-Agent", relayUserAgent())
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxRelayReplyBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxRelayReplyBytes {
		return nil, fmt.Errorf("the broker's reply passed %d bytes", maxRelayReplyBytes)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("the broker answered HTTP %d%s", resp.StatusCode, brokerErrorDetail(body))
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return nil, nil
	}
	mediaType, _, _ := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if mediaType == "text/event-stream" {
		return eventStreamMessages(body), nil
	}
	return jsonMessages(body)
}

// relayCredentials reads the pod's two credentials, on every request as the
// shims do: the kubelet rewrites the projected token in place, and a warm
// pod's next task brings its own task credential. A missing task file is no
// task; the broker then refuses what needs one.
func relayCredentials() (map[string]string, error) {
	headers := map[string]string{}
	tokenFile := strings.TrimSpace(os.Getenv(EnvCredentialProxyTokenFile))
	if tokenFile == "" {
		return nil, errors.New(EnvCredentialProxyTokenFile + " is not set, so the broker can't tell who is calling")
	}
	token, err := os.ReadFile(tokenFile) // #nosec G304 G703 -- the pod's own projected token mount, from CREDENTIAL_PROXY_TOKEN_FILE
	if err != nil {
		return nil, fmt.Errorf("reading the broker token: %w", err)
	}
	if t := strings.TrimSpace(string(token)); t != "" {
		headers["Authorization"] = "Bearer " + t
	} else {
		return nil, errors.New("the broker token file is empty")
	}
	taskFile := strings.TrimSpace(os.Getenv(EnvCredentialProxyTaskToken))
	if taskFile == "" {
		return headers, nil
	}
	task, err := os.ReadFile(taskFile) // #nosec G304 G703 -- the pod's own task credential mount, from CREDENTIAL_PROXY_TASK_TOKEN_FILE
	switch {
	case errors.Is(err, os.ErrNotExist):
		return headers, nil
	case err != nil:
		return nil, fmt.Errorf("reading the task credential: %w", err)
	}
	t := strings.TrimSpace(string(task))
	if t == "" {
		return headers, nil
	}
	if len(t) > maxTaskCredentialBytes || !taskCredentialShape.MatchString(t) {
		return nil, errors.New("the task credential file doesn't hold a task credential")
	}
	headers[taskCredentialHeader] = t
	return headers, nil
}

// jsonMessages is a JSON body's messages: one object, or each of a batch.
func jsonMessages(body []byte) ([][]byte, error) {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		var batch []json.RawMessage
		if err := json.Unmarshal(trimmed, &batch); err != nil {
			return nil, fmt.Errorf("the broker's reply isn't JSON: %w", err)
		}
		out := make([][]byte, 0, len(batch))
		for _, m := range batch {
			out = append(out, compactJSON(m))
		}
		return out, nil
	}
	if !json.Valid(trimmed) {
		return nil, errors.New("the broker's reply isn't JSON")
	}
	return [][]byte{compactJSON(trimmed)}, nil
}

// eventStreamMessages is each event's data in an event-stream body, joined
// across its data lines as the format says, keeping only valid JSON.
func eventStreamMessages(body []byte) [][]byte {
	var out [][]byte
	var data []string
	flush := func() {
		if len(data) > 0 {
			if m := []byte(strings.Join(data, "\n")); json.Valid(m) {
				out = append(out, compactJSON(m))
			}
			data = nil
		}
	}
	for _, line := range strings.Split(string(body), "\n") {
		line = strings.TrimSuffix(line, "\r")
		switch {
		case line == "":
			flush()
		case strings.HasPrefix(line, "data:"):
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	flush()
	return out
}

// brokerErrorDetail is the broker's own reason from an error body
// ({"error"|"message", "rule", "code"}), bounded, so the model sees which rule
// refused it rather than a bare status.
func brokerErrorDetail(body []byte) string {
	var e struct {
		Error   string `json:"error"`
		Message string `json:"message"`
		Rule    string `json:"rule"`
		Code    string `json:"code"`
	}
	if json.Unmarshal(body, &e) != nil {
		return ""
	}
	var parts []string
	for _, p := range []string{e.Rule, e.Code, e.Message, e.Error} {
		if p = strings.TrimSpace(p); p != "" {
			parts = append(parts, p)
		}
	}
	detail := strings.Join(parts, ": ")
	if len(detail) > 300 {
		detail = detail[:300]
	}
	if detail == "" {
		return ""
	}
	return " (" + detail + ")"
}

// relayUserAgent is the product, build and mode the platform agent's MCP
// clients send (deploy/shared/defaults/config.yaml), which the broker
// forwards and the API teams key their telemetry on: dev for a build that
// carries no version.
func relayUserAgent() string {
	v := strings.TrimSpace(os.Getenv("KUBE_AGENTS_VERSION"))
	if v == "" {
		v = "dev"
	}
	return "kube-agents/" + v + " (daemon)"
}

func compactJSON(m []byte) []byte {
	var b bytes.Buffer
	if err := json.Compact(&b, m); err != nil {
		return m
	}
	return b.Bytes()
}

// relayError is a JSON-RPC error reply to id, saying what the bridge saw.
func relayError(id json.RawMessage, err error) []byte {
	reply, _ := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"error":   map[string]any{"code": jsonRPCServerError, "message": "broker MCP relay: " + err.Error()},
	})
	return reply
}
