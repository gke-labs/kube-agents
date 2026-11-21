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
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Inference gateway caller keys. The gateway (the chart's LiteLLM, with the
// caller-auth hook in charts/kube-agents/files/litellm_caller_auth.py) reads
// an optional Secret, litellm-caller-keys, whose keys.json maps the
// lowercase-hex sha256 of a caller key to the caller class it is admitted
// as. This file mints one key per class, writes that map, and asks every
// Ready gateway pod whether it has loaded the map yet, which is the
// InferenceKeysServed condition. Callers are given their key only once a
// pass has found the condition True for a map holding it
// (inference_key_callers.go), so a caller never sends a key the gateway
// would refuse.
//
// The question is asked with a probe key of its own, in the reserved class
// gateway-probe, which the hook admits on GET /v1/models alone and no
// caller is ever given. A fresh probe key goes with every map the operator
// writes, its digest in the same update as the callers' and the previous
// probe digest dropped, so a pod that admits it has loaded that map, and a
// pod still on an older one refuses it. A caller's key
// is never sent in a probe: which pods the probe reaches is chosen from pod
// objects anyone who can create a pod in the namespace can imitate
// (gatewayOwnsPod).
//
// The classes: the agent pod (agent), the A2A gateway's session pods
// (a2a-session), the curator (curator), and one per AgentProfile tier in
// use (tier-<tier>), which scheduled runs share since they run on their
// tier's template. Hindsight and the web console stay keyless.
//
// The key Secrets for agent, a2a-session and curator are in the agent's
// namespace with a controller reference to the PlatformAgent. A tier's is
// in the profile namespace, where the workers run, and can't carry an owner
// reference across namespaces (agentprofile_namespace.go), so it carries
// this agent's labels and is deleted when no bound profile lists the tier,
// a draining one included, and on the PlatformAgent's deletion.
//
// Whoever creates a Secret writes its labels and owner references, so
// neither says the operator made it. The operator records the UID of every
// Secret it creates here (each key Secret and the map Secret) on the
// PlatformAgent's status, which only it writes (status.inferenceKeys), and
// uses a Secret at one of those names only when its UID is the recorded one.
// The names are reserved for the operator: any other Secret found at one is
// replaced, never adopted. It is deleted with its UID as a precondition, so
// exactly the object read goes, a Warning Event names it, and the operator
// creates its own and records that; the replaced Secret's key never reaches
// the map. This is also how an install recovers when the record is lost (a
// rollback to an operator that drops the field, or a crash between a create
// and its record): every key is replaced once, and the condition comes back
// on a later pass. A Secret at any other name is never deleted. Before any
// write the operator checks the served CRD's schema has the status field
// (servedCRDHasInferenceKeys); without it, it writes nothing at all.
//
// A key Secret is created once and never regenerated while it holds a
// well-formed key: rotation is a separate, staged step. One that holds no
// key, or a malformed one, is repaired.
//
// Secrets are read by name through the uncached reader: the operator never
// lists or watches a Secret (tests/test_operator_secrets_grant.py). Nothing
// here logs, records or reports a key or a key's digest; counts, class names
// and Secret names only. The one exception is a key Secret's own served mark
// (inferenceKeyServedAnnotation), its key's digest on the same Secret.
const (
	inferenceKeysServedCondition = "InferenceKeysServed"
	// Reasons the condition carries.
	inferenceKeysReasonServed         = "Served"
	inferenceKeysReasonNotServed      = "NotServed"
	inferenceKeysReasonProbeFailed    = "ProbeIncomplete"
	inferenceKeysReasonNoReadyGateway = "NoReadyGatewayPod"
	inferenceKeysReasonNotWritten     = "NotWritten"
	inferenceKeysReasonCRDOutdated    = "CRDOutdated"

	// The caller classes with one key for the whole install.
	inferenceKeyClassAgent   = "agent"
	inferenceKeyClassSession = "a2a-session"
	inferenceKeyClassCurator = "curator"
	// inferenceKeyClassProbe is the probe key's class: the hook's
	// PROBE_CLASS. Not a caller class; no caller is given its key.
	inferenceKeyClassProbe = "gateway-probe"
	// inferenceKeyTierClassPrefix starts a profile tier's class name.
	inferenceKeyTierClassPrefix = "tier-"

	// A class's key Secret is <agent>-inference-key-<class>. The session
	// class's Secret name and data key are fixed: the A2A gateway pod
	// admission policy is to pin them, so neither may change on its own.
	inferenceKeySecretInfix = "-inference-key-"
	// inferenceKeyDataKey is the data key the key is stored under, the one a
	// caller's secretKeyRef names.
	inferenceKeyDataKey = "api-key"
	// inferenceKeyPrefix starts every key. LiteLLM hashes a presented key
	// that starts with it before it uses the key as a metric label or in its
	// spend records, so a key that reaches a gateway without the caller-auth
	// hook is still never exported raw.
	inferenceKeyPrefix = "sk-"
	// inferenceKeyRandomBytes is the key's entropy, from crypto/rand.
	inferenceKeyRandomBytes = 32
	// inferenceKeyComponent is the component label on every key Secret, the
	// label renderedByAgent reads on a tier's in the profile namespace.
	inferenceKeyComponent = "inference-key"
	// inferenceKeyClassLabel names the class a key Secret holds the key of.
	inferenceKeyClassLabel = "kubeagents.x-k8s.io/inference-key-class"

	// callerKeysSecretName and callerKeysFileKey are the map the gateway
	// mounts (the chart's litellm.yaml); callerKeysComponent labels it.
	callerKeysSecretName = "litellm-caller-keys" // #nosec G101 -- Secret name, not a credential
	callerKeysFileKey    = "keys.json"
	callerKeysComponent  = "caller-keys"

	// inferenceKeyProbePath is the one route the hook admits the probe class
	// on (its PROBE_ROUTE, with GET), which answers without reaching a model
	// provider.
	inferenceKeyProbePath = "/v1/models"
	// inferenceKeyProbeScheme is the scheme a gateway pod serves on its pod IP.
	inferenceKeyProbeScheme = "http://"
	// inferenceKeyAuthScheme is the Authorization header's scheme.
	inferenceKeyAuthScheme = "Bearer "
	// replicaSetKind is the kind of a gateway pod's controller.
	replicaSetKind = "ReplicaSet"
	// inferenceKeyProbeTimeout bounds every probe of one pass, dial included:
	// the probes run side by side under one deadline.
	inferenceKeyProbeTimeout = 2 * time.Second
	// inferenceKeyProbeMaxBodyBytes bounds what is drained of an answer
	// before the connection closes; the body is never read for content.
	inferenceKeyProbeMaxBodyBytes = 1 << 16
	// inferenceKeysRecheck is how soon a pass looks again while a gateway
	// pod hasn't admitted a key yet: the kubelet refreshes a mounted Secret
	// on its sync loop, about a minute by default, and nothing the operator
	// watches changes when it does.
	inferenceKeysRecheck = 15 * time.Second
	// inferenceKeysUnreachableRecheck is how soon a pass looks again while a
	// Ready gateway pod doesn't answer at all. Longer than a refusal's: a pod
	// the operator can't reach (a PlatformAgent outside the operator's
	// namespace, whose gateway admits its own namespace alone) may never
	// answer, and should not hold the agent at four passes a minute.
	inferenceKeysUnreachableRecheck = time.Minute

	// servedPlatformAgentCRDName is the PlatformAgent CRD, read for whether its
	// schema has status.inferenceKeys before any write here.
	servedPlatformAgentCRDName = "platformagents.kubeagents.x-k8s.io"

	// reasonInferenceKeyMinted is the Normal Event a new key writes.
	reasonInferenceKeyMinted = "InferenceKeyMinted"
	// reasonInferenceKeySecretReplaced is the Warning Event a replaced
	// Secret at a reserved name writes.
	reasonInferenceKeySecretReplaced = "InferenceKeySecretReplaced"
)

// callerClassPattern and reservedCallerClasses are the hook's
// CLASS_NAME_PATTERN and RESERVED_CLASSES: a map entry whose class fails
// them is dropped by the gateway, so the operator never writes one.
// keyDigestPattern is its KEY_DIGEST_PATTERN.
var (
	callerClassPattern    = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	keyDigestPattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	reservedCallerClasses = []string{"keyless", "litellm_proxy_master_key"}
)

// inferenceKeyEncoding is how the random bytes are written after the
// prefix: base64url without padding, LiteLLM's own key alphabet
// (secrets.token_urlsafe), 43 characters for 32 bytes, with no character a
// header, an env value or YAML has to quote. Strict, so a key read back has
// exactly one spelling.
var inferenceKeyEncoding = base64.RawURLEncoding.Strict()

// installInferenceKeyClasses are the classes whose key is in the agent's
// namespace, in the order the condition names them.
var installInferenceKeyClasses = []string{inferenceKeyClassAgent, inferenceKeyClassSession, inferenceKeyClassCurator}

// inferenceKeyProber asks the gateway pod at addr (host:port) what it
// answers to a request carrying apiKey: the HTTP status, or an error when
// nothing answered.
type inferenceKeyProber interface {
	Probe(ctx context.Context, addr, apiKey string) (int, error)
}

// httpInferenceKeyProber is the prober the reconciler uses: the usage
// scrape's stance (no proxy, no redirects, a short timeout), so the key is
// sent to the pod IP and nowhere else.
type httpInferenceKeyProber struct{ client *http.Client }

func newHTTPInferenceKeyProber() *httpInferenceKeyProber {
	return &httpInferenceKeyProber{client: &http.Client{
		Timeout: inferenceKeyProbeTimeout,
		Transport: &http.Transport{
			Proxy:                  nil,
			DialContext:            (&net.Dialer{Timeout: inferenceKeyProbeTimeout}).DialContext,
			DisableKeepAlives:      true,
			MaxResponseHeaderBytes: usageScrapeMaxHeaderBytes,
		},
		// A redirect would carry the Authorization header elsewhere; it is
		// read as the answer instead, which is not a 200.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// defaultInferenceKeyProber is one client for the life of the process.
var defaultInferenceKeyProber = newHTTPInferenceKeyProber()

// Probe GETs inferenceKeyProbePath at addr with apiKey as the bearer token.
// Its errors are a closed vocabulary (usageConnectKind), never text built
// from the peer's bytes or the request: the error reaches a condition.
func (p *httpInferenceKeyProber) Probe(ctx context.Context, addr, apiKey string) (int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, inferenceKeyProbeScheme+addr+inferenceKeyProbePath, nil)
	if err != nil {
		return 0, errors.New(usageScrapeKindConnect)
	}
	req.Header.Set("Authorization", inferenceKeyAuthScheme+apiKey)
	resp, err := p.client.Do(req)
	if err != nil {
		return 0, errors.New(usageConnectKind(err))
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, inferenceKeyProbeMaxBodyBytes))
	return resp.StatusCode, nil
}

// inferenceKeyClass is one class and where its key Secret is.
type inferenceKeyClass struct {
	name   string
	secret types.NamespacedName
	// inProfileNamespace marks a tier's Secret, which carries labels in
	// place of an owner reference.
	inProfileNamespace bool
}

// inferenceKeysOutcome is what a pass found: the condition to write on the
// way out (nil leaves it as it is, inferenceKeysRemoved takes it away) and
// how soon to look again (zero: no sooner than the steady requeue).
type inferenceKeysOutcome struct {
	condition *metav1.Condition
	requeue   time.Duration
}

// inferenceKeysRemoved is the verdict on a pass where the namespace runs no
// inference gateway: there is nothing to serve the keys, so no condition.
var inferenceKeysRemoved = &metav1.Condition{}

func inferenceKeySecretName(agent *agentv1alpha1.PlatformAgent, class string) string {
	return agent.Name + inferenceKeySecretInfix + class
}

func installInferenceKeyClass(agent *agentv1alpha1.PlatformAgent, class string) inferenceKeyClass {
	return inferenceKeyClass{name: class, secret: types.NamespacedName{Namespace: agent.Namespace, Name: inferenceKeySecretName(agent, class)}}
}

func tierInferenceKeyClass(agent *agentv1alpha1.PlatformAgent, tier string) inferenceKeyClass {
	class := inferenceKeyTierClassPrefix + tier
	return inferenceKeyClass{
		name:               class,
		secret:             types.NamespacedName{Namespace: profileNamespaceName(agent), Name: inferenceKeySecretName(agent, class)},
		inProfileNamespace: true,
	}
}

// validCallerClass reports whether the gateway would take class from the map.
func validCallerClass(class string) bool {
	return callerClassPattern.MatchString(class) && !slices.Contains(reservedCallerClasses, class)
}

// newInferenceKey is a fresh key: the prefix and 32 random bytes.
func newInferenceKey() (string, error) {
	buf := make([]byte, inferenceKeyRandomBytes)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("generating an inference gateway key: %w", err)
	}
	return inferenceKeyPrefix + inferenceKeyEncoding.EncodeToString(buf), nil
}

// wellFormedInferenceKey reports whether apiKey is one newInferenceKey could
// have made.
func wellFormedInferenceKey(apiKey string) bool {
	rest, ok := strings.CutPrefix(apiKey, inferenceKeyPrefix)
	if !ok {
		return false
	}
	raw, err := inferenceKeyEncoding.DecodeString(rest)
	return err == nil && len(raw) == inferenceKeyRandomBytes
}

// inferenceKeyDigest is the map's index for a key: its sha256, lowercase
// hex, as the hook computes it.
func inferenceKeyDigest(apiKey string) string {
	// A fast digest is the right one, not a password KDF: every input is a
	// 256-bit random key the operator minted (newInferenceKey), so its digest
	// can't be guessed back, and the gateway's hook compares this same
	// sha256. CodeQL's weak-sensitive-data-hashing query reads apiKey as a
	// password; the name stays so its clear-text-logging query keeps
	// watching it.
	sum := sha256.Sum256([]byte(apiKey))
	return hex.EncodeToString(sum[:])
}

// callerKeysDocument is keys.json for digests (digest to class): a JSON
// object, keys sorted by encoding/json, so the same keys always write the
// same bytes.
func callerKeysDocument(digests map[string]string) ([]byte, error) {
	if digests == nil {
		digests = map[string]string{}
	}
	return json.Marshal(digests)
}

// tierClassesInMap is the profile tiers a map names, from its classes.
func tierClassesInMap(doc []byte) []string {
	var entries map[string]string
	if json.Unmarshal(doc, &entries) != nil {
		return nil
	}
	seen := map[string]bool{}
	for _, class := range entries {
		if tier, ok := strings.CutPrefix(class, inferenceKeyTierClassPrefix); ok && tier != "" {
			seen[tier] = true
		}
	}
	out := make([]string, 0, len(seen))
	for tier := range seen {
		out = append(out, tier)
	}
	slices.Sort(out)
	return out
}

// crdGVK is the kind of a CustomResourceDefinition. The operator's scheme
// doesn't register apiextensions, so the CRD is read as metadata and as
// unstructured, through the uncached reader (no informer starts for it).
var crdGVK = schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"}

// inferenceKeysCRDCache is the last answer to whether the served PlatformAgent
// CRD's schema has status.inferenceKeys, for the CRD resourceVersion it was
// read at.
type inferenceKeysCRDCache struct {
	mu              sync.Mutex
	resourceVersion string
	hasField        bool
}

// servedCRDHasInferenceKeys reports whether the served PlatformAgent CRD's
// schema, at the version this operator reads and writes, has
// status.inferenceKeys. Helm doesn't upgrade CRDs from crds/ on an upgrade,
// so an older CRD is a normal state after one, and every write in this step
// waits on this answer. Each call reads the CRD's metadata alone; the schema
// is read again only when the resourceVersion moved since the cached answer.
func (r *PlatformAgentReconciler) servedCRDHasInferenceKeys(ctx context.Context) (bool, error) {
	key := types.NamespacedName{Name: servedPlatformAgentCRDName}
	head := &metav1.PartialObjectMetadata{}
	head.SetGroupVersionKind(crdGVK)
	if err := r.a2aReader().Get(ctx, key, head); err != nil {
		return false, err
	}
	cache := &r.inferenceKeysCRD
	cache.mu.Lock()
	defer cache.mu.Unlock()
	if cache.resourceVersion != "" && cache.resourceVersion == head.GetResourceVersion() {
		return cache.hasField, nil
	}
	crd := &unstructured.Unstructured{}
	crd.SetGroupVersionKind(crdGVK)
	if err := r.a2aReader().Get(ctx, key, crd); err != nil {
		return false, err
	}
	cache.resourceVersion, cache.hasField = crd.GetResourceVersion(), crdSchemaHasInferenceKeys(crd, agentv1alpha1.GroupVersion.Version)
	return cache.hasField, nil
}

// forgetServedCRDAnswer drops the cached answer, for the backstop: a record
// write the served CRD dropped although the cache said it had the field.
func (r *PlatformAgentReconciler) forgetServedCRDAnswer() {
	r.inferenceKeysCRD.mu.Lock()
	defer r.inferenceKeysCRD.mu.Unlock()
	r.inferenceKeysCRD.resourceVersion = ""
}

// crdSchemaHasInferenceKeys reports whether crd's schema for version has
// status.inferenceKeys.
func crdSchemaHasInferenceKeys(crd *unstructured.Unstructured, version string) bool {
	versions, _, _ := unstructured.NestedSlice(crd.Object, "spec", "versions")
	for _, v := range versions {
		entry, ok := v.(map[string]any)
		if !ok || entry["name"] != version {
			continue
		}
		_, found, _ := unstructured.NestedMap(entry, "schema", "openAPIV3Schema", "properties", "status", "properties", "inferenceKeys")
		return found
	}
	return false
}

// errInferenceKeysStatusPruned is a record write the served CRD dropped.
var errInferenceKeysStatusPruned = errors.New("the served PlatformAgent CRD has no status.inferenceKeys")

// recordedSecretUIDs is status.inferenceKeys.secretUIDs, by Secret name.
func recordedSecretUIDs(agent *agentv1alpha1.PlatformAgent) map[string]types.UID {
	out := map[string]types.UID{}
	if agent.Status.InferenceKeys != nil {
		for name, uid := range agent.Status.InferenceKeys.SecretUIDs {
			out[name] = types.UID(uid)
		}
	}
	return out
}

// updateSecretUIDs records set and forgets drop in status.inferenceKeys, as
// recordProfileSubmitMaxAge records its field: through a copy, so the
// stored status doesn't replace this pass's in-memory one, against the
// version this pass read, carrying back only the field and the version. A
// write whose answer lacks a recorded UID is errInferenceKeysStatusPruned.
func (r *PlatformAgentReconciler) updateSecretUIDs(ctx context.Context, agent *agentv1alpha1.PlatformAgent, set map[string]types.UID, drop []string) error {
	patch := client.MergeFromWithOptions(agent.DeepCopy(), client.MergeFromWithOptimisticLock{})
	written := agent.DeepCopy()
	if written.Status.InferenceKeys == nil {
		written.Status.InferenceKeys = &agentv1alpha1.InferenceKeysStatus{}
	}
	if written.Status.InferenceKeys.SecretUIDs == nil {
		written.Status.InferenceKeys.SecretUIDs = map[string]string{}
	}
	for name, uid := range set {
		written.Status.InferenceKeys.SecretUIDs[name] = string(uid)
	}
	for _, name := range drop {
		delete(written.Status.InferenceKeys.SecretUIDs, name)
	}
	if err := r.Status().Patch(ctx, written, patch); err != nil {
		return err
	}
	for name, uid := range set {
		if written.Status.InferenceKeys == nil || written.Status.InferenceKeys.SecretUIDs[name] != string(uid) {
			return errInferenceKeysStatusPruned
		}
	}
	agent.Status.InferenceKeys = written.Status.InferenceKeys
	agent.ResourceVersion = written.ResourceVersion
	return nil
}

// createRecorded creates secret and records its UID. When the record can't
// be written the Secret is deleted again, by that UID, so the operator never
// leaves behind a Secret it would itself refuse to use.
func (r *PlatformAgentReconciler) createRecorded(ctx context.Context, agent *agentv1alpha1.PlatformAgent, secret *corev1.Secret) error {
	if err := r.Create(ctx, secret); err != nil {
		return err
	}
	recordErr := r.updateSecretUIDs(ctx, agent, map[string]types.UID{secret.Name: secret.UID}, nil)
	if recordErr == nil {
		return nil
	}
	uid := secret.UID
	if err := r.Delete(ctx, secret, client.Preconditions{UID: &uid}); client.IgnoreNotFound(err) != nil {
		logf.FromContext(ctx).Info("could not delete an inference gateway key Secret whose record failed; delete it by hand and the operator mints a fresh one",
			"secret", secret.Namespace+"/"+secret.Name, "failure", apiFailure(err))
	}
	return recordErr
}

// notWrittenFor is the outcome of a failed key or map write: the pruned CRD
// says so and waits for the steady requeue; anything else looks again soon.
func (r *PlatformAgentReconciler) notWrittenFor(what string, err error) inferenceKeysOutcome {
	if errors.Is(err, errInferenceKeysStatusPruned) {
		r.forgetServedCRDAnswer()
		return notWritten("the served PlatformAgent CRD has no status.inferenceKeys, so the operator can't record the Secrets it creates and creates none; apply the current CRD", false)
	}
	if errors.Is(err, errReservedSecretTerminating) {
		return notWritten(what+" is being deleted; the operator creates its own once it's gone", true)
	}
	if errors.Is(err, errFreshRecordUnreadable) {
		return notWritten(what+" is not in the record, and the PlatformAgent could not be read past the cache to confirm that ("+apiFailure(err)+"); nothing is replaced on this pass", true)
	}
	return notWritten(what+" could not be read or written: "+apiFailure(err), true)
}

// tiersInUse is every tier a bound profile lists, live or draining, sorted.
func tiersInUse(profiles []agentv1alpha1.AgentProfile) []string {
	seen := map[string]bool{}
	for i := range profiles {
		for _, tier := range profiles[i].AllowedTierList() {
			seen[tier] = true
		}
	}
	out := make([]string, 0, len(seen))
	for tier := range seen {
		out = append(out, tier)
	}
	slices.Sort(out)
	return out
}

// inferenceKeysCondition is a condition of the type this file writes.
func inferenceKeysCondition(status metav1.ConditionStatus, reason, message string) *metav1.Condition {
	return &metav1.Condition{Type: inferenceKeysServedCondition, Status: status, Reason: reason, Message: message}
}

// notWritten is the outcome of a pass that could not write the keys or the
// map; recheck says whether the cause can clear on its own.
func notWritten(message string, recheck bool) inferenceKeysOutcome {
	out := inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionFalse, inferenceKeysReasonNotWritten, message)}
	if recheck {
		out.requeue = inferenceKeysRecheck
	}
	return out
}

