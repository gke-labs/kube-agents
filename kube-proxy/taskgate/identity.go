package taskgate

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// identityProfileKey and identityTierKey are the two keys an identity map entry
	// carries, and the only two.
	identityProfileKey = "profile"
	identityTierKey    = "tier"

	// identityKeyLogLimit bounds how much of a refused map key an error quotes, the
	// broker's bound.
	identityKeyLogLimit = 128
)

// serviceAccountUser matches the TokenReview username of a ServiceAccount, the identity
// map's key: task_credential.py's _SERVICE_ACCOUNT_USER.
var serviceAccountUser = regexp.MustCompile(`\Asystem:serviceaccount:[a-z0-9]([a-z0-9-]*[a-z0-9])?:[a-z0-9]([a-z0-9.-]*[a-z0-9])?\z`)

// Identity is a profile worker ServiceAccount's profile and tier, as the operator
// rendered it.
type Identity struct {
	Profile string
	Tier    string
}

// ParseIdentityMap reads the profile identity map: a JSON object keyed by ServiceAccount
// username, each value an object with exactly "profile" and "tier", both DNS-1123 labels.
// It is task_credential.py's parse_identity_map, as strict: a key twice at either level,
// a key outside the two, a value that is not a string, a key that is not a ServiceAccount
// username and anything after the object are errors. Blank is an empty map.
func ParseIdentityMap(raw string) (map[string]Identity, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	if !utf8.ValidString(raw) {
		return nil, errors.New("identity map is not UTF-8")
	}
	dec := json.NewDecoder(strings.NewReader(raw))
	if err := expectObject(dec); err != nil {
		return nil, fmt.Errorf("identity map is not a JSON object: %w", err)
	}
	out := map[string]Identity{}
	for dec.More() {
		user, err := objectKey(dec)
		if err != nil {
			return nil, fmt.Errorf("identity map is not JSON: %w", err)
		}
		if _, seen := out[user]; seen {
			return nil, fmt.Errorf("identity map key %q appears twice", clip(user))
		}
		if !serviceAccountUser.MatchString(user) {
			return nil, fmt.Errorf("identity map key %q is not a ServiceAccount user", clip(user))
		}
		identity, err := identityEntry(dec, user)
		if err != nil {
			return nil, err
		}
		out[user] = identity
	}
	if err := endOfDocument(dec); err != nil {
		return nil, fmt.Errorf("identity map is not JSON: %w", err)
	}
	return out, nil
}

// identityEntry reads one map value: an object with exactly the profile and tier keys.
func identityEntry(dec *json.Decoder, user string) (Identity, error) {
	if err := expectObject(dec); err != nil {
		return Identity{}, fmt.Errorf("identity map entry for %q is not an object: %w", user, err)
	}
	fields := map[string]string{}
	for dec.More() {
		key, err := objectKey(dec)
		if err != nil {
			return Identity{}, fmt.Errorf("identity map entry for %q is not JSON: %w", user, err)
		}
		if _, seen := fields[key]; seen {
			return Identity{}, fmt.Errorf("identity map entry %q: key %q appears twice", user, clip(key))
		}
		if key != identityProfileKey && key != identityTierKey {
			return Identity{}, fmt.Errorf("identity map entry %q carries an unknown key %q", user, clip(key))
		}
		tok, err := dec.Token()
		if err != nil {
			return Identity{}, fmt.Errorf("identity map entry for %q is not JSON: %w", user, err)
		}
		value, ok := tok.(string)
		if !ok {
			return Identity{}, fmt.Errorf("identity map entry %q: %s is not a string", user, key)
		}
		if !lib.ValidSubjectToken(value) {
			return Identity{}, fmt.Errorf("identity map entry %q: %s %q is not a dot-free DNS-1123 label", user, key, clip(value))
		}
		fields[key] = value
	}
	// The entry's closing brace.
	if _, err := dec.Token(); err != nil {
		return Identity{}, fmt.Errorf("identity map entry for %q is not JSON: %w", user, err)
	}
	for _, key := range []string{identityProfileKey, identityTierKey} {
		if _, ok := fields[key]; !ok {
			return Identity{}, fmt.Errorf("identity map entry %q has no %q", user, key)
		}
	}
	return Identity{Profile: fields[identityProfileKey], Tier: fields[identityTierKey]}, nil
}

// expectObject consumes the opening brace of an object, or says what was there instead.
func expectObject(dec *json.Decoder) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	if tok != json.Delim('{') {
		return fmt.Errorf("found %T", tok)
	}
	return nil
}

// objectKey reads the next key of the object the decoder is in.
func objectKey(dec *json.Decoder) (string, error) {
	tok, err := dec.Token()
	if err != nil {
		return "", err
	}
	key, ok := tok.(string)
	if !ok {
		return "", fmt.Errorf("found %T where a key belongs", tok)
	}
	return key, nil
}

// endOfDocument consumes the top-level closing brace and refuses anything after it but
// whitespace.
func endOfDocument(dec *json.Decoder) error {
	if _, err := dec.Token(); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		return errors.New("trailing data after the object")
	}
	return nil
}

// clip bounds a value an error quotes.
func clip(s string) string {
	if len(s) <= identityKeyLogLimit {
		return s
	}
	return strings.ToValidUTF8(s[:identityKeyLogLimit], "")
}
