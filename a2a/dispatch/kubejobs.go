package dispatch

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"strings"
	"sync"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/utils/ptr"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// jobTTL is how long a finished Job stays for its logs before the TTL
// controller removes it, if the dispatcher hasn't deleted it first.
const jobTTL = 300

// jobNameLabel is the label the Job controller puts on its pods.
const jobNameLabel = "batch.kubernetes.io/job-name"

// Annotations on a dispatcher Job, for whoever reads it: the task it runs.
const (
	jobProfileAnnotation = "kubeagents.x-k8s.io/dispatch-profile"
	jobTierAnnotation    = "kubeagents.x-k8s.io/dispatch-tier"
	jobTaskAnnotation    = "kubeagents.x-k8s.io/dispatch-task"
	jobKindAnnotation    = "kubeagents.x-k8s.io/dispatch-kind"
)

// KubeJobs is the binary's JobRunner: one Job per unattended task in the
// profile namespace, from the profile's Job template. The template is the
// DispatchJobTemplate the config names (JobTemplateRef), read when the Job is
// launched, or the copy inline in the config from an operator that predates
// the reference. It is used exactly as the operator rendered it, since the
// Job admission policy admits only that; the Job gets a name, the config's
// jobLabels, and the shape the policy requires (backoffLimit 0, one pod, a
// deadline, a TTL). The pod the task is offered to is the Job's own, by
// name, so the confirm's credentials bind to that pod, never to the Job.
type KubeJobs struct {
	kube   kubernetes.Interface
	config func() lib.DispatchConfig
	// getTemplate reads a DispatchJobTemplate's JSON; getJobTemplate by
	// default, a stand-in in tests.
	getTemplate func(ctx context.Context, namespace, name string) ([]byte, error)
	// launched is each Job this process made or found and checked at Launch,
	// by name: the ServiceAccount its template runs as, which Pod holds the
	// Job's pod to. Pod refuses a Job Launch didn't check.
	mu       sync.Mutex
	launched map[string]string
}

// NewKubeJobs returns a JobRunner reading the current config from config.
func NewKubeJobs(kube kubernetes.Interface, config func() lib.DispatchConfig) *KubeJobs {
	k := &KubeJobs{kube: kube, config: config, launched: map[string]string{}}
	k.getTemplate = k.getJobTemplate
	return k
}

// The DispatchJobTemplate's API group path and resource. The CRD's Go type
// lives in the operator's module, so it is read as JSON.
const (
	dispatchJobTemplateGroupPath = "/apis/kubeagents.x-k8s.io/v1alpha1"
	dispatchJobTemplateResource  = "dispatchjobtemplates"
)

// ErrJobTemplateNotCurrent marks a Launch whose profile's DispatchJobTemplate
// is missing or holds a rendering other than the one the config names: an
// operator pass may be under way. The task stays queued.
var ErrJobTemplateNotCurrent = errors.New("the profile's DispatchJobTemplate is not the rendering the dispatch config names")

func (k *KubeJobs) getJobTemplate(ctx context.Context, namespace, name string) ([]byte, error) {
	return k.kube.Discovery().RESTClient().Get().AbsPath(dispatchJobTemplateGroupPath).
		Namespace(namespace).Resource(dispatchJobTemplateResource).Name(name).DoRaw(ctx)
}

// unattendedModeTier is the tier whose template is prof's own param
// (lib.DispatchJobTemplateName): its unattended-mode tier, a Tier resource's
// when the config names one, else the built-in.
func unattendedModeTier(prof *lib.DispatchProfile) string {
	if prof.UnattendedTier != "" {
		return prof.UnattendedTier
	}
	return DefaultUnattendedTier
}

// jobTemplateName is the param that holds prof's template at tier, derived
// from (profile, tier), never read from the config, so a config can't point
// a task at another profile's or another tier's template: the profile's own
// param for its unattended-mode tier (and for any tier, under a reference
// that isn't per tier, which covers that one tier alone), else the tier's
// (#5646).
func jobTemplateName(prof *lib.DispatchProfile, tier string) string {
	if !prof.JobTemplatesPerTier() || tier == unattendedModeTier(prof) {
		return lib.DispatchJobTemplateName(prof.Name)
	}
	return lib.DispatchJobTierTemplateName(prof.Name, tier)
}