// apiFailure names a failed API call for a condition or a log line by its
// status reason and code alone. The error's text is not used: an admission
// webhook's refusal of a Secret write is free text the operator doesn't
// control.
func apiFailure(err error) string {
	return fmt.Sprintf("%s (HTTP %d)", apierrors.ReasonForError(err), apiStatusCode(err))
}

func apiStatusCode(err error) int32 {
	var status apierrors.APIStatus
	if errors.As(err, &status) {
		return status.Status().Code
	}
	return 0
}

// reconcileInferenceKeys mints the class keys, writes the map and probes the
// gateway pods. It never fails the pass: nothing consumes the keys until the
// condition is True, so a failure here is the condition's to report and the
// rest of the reconcile carries on.
func (r *PlatformAgentReconciler) reconcileInferenceKeys(ctx context.Context, agent *agentv1alpha1.PlatformAgent) inferenceKeysOutcome {
	log := logf.FromContext(ctx)

	// No gateway, nothing to serve the keys: mint nothing and say nothing.
	var gateway appsv1.Deployment
	switch err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: litellmDeploymentName}, &gateway); {
	case apierrors.IsNotFound(err):
		return inferenceKeysOutcome{condition: inferenceKeysRemoved}
	case err != nil:
		return notWritten("the inference gateway Deployment could not be read: "+apiFailure(err), true)
	case gateway.DeletionTimestamp != nil:
		return inferenceKeysOutcome{condition: inferenceKeysRemoved}
	}

	// One gateway serves one namespace, and its map can speak for one agent.
	var agents agentv1alpha1.PlatformAgentList
	if err := r.List(ctx, &agents, client.InNamespace(agent.Namespace)); err != nil {
		return notWritten("the PlatformAgents in the namespace could not be listed: "+apiFailure(err), true)
	}
	if len(agents.Items) != 1 {
		return notWritten(fmt.Sprintf("%d PlatformAgents share namespace %s; the operator writes caller keys only for the one PlatformAgent in a namespace", len(agents.Items), agent.Namespace), false)
	}

	// No write of any kind until the served CRD can hold the record: with an
	// older one, nothing is created, replaced or written.
	switch has, err := r.servedCRDHasInferenceKeys(ctx); {
	case err != nil:
		return notWritten("the PlatformAgent CRD could not be read to check it has status.inferenceKeys ("+apiFailure(err)+"); nothing is written on this pass", true)
	case !has:
		return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionFalse, inferenceKeysReasonCRDOutdated,
			"the served PlatformAgent CRD has no status.inferenceKeys, so the operator creates no inference gateway key until the current PlatformAgent CRD is applied")}
	}

	recorded := recordedSecretUIDs(agent)
	mapKey := types.NamespacedName{Namespace: agent.Namespace, Name: callerKeysSecretName}
	existingMap := &corev1.Secret{}
	mapFound := true
	if err := r.a2aReader().Get(ctx, mapKey, existingMap); err != nil {
		if !apierrors.IsNotFound(err) {
			return notWritten("Secret "+callerKeysSecretName+" could not be read: "+apiFailure(err), true)
		}
		mapFound = false
	}
	// A map at the name that isn't the recorded one is replaced when the new
	// map is written, not before, and nothing is read from it.
	mapRecorded := mapFound && recorded[callerKeysSecretName] != "" && existingMap.UID == recorded[callerKeysSecretName]
	if mapFound && !mapRecorded {
		// The pass's copy of the agent can lag the record: take the record
		// from the API server before treating the map, or anything, as not
		// the operator's.
		fresh, err := r.freshRecordedSecretUIDs(ctx, agent)
		if err != nil {
			return r.notWrittenFor("Secret "+mapKey.String(), err)
		}
		recorded = fresh
		mapRecorded = recorded[callerKeysSecretName] != "" && existingMap.UID == recorded[callerKeysSecretName]
	}
	var previousTiers []string
	previousEntries := map[string]string{}
	if mapRecorded {
		previousTiers = tierClassesInMap(existingMap.Data[callerKeysFileKey])
		_ = json.Unmarshal(existingMap.Data[callerKeysFileKey], &previousEntries)
	}
	// carry keeps a tier's entry from the stored map on a pass that can't
	// reach its key in the profile namespace, so a failure confined there
	// costs the rest of the map nothing and that tier only its key's repair.
	carried := map[string]string{}
	carry := func(class string) {
		for digest, entry := range previousEntries {
			if entry == class {
				carried[digest] = class
			}
		}
	}

	// The tiers in use. Unknown when the operator can't read AgentProfiles:
	// then the tiers the map already names are kept as they are, so an
	// install whose role lost the read revokes no tier's key.
	tiers, tiersKnown, profileNamespaceReady := previousTiers, false, false
	if r.agentProfilesUnreadable && len(previousTiers) > 0 {
		// Kept tiers are still written only into a profile namespace that is
		// there: one that has gone takes its keys with it.
		switch ready, err := r.profileNamespaceExists(ctx, agent); {
		case err != nil:
			r.profileNamespaceFailure(ctx, agent, "read the profile namespace for the inference gateway tier keys", errors.New(apiFailure(err)))
			for _, tier := range previousTiers {
				carry(inferenceKeyTierClassPrefix + tier)
			}
			tiers = nil
		case !ready:
			tiers = nil
		}
	}
	if !r.agentProfilesUnreadable {
		profiles, err := boundAgentProfiles(ctx, r.Client, agent, r.tiersUnreadable)
		if err != nil {
			return notWritten("the AgentProfiles could not be listed: "+apiFailure(err), true)
		}
		ready, err := r.profileNamespaceExists(ctx, agent)
		if err != nil {
			return notWritten("the profile namespace could not be read: "+apiFailure(err), true)
		}
		tiers, tiersKnown, profileNamespaceReady = nil, true, ready
		if ready {
			tiers = tiersInUse(profiles)
		}
	}

	classes := make([]inferenceKeyClass, 0, len(installInferenceKeyClasses)+len(tiers))
	for _, class := range installInferenceKeyClasses {
		classes = append(classes, installInferenceKeyClass(agent, class))
	}
	for _, tier := range tiers {
		c := tierInferenceKeyClass(agent, tier)
		if !validCallerClass(c.name) {
			log.Info("a profile tier's name doesn't fit the gateway's caller class pattern; it gets no inference gateway key", "class", c.name)
			continue
		}
		classes = append(classes, c)
	}

	// Every key, read or minted, before the map: the map is written only
	// from a complete read, so a failed read never drops a key that works.
	// The probe key last, beside the callers' and in the same map.
	classes = append(classes, installInferenceKeyClass(agent, inferenceKeyClassProbe))
	keys := make(map[string]string, len(classes))
	var served []inferenceKeyClass
	var minted []string
	for _, c := range classes {
		apiKey, wasMinted, err := r.ensureInferenceKey(ctx, agent, c, recorded[c.secret.Name])
		if errors.Is(err, errReservedSecretTerminating) && c.inProfileNamespace {
			carry(c.name)
			continue
		}
		if err != nil && c.inProfileNamespace && !errors.Is(err, errInferenceKeysStatusPruned) && !errors.Is(err, errFreshRecordUnreadable) {
			// Confined to the profile namespace (profileNamespaceFailure):
			// reported there, and the tier keeps its stored entry, if any.
			r.profileNamespaceFailure(ctx, agent, "read or write the "+c.name+" inference gateway key", errors.New(apiFailure(err)))
			carry(c.name)
			continue
		}
		if err != nil {
			log.Info("could not read or write an inference gateway key Secret; the map is left as it is", "class", c.name, "secret", c.secret.String(), "failure", apiFailure(err))
			return r.notWrittenFor("the "+c.name+" key's Secret "+c.secret.String(), err)
		}
		if wasMinted {
			minted = append(minted, c.name)
			r.recordEvent(agent, corev1.EventTypeNormal, reasonInferenceKeyMinted,
				fmt.Sprintf("minted the %s inference gateway key in Secret %s", c.name, c.secret.String()))
		}
		keys[c.name] = apiKey
		if c.name != inferenceKeyClassProbe {
			served = append(served, c)
		}
	}

	// A tier key no bound profile lists any more goes, where its UID is the
	// recorded one, and its record with it. Only with the tiers known and the
	// profile namespace there; a key whose delete fails keeps its record.
	var removed []string
	if tiersKnown && profileNamespaceReady {
		// Only recorded tiers: deleteStaleTierKeys deletes nothing else, so
		// any other candidate would be an uncached read for nothing.
		var gone []string
		removed, gone = r.deleteStaleTierKeys(ctx, agent, recordedTiers(agent, recorded), tiers, recorded)
		if len(gone) > 0 {
			if err := r.updateSecretUIDs(ctx, agent, nil, gone); err != nil {
				log.Info("could not drop the records of removed tier keys; the next pass drops them", "failure", apiFailure(err))
			}
		}
	}
	slices.Sort(removed)

	callerDigests := make(map[string]string, len(keys)+len(carried))
	for digest, class := range carried {
		callerDigests[digest] = class
	}
	for class, apiKey := range keys {
		if class != inferenceKeyClassProbe {
			callerDigests[inferenceKeyDigest(apiKey)] = class
		}
	}
	// Every map the operator writes carries a probe key of its own: a pod
	// still on an older map then refuses the probe, so Served can't be read
	// off a map no pod has loaded. When this pass's map differs from the
	// stored one, the probe key is replaced in its Secret first and the map
	// written after, carrying the new digest alone. A map write that fails
	// leaves a map that differs again on the next pass, which rotates once
	// more and writes it. A probe key minted this pass is already new.
	doc, err := mapWithProbe(callerDigests, keys[inferenceKeyClassProbe])
	if err != nil {
		return notWritten("the caller-key map could not be encoded", false)
	}
	if !slices.Contains(minted, inferenceKeyClassProbe) && (!mapRecorded || string(existingMap.Data[callerKeysFileKey]) != string(doc)) {
		probe := installInferenceKeyClass(agent, inferenceKeyClassProbe)
		fresh, err := r.rotateProbeKey(ctx, agent, probe, recorded[probe.secret.Name])
		if err != nil {
			log.Info("could not replace the probe key; the map is left as it is", "failure", apiFailure(err))
			return r.notWrittenFor("the "+inferenceKeyClassProbe+" key's Secret", err)
		}
		keys[inferenceKeyClassProbe] = fresh
		if doc, err = mapWithProbe(callerDigests, fresh); err != nil {
			return notWritten("the caller-key map could not be encoded", false)
		}
	}
	mapWritten, err := r.writeCallerKeysMap(ctx, agent, existingMap, mapFound, mapRecorded, doc)
	if err != nil {
		log.Info("could not write the caller-key map", "secret", mapKey.String(), "failure", apiFailure(err))
		return r.notWrittenFor("Secret "+mapKey.String(), err)
	}

	if len(minted) > 0 || mapWritten || len(removed) > 0 {
		log.Info("inference gateway caller keys reconciled", "callerClasses", len(served), "minted", minted, "mapWritten", mapWritten, "removedTiers", removed)
	}

	outcome := r.probeInferenceKeys(ctx, agent, &gateway, served, keys, doc)
	// Served: every Ready pod has loaded a map holding each of these keys, so
	// each class's Secret is marked for the key it holds, and its callers get
	// it from the next pass on (inference_key_callers.go).
	if outcome.condition != nil && outcome.condition.Status == metav1.ConditionTrue {
		if r.markInferenceKeysServed(ctx, agent, served, keys) > 0 && (outcome.requeue == 0 || outcome.requeue > inferenceKeysMarkedRecheck) {
			outcome.requeue = inferenceKeysMarkedRecheck
		}
	}
	return outcome
}

