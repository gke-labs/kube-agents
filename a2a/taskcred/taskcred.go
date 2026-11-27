// Package taskcred is the task credential: a short-lived token the dispatcher
// signs for one task, bound to the one pod it handed that task to (#3889).
//
// The auth callout mints a profile worker's grants from what the cluster
// attests about the connecting pod, and the task is nowhere in that. A
// credential from the dispatcher, the one component that checks the grant and
// picks the pod, is how the task gets into the decision. Verifiers are told
// apart by audience. The callout reads AudienceNATS and narrows a worker's
// second bus connection to that task's subjects. AudienceCredentialProxy is for
// the credential proxy, which confines a task's kubectl and gcloud to the
// cluster and project its target names, and admits a task's version-control
// writes only under the permit the credential carries; that verifier is Python
// (agents/platform/scripts/task_credential.py), held to this one by the
// fixtures in proxy_fixtures_test.go. A token minted for one audience is
// refused by the other.
//
// The credential authenticates nothing by itself. Its pod and pod UID must
// match a pod the cluster attests on the same connection, and its profile and
// tier must match that pod's identity-map entry, so a copied credential is
// useless without the pod's own ServiceAccount token. What a stolen signing key
// adds is bounded by those checks, which live in the verifiers, not here; see
// the plan's addendum 1 on #3889.
//
// This package holds the format, Mint and Verify, and nothing about any
// verifier, so the dispatcher can import the minter without the callout.
package taskcred

