package upstream

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/net/http/httpguts"
)

const (
	// bearerTokenFileInterval is the least time between two reads of a bearer token file,
	// so a rotated token is sent within a second and a request costs no read of its own.
	bearerTokenFileInterval = time.Second
	// maxBearerTokenFileBytes caps what is read from a bearer token file; a Kubernetes
	// ServiceAccount token or an OIDC token is a few kilobytes.
	maxBearerTokenFileBytes = 64 << 10
	bearerPrefix            = "Bearer "
	// bearerTokenFileOpenFlags open the file without waiting for a writer when the path
	// names a FIFO, so the regular-file check runs instead of the open blocking.
	bearerTokenFileOpenFlags = os.O_RDONLY | syscall.O_NONBLOCK
)

// bearerTokenFile is a bearer token read from a file that is rotated on disk, such as a
// projected ServiceAccount token. The file is read again at most once per
// bearerTokenFileInterval, when a request asks for the token. Unlike a certificate, a
// token that can no longer be read is not replaced by the last one that could: the request
// fails, since the file's owner may have withdrawn the credential.
type bearerTokenFile struct {
	path string
	now  func() time.Time

	mu     sync.Mutex
	token  string
	err    error
	readAt time.Time
}

// newBearerTokenFile reads path once and fails when it holds no usable token.
func newBearerTokenFile(path string, now func() time.Time) (*bearerTokenFile, error) {
	f := &bearerTokenFile{path: path, now: now}
	f.token, f.err = f.read()
	f.readAt = now()
	if f.err != nil {
		return nil, f.err
	}
	return f, nil
}

// headerValue returns the Authorization header value for the current token, or why the
// file holds none. The error names the file and never the token.
func (f *bearerTokenFile) headerValue() (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if now := f.now(); now.Sub(f.readAt) >= bearerTokenFileInterval {
		f.token, f.err = f.read()
		f.readAt = now
	}
	if f.err != nil {
		return "", f.err
	}
	return bearerPrefix + f.token, nil
}

func (f *bearerTokenFile) read() (string, error) {
	file, err := os.OpenFile(f.path, bearerTokenFileOpenFlags, 0) // #nosec G304 -- a path the operator configured, not request data
	if err != nil {
		return "", fmt.Errorf("bearer token file: %w", err)
	}
	defer func() { _ = file.Close() }()
	// Checked on the open descriptor, so what is read is what was checked: a FIFO or a
	// device would block the read or never end it, and is no rotated file.
	info, err := file.Stat()
	if err != nil {
		return "", fmt.Errorf("bearer token file: %w", err)
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("bearer token file %s is not a regular file", f.path)
	}
	data, err := io.ReadAll(io.LimitReader(file, maxBearerTokenFileBytes+1))
	if err != nil {
		return "", fmt.Errorf("bearer token file %s: %w", f.path, err)
	}
	if len(data) > maxBearerTokenFileBytes {
		return "", fmt.Errorf("bearer token file %s is larger than %d bytes", f.path, maxBearerTokenFileBytes)
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return "", fmt.Errorf("bearer token file %s is empty", f.path)
	}
	// A space or a control character would split or forge a header line.
	if strings.ContainsAny(token, " \t") || !httpguts.ValidHeaderFieldValue(token) {
		return "", errors.New("bearer token file " + f.path + " holds characters a bearer token cannot carry")
	}
	return token, nil
}