// ensureInferenceKey returns c's key, creating its Secret when absent and
// recording it, and repairing the recorded one when it holds no well-formed
// key. A Secret at that name whose UID isn't recordedUID, nor the UID the
// record holds when read again past the cache, is replaced
// (replaceUnrecorded) and its key never read.
func (r *PlatformAgentReconciler) ensureInferenceKey(ctx context.Context, agent *agentv1alpha1.PlatformAgent, c inferenceKeyClass, recordedUID types.UID) (apiKey string, minted bool, err error) {
	existing := &corev1.Secret{}
	err = r.a2aReader().Get(ctx, c.secret, existing)
	if err != nil && !apierrors.IsNotFound(err) {
		return "", false, err
	}
	if err == nil && existing.DeletionTimestamp != nil {
		return "", false, errReservedSecretTerminating
	}
	if err == nil && !inferenceKeySecretIsOurs(existing, agent, c, recordedUID) {
		// The record came from the pass's copy of the agent, which can lag a
		// record this operator just wrote: read it again, uncached, first.
		fresh, ferr := r.freshRecordedSecretUIDs(ctx, agent)
		if ferr != nil {
			return "", false, ferr
		}
		recordedUID = fresh[c.secret.Name]
		if !inferenceKeySecretIsOurs(existing, agent, c, recordedUID) {
			if err := r.replaceUnrecorded(ctx, agent, existing); err != nil {
				return "", false, err
			}
			existing = nil
		}
	}
	if err == nil && existing != nil {
		if current := string(existing.Data[inferenceKeyDataKey]); wellFormedInferenceKey(current) {
			return current, false, nil
		}
		fresh, gerr := newInferenceKey()
		if gerr != nil {
			return "", false, gerr
		}
		if existing.Data == nil {
			existing.Data = map[string][]byte{}
		}
		existing.Data[inferenceKeyDataKey] = []byte(fresh)
		if err := r.Update(ctx, existing); err != nil {
			return "", false, err
		}
		return fresh, true, nil
	}

	fresh, err := newInferenceKey()
	if err != nil {
		return "", false, err
	}
	labels := a2aLabels(agent, inferenceKeyComponent)
	labels[inferenceKeyClassLabel] = c.name
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: c.secret.Name, Namespace: c.secret.Namespace, Labels: labels},
		Type:       corev1.SecretTypeOpaque,
		Data:       map[string][]byte{inferenceKeyDataKey: []byte(fresh)},
	}
	if !c.inProfileNamespace {
		if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
			return "", false, err
		}
	}
	if err := r.createRecorded(ctx, agent, secret); err != nil {
		return "", false, err
	}
	return fresh, true, nil
}