import (
	"bytes"
	"crypto/rand"
	"encoding/base32"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Audience is which verifier a credential is for.
type Audience string

const (
	// AudienceNATS is the callout's: a worker's task connection.
	AudienceNATS Audience = "nats"

	// AudienceCredentialProxy is the credential proxy's: what a task's tool
	// calls may reach. Only this audience may carry a Target or Permits.
	AudienceCredentialProxy Audience = "credential-proxy" // #nosec G101 -- an audience name, not a credential
)

// Permit is one thing a credential-proxy credential lets its task do beyond
// what its role does without one. The set is closed: Verify refuses a
// credential naming a permit outside it rather than ignoring the name, since
// a permit a verifier skips is one someone believes is granted (#3993).
type Permit string

const (
	// PermitVCSProposalCreate lets a profile worker reach the broker's
	// version-control write verbs that publish a change: publish,
	// proposal-create and proposal-update. The dispatcher grants it only to a
	// task whose submission says the requester asked for a change
	// (lib.TaskParameters.ChangeRequested), which only a structured confirm
	// sets.
	PermitVCSProposalCreate Permit = "vcs:proposal-create"

	// PermitVCSUnattendedWrite is the write permit of a run that a built-in
	// schedule started with no person to confirm it
	// (docs/designs/unattended-write-permit.md, rows 1 and 2). A credential
	// carries it only beside a trigger unattendedWriteTrigger accepts, and
	// never beside PermitVCSProposalCreate. The verifiers know the name one
	// release before anything mints it: no caller of Mint passes it yet, and
	// the broker's gate does not read it.
	PermitVCSUnattendedWrite Permit = "vcs:unattended-write"
)

// known reports whether p is a permit this package mints and verifies.
func (p Permit) known() bool {
	return p == PermitVCSProposalCreate || p == PermitVCSUnattendedWrite
}

// unattendedWriteTrigger reports whether t is a trigger a credential may
// carry PermitVCSUnattendedWrite beside: origin builtin, a stream that is not
// empty, and the name schedule/<stream>. Trigger.validate already holds a
// stream to that name and the origin to builtin; this states the permit's own
// rule whole, so a trigger shape admitted later does not widen it.
func unattendedWriteTrigger(t *Trigger) bool {
	return t != nil && t.Origin == TriggerOriginBuiltin && t.Stream != "" && t.Name == lib.TriggerSourceSchedule+"/"+t.Stream
}

// known reports whether a is an audience this package mints and verifies.
// Verify refuses any other, on either side of the comparison.
func (a Audience) known() bool {
	return a == AudienceNATS || a == AudienceCredentialProxy
}

const (
	// TokenType is the nats.type claim. jwt/v2 decodes any well-signed JWT as
	// a GenericClaims, a user or account JWT included, so the type is what
	// says this one was minted as a task credential.
	TokenType = "kube-agents-task" // #nosec G101 -- a token type label, not a credential

	// MaxTrustedKeys is how many public keys a verifier may trust at once:
	// the current key and the one it replaces, for one deadline ceiling
	// during a rotation.
	MaxTrustedKeys = 2

	// Skew is the clock skew a credential's expiry and issue time are
	// allowed against a verifier's clock: the dispatcher that signed it runs
	// on another node, and adds the same allowance to a task's deadline when
	// it mints. DefaultMaxLifetime bounds how far out an expiry may be where
	// no lower ceiling is configured. The callout, the dispatcher and the
	// worker share both.
	Skew               = 30 * time.Second
	DefaultMaxLifetime = 6 * time.Hour

	// trustedKeySeparator separates the keys in a trusted-key list
	// (A2A_TASK_SIGNING_PUBLIC_KEYS).
	trustedKeySeparator = ","

	// maxPodUID and maxTargetField bound the claims that aren't subject
	// tokens. A Kubernetes UID is 36 bytes; a GCP project id is at most 30
	// and a cluster name 40.
	maxPodUID      = 64
	maxTargetField = 128

	// jwtClaimsVersion is the nats.version jwt/v2 writes into every claim it
	// encodes (its unexported libVersion, 2 for the v2 library). A credential
	// minted by Mint always carries it; one without it, or with another
	// value, wasn't.
	jwtClaimsVersion = 2

	// nonceBytes is the randomness in each credential's subject; see Mint.
	nonceBytes = 16
)

// The signing key is an nkey with the CLUSTER prefix (seed SC..., public
// C...), and the choice is about what else would accept it.
//
// nkeys has no prefix of our own, so the question is which existing one no
// verifier on this bus trusts for anything. Not an account key: the callout's
// issuer is one, an account key signs user JWTs and authorization responses,
// and a task key pasted into auth_callout.issuer would be trusted with every
// grant on the bus. Not an operator key: in operator mode it signs accounts.
// Not a user key: the server authenticates nkey users by their U... public
// key. Not a server key: it signs the authorization requests the callout
// decodes. Not a curve key: it can't sign. jwt/v2 expects a cluster issuer on
// no claim type, and nats-server reads a cluster nkey nowhere, so a C... key
// signs one thing in this deployment, and a seed leaked from the dispatcher
// can't be mistaken for, or configured as, anything with more reach.
const keyPrefix = nkeys.PrefixByteCluster

// ParseTrustedKeys reads a comma-separated list of task-signing public keys,
// as A2A_TASK_SIGNING_PUBLIC_KEYS carries it: empty is none, and an empty
// element, a malformed key or more than MaxTrustedKeys is an error.
func ParseTrustedKeys(raw string) ([]string, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var keys []string
	for _, field := range strings.Split(raw, trustedKeySeparator) {
		key := strings.TrimSpace(field)
		if key == "" {
			return nil, fmt.Errorf("the task-signing key list %q has an empty element", raw)
		}
		if !ValidPublicKey(key) {
			return nil, fmt.Errorf("%q is not a task-signing public key (C...)", key)
		}
		keys = append(keys, key)
	}
	if len(keys) > MaxTrustedKeys {
		return nil, fmt.Errorf("%d task-signing keys; at most %d (the current one and the one it replaces)", len(keys), MaxTrustedKeys)
	}
	return keys, nil
}

// UnverifiedTrigger is the trigger a credential's claims name, read without
// checking its signature, nil when they name none. It is for refusing
// only: a worker that can't run a task as a model turn when its credential
// names a script asks it, and an unsigned or forged claim can only make it
// refuse more. Nothing may grant on what it returns.
func UnverifiedTrigger(token string) (*Trigger, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("a task credential is three dot-separated parts")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("the task credential's claims aren't base64url: %w", err)
	}
	var claims struct {
		Nats struct {
			Trigger *Trigger `json:"trigger"`
		} `json:"nats"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil {
		return nil, fmt.Errorf("the task credential's claims aren't JSON: %w", err)
	}
	return claims.Nats.Trigger, nil
}

// UnverifiedTarget is the cluster a credential's claims name, read without
// checking its signature, nil when they name none (a report credential). It
// is for routing and for messages only: a worker asks it which cluster a
// task's kubectl should be pointed at and held to on the worker's own side,
// where a forged claim can only send a request the verifier then refuses.
// Nothing may authorise on what it returns; Verify is what does.
func UnverifiedTarget(token string) (*Target, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("a task credential is three dot-separated parts")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("the task credential's claims aren't base64url: %w", err)
	}
	var claims struct {
		Nats struct {
			Target *Target `json:"target"`
		} `json:"nats"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil {
		return nil, fmt.Errorf("the task credential's claims aren't JSON: %w", err)
	}
	return claims.Nats.Target, nil
}

// NewSigningKey generates a task-signing keypair.
func NewSigningKey() (nkeys.KeyPair, error) {
	return nkeys.CreateCluster()
}

// ParseSigningKey loads a task-signing seed, refusing a seed of any other key
// type.
func ParseSigningKey(seed []byte) (nkeys.KeyPair, error) {
	prefix, _, err := nkeys.DecodeSeed(seed)
	if err != nil {
		return nil, fmt.Errorf("task-signing seed: %w", err)
	}
	if prefix != keyPrefix {
		return nil, fmt.Errorf("task-signing seed is not a cluster seed (SC...)")
	}
	return nkeys.FromSeed(seed)
}

// ConsumerStem is the stem of a task connection's consumer names and inbox:
// "<pod>_<task>". The worker names its three consumers
// <stem>-origin, <stem>-in and <stem>-events, and its inbox prefix is
// _INBOX.<stem>; the callout grants exactly those.
//
// The task is in the stem because a consumer's INFO, MSG.NEXT and DELETE
// subjects carry only its name, not its filter (only CREATE pins one): with a
// pod-only stem, task B's connection on a warm pod could pull from, or keep
// alive, the consumer task A's connection made. The separator is "_" because
// neither a pod name nor a task id (both DNS-1123 labels) can contain one, so
// no other (pod, task), and no pod's own pod-connection names
// (<pod>-<role>), can produce the same stem. Both halves are at most 63 bytes,
// so a stem is at most 127 and a consumer name at most 134, inside
// nats-server's 255 (JSMaxNameLen). It is not a lib.ValidSubjectToken (that
// is a DNS-1123 label): it is a legal NATS consumer name and subject token,
// with no ".", "*", ">" or whitespace.
func ConsumerStem(pod, task string) string {
	return pod + "_" + task
}

// ValidPublicKey reports whether pub is a task-signing public key.
func ValidPublicKey(pub string) bool {
	return nkeys.IsValidPublicClusterKey(pub)
}

// Target is the cluster a credential-proxy credential confines a task to: the
// one the dispatcher checked the grant against.
type Target struct {
	Project  string `json:"project"`
	Location string `json:"location"`
	Cluster  string `json:"cluster"`
}

// Trigger is the unattended trigger that started a task, as the grant the
// verifier checked binds it (#4685): Name is "schedule/<name>" or a bare
// source ("stall-watch"), Origin is "builtin", and Stream is the report
// stream the grant binds (only ever the schedule's own name), empty when it
// binds none. The broker reads it to decide which audit stream a task may
// write (the broker's reader comes in a later #4685 change). Only
// AudienceCredentialProxy carries one.
type Trigger struct {
	Name   string `json:"name"`
	Origin string `json:"origin"`
	Stream string `json:"stream,omitempty"`
	// Script is the profile script a built-in schedule's run executes
	// (capability.Trigger.Script, #5622); empty on every other trigger.
	Script string `json:"script,omitempty"`
}

// TriggerOriginBuiltin is the one trigger origin a credential may name, lib's.
const TriggerOriginBuiltin = lib.TriggerOriginBuiltin

// ReportTier is the one tier an AudienceCredentialProxy credential with no
// Target may name, and only with a Trigger: a report credential (#5619), what
// an unattended task a trigger started gets when it names no cluster. It
// confines nothing, so the broker holds it to the audit-report routes alone.
// It is capability.TierUnattended, the only tier a grant may bind a trigger
// at, and the Python verifier's REPORT_TIER.
const ReportTier = "unattended"

// Claims is a task credential's content.
type Claims struct {
	// Task is the task id: a2a.tasks.<Profile>.<Task>.… on the bus.
	Task string
	// Profile and Tier are the AgentProfile and the tier the task runs at,
	// which the verifier matches against the pod's identity.
	Profile string
	Tier    string
	// Pod and PodUID are the pod the dispatcher handed the task to, which
	// the verifier matches against the pod the cluster attests.
	Pod    string
	PodUID string

	Audience Audience
	Expires  time.Time

	// Target is for AudienceCredentialProxy only, and required there except
	// on a report credential (ReportTier).
	Target *Target

	// Permits is for AudienceCredentialProxy only: each a known Permit, none
	// twice. Mint omits the claim when Permits is empty, so a credential
	// without one is the same bytes it was before the claim existed, and
	// Verify refuses the claim present but empty.
	Permits []Permit
	// Trigger is for AudienceCredentialProxy only, and optional there except
	// beside PermitVCSUnattendedWrite: set when an unattended trigger
	// started the task.
	Trigger *Trigger

	// IssuedAt, ID (the jti) and Issuer are set by Mint and read back by
	// Verify; Mint ignores what a caller puts in them.
	IssuedAt time.Time
	ID       string
	Issuer   string
}

// payload is the nats claim block, the JWT's private claims. Verify decodes it
// strictly: an unknown field is refused rather than ignored, since a field a
// verifier skips is a constraint someone believes is enforced.
type payload struct {
	Type string `json:"type"`
	// Version is jwt/v2's own: GenericClaims.Encode writes nats.version as
	// the library's JWT version (updateVersion), so every credential Mint
	// produces carries it, and Verify requires exactly that value.
	Version int      `json:"version"`
	Task    string   `json:"task"`
	Profile string   `json:"profile"`
	Tier    string   `json:"tier"`
	Pod     string   `json:"pod"`
	PodUID  string   `json:"pod_uid"`
	Target  *Target  `json:"target,omitempty"`
	Permits []Permit `json:"permits,omitempty"`
	Trigger *Trigger `json:"trigger,omitempty"`
}

// Mint signs a credential with a task-signing key.
//
// The jti is not ours to set: jwt/v2 overwrites it with a hash of the standard
// claims at encode, and sets iat to the wall clock. So the subject is a fresh
// random nonce, which makes that hash, and so the jti, unique per mint; two
// credentials minted in the same second for the same pod and deadline would
// otherwise share one.
func Mint(kp nkeys.KeyPair, c Claims) (string, error) {
	if kp == nil {
		return "", fmt.Errorf("no task-signing key")
	}
	pub, err := kp.PublicKey()
	if err != nil {
		return "", fmt.Errorf("task-signing key: %w", err)
	}
	if !ValidPublicKey(pub) {
		return "", fmt.Errorf("task-signing key is not a cluster key (C...)")
	}
	if !c.Audience.known() {
		return "", fmt.Errorf("audience %q is not one this package mints", c.Audience)
	}
	if err := c.validate(); err != nil {
		return "", err
	}
	if c.Expires.IsZero() {
		return "", fmt.Errorf("a task credential needs an expiry")
	}

	nonce := make([]byte, nonceBytes)
	if _, err := rand.Read(nonce); err != nil {
		return "", fmt.Errorf("task credential nonce: %w", err)
	}
	gc := jwt.NewGenericClaims(strings.ToLower(base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(nonce)))
	gc.Audience = string(c.Audience)
	gc.Expires = c.Expires.Unix()
	gc.Data = map[string]interface{}{
		"type":    TokenType,
		"task":    c.Task,
		"profile": c.Profile,
		"tier":    c.Tier,
		"pod":     c.Pod,
		"pod_uid": c.PodUID,
	}
	if c.Target != nil {
		gc.Data["target"] = *c.Target
	}
	if len(c.Permits) > 0 {
		gc.Data["permits"] = slices.Clone(c.Permits)
	}
	if c.Trigger != nil {
		gc.Data["trigger"] = *c.Trigger
	}
	return gc.Encode(kp)
}

// Verify checks a credential and returns its claims. Every failure is an error
// with no claims, and the checks refuse rather than clamp: a credential that
// asks for more than the verifier allows is not trimmed to fit.
//
//   - trusted holds the public keys the verifier trusts, at most
//     MaxTrustedKeys, each a task-signing key. The issuer must be one of them;
//     jwt/v2 checks the signature against whatever issuer the token names, so
//     the issuer check is what makes the signature mean anything.
//   - aud must be a known audience, and the credential's must equal it.
//   - exp must be after now-skew, and no further than maxLifetime+skew past
//     now. A credential minted for longer than the verifier's ceiling is
//     refused, not shortened.
//   - iat may not be after now+skew or after exp, and exp-iat, the
//     credential's own span, is held to maxLifetime+skew as well.
//   - task, profile, tier and pod must be single subject tokens (dot-free
//     DNS-1123 labels: no ".", "*", ">" or whitespace, at most 63 bytes), and
//     pod_uid a bounded UID.
//   - target, permits and trigger may appear only under
//     AudienceCredentialProxy; permits, when present, is a non-empty list of
//     known permits, none twice; and an AudienceCredentialProxy credential
//     with no target must be a report credential: tier ReportTier and a
//     trigger.
//   - PermitVCSUnattendedWrite needs a builtin schedule/<name> trigger that
//     binds its stream, and never appears beside PermitVCSProposalCreate.
func Verify(token string, trusted []string, aud Audience, now time.Time, maxLifetime, skew time.Duration) (Claims, error) {
	if !aud.known() {
		return Claims{}, fmt.Errorf("audience %q is not one this package verifies", aud)
	}
	if len(trusted) == 0 {
		return Claims{}, fmt.Errorf("no task-signing key is trusted")
	}
	if len(trusted) > MaxTrustedKeys {
		return Claims{}, fmt.Errorf("%d task-signing keys are trusted; at most %d (the current one and the one it replaces)", len(trusted), MaxTrustedKeys)
	}
	for _, k := range trusted {
		if !ValidPublicKey(k) {
			return Claims{}, fmt.Errorf("trusted key %q is not a task-signing public key (C...)", k)
		}
	}
	if maxLifetime <= 0 || skew < 0 {
		return Claims{}, fmt.Errorf("a verifier needs a positive maximum lifetime and a non-negative skew")
	}

	if err := checkHeader(token); err != nil {
		return Claims{}, err
	}
	gc, err := jwt.DecodeGeneric(token)
	if err != nil {
		return Claims{}, fmt.Errorf("task credential: %w", err)
	}
	if !ValidPublicKey(gc.Issuer) || !slices.Contains(trusted, gc.Issuer) {
		return Claims{}, fmt.Errorf("task credential is signed by %q, which is not a trusted task-signing key", gc.Issuer)
	}
	if err := checkStrictClaims(token); err != nil {
		return Claims{}, err
	}

	p, err := decodePayload(gc.Data)
	if err != nil {
		return Claims{}, err
	}
	// Mint omits an empty permits claim, so one that is present and empty
	// (or null) wasn't minted here: one credential has one spelling.
	if _, present := gc.Data["permits"]; present && len(p.Permits) == 0 {
		return Claims{}, fmt.Errorf("task credential permits is present but empty")
	}
	if p.Type != TokenType {
		return Claims{}, fmt.Errorf("token type %q is not a task credential", p.Type)
	}
	if p.Version != jwtClaimsVersion {
		return Claims{}, fmt.Errorf("task credential claims version %d, want %d (jwt/v2's)", p.Version, jwtClaimsVersion)
	}

	// aud is known (checked first thing), so equality also refuses a
	// credential naming an unknown audience.
	got := Audience(gc.Audience)
	if got != aud {
		return Claims{}, fmt.Errorf("task credential is for %q, not %q", got, aud)
	}

	if gc.Expires == 0 {
		return Claims{}, fmt.Errorf("task credential has no expiry")
	}
	exp := time.Unix(gc.Expires, 0)
	if !exp.After(now.Add(-skew)) {
		return Claims{}, fmt.Errorf("task credential expired at %s", exp.UTC().Format(time.RFC3339))
	}
	if exp.Sub(now) > maxLifetime+skew {
		return Claims{}, fmt.Errorf("task credential expires %s from now, past the %s ceiling; refused rather than shortened", exp.Sub(now).Round(time.Second), maxLifetime)
	}
	if gc.IssuedAt == 0 {
		return Claims{}, fmt.Errorf("task credential has no issue time")
	}
	iat := time.Unix(gc.IssuedAt, 0)
	if iat.After(now.Add(skew)) || iat.After(exp) {
		return Claims{}, fmt.Errorf("task credential was issued at %s, after now or after its expiry", iat.UTC().Format(time.RFC3339))
	}
	// The credential's own span, as well as what is left of it: a credential
	// minted for longer than the ceiling is refused even once enough of it
	// has passed that exp-now fits. The same skew as above, so a minter whose
	// clock is ahead by less than the skew isn't refused for it.
	if exp.Sub(iat) > maxLifetime+skew {
		return Claims{}, fmt.Errorf("task credential spans %s from issue to expiry, past the %s ceiling; refused rather than shortened", exp.Sub(iat).Round(time.Second), maxLifetime)
	}
	if gc.NotBefore != 0 && time.Unix(gc.NotBefore, 0).After(now.Add(skew)) {
		return Claims{}, fmt.Errorf("task credential is not valid yet")
	}
	if gc.ID == "" || gc.Subject == "" {
		return Claims{}, fmt.Errorf("task credential has no jti or no subject")
	}

	c := Claims{
		Task:     p.Task,
		Profile:  p.Profile,
		Tier:     p.Tier,
		Pod:      p.Pod,
		PodUID:   p.PodUID,
		Audience: got,
		Expires:  exp,
		Target:   p.Target,
		Permits:  p.Permits,
		Trigger:  p.Trigger,
		IssuedAt: iat,
		ID:       gc.ID,
		Issuer:   gc.Issuer,
	}
	if err := c.validate(); err != nil {
		return Claims{}, err
	}
	return c, nil
}

// validate is the shape check Mint and Verify share.
func (c Claims) validate() error {
	for _, f := range []struct{ name, value string }{
		{"task", c.Task},
		{"profile", c.Profile},
		{"tier", c.Tier},
		{"pod", c.Pod},
	} {
		if !lib.ValidSubjectToken(f.value) {
			return fmt.Errorf("task credential %s %q is not a single subject token (a dot-free DNS-1123 label)", f.name, f.value)
		}
	}
	if !validUID(c.PodUID) {
		return fmt.Errorf("task credential pod_uid %q is not a UID", c.PodUID)
	}
	if c.Target != nil {
		if c.Audience != AudienceCredentialProxy {
			return fmt.Errorf("a %q task credential may not carry a target; only %q confines one", c.Audience, AudienceCredentialProxy)
		}
		for _, f := range []struct{ name, value string }{
			{"project", c.Target.Project},
			{"location", c.Target.Location},
			{"cluster", c.Target.Cluster},
		} {
			if !validTargetField(f.value) {
				return fmt.Errorf("task credential target %s %q is empty, too long or not printable", f.name, f.value)
			}
		}
	}
	if len(c.Permits) > 0 {
		// The same rule as Target: the callout enforces no permit, so a nats
		// credential carrying one would be a claim nobody checks.
		if c.Audience != AudienceCredentialProxy {
			return fmt.Errorf("a %q task credential may not carry permits; only %q enforces them", c.Audience, AudienceCredentialProxy)
		}
		seen := map[Permit]bool{}
		for _, p := range c.Permits {
			if !p.known() {
				return fmt.Errorf("task credential permit %q is not one this package knows", p)
			}
			if seen[p] {
				return fmt.Errorf("task credential permit %q appears twice", p)
			}
			seen[p] = true
		}
	}
	if c.Trigger != nil {
		if c.Audience != AudienceCredentialProxy {
			return fmt.Errorf("a %q task credential may not carry a trigger; only %q does", c.Audience, AudienceCredentialProxy)
		}
		if err := c.Trigger.validate(); err != nil {
			return err
		}
	}
	if slices.Contains(c.Permits, PermitVCSUnattendedWrite) {
		if slices.Contains(c.Permits, PermitVCSProposalCreate) {
			return fmt.Errorf("a task credential may not carry both %q and %q", PermitVCSUnattendedWrite, PermitVCSProposalCreate)
		}
		if !unattendedWriteTrigger(c.Trigger) {
			return fmt.Errorf("task credential permit %q needs a builtin %s/<name> trigger that binds its stream", PermitVCSUnattendedWrite, lib.TriggerSourceSchedule)
		}
	}
	// A proxy credential with no target confines nothing, so only a report
	// credential may omit it: the Python verifier's rule, held here too so
	// Mint never makes a shape the broker refuses (#5619).
	if c.Audience == AudienceCredentialProxy && c.Target == nil && (c.Tier != ReportTier || c.Trigger == nil) {
		return fmt.Errorf("a %q task credential names no target; only a %q task started by a trigger may omit it", AudienceCredentialProxy, ReportTier)
	}
	return nil
}

// validate is a trigger's shape check, lib.ValidateTrigger's.
func (t Trigger) validate() error {
	if err := lib.ValidateTrigger(t.Name, t.Origin, t.Stream, t.Script); err != nil {
		return fmt.Errorf("task credential %w", err)
	}
	return nil
}

// checkHeader refuses anything but the v2 nkey algorithm. jwt/v2 also decodes
// the v1 "ed25519" header, which signs only the payload; a task credential has
// only ever been v2, so accepting v1 would be a second signing input for no
// reason.
func checkHeader(token string) error {
	head, _, ok := strings.Cut(token, ".")
	if !ok {
		return fmt.Errorf("task credential is not a JWT")
	}
	raw, err := base64.RawURLEncoding.DecodeString(head)
	if err != nil {
		return fmt.Errorf("task credential header: %w", err)
	}
	var h jwt.Header
	if err := json.Unmarshal(raw, &h); err != nil {
		return fmt.Errorf("task credential header: %w", err)
	}
	if h.Algorithm != jwt.AlgorithmNkey || strings.ToUpper(h.Type) != jwt.TokenTypeJwt {
		return fmt.Errorf("task credential header names %q/%q, want %q/%q", h.Type, h.Algorithm, jwt.TokenTypeJwt, jwt.AlgorithmNkey)
	}
	return nil
}

// The claim keys a credential may carry, exactly as spelled: the standard
// claims jwt/v2's ClaimsData emits, the nats block's fields, the target's and
// the trigger's. permits is a list of strings, so it has no keys of its own.
var (
	topLevelClaims = map[string]bool{"jti": true, "iat": true, "iss": true, "name": true, "sub": true, "aud": true, "exp": true, "nbf": true, "nats": true}
	natsClaims     = map[string]bool{"type": true, "version": true, "task": true, "profile": true, "tier": true, "pod": true, "pod_uid": true, "target": true, "permits": true, "trigger": true}
	targetClaims   = map[string]bool{"project": true, "location": true, "cluster": true}
	triggerClaims  = map[string]bool{"name": true, "origin": true, "stream": true, "script": true}
)

// checkStrictClaims re-reads the raw payload segment before anything trusts
// the decoded claims. encoding/json matches keys case-insensitively and lets
// the last of two equal keys win, and jwt/v2 ignores keys it doesn't know, so
// {"aud":"credential-proxy","AUD":"nats"} would decode as a nats credential
// here while a verifier reading exact keys (the credential proxy's) read it as
// a proxy one. So, at every level: no key twice, no key that is an allowed key
// in another case; and at the top level no key outside the standard claims and
// nats. Unknown keys inside nats are refused by decodePayload.
func checkStrictClaims(token string) error {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return fmt.Errorf("task credential is not a JWT")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return fmt.Errorf("task credential payload: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := strictValue(dec, ""); err != nil {
		return fmt.Errorf("task credential payload: %w", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return fmt.Errorf("task credential payload: trailing data")
	}
	return nil
}

// strictValue walks one JSON value; path names the object it is in ("" for
// the top level, "nats", "nats.target").
func strictValue(dec *json.Decoder, path string) error {
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	switch tok {
	case json.Delim('{'):
		allowed := map[string]map[string]bool{"": topLevelClaims, "nats": natsClaims, "nats.target": targetClaims, "nats.trigger": triggerClaims}[path]
		seen := map[string]bool{}
		for dec.More() {
			kt, err := dec.Token()
			if err != nil {
				return err
			}
			key := kt.(string)
			if seen[key] {
				return fmt.Errorf("key %q appears twice", key)
			}
			seen[key] = true
			if allowed != nil && !allowed[key] {
				if foldsTo(allowed, key) {
					return fmt.Errorf("key %q is an allowed key in the wrong case", key)
				}
				if path == "" {
					return fmt.Errorf("unknown top-level claim %q", key)
				}
			}
			child := key
			if path != "" {
				child = path + "." + key
			}
			if err := strictValue(dec, child); err != nil {
				return err
			}
		}
		_, err := dec.Token()
		return err
	case json.Delim('['):
		for dec.More() {
			if err := strictValue(dec, path+"[]"); err != nil {
				return err
			}
		}
		_, err := dec.Token()
		return err
	}
	return nil
}

// foldsTo reports whether key equals an allowed key under case folding.
func foldsTo(allowed map[string]bool, key string) bool {
	for k := range allowed {
		if strings.EqualFold(k, key) {
			return true
		}
	}
	return false
}

// decodePayload reads the nats claim block into payload, refusing unknown
// fields.
func decodePayload(data map[string]interface{}) (payload, error) {
	raw, err := json.Marshal(data)
	if err != nil {
		return payload{}, fmt.Errorf("task credential claims: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var p payload
	if err := dec.Decode(&p); err != nil {
		return payload{}, fmt.Errorf("task credential claims: %w", err)
	}
	return p, nil
}

// validUID accepts lowercase alphanumerics and dashes. Kubernetes UIDs are
// lowercase hex and dashes, a subset; the check is the wider set because it is
// a shape bound, not a UID parser, and the callout compares the claim to the
// attested UID byte for byte anyway.
func validUID(s string) bool {
	if s == "" || len(s) > maxPodUID {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
			return false
		}
	}
	return true
}

// validTargetField accepts a bounded, non-empty, printable value with no
// whitespace: project ids, locations and cluster names, which the proxy
// compares byte for byte.
func validTargetField(s string) bool {
	if s == "" || len(s) > maxTargetField {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] <= ' ' || s[i] > '~' {
			return false
		}
	}
	return true
}
