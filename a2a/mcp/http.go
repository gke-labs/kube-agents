package mcp

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"slices"
	"strings"
	"sync"
)

const (
	// Path is where the JSON-RPC endpoint answers.
	Path = "/mcp"
	// HealthPath is the kubelet's probe. It is the one unauthenticated path.
	HealthPath = "/healthz"

	headerAuthorization = "Authorization"
	headerContentType   = "Content-Type"
	headerOrigin        = "Origin"
	bearerPrefix        = "Bearer "
	contentTypeJSON     = "application/json"

	// maxBodyBytes bounds one JSON-RPC frame.
	maxBodyBytes = 4 << 20

	msgUnauthorized = "unauthorized"
	msgNoBrowsers   = "browser requests are refused; MCP clients send no Origin"
	msgBodyTooLarge = "request body too large or unreadable"
	msgNoToken      = "the token file %s is empty"
	// headerProtocolVersion is the MCP revision a client names on each
	// request after initialize (the 2025-06-18 transport).
	headerProtocolVersion = "MCP-Protocol-Version"
	msgUnsupportedVersion = "unsupported MCP protocol version %q"
)

// requestBudget bounds one request, every member of a batch together, inside
// the server's write timeout (MaxTurnWait plus a margin, cmd/mcp). A member
// that starts with little of it left waits that much: a turn returns its task
// still running, and one with none left is refused with the context error.
var requestBudget = MaxTurnWait

// errEmptyToken is a token file that holds nothing, which authenticates no one.
var errEmptyToken = errors.New("empty token")

// ReadToken reads the bearer token callers must present. A file that is
// missing or empty is an error: a server with no token authenticates no one,
// and must say so rather than fall back to anything.
func ReadToken(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return "", fmt.Errorf(msgNoToken+": %w", path, errEmptyToken)
	}
	return token, nil
}

// Handler serves the MCP endpoint and the health probe. The token is read
// from tokenFile on every request, so a rotated Secret takes effect without a
// restart, and a file that becomes unreadable or empty refuses every request.
func Handler(s *Server, tokenFile string, log *slog.Logger) http.Handler {
	tl := &tokenLog{log: log}
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+HealthPath, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(headerContentType, contentTypeJSON)
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "name": ServerName, "version": ServerVersion})
	})
	mux.HandleFunc("POST "+Path, func(w http.ResponseWriter, r *http.Request) {
		// MCP clients are programs, not pages, and a browser sends Origin on
		// every POST ("null" included). The bearer token is what keeps a
		// DNS-rebinding page out, since a browser never attaches it on its
		// own; this refuses such a page before it reaches the token check.
		if r.Header.Get(headerOrigin) != "" {
			http.Error(w, msgNoBrowsers, http.StatusForbidden)
			return
		}
		if !authorized(r, tokenFile, tl) {
			http.Error(w, msgUnauthorized, http.StatusUnauthorized)
			return
		}
		// The transport has clients name their revision on every request
		// after initialize; an absent header means an older client, and an
		// unknown revision is refused rather than served in another dialect.
		if v := r.Header.Get(headerProtocolVersion); v != "" && !slices.Contains(supportedProtocolVersions, v) {
			http.Error(w, fmt.Sprintf(msgUnsupportedVersion, v), http.StatusBadRequest)
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
		if err != nil {
			http.Error(w, msgBodyTooLarge, http.StatusRequestEntityTooLarge)
			return
		}
		// One budget for the whole request: a batch's members share it, so
		// the response always lands inside the server's write timeout.
		ctx, cancel := context.WithTimeout(r.Context(), requestBudget)
		defer cancel()
		resp := s.HandleJSONRPC(ctx, body)
		if len(resp) == 0 {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set(headerContentType, contentTypeJSON)
		_, _ = w.Write(resp)
	})
	return mux
}

// authorized reports whether r carries the server's token. The scheme is
// matched without regard to case, as RFC 7235 has it.
func authorized(r *http.Request, tokenFile string, log *tokenLog) bool {
	want, err := ReadToken(tokenFile)
	log.note(tokenFile, err)
	if err != nil {
		return false
	}
	h := r.Header.Get(headerAuthorization)
	if len(h) < len(bearerPrefix) || !strings.EqualFold(h[:len(bearerPrefix)], bearerPrefix) {
		return false
	}
	got := strings.TrimSpace(h[len(bearerPrefix):])
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}

// tokenLog reports the token file going bad, and recovering, once each
// rather than on every request, so unauthenticated traffic cannot flood the
// log.
type tokenLog struct {
	log  *slog.Logger
	mu   sync.Mutex
	last string
}

func (l *tokenLog) note(file string, err error) {
	state := ""
	if err != nil {
		state = err.Error()
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if state == l.last {
		return
	}
	l.last = state
	if err != nil {
		l.log.Error("mcp token unreadable; refusing every request", "file", file, "err", err)
	} else {
		l.log.Info("mcp token readable again", "file", file)
	}
}