// errReservedSecretTerminating is a Secret at a reserved name that is
// already being deleted: nothing is deleted or reported, and the operator
// creates its own once it's gone.
var errReservedSecretTerminating = errors.New("a Secret at that name is being deleted")

// errFreshRecordUnreadable is a failed uncached read of the record before a
// replacement: nothing is replaced on that pass.
var errFreshRecordUnreadable = errors.New("the PlatformAgent could not be read past the cache to confirm the record")

// freshRecordedSecretUIDs is the record as the API server holds it, read
// past the cache. The pass's copy of the agent can predate a record this
// operator wrote a moment ago, and replacing on that copy would delete the
// operator's own new Secret. The read happens only before a replacement, so
// a steady install never makes it.
func (r *PlatformAgentReconciler) freshRecordedSecretUIDs(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (map[string]types.UID, error) {
	live := &agentv1alpha1.PlatformAgent{}
	if err := r.a2aReader().Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		return nil, fmt.Errorf("%w: %w", errFreshRecordUnreadable, err)
	}
	return recordedSecretUIDs(live), nil
}

// replaceUnrecorded deletes existing, a Secret at a name reserved for the
// operator whose UID isn't the recorded one, with that UID as a
// precondition so exactly the object read goes, and writes a Warning Event
// naming it. The caller then creates the operator's own. A Secret gone
// already is not an error; one replaced since the read is, and the next
// pass looks again.
func (r *PlatformAgentReconciler) replaceUnrecorded(ctx context.Context, agent *agentv1alpha1.PlatformAgent, existing *corev1.Secret) error {
	uid := existing.UID
	if err := r.Delete(ctx, existing, client.Preconditions{UID: &uid}); client.IgnoreNotFound(err) != nil {
		return err
	}
	name := existing.Namespace + "/" + existing.Name
	logf.FromContext(ctx).Info("replaced a Secret at a name reserved for the operator's inference gateway keys", "secret", name)
	r.recordEvent(agent, corev1.EventTypeWarning, reasonInferenceKeySecretReplaced,
		fmt.Sprintf("replaced Secret %s: the name is reserved for the operator's inference gateway keys, and this Secret was not one the operator recorded creating", name))
	return nil
}

