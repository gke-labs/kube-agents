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
	"context"
	"fmt"

	"github.com/nats-io/nkeys"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The auth callout's two keypairs.
//
// **The issuer seed is the most powerful secret in this deployment, and it is
// worth being blunt about why.** It signs the user JWTs that carry the publish
// and subscribe permissions the server then enforces, so it does not merely
// authenticate connections — it decides what every connection may do. Whoever
// holds it can issue themselves a user with any grants at all — publish on
// $KV.cap.root.> and read on $KV.cap.>, which mints any capability and reads
// every capability in flight. That path has no crypto behind it: subject
// permissions ARE the integrity control for the capability envelope, and this
// key is what signs them. It wants gateway-grade custody, a rotation story and
// a compromise runbook, not the handling a thing described as "authenticating
// connections" would get. The capability envelope design (architecture 09, "one
// cryptographic key does exist") owns that analysis; this is the object it is
// talking about.
//
// So: the seeds live in their own Secret, read by the callout Deployment and by
// nothing else. They are deliberately NOT in the per-user creds Secret, which
// the gateway, the agent pod and the session spawner all read.
//
// The server holds only the public halves, in nats.conf. It never sees a seed.
//
// What custody this actually has, measured against a live install rather than
// asserted (2026-09-09): one consumer, and no ServiceAccount in the namespace
// except the operator's own can read a Secret there — not the gateway, not the
// session pods, not the shell. Three gaps stand open and are deliberately not
// closed here: the seeds reach the callout as environment variables rather than
// a projected file (see the Deployment); the cluster has no application-layer
// secrets encryption, so this sits in etcd under disk encryption alone; and
// there is no rotation runbook, because CreateOnce below is structural —
// rotating means restarting NATS, since the server refuses a config reload that
// touches auth_callout. Nothing revokes an already-issued user JWT either.

const (
	// a2aCalloutIssuerSeedKey is the account seed (SA...) the callout signs
	// user JWTs with.
	a2aCalloutIssuerSeedKey = "issuer-seed"

	// a2aCalloutIssuerPubKey is the account public key (A...) that goes into
	// nats.conf as auth_callout.issuer.
	a2aCalloutIssuerPubKey = "issuer-public"

	// a2aCalloutXKeySeedKey is the curve seed (SX...) the callout decrypts
	// authorization requests with.
	a2aCalloutXKeySeedKey = "xkey-seed"

	// a2aCalloutXKeyPubKey is the curve public key (X...) that goes into
	// nats.conf as auth_callout.xkey.
	a2aCalloutXKeyPubKey = "xkey-public"
)

// a2aCalloutKeysName is the Secret holding the callout's keypairs.
func a2aCalloutKeysName(agent *agentv1alpha1.PlatformAgent) string {
	return agentv1alpha1.A2ACalloutKeysSecretName(agent.Name)
}

// a2aCalloutKeys is the rendered view: seeds for the callout, public halves for
// the server config.
type a2aCalloutKeys struct {
	IssuerSeed   string
	IssuerPublic string
	XKeySeed     string
	XKeyPublic   string
}

