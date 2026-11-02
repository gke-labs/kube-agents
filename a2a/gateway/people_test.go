package gateway

import (
	"strings"
	"testing"
)

func TestPeopleKeyIsDerivedForTheBucketAlone(t *testing.T) {
	salt := []byte("a fixed salt for the test")
	k, err := newPeopleKeyer(salt)
	if err != nil {
		t.Fatal(err)
	}
	key := k.Key("user@example.com")
	if !strings.HasPrefix(key, peopleKeyPrefix) || len(key) != len(peopleKeyPrefix)+peopleKeyHexLen {
		t.Fatalf("key %q is not people.<32 hex>", key)
	}
	if key != k.Key("user@example.com") {
		t.Fatal("the key is not stable")
	}
	if strings.TrimPrefix(key, peopleKeyPrefix) == strings.TrimPrefix(NewPseudonymizer(salt).Hash("user@example.com"), "hmac:") {
		t.Fatal("the people key equals the authority block's pseudonym; it must join nothing")
	}
	if k.Key("other@example.com") == key {
		t.Fatal("two principals share a key")
	}
	if k.Key("") != "" {
		t.Fatal("an empty principal must yield no key")
	}
}