// inferenceKeySecretIsOurs reports whether s is the key Secret for c this
// agent created: its UID is the recorded one, and it carries c's class
// label (and, in the profile namespace, the agent's labels).
func inferenceKeySecretIsOurs(s *corev1.Secret, agent *agentv1alpha1.PlatformAgent, c inferenceKeyClass, recordedUID types.UID) bool {
	if recordedUID == "" || s.UID != recordedUID || s.Labels[inferenceKeyClassLabel] != c.name {
		return false
	}
	return !c.inProfileNamespace || renderedByAgent(s, agent, inferenceKeyComponent)
}

// mapWithProbe is keys.json for the caller digests and the probe key.
func mapWithProbe(callerDigests map[string]string, probeAPIKey string) ([]byte, error) {
	entries := make(map[string]string, len(callerDigests)+1)
	for digest, class := range callerDigests {
		entries[digest] = class
	}
	entries[inferenceKeyDigest(probeAPIKey)] = inferenceKeyClassProbe
	return callerKeysDocument(entries)
}

// rotateProbeKey writes a fresh key into the recorded probe key Secret, in
// place, and returns it. Only the operator's own recorded Secret is written.
func (r *PlatformAgentReconciler) rotateProbeKey(ctx context.Context, agent *agentv1alpha1.PlatformAgent, c inferenceKeyClass, recordedUID types.UID) (string, error) {
	existing := &corev1.Secret{}
	if err := r.a2aReader().Get(ctx, c.secret, existing); err != nil {
		return "", err
	}
	if !inferenceKeySecretIsOurs(existing, agent, c, recordedUID) {
		return "", fmt.Errorf("secret %s changed under the pass", c.secret.String())
	}
	fresh, err := newInferenceKey()
	if err != nil {
		return "", err
	}
	if existing.Data == nil {
		existing.Data = map[string][]byte{}
	}
	existing.Data[inferenceKeyDataKey] = []byte(fresh)
	if err := r.Update(ctx, existing); err != nil {
		return "", err
	}
	return fresh, nil
}