// ensureA2ACalloutKeysSecret creates the keypairs once and then leaves them
// alone.
//
// Create-once matters more here than for the passwords. Rotating the issuer is
// not a credential refresh: the public half is in nats.conf, so a new key is a
// new server config, and the server refuses a config reload that touches the
// auth_callout block at all — the reload fails outright and takes every other
// pending change in the file with it. So a rotation is a NATS restart, and a
// restart while the callout is serving a key the server no longer trusts
// refuses every new connection in between. Regenerating these on reconcile
// would do that every few seconds.
//
// A partially-populated Secret is repaired as a set rather than per key: the
// four values are two keypairs, and filling in a seed without its public half
// would leave the server trusting a key nothing holds.
//
// An existing Secret at the name that this PlatformAgent doesn't control is
// refused rather than adopted or repaired, as ensureA2ADoorTokenSecret does.
func (r *PlatformAgentReconciler) ensureA2ACalloutKeysSecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*a2aCalloutKeys, error) {
	name := types.NamespacedName{Name: a2aCalloutKeysName(agent), Namespace: agent.Namespace}

	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err == nil {
		// Only a Secret this PlatformAgent controls is read or repaired, as
		// for the door's token: one created by anything else is neither
		// trusted for its keys nor overwritten.
		if !metav1.IsControlledBy(existing, agent) {
			return nil, fmt.Errorf("refusing to adopt unowned Secret %s/%s as the A2A callout keys; delete it and the operator creates a fresh one (a controller reference is not a remedy: it would adopt keys of unknown origin as they are)", name.Namespace, name.Name)
		}
		keys, ok := readA2ACalloutKeys(existing)
		if ok {
			return keys, nil
		}
		// Incomplete or corrupt. Regenerate the whole set — see above on
		// why this is not a thing to do by halves.
		generated, data, gerr := generateA2ACalloutKeys()
		if gerr != nil {
			return nil, gerr
		}
		existing.Data = data
		if err := r.Update(ctx, existing); err != nil {
			return nil, err
		}
		return generated, nil
	}
	if !errors.IsNotFound(err) {
		return nil, err
	}

	generated, data, err := generateA2ACalloutKeys()
	if err != nil {
		return nil, err
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, "callout-keys")},
		Data:       data,
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, secret); err != nil {
		return nil, err
	}
	return generated, nil
}

// readA2ACalloutKeys loads the seeds and DERIVES the public halves from them.
//
// The stored public keys are never read back for rendering, and that is a
// security property rather than tidiness. Both public halves are interpolated
// unquoted into nats.conf, so a value carrying a quote and a newline is a config
// injection — a whole replacement `accounts { }` block, with attacker-chosen
// users, passwords and grants, which the operator would then faithfully
// re-render on every reconcile. That converts one Secret write into durable
// authority over the entire bus. The per-user passwords on this same render path
// have carried a shape guard against exactly this since before the callout
// existed; these two values arrived without one.
//
// Deriving closes it completely rather than filtering for it: a public key
// computed from a validated seed cannot contain anything but a public key. It
// also closes a second failure the stored copy allowed — an issuer public that
// is not the public half of the issuer seed, which the server accepts happily
// and which then refuses every callout-authenticated connection with a bare
// Authorization Violation naming nothing.
//
// The Secret keeps its public entries. They are for a human reading the object,
// and nothing renders from them.
func readA2ACalloutKeys(secret *corev1.Secret) (*a2aCalloutKeys, bool) {
	issuerSeed := string(secret.Data[a2aCalloutIssuerSeedKey])
	xkeySeed := string(secret.Data[a2aCalloutXKeySeedKey])
	if issuerSeed == "" || xkeySeed == "" {
		return nil, false
	}

	// The key TYPE is checked, not just decodability. The server validates
	// auth_callout.issuer as a public ACCOUNT nkey and auth_callout.xkey as a
	// public curve key, and refuses to start on either being wrong — so a
	// Secret hand-edited with a user key would take the bus down at the next
	// config change rather than at the edit.
	if prefix, _, err := nkeys.DecodeSeed([]byte(issuerSeed)); err != nil || prefix != nkeys.PrefixByteAccount {
		return nil, false
	}
	if prefix, _, err := nkeys.DecodeSeed([]byte(xkeySeed)); err != nil || prefix != nkeys.PrefixByteCurve {
		return nil, false
	}

	issuerKP, err := nkeys.FromSeed([]byte(issuerSeed))
	if err != nil {
		return nil, false
	}
	issuerPublic, err := issuerKP.PublicKey()
	if err != nil {
		return nil, false
	}
	xkeyKP, err := nkeys.FromSeed([]byte(xkeySeed))
	if err != nil {
		return nil, false
	}
	xkeyPublic, err := xkeyKP.PublicKey()
	if err != nil {
		return nil, false
	}

	return &a2aCalloutKeys{
		IssuerSeed:   issuerSeed,
		IssuerPublic: issuerPublic,
		XKeySeed:     xkeySeed,
		XKeyPublic:   xkeyPublic,
	}, true
}

