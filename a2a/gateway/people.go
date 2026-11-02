package gateway

import (
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
)

// peopleKeyPrefix starts every key in the people bucket (lib.PeopleBucket).
const peopleKeyPrefix = "people."

// peopleKeyInfo is the HKDF info string that binds the derived HMAC key to
// the people bucket, so a people key joins no other pseudonym the gateway
// derives from the same salt.
const peopleKeyInfo = "people"

// peopleKeyLen is how many bytes the derived HMAC key gets: one SHA-256
// block of output.
const peopleKeyLen = 32

// peopleKeyHexLen is how many hex characters of the HMAC a people key keeps
// after its prefix.
const peopleKeyHexLen = 32

// peopleKeyer derives the people bucket's key for a principal: an HMAC-SHA-256
// under a key that HKDF derives from the salt for this bucket alone.
type peopleKeyer struct {
	key []byte
}

// newPeopleKeyer derives the people bucket's HMAC key from salt.
func newPeopleKeyer(salt []byte) (*peopleKeyer, error) {
	key, err := hkdf.Key(sha256.New, salt, nil, peopleKeyInfo, peopleKeyLen)
	if err != nil {
		return nil, err
	}
	return &peopleKeyer{key: key}, nil
}

// Key returns the people bucket's key for principal, "people." plus
// peopleKeyHexLen hex characters, or "" for an empty principal.
func (k *peopleKeyer) Key(principal string) string {
	if principal == "" {
		return ""
	}
	mac := hmac.New(sha256.New, k.key)
	mac.Write([]byte(principal))
	return peopleKeyPrefix + hex.EncodeToString(mac.Sum(nil))[:peopleKeyHexLen]
}