// recordedTiers is the tiers whose key Secret names the record holds.
func recordedTiers(agent *agentv1alpha1.PlatformAgent, recorded map[string]types.UID) []string {
	prefix := inferenceKeySecretName(agent, inferenceKeyTierClassPrefix)
	var out []string
	for name := range recorded {
		if tier, ok := strings.CutPrefix(name, prefix); ok && tier != "" {
			out = append(out, tier)
		}
	}
	slices.Sort(out)
	return out
}

// writeCallerKeysMap writes doc as the map: an update of the recorded map
// Secret, else a recorded create, replacing first a map Secret at the name
// that isn't the recorded one. It reports whether it wrote.
func (r *PlatformAgentReconciler) writeCallerKeysMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent, existing *corev1.Secret, found, recorded bool, doc []byte) (bool, error) {
	if found && existing.DeletionTimestamp != nil {
		return false, errReservedSecretTerminating
	}
	if found && !recorded {
		// As for a key Secret: the uncached record decides, not the pass's copy.
		fresh, err := r.freshRecordedSecretUIDs(ctx, agent)
		if err != nil {
			return false, err
		}
		if uid := fresh[callerKeysSecretName]; uid == "" || existing.UID != uid {
			if err := r.replaceUnrecorded(ctx, agent, existing); err != nil {
				return false, err
			}
			found = false
		}
	}
	if found {
		if string(existing.Data[callerKeysFileKey]) == string(doc) {
			return false, nil
		}
		if existing.Data == nil {
			existing.Data = map[string][]byte{}
		}
		existing.Data[callerKeysFileKey] = doc
		return true, r.Update(ctx, existing)
	}
	secret := &corev1.Secret{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
		ObjectMeta: metav1.ObjectMeta{Name: callerKeysSecretName, Namespace: agent.Namespace, Labels: a2aLabels(agent, callerKeysComponent)},
		Type:       corev1.SecretTypeOpaque,
		Data:       map[string][]byte{callerKeysFileKey: doc},
	}
	if err := ctrl.SetControllerReference(agent, secret, r.Scheme); err != nil {
		return false, err
	}
	return true, r.createRecorded(ctx, agent, secret)
}

