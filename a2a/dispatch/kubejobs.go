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
}

// NewKubeJobs returns a JobRunner reading the current config from config.
func NewKubeJobs(kube kubernetes.Interface, config func() lib.DispatchConfig) *KubeJobs {
	k := &KubeJobs{kube: kube, config: config}
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

// jobTemplate is prof's pod template: from the DispatchJobTemplate its
// reference names, only while that object carries the reference's hash, or
// from the inline copy an older operator sends.
func (k *KubeJobs) jobTemplate(ctx context.Context, namespace string, prof *lib.DispatchProfile) (json.RawMessage, error) {
	ref := prof.JobTemplateRef
	if ref == nil {
		return prof.JobTemplate, nil
	}
	// A profile's template is its own param: a reference to another
	// profile's is refused rather than followed.
	// A hash-only reference (#5479) names the profile's own param.
	name := ref.NameFor(prof.Name)
	if name != lib.DispatchJobTemplateName(prof.Name) {
		return nil, fmt.Errorf("profile %s's jobTemplateRef names %s, not its own param", prof.Name, name)
	}
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
		return nil, fmt.Errorf("profile %s's %s holds no pod template with containers", prof.Name, name)
	}
	return obj.Spec.Template, nil
}

// JobName is the task's Job name: a hash of its key, so it is the same after
// a restart, fits a label value, and carries nothing a caller chose.
func JobName(profile, tier, taskID string) string {
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

func (k *KubeJobs) Launch(ctx context.Context, profile, tier, taskID string) (time.Time, error) {
	cfg := k.config()
	var prof *lib.DispatchProfile
	for i := range cfg.Profiles {
		if cfg.Profiles[i].Name == profile {
			prof = &cfg.Profiles[i]
		}
	}
	if prof == nil || !prof.HasJobTemplate() {
		return time.Time{}, fmt.Errorf("profile %s has no Job template in the dispatch config", profile)
	}
	tmpl, err := k.jobTemplate(ctx, cfg.ProfileNamespace, prof)
	if err != nil {
		return time.Time{}, err
	}
	job, err := buildJob(cfg, prof, tmpl, tier, taskID)
	if err != nil {
		return time.Time{}, err
	}
	made, err := k.kube.BatchV1().Jobs(cfg.ProfileNamespace).Create(ctx, job, metav1.CreateOptions{})
	switch {
	case err == nil:
		return made.CreationTimestamp.Time, nil
	case apierrors.IsAlreadyExists(err):
		found, err := k.kube.BatchV1().Jobs(cfg.ProfileNamespace).Get(ctx, job.Name, metav1.GetOptions{})
		if err != nil {
			return time.Time{}, fmt.Errorf("read the task's existing Job: %w", err)
		}
		return found.CreationTimestamp.Time, nil
	case apierrors.IsForbidden(err) && strings.Contains(err.Error(), jobPolicyName):
		return time.Time{}, fmt.Errorf("%w: %v", ErrJobRefusedByPolicy, err)
	}
	return time.Time{}, err
}

// buildJob is the task's Job from cfg and prof's template. The template is
// not touched.
func buildJob(cfg lib.DispatchConfig, prof *lib.DispatchProfile, template json.RawMessage, tier, taskID string) (*batchv1.Job, error) {
	profile := prof.Name
	var tmpl corev1.PodTemplateSpec
	if err := json.Unmarshal(template, &tmpl); err != nil {
		return nil, fmt.Errorf("profile %s's Job template: %w", profile, err)
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
			Name:      JobName(profile, tier, taskID),
			Namespace: cfg.ProfileNamespace,
			Labels:    labels,
			Annotations: map[string]string{
				jobProfileAnnotation: profile,
				jobTierAnnotation:    tier,
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

func (k *KubeJobs) Pod(ctx context.Context, profile, tier, taskID string) (string, bool, error) {
	ns, name := k.config().ProfileNamespace, JobName(profile, tier, taskID)
	job, err := k.kube.BatchV1().Jobs(ns).Get(ctx, name, metav1.GetOptions{})
	switch {
	case apierrors.IsNotFound(err):
		return "", true, nil
	case err != nil:
		return "", false, err
	case job.DeletionTimestamp != nil:
		return "", true, nil
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
		if p.DeletionTimestamp == nil && podReady(p) && lib.ValidSubjectToken(p.Name) {
			return p.Name, false, nil
		}
	}
	return "", false, nil
}

func (k *KubeJobs) Delete(ctx context.Context, profile, tier, taskID string) error {
	err := k.kube.BatchV1().Jobs(k.config().ProfileNamespace).Delete(ctx, JobName(profile, tier, taskID),
		metav1.DeleteOptions{PropagationPolicy: ptr.To(metav1.DeletePropagationBackground)})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}