func generateA2ACalloutKeys() (*a2aCalloutKeys, map[string][]byte, error) {
	issuer, err := nkeys.CreateAccount()
	if err != nil {
		return nil, nil, fmt.Errorf("generating the callout issuer key: %w", err)
	}
	issuerSeed, err := issuer.Seed()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the callout issuer seed: %w", err)
	}
	issuerPub, err := issuer.PublicKey()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the callout issuer public key: %w", err)
	}

	// The curve key encrypts the authorization request, which carries the
	// client's raw ServiceAccount token. Without it that token crosses the
	// bus in cleartext on every single connection attempt.
	xkey, err := nkeys.CreateCurveKeys()
	if err != nil {
		return nil, nil, fmt.Errorf("generating the callout xkey: %w", err)
	}
	xkeySeed, err := xkey.Seed()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the callout xkey seed: %w", err)
	}
	xkeyPub, err := xkey.PublicKey()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the callout xkey public key: %w", err)
	}

	keys := &a2aCalloutKeys{
		IssuerSeed:   string(issuerSeed),
		IssuerPublic: issuerPub,
		XKeySeed:     string(xkeySeed),
		XKeyPublic:   xkeyPub,
	}
	return keys, map[string][]byte{
		a2aCalloutIssuerSeedKey: issuerSeed,
		a2aCalloutIssuerPubKey:  []byte(issuerPub),
		a2aCalloutXKeySeedKey:   xkeySeed,
		a2aCalloutXKeyPubKey:    []byte(xkeyPub),
	}, nil
}

// The task-signing key (#3889).
//
// The dispatcher signs a per-task credential with it, and the callout verifies
// that credential on a profile worker's task connection and grants that one
// task's subjects (a2a/taskcred, a2a/authcallout). It is generated here, beside
// the callout's keys and on the same create-once terms, but it is not the
// callout's: the seed goes in a Secret of its own that only the dispatcher
// Deployment will mount, and the callout gets the public half as a plain
// environment value (buildA2ACalloutDeployment), never a reference to that
// Secret. No dispatcher Deployment is rendered yet; until one is, nothing
// mounts the seed at all.
//
// Much less than the issuer, but not nothing. The callout honours a credential
// only for the pod the cluster attests on the same connection and only within
// that pod's own profile and tier, so the seed alone authenticates nothing.
// With a worker pod's ServiceAccount token as well, it scopes that pod to any
// task of its own profile, for as long as the attacker holds that token: each
// credential lasts up to the callout's maximum task lifetime
// (A2A_TASK_CREDENTIAL_MAX_LIFETIME) and each connection up to GrantTTL, and a
// seed holder can mint another. That is the per-profile reach #3889 removes,
// and no more. Hence a Secret of its own rather than a
// key in the callout's, which would put it in front of the callout's reader,
// and a place in A2ACredentialSecretNames, so no user volume can mount it.
//
// Rotation is as for the callout keys: none yet. The callout accepts two
// public keys at once (taskcred.MaxTrustedKeys) so a rotation can overlap the
// old key for one task deadline; the operator renders one. A repaired Secret
// is a new key, which re-renders the callout's environment and so rolls it.

const (
	// a2aTaskSigningSeedKey is the task-signing seed (SC...) the dispatcher
	// signs task credentials with.
	a2aTaskSigningSeedKey = "task-signing-seed"

	// a2aTaskSigningPubKey is its public half (C...), kept for a human
	// reading the Secret. Nothing renders from it; see readA2ATaskSigningKey.
	a2aTaskSigningPubKey = "task-signing-public"

	// a2aTaskSigningKeyPrefix is the nkey type of a task-signing key: the
	// cluster prefix, which nothing in NATS trusts for anything, so the key
	// can't be mistaken for, or configured as, a key with more reach.
	// a2a/taskcred makes the choice and argues it; the operator cannot
	// import that module, so the prefix is repeated here and the callout's
	// startup check (authcallout.ParseTaskSigningKeys) is what refuses a
	// mismatch.
	a2aTaskSigningKeyPrefix = nkeys.PrefixByteCluster
)