// deleteStaleTierKeys deletes the tier key Secret of every candidate tier
// not in keep, where its UID is the recorded one, and returns the classes it
// removed and the names whose records can go: the deleted Secrets and the
// recorded ones already gone. A failure is confined to the profile
// namespace and is reported there; its record stays.
func (r *PlatformAgentReconciler) deleteStaleTierKeys(ctx context.Context, agent *agentv1alpha1.PlatformAgent, candidates, keep []string, recorded map[string]types.UID) (removed, gone []string) {
	seen := map[string]bool{}
	for _, tier := range candidates {
		if seen[tier] || slices.Contains(keep, tier) {
			continue
		}
		seen[tier] = true
		c := tierInferenceKeyClass(agent, tier)
		existing := &corev1.Secret{}
		_, isRecorded := recorded[c.secret.Name]
		if err := r.a2aReader().Get(ctx, c.secret, existing); err != nil {
			if apierrors.IsNotFound(err) && isRecorded {
				gone = append(gone, c.secret.Name)
			} else if !apierrors.IsNotFound(err) {
				r.profileNamespaceFailure(ctx, agent, "read the "+c.name+" inference gateway key to remove it", errors.New(apiFailure(err)))
			}
			continue
		}
		if !inferenceKeySecretIsOurs(existing, agent, c, recorded[c.secret.Name]) {
			continue
		}
		if err := r.Delete(ctx, existing, client.Preconditions{UID: &existing.UID}); client.IgnoreNotFound(err) != nil {
			r.profileNamespaceFailure(ctx, agent, "remove the "+c.name+" inference gateway key", errors.New(apiFailure(err)))
			continue
		}
		removed = append(removed, c.name)
		gone = append(gone, c.secret.Name)
	}
	return removed, gone
}

// removeInferenceKeysOnDeletion deletes the tier key Secrets, which no owner
// reference reclaims, while the finalizer holds the PlatformAgent: those
// status.inferenceKeys records, each only at its recorded UID. Under the
// profile-namespace rule a failure is reported and never holds the
// finalizer; the chart's uninstall deletes the profile namespace and
// anything left in it.
func (r *PlatformAgentReconciler) removeInferenceKeysOnDeletion(ctx context.Context, agent *agentv1alpha1.PlatformAgent) {
	r.inferenceKeysServedFor.Delete(client.ObjectKeyFromObject(agent))
	recorded := recordedSecretUIDs(agent)
	r.deleteStaleTierKeys(ctx, agent, recordedTiers(agent, recorded), nil, recorded)
}

// gatewayOwnsPod is a filter on which pods the probe asks, not a check of
// who they are: pod's controller
// reference names a ReplicaSet in the namespace whose controller is gateway,
// both matched by UID, and the pod isn't on the host network. It drops pods
// that merely carry the gateway's labels, such as the replay-shadow proxy or
// a leftover. It does not establish that a pod is the gateway: a pod's
// creator writes its owner references, and a ReplicaSet's UID is readable,
// so a pod can name the real ReplicaSet. That is why the probe sends only
// the probe key (probeKey), which reaches no model. replicaSets caches this pass's reads by name;
// ReplicaSets are read uncached, as a2aCalloutHasOlderPods reads them, on
// the operator's existing read grant.
func (r *PlatformAgentReconciler) gatewayOwnsPod(ctx context.Context, gateway *appsv1.Deployment, pod *corev1.Pod, replicaSets map[string]*appsv1.ReplicaSet) (bool, error) {
	if pod.Spec.HostNetwork {
		return false, nil
	}
	owner := metav1.GetControllerOf(pod)
	if owner == nil || owner.Kind != replicaSetKind || owner.APIVersion != appsv1.SchemeGroupVersion.String() {
		return false, nil
	}
	rs, seen := replicaSets[owner.Name]
	if !seen {
		rs = &appsv1.ReplicaSet{}
		switch err := r.a2aReader().Get(ctx, types.NamespacedName{Namespace: pod.Namespace, Name: owner.Name}, rs); {
		case apierrors.IsNotFound(err):
			rs = nil
		case err != nil:
			return false, err
		}
		replicaSets[owner.Name] = rs
	}
	if rs == nil || rs.UID != owner.UID {
		return false, nil
	}
	return metav1.IsControlledBy(rs, gateway), nil
}

