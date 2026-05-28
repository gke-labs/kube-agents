package controller

import (
	"context"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	batchv1v1 "mycompany.com/gcp-operator/api/v1"
)

// MyOwnCronJobReconciler reconciles a MyOwnCronJob object
type MyOwnCronJobReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

// +kubebuilder:rbac:groups=batch.mycompany.com,resources=myowncronjobs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=batch.mycompany.com,resources=myowncronjobs/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=batch.mycompany.com,resources=myowncronjobs/finalizers,verbs=update
// +kubebuilder:rbac:groups=batch,resources=cronjobs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=batch,resources=cronjobs/status,verbs=get

// Reconcile is part of the main reconciliation loop
func (r *MyOwnCronJobReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// 1. Fetch the Custom Resource (CR) instance from etcd
	var myCR batchv1v1.MyOwnCronJob
	if err := r.Get(ctx, req.NamespacedName, &myCR); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// 2. Define and map properties into the native batch/v1 CronJob struct
	cronJob := &batchv1.CronJob{
		ObjectMeta: metav1.ObjectMeta{
			Name:      myCR.Name + "-built-by-operator",
			Namespace: myCR.Namespace,
		},
		Spec: batchv1.CronJobSpec{
			Schedule: "0 0 1 1 *", // Runs once a year (effectively manual-only)
			JobTemplate: batchv1.JobTemplateSpec{
				Spec: batchv1.JobSpec{
					Template: corev1.PodTemplateSpec{
						Spec: corev1.PodSpec{
							RestartPolicy: corev1.RestartPolicyNever,
							Containers: []corev1.Container{
								{
									Name:  "logger",
									Image: "busybox:latest",
									// DYNAMIC MAPPING: Injects custom text directly into the command payload
									Command: []string{"echo", "CronJob Trigger: " + myCR.Spec.Text},
								},
							},
						},
					},
				},
			},
		},
	}

	// 3. Set OwnerReference (Garbage collection: If CR is deleted, this CronJob terminates too)
	if err := ctrl.SetControllerReference(&myCR, cronJob, r.Scheme); err != nil {
		return ctrl.Result{}, err
	}

	// 4. Server-side Validation: Create or update the CronJob via Kubernetes API Client
	foundCronJob := &batchv1.CronJob{}
	err := r.Get(ctx, types.NamespacedName{Name: cronJob.Name, Namespace: cronJob.Namespace}, foundCronJob)
	if err != nil && errors.IsNotFound(err) {
		log.Info("Mapping CR to Native CronJob", "TextToLog", myCR.Spec.Text)
		err = r.Create(ctx, cronJob)
		if err != nil {
			return ctrl.Result{}, err // If invalid type or param, API server returns 404/422 and we retry
		}
	}

	return ctrl.Result{}, nil
}

// SetupWithManager sets up the controller with the Manager.
func (r *MyOwnCronJobReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&batchv1v1.MyOwnCronJob{}).
		Named("myowncronjob").
		Complete(r)
}