// a2aTaskSigningKeyName is the Secret holding the task-signing key.
func a2aTaskSigningKeyName(agent *agentv1alpha1.PlatformAgent) string {
	return agentv1alpha1.A2ATaskSigningKeySecretName(agent.Name)
}

// a2aTaskSigningKey is the rendered view: the seed, which only the dispatcher
// will read, and the public half for the callout.
type a2aTaskSigningKey struct {
	Seed   string
	Public string
}

// ensureA2ATaskSigningKeySecret creates the task-signing key once and then
// leaves it alone, repairing an incomplete or corrupt Secret as a whole, the
// way ensureA2ACalloutKeysSecret does and for the same reasons: a key that
// changed on every reconcile would roll the callout every few seconds and
// refuse every credential the dispatcher had already handed out.
func (r *PlatformAgentReconciler) ensureA2ATaskSigningKeySecret(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (*a2aTaskSigningKey, error) {
	name := types.NamespacedName{Name: a2aTaskSigningKeyName(agent), Namespace: agent.Namespace}

	existing := &corev1.Secret{}
	err := r.a2aReader().Get(ctx, name, existing)
	if err == nil {
		// Not ours: refused before it is read, as ensureA2ADoorTokenSecret
		// does. Adopting it would let whoever created a Secret at this name
		// choose the task-signing seed (and so mint task credentials the
		// callout honours), and repairing it would overwrite somebody
		// else's object. Only a Secret this PlatformAgent controls is read
		// or repaired.
		if !metav1.IsControlledBy(existing, agent) {
			return nil, fmt.Errorf("refusing to adopt unowned Secret %s/%s as the A2A task-signing key; delete it, or give it a controller reference to this PlatformAgent", name.Namespace, name.Name)
		}
		if key, ok := readA2ATaskSigningKey(existing); ok {
			return key, nil
		}
		generated, data, gerr := generateA2ATaskSigningKey()
		if gerr != nil {
			return nil, gerr
		}
		existing.Data = data
		if err := r.Update(ctx, existing); err != nil {
			return nil, err
		}
		return generated, nil
	}
	if !errors.IsNotFound(err) {
		return nil, err
	}

	generated, data, err := generateA2ATaskSigningKey()
	if err != nil {
		return nil, err
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: name.Name, Namespace: name.Namespace, Labels: a2aLabels(agent, "task-signing-key")},
		Data:       data,
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return nil, err
	}
	if err := r.Create(ctx, secret); err != nil {
		return nil, err
	}
	return generated, nil
}

// readA2ATaskSigningKey loads the seed and derives the public half from it,
// for readA2ACalloutKeys' reason: the public half is rendered into the
// callout's environment, and one derived from a validated seed can't be
// anything but a public key, nor the public half of some other seed.
func readA2ATaskSigningKey(secret *corev1.Secret) (*a2aTaskSigningKey, bool) {
	seed := string(secret.Data[a2aTaskSigningSeedKey])
	if seed == "" {
		return nil, false
	}
	if prefix, _, err := nkeys.DecodeSeed([]byte(seed)); err != nil || prefix != a2aTaskSigningKeyPrefix {
		return nil, false
	}
	kp, err := nkeys.FromSeed([]byte(seed))
	if err != nil {
		return nil, false
	}
	public, err := kp.PublicKey()
	if err != nil {
		return nil, false
	}
	return &a2aTaskSigningKey{Seed: seed, Public: public}, true
}

func generateA2ATaskSigningKey() (*a2aTaskSigningKey, map[string][]byte, error) {
	kp, err := nkeys.CreateCluster()
	if err != nil {
		return nil, nil, fmt.Errorf("generating the task-signing key: %w", err)
	}
	seed, err := kp.Seed()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the task-signing seed: %w", err)
	}
	public, err := kp.PublicKey()
	if err != nil {
		return nil, nil, fmt.Errorf("reading the task-signing public key: %w", err)
	}
	return &a2aTaskSigningKey{Seed: string(seed), Public: public}, map[string][]byte{
		a2aTaskSigningSeedKey: seed,
		a2aTaskSigningPubKey:  []byte(public),
	}, nil
}
