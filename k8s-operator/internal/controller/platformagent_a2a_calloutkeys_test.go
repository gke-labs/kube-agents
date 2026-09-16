/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

	http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"strings"
	"testing"

	"github.com/nats-io/nkeys"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func calloutKeysSecret(data map[string][]byte) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "agent-a2a-callout-keys", Namespace: "kubeagents-system"},
		Data:       data,
	}
}

// The injection this render path invites, closed.
//
// Both public halves land unquoted in nats.conf, so a stored value carrying a
// quote and a newline is a config injection: a whole replacement `accounts { }`
// block with attacker-chosen users, passwords and grants, which the operator
// then re-renders faithfully on every reconcile. One Secret write becomes
// durable authority over the entire bus.
//
// The close is that nothing renders from the stored public keys at all — they
// are derived from the seeds, and a key computed from a validated seed cannot
// carry a payload. This asserts the property at the point it matters: what
// reaches the config.
func TestARiggedPublicKeyInTheSecretCannotReachTheConfig(t *testing.T) {
	real, data, err := generateA2ACalloutKeys()
	if err != nil {
		t.Fatalf("generateA2ACalloutKeys: %v", err)
	}

	injected := real.IssuerPublic + "\"\n}\naccounts {\n  APP {\n    users [ { user: pwned, password: \"pwned\" } ]\n  }\n}\nx: \""
	data[a2aCalloutIssuerPubKey] = []byte(injected)
	data[a2aCalloutXKeyPubKey] = []byte(injected)

	keys, ok := readA2ACalloutKeys(calloutKeysSecret(data))
	if !ok {
		t.Fatal("a Secret with valid seeds was rejected; the seeds are what the render must trust")
	}
	if keys.IssuerPublic != real.IssuerPublic || keys.XKeyPublic != real.XKeyPublic {
		t.Fatal("the stored public keys were used instead of being derived from the seeds")
	}

	agent := identityTestAgent()
	conf := string(buildA2ANATSConfigSecret(agent, a2aTestCreds(), keys).Data["nats.conf"])
	if strings.Contains(conf, "pwned") {
		t.Errorf("the injected payload reached nats.conf:\n%s", conf)
	}
	if strings.Count(conf, "accounts {") != 1 {
		t.Errorf("nats.conf carries %d accounts blocks, want 1", strings.Count(conf, "accounts {"))
	}
}

// A public key that is not the public half of its seed is not a hypothetical
// typo: the server accepts it, trusts the wrong key, and then refuses every
// callout-authenticated connection with a bare Authorization Violation — the
// same thing a bad token produces, naming nothing.
func TestAMismatchedPublicKeyIsIgnoredInFavourOfTheSeed(t *testing.T) {
	real, data, err := generateA2ACalloutKeys()
	if err != nil {
		t.Fatalf("generateA2ACalloutKeys: %v", err)
	}
	other, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	otherPub, _ := other.PublicKey()
	data[a2aCalloutIssuerPubKey] = []byte(otherPub)

	keys, ok := readA2ACalloutKeys(calloutKeysSecret(data))
	if !ok {
		t.Fatal("valid seeds were rejected")
	}
	if keys.IssuerPublic != real.IssuerPublic {
		t.Errorf("IssuerPublic = %q, want the public half of the stored seed (%q)", keys.IssuerPublic, real.IssuerPublic)
	}
}

// Wrong key TYPES are refused rather than derived from, because the server
// refuses to start on either being wrong and would take the bus down at the
// next config change rather than at the edit.
func TestKeysOfTheWrongTypeAreRefused(t *testing.T) {
	good, base, err := generateA2ACalloutKeys()
	if err != nil {
		t.Fatalf("generateA2ACalloutKeys: %v", err)
	}
	_ = good

	userKP, _ := nkeys.CreateUser()
	userSeed, _ := userKP.Seed()

	cases := []struct {
		name string
		key  string
		val  []byte
	}{
		{"a user seed as the issuer", a2aCalloutIssuerSeedKey, userSeed},
		{"a user seed as the xkey", a2aCalloutXKeySeedKey, userSeed},
		{"garbage as the issuer seed", a2aCalloutIssuerSeedKey, []byte("not-a-seed")},
		{"an empty issuer seed", a2aCalloutIssuerSeedKey, []byte("")},
		{"an empty xkey seed", a2aCalloutXKeySeedKey, []byte("")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			data := map[string][]byte{}
			for k, v := range base {
				data[k] = v
			}
			data[tc.key] = tc.val
			if _, ok := readA2ACalloutKeys(calloutKeysSecret(data)); ok {
				t.Error("accepted; the Secret is regenerated as a set rather than served like this")
			}
		})
	}
}
