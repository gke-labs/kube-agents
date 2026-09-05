package gateway

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// PrincipalMap resolves backend-native user ids to principals in our trust
// domain. For Discord it is a mounted install-side mapping table (a
// ConfigMap; nothing is checked into the repo) and a test fixture by
// construction: a Discord identity never maps to a real cloud principal,
// full stop (spec-chatops-gateway.md). A sender with no entry cannot be
// verified and their message is dropped at ingress.
type PrincipalMap struct {
	mu sync.RWMutex
	m  map[string]string
}

// LoadPrincipalMap reads the map from a directory of files (the mounted
// principal-map ConfigMap: one file per backend user id, content is the
// principal) or from a single file of "id principal" lines. A missing path
// yields an empty map — the gateway runs, and every message drops at
// verification, which is the honest failure for an install without W0's
// ConfigMap.
func LoadPrincipalMap(path string) (*PrincipalMap, error) {
	pm := &PrincipalMap{m: map[string]string{}}
	info, err := os.Stat(path)
	if os.IsNotExist(err) {
		return pm, nil
	}
	if err != nil {
		return nil, fmt.Errorf("principal map %s: %w", path, err)
	}
	if !info.IsDir() {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("principal map %s: %w", path, err)
		}
		for _, line := range strings.Split(string(data), "\n") {
			fields := strings.Fields(line)
			if len(fields) == 2 && !strings.HasPrefix(fields[0], "#") {
				pm.m[fields[0]] = fields[1]
			}
		}
		return pm, nil
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, fmt.Errorf("principal map %s: %w", path, err)
	}
	for _, e := range entries {
		// Kubelet renders ConfigMap volumes with ..data/..timestamp symlink
		// machinery; only plain keys are entries.
		if strings.HasPrefix(e.Name(), ".") || e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(path, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("principal map entry %s: %w", e.Name(), err)
		}
		pm.m[e.Name()] = strings.TrimSpace(string(data))
	}
	return pm, nil
}

// Resolve returns the principal for a backend user id, or "" if the id is
// unmapped.
func (p *PrincipalMap) Resolve(userID string) string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.m[userID]
}

// Len reports how many identities are mapped.
func (p *PrincipalMap) Len() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.m)
}

// Pseudonymizer HMACs identifiers with the install's attribution salt before
// anything is written to the bus — the bus holds labelled content at rest for
// the whole retention window, so identifiers on it get the same treatment as
// the session KV. The plaintext join lives in the gateway's local ingress
// log.
type Pseudonymizer struct {
	salt []byte
}

// NewPseudonymizer builds one from the install's salt.
func NewPseudonymizer(salt []byte) *Pseudonymizer {
	return &Pseudonymizer{salt: salt}
}

// Hash returns the pseudonym for an identifier: "hmac:" plus the first 16
// hex bytes of HMAC-SHA256 under the install salt, matching the shipped
// attribution posture.
func (p *Pseudonymizer) Hash(id string) string {
	if id == "" {
		return ""
	}
	mac := hmac.New(sha256.New, p.salt)
	mac.Write([]byte(id))
	return "hmac:" + hex.EncodeToString(mac.Sum(nil))[:32]
}