// probeInferenceKeys asks every Ready gateway pod, by pod IP, what it
// answers to the key probeKey picks, side by side under one deadline. Pods
// are filtered by gatewayOwnsPod first. classes are the caller classes the
// map carries beside the probe key, for the message. Every pod answering
// 200 is Served: the probe key is new with the map this pass wrote or
// found, so a pod that admits it has loaded that map. A pod answering
// anything else is NotServed, which names the pod and the status; a pod
// that doesn't answer is ProbeIncomplete. A gateway without the caller-auth
// hook admits any key, so it answers 200, which is right: it would serve a
// caller sending its key. A pass whose map and Ready pods are the ones the
// last Served pass probed sends nothing and stays Served.
func (r *PlatformAgentReconciler) probeInferenceKeys(ctx context.Context, agent *agentv1alpha1.PlatformAgent, gateway *appsv1.Deployment, classes []inferenceKeyClass, keys map[string]string, doc []byte) inferenceKeysOutcome {
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(litellmPodSelector())); err != nil {
		return inferenceKeysOutcome{
			condition: inferenceKeysCondition(metav1.ConditionUnknown, inferenceKeysReasonProbeFailed, "the inference gateway pods could not be listed: "+apiFailure(err)),
			requeue:   inferenceKeysRecheck,
		}
	}
	var ready []*corev1.Pod
	replicaSets := map[string]*appsv1.ReplicaSet{}
	for i := range pods.Items {
		pod := &pods.Items[i]
		// The LiteLLM container, Ready, on a running pod with an address:
		// the replay-shadow proxy under the same app label is not a gateway.
		if pod.DeletionTimestamp != nil || pod.Status.Phase != corev1.PodRunning || pod.Status.PodIP == "" ||
			!usagePodHasContainer(pod, litellmContainerName) || !usageContainerReady(pod, litellmContainerName) {
			continue
		}
		owned, err := r.gatewayOwnsPod(ctx, gateway, pod, replicaSets)
		if err != nil {
			return inferenceKeysOutcome{
				condition: inferenceKeysCondition(metav1.ConditionUnknown, inferenceKeysReasonProbeFailed, "the inference gateway's ReplicaSets could not be read: "+apiFailure(err)),
				requeue:   inferenceKeysUnreachableRecheck,
			}
		}
		if owned {
			ready = append(ready, pod)
		}
	}
	if len(ready) == 0 {
		// Pods aren't watched, so the pass that sees one turn Ready is asked for.
		return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionUnknown, inferenceKeysReasonNoReadyGateway,
			"no inference gateway pod is Ready, so none can say whether it has loaded the caller keys"), requeue: inferenceKeysUnreachableRecheck}
	}
	slices.SortFunc(ready, func(a, b *corev1.Pod) int { return strings.Compare(a.Name, b.Name) })
	names := make([]string, len(classes))
	for i, c := range classes {
		names[i] = c.name
	}
	servedMessage := fmt.Sprintf("every Ready inference gateway pod (%d) admits the %s key, written in the same map as the keys of classes %s",
		len(ready), inferenceKeyClassProbe, strings.Join(names, ", "))

	fingerprint := inferenceKeysFingerprint(doc, ready)
	agentKey := client.ObjectKeyFromObject(agent)
	if last, ok := r.inferenceKeysServedFor.Load(agentKey); ok && last == fingerprint {
		return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionTrue, inferenceKeysReasonServed, servedMessage)}
	}

	prober := r.inferenceKeyProbe
	if prober == nil {
		prober = defaultInferenceKeyProber
	}
	type answer struct {
		status int
		err    error
	}
	answers := make([]answer, len(ready))
	pctx, cancel := context.WithTimeout(ctx, inferenceKeyProbeTimeout)
	defer cancel()
	sent := probeKey(keys)
	var wg sync.WaitGroup
	for i, pod := range ready {
		wg.Add(1)
		go func(i int, addr string) {
			defer wg.Done()
			status, err := prober.Probe(pctx, addr, sent)
			answers[i] = answer{status, err}
		}(i, net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(litellmPort))))
	}
	wg.Wait()

	var refusedBy, unanswered string
	for i, pod := range ready {
		a := answers[i]
		switch {
		case a.err != nil:
			if unanswered == "" {
				unanswered = fmt.Sprintf("inference gateway pod %s did not answer the %s key's probe (%s)", pod.Name, inferenceKeyClassProbe, a.err.Error())
			}
		case a.status != http.StatusOK && refusedBy == "":
			refusedBy = fmt.Sprintf("inference gateway pod %s answered HTTP %d to the %s key; a pod loads a new map when the kubelet refreshes its mounted %s Secret",
				pod.Name, a.status, inferenceKeyClassProbe, callerKeysSecretName)
		}
	}
	switch {
	case refusedBy != "":
		r.inferenceKeysServedFor.Delete(agentKey)
		return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionFalse, inferenceKeysReasonNotServed, refusedBy), requeue: inferenceKeysRecheck}
	case unanswered != "":
		r.inferenceKeysServedFor.Delete(agentKey)
		return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionUnknown, inferenceKeysReasonProbeFailed, unanswered), requeue: inferenceKeysUnreachableRecheck}
	}
	r.inferenceKeysServedFor.Store(agentKey, fingerprint)
	return inferenceKeysOutcome{condition: inferenceKeysCondition(metav1.ConditionTrue, inferenceKeysReasonServed, servedMessage)}
}

// probeKey is the one key a probe ever sends: the gateway-probe key, never
// a caller class's. It is the one place that choice is made.
func probeKey(keys map[string]string) string {
	return keys[inferenceKeyClassProbe]
}

// inferenceKeysFingerprint identifies what a Served verdict was about: the
// map's bytes and the Ready pods, by UID and address. A pod restarted in
// place mounts the current map, so a container restart needs no new probe.
func inferenceKeysFingerprint(doc []byte, pods []*corev1.Pod) string {
	h := sha256.New()
	h.Write(doc)
	for _, pod := range pods {
		h.Write([]byte("\n" + string(pod.UID) + "@" + pod.Status.PodIP))
	}
	return hex.EncodeToString(h.Sum(nil))
}

// syncInferenceKeysServed writes the InferenceKeysServed condition on the
// way out of Reconcile, as syncDispatcherCeiling does: nil leaves it as it
// was, inferenceKeysRemoved takes it away, anything else is its new value.
//
// Step 12a's record write moves the stored PlatformAgent's version on the
// render copy, and agent (the pass's instance) takes that version only when
// the A2A step succeeds. A pass that fails between them would see its own
// write as a conflict here. On a conflict the change alone is applied to the
// PlatformAgent as read past the cache, with a bounded retry; agent's
// version is left as it is, so the writers after this one behave as before.
func (r *PlatformAgentReconciler) syncInferenceKeysServed(ctx context.Context, agent *agentv1alpha1.PlatformAgent, verdict *metav1.Condition) error {
	if verdict == nil {
		return nil
	}
	var condition metav1.Condition
	if verdict != inferenceKeysRemoved {
		condition = *verdict
		condition.ObservedGeneration = agent.Generation
		condition.LastTransitionTime = metav1.Now()
	}
	apply := func(a *agentv1alpha1.PlatformAgent) bool {
		if verdict == inferenceKeysRemoved {
			return meta.RemoveStatusCondition(&a.Status.Conditions, inferenceKeysServedCondition)
		}
		return meta.SetStatusCondition(&a.Status.Conditions, condition)
	}
	if !apply(agent) {
		return nil
	}
	err := r.Status().Update(ctx, agent)
	if !apierrors.IsConflict(err) {
		return err
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		live := &agentv1alpha1.PlatformAgent{}
		if err := r.a2aReader().Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
			return err
		}
		if !apply(live) {
			return nil
		}
		return r.Status().Update(ctx, live)
	})
}