// jobTemplate is prof's pod template at tier: from the DispatchJobTemplate
// its reference names, only while that object carries the reference's hash,
// or from the inline copy an older operator sends.
func (k *KubeJobs) jobTemplate(ctx context.Context, namespace string, prof *lib.DispatchProfile, tier string) (json.RawMessage, error) {
	ref := prof.JobTemplateRef
	if ref == nil {
		return prof.JobTemplate, nil
	}
	// A profile's template is its own param: a reference to another
	// profile's is refused rather than followed.
	// A hash-only reference (#5479) names the profile's own param.
	if own := ref.NameFor(prof.Name); own != lib.DispatchJobTemplateName(prof.Name) {
		return nil, fmt.Errorf("profile %s's jobTemplateRef names %s, not its own param", prof.Name, own)
	}
	name := jobTemplateName(prof, tier)
	raw, err := k.getTemplate(ctx, namespace, name)
	if apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("%w: profile %s's %s is not there", ErrJobTemplateNotCurrent, prof.Name, name)
	}
	if err != nil {
		return nil, fmt.Errorf("read profile %s's Job template %s: %w", prof.Name, name, err)
	}
	var obj struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
		Spec struct {
			Template json.RawMessage `json:"template"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, fmt.Errorf("profile %s's Job template %s: %w", prof.Name, name, err)
	}
	if got := obj.Metadata.Annotations[lib.DispatchJobTemplateHashAnnotation]; got != ref.Hash {
		return nil, fmt.Errorf("%w: profile %s's %s holds rendering %q, the config names %q", ErrJobTemplateNotCurrent, prof.Name, name, got, ref.Hash)
	}
	// Checked here, per profile, where the inline copy is checked in the
	// config's Validate: a bad param holds its own profile's tasks, not
	// every profile's config.
	if !lib.PodTemplateWithContainers(obj.Spec.Template) {
		return nil, fmt.Errorf("%w: profile %s's %s holds no pod template with containers", ErrJobTemplateNotCurrent, prof.Name, name)
	}
	return obj.Spec.Template, nil
}

// JobName is the task's Job name: a hash of its whole key, kind included, so
// it is the same after a restart, fits a label value, carries nothing a
// caller chose, and no two held tasks share one (#5646). The key is hashed as
// a JSON array, so no two keys encode alike whatever their fields hold.
func JobName(profile, tier, kind, taskID string) string {
	raw, _ := json.Marshal([]string{profile, tier, kind, taskID})
	sum := sha256.Sum256(raw)
	return "dispatch-" + hex.EncodeToString(sum[:10])
}

// validJobKey says every part of a task's key is a subject token, the only
// keys a Job is made, found or deleted for. The lib client refuses to publish
// a submission whose profile, tier or task id isn't one (Client.PublishSeq), and
// only the gateway may publish submissions, but ParseSubmitSubject, which the
// dispatcher keys a task by, checks only the kind, so the key is checked here
// again before it names a Job.
func validJobKey(profile, tier, kind, taskID string) error {
	for _, part := range []string{profile, tier, kind, taskID} {
		if !lib.ValidSubjectToken(part) {
			return fmt.Errorf("the task key %q/%q/%q/%q has a part that is not a subject token, so it names no Job", profile, tier, kind, taskID)
		}
	}
	return nil
}

// ErrJobNotOurs marks a Job under a task's name that isn't the one this
// dispatcher would have made for it: no dispatcher marker, annotations naming
// another task, or a pod template running as another ServiceAccount. It is
// never offered the task; the task stays queued.
var ErrJobNotOurs = errors.New("the Job under the task's name is not the dispatcher's Job for that task")

// ourJob says whether job is the dispatcher's Job for the task, with a pod
// template running as sa.
func ourJob(job *batchv1.Job, profile, tier, kind, taskID, sa string) error {
	a := job.Annotations
	switch {
	case job.Labels[lib.DispatchJobLabel] != "true":
		return fmt.Errorf("%w: %s has no %s label", ErrJobNotOurs, job.Name, lib.DispatchJobLabel)
	case a[jobProfileAnnotation] != profile || a[jobTierAnnotation] != tier || a[jobKindAnnotation] != kind || a[jobTaskAnnotation] != taskID:
		return fmt.Errorf("%w: %s is annotated for %q/%q/%q/%q", ErrJobNotOurs, job.Name,
			a[jobProfileAnnotation], a[jobTierAnnotation], a[jobKindAnnotation], a[jobTaskAnnotation])
	case sa == "":
		return fmt.Errorf("%w: %s wasn't launched or checked by this dispatcher", ErrJobNotOurs, job.Name)
	case job.Spec.Template.Spec.ServiceAccountName != sa:
		return fmt.Errorf("%w: %s runs as %q, its template as %q", ErrJobNotOurs, job.Name, job.Spec.Template.Spec.ServiceAccountName, sa)
	}
	return nil
}

func (k *KubeJobs) remember(name, sa string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.launched[name] = sa
}

func (k *KubeJobs) launchedAs(name string) (string, bool) {
	k.mu.Lock()
	defer k.mu.Unlock()
	sa, ok := k.launched[name]
	return sa, ok
}

func (k *KubeJobs) forget(name string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	delete(k.launched, name)
}

// legacyJobName is the name a dispatcher from before the kind was in it gave
// the task's Job, its key joined with slashes. Delete removes a Job under it
// only when the Job's own annotations say it is this task's; to go a release
// on.
func legacyJobName(profile, tier, taskID string) string {
	sum := sha256.Sum256([]byte(profile + "/" + tier + "/" + taskID))
	return "dispatch-" + hex.EncodeToString(sum[:10])
}

// ErrJobRefusedByPolicy marks a Launch the dispatcher Job admission policy
// refused: the Job, built from the operator's template, didn't match the
// policy's copy of it.
var ErrJobRefusedByPolicy = errors.New("the dispatcher Job admission policy refused the Job")

// jobPolicyName is the dispatcher Job admission policy's name, as its
// refusals name it.
const jobPolicyName = "kube-agents-a2a-dispatcher-job"

func (k *KubeJobs) Launch(ctx context.Context, profile, tier, kind, taskID string) (time.Time, error) {
	if err := validJobKey(profile, tier, kind, taskID); err != nil {
		return time.Time{}, err
	}
	cfg := k.config()
	var prof *lib.DispatchProfile
	for i := range cfg.Profiles {
		if cfg.Profiles[i].Name == profile {
			prof = &cfg.Profiles[i]
		}
	}
	if prof == nil || !prof.HasJobTemplate(tier) {
		return time.Time{}, fmt.Errorf("profile %s has no Job template for tier %s in the dispatch config", profile, tier)
	}
	tmpl, err := k.jobTemplate(ctx, cfg.ProfileNamespace, prof, tier)
	if err != nil {
		return time.Time{}, err
	}
	job, err := buildJob(cfg, prof, tmpl, tier, kind, taskID)
	if err != nil {
		return time.Time{}, err
	}
	made, err := k.kube.BatchV1().Jobs(cfg.ProfileNamespace).Create(ctx, job, metav1.CreateOptions{})
	switch {
	case err == nil:
		k.remember(job.Name, job.Spec.Template.Spec.ServiceAccountName)
		return made.CreationTimestamp.Time, nil
	case apierrors.IsAlreadyExists(err):
		found, err := k.kube.BatchV1().Jobs(cfg.ProfileNamespace).Get(ctx, job.Name, metav1.GetOptions{})
		if err != nil {
			return time.Time{}, fmt.Errorf("read the task's existing Job: %w", err)
		}
		// Found by name only: adopted only if it is the Job this task's
		// would be, so no permit is minted for a pod someone else shaped.
		if err := ourJob(found, profile, tier, kind, taskID, job.Spec.Template.Spec.ServiceAccountName); err != nil {
			return time.Time{}, err
		}
		k.remember(job.Name, job.Spec.Template.Spec.ServiceAccountName)
		return found.CreationTimestamp.Time, nil
	case apierrors.IsForbidden(err) && strings.Contains(err.Error(), jobPolicyName):
		return time.Time{}, fmt.Errorf("%w: %v", ErrJobRefusedByPolicy, err)
	}
	return time.Time{}, err
}

// buildJob is the task's Job from cfg and prof's template. The template is
// not touched. It must be the (profile, tier)'s own, by the worker labels the
// operator renders on it: a template for another tier runs as that tier's
// ServiceAccount, which the admission policy would admit (#5646).
func buildJob(cfg lib.DispatchConfig, prof *lib.DispatchProfile, template json.RawMessage, tier, kind, taskID string) (*batchv1.Job, error) {
	profile := prof.Name
	var tmpl corev1.PodTemplateSpec
	if err := json.Unmarshal(template, &tmpl); err != nil {
		return nil, fmt.Errorf("profile %s's Job template: %w", profile, err)
	}
	if got := tmpl.Labels[lib.ProfileTierLabel]; got != tier || tmpl.Labels[lib.ProfileNameLabel] != profile {
		return nil, fmt.Errorf("%w: profile %s's Job template for tier %s is labelled profile %q, tier %q",
			ErrJobTemplateNotCurrent, profile, tier, tmpl.Labels[lib.ProfileNameLabel], got)
	}
	labels := maps.Clone(cfg.JobLabels)
	if labels == nil {
		labels = map[string]string{}
	}
	labels[lib.DispatchJobLabel] = "true"
	// A backstop past the dispatcher's own deadline, which it enforces from
	// the task's start: the pod's start and the stop grace on top.
	deadline := int64(max(prof.ActiveDeadlineSeconds, 1)) + int64(jobStart(cfg).Seconds()) + int64(stopGrace(cfg).Seconds())
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      JobName(profile, tier, kind, taskID),
			Namespace: cfg.ProfileNamespace,
			Labels:    labels,
			Annotations: map[string]string{
				jobProfileAnnotation: profile,
				jobTierAnnotation:    tier,
				jobKindAnnotation:    kind,
				jobTaskAnnotation:    taskID,
			},
		},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(0)),
			ActiveDeadlineSeconds:   ptr.To(deadline),
			TTLSecondsAfterFinished: ptr.To(int32(jobTTL)),
			Template:                tmpl,
		},
	}, nil
}

func (k *KubeJobs) Pod(ctx context.Context, profile, tier, kind, taskID string) (string, bool, error) {
	if err := validJobKey(profile, tier, kind, taskID); err != nil {
		return "", false, err
	}
	ns, name := k.config().ProfileNamespace, JobName(profile, tier, kind, taskID)
	job, err := k.kube.BatchV1().Jobs(ns).Get(ctx, name, metav1.GetOptions{})
	switch {
	case apierrors.IsNotFound(err):
		return "", true, nil
	case err != nil:
		return "", false, err
	case job.DeletionTimestamp != nil:
		return "", true, nil
	}
	// Read again by name: the Job may have been replaced since Launch
	// checked it. A Job Launch never checked has no expected ServiceAccount
	// (""), which ourJob refuses.
	sa, _ := k.launchedAs(name)
	if err := ourJob(job, profile, tier, kind, taskID, sa); err != nil {
		return "", false, err
	}
	for _, c := range job.Status.Conditions {
		// Failed, or already finished: a Job's one pod ran without this
		// dispatcher's confirm (an abandoned offer it exited on), so it will
		// never take the task.
		switch c.Type {
		case batchv1.JobFailed, batchv1.JobFailureTarget, batchv1.JobComplete, batchv1.JobSuccessCriteriaMet:
			if c.Status == corev1.ConditionTrue {
				return "", true, nil
			}
		}
	}
	pods, err := k.kube.CoreV1().Pods(ns).List(ctx, metav1.ListOptions{LabelSelector: jobNameLabel + "=" + name})
	if err != nil {
		return "", false, err
	}
	for i := range pods.Items {
		p := &pods.Items[i]
		if !metav1.IsControlledBy(p, job) {
			continue
		}
		if p.Status.Phase == corev1.PodFailed || p.Status.Phase == corev1.PodSucceeded {
			return "", true, nil
		}
		if p.Spec.ServiceAccountName != sa {
			return "", false, fmt.Errorf("%w: pod %s runs as %q, its template as %q", ErrJobNotOurs, p.Name, p.Spec.ServiceAccountName, sa)
		}
		if p.DeletionTimestamp == nil && podReady(p) && lib.ValidSubjectToken(p.Name) {
			return p.Name, false, nil
		}
	}
	return "", false, nil
}

func (k *KubeJobs) Delete(ctx context.Context, profile, tier, kind, taskID string) error {
	if err := validJobKey(profile, tier, kind, taskID); err != nil {
		return err
	}
	jobs := k.kube.BatchV1().Jobs(k.config().ProfileNamespace)
	k.forget(JobName(profile, tier, kind, taskID))
	err := jobs.Delete(ctx, JobName(profile, tier, kind, taskID),
		metav1.DeleteOptions{PropagationPolicy: ptr.To(metav1.DeletePropagationBackground)})
	if err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return k.deleteLegacy(ctx, profile, tier, kind, taskID)
}

// deleteLegacy deletes the Job a dispatcher from before the kind was in the
// name launched for this task, and only that one: the Job under the legacy
// name must say, by its own annotations, that it is this (profile, tier,
// task)'s, and of this kind if it names one (an older Job names none), and
// the delete is held to the UID that was read, so a Job made under the name
// meanwhile is left alone.
func (k *KubeJobs) deleteLegacy(ctx context.Context, profile, tier, kind, taskID string) error {
	jobs := k.kube.BatchV1().Jobs(k.config().ProfileNamespace)
	old, err := jobs.Get(ctx, legacyJobName(profile, tier, taskID), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	// TODO(#6166): remove deleteLegacy a release after #5646.
	a := old.Annotations
	if a[jobProfileAnnotation] != profile || a[jobTierAnnotation] != tier || a[jobTaskAnnotation] != taskID {
		return nil
	}
	// An older Job names no kind; a dispatcher from before #5646 ran
	// top-level tasks on Jobs under that name, and a child's cleanup mustn't
	// take one.
	if got := a[jobKindAnnotation]; (got == "" && kind != lib.SubmitKindTop) || (got != "" && got != kind) {
		return nil
	}
	err = jobs.Delete(ctx, old.Name, metav1.DeleteOptions{
		PropagationPolicy: ptr.To(metav1.DeletePropagationBackground),
		Preconditions:     &metav1.Preconditions{UID: &old.UID},
	})
	if apierrors.IsNotFound(err) || apierrors.IsConflict(err) {
		return nil
	}
	return err
}
