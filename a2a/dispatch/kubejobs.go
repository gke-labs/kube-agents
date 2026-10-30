package dispatch

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"

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
// profile namespace, from the profile's JobTemplate in the dispatch config.
// The template is used exactly as the operator rendered it, since the Job
// admission policy admits only that; the Job gets a name, the config's
// jobLabels, and the shape the policy requires (backoffLimit 0, one pod, a
// deadline, a TTL). The pod the task is offered to is the Job's own, by
// name, so the confirm's credentials bind to that pod, never to the Job.
type KubeJobs struct {
	kube   kubernetes.Interface
	config func() lib.DispatchConfig
}

// NewKubeJobs returns a JobRunner reading the current config from config.
func NewKubeJobs(kube kubernetes.Interface, config func() lib.DispatchConfig) *KubeJobs {
	return &KubeJobs{kube: kube, config: config}
}

// JobName is the task's Job name: a hash of its key, so it is the same after
// a restart, fits a label value, and carries nothing a caller chose.
func JobName(profile, tier, taskID string) string {
	sum := sha256.Sum256([]byte(profile + "/" + tier + "/" + taskID))
	return "dispatch-" + hex.EncodeToString(sum[:10])
}

func (k *KubeJobs) Launch(ctx context.Context, profile, tier, taskID string) error {
	cfg := k.config()
	job, err := buildJob(cfg, profile, tier, taskID)
	if err != nil {
		return err
	}
	_, err = k.kube.BatchV1().Jobs(cfg.ProfileNamespace).Create(ctx, job, metav1.CreateOptions{})
	if apierrors.IsAlreadyExists(err) {
		return nil
	}
	return err
}

// buildJob is the task's Job from cfg. The template is not touched.
func buildJob(cfg lib.DispatchConfig, profile, tier, taskID string) (*batchv1.Job, error) {
	var prof *lib.DispatchProfile
	for i := range cfg.Profiles {
		if cfg.Profiles[i].Name == profile {
			prof = &cfg.Profiles[i]
		}
	}
	if prof == nil || len(prof.JobTemplate) == 0 {
		return nil, fmt.Errorf("profile %s has no Job template in the dispatch config", profile)
	}
	var tmpl corev1.PodTemplateSpec
	if err := json.Unmarshal(prof.JobTemplate, &tmpl); err != nil {
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
		if (c.Type == batchv1.JobFailed || c.Type == batchv1.JobFailureTarget) && c.Status == corev1.ConditionTrue {
			return "", true, nil
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
		if p.Status.Phase == corev1.PodFailed {
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
