package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/utils/ptr"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// RouteSession is the DefaultAddressee sentinel that switches a conversation
// from a fixed addressee (the Hermes bridge's "platform") to a session pod
// of its own. Flipping to it — plus A2A_SPAWN_SESSIONS=true — is the W4
// switch; a setting, not surgery (retarget 8/26).
const RouteSession = "session"

const (
	// sweepInterval paces the orphan sweep; sweepPassTimeout bounds one
	// pass, so a hung stream or API call cannot make passes pile up.
	sweepInterval    = time.Minute
	sweepPassTimeout = time.Minute
	// podDeadlineGrace is what the pod-level activeDeadlineSeconds adds
	// above the adapter's task deadline. The adapter's contract is that its
	// deadline sits BELOW the pod's, so the failure is the adapter's to
	// report — the pod deadline only fires for a wedged adapter, handing it
	// to Sweep instead of letting it hold its bus credential indefinitely.
	// The grace covers what the adapter's clock does not: the image pull
	// before the process starts (activeDeadlineSeconds runs from pod start)
	// plus the adapter's own shutdown-and-publish window at its deadline.
	podDeadlineGrace = 10 * time.Minute
	// ownerResolveTimeout bounds the boot-time GET for the owner
	// Deployment, so a hung API server fails boot fast into the crashloop
	// backoff instead of hanging it.
	ownerResolveTimeout = 30 * time.Second
	// primerCap bounds the rehydration-primer annotation, well under the
	// object annotation budget.
	primerCap = 8192
	// workerRunAsUser is the arbitrary non-root UID session pods run as.
	workerRunAsUser = 1000
	// Worker requests: the harness spike's per-session footprint, and what
	// the session cap's sizing math multiplies.
	workerCPURequest    = "250m"
	workerMemoryRequest = "512Mi"
	// sessionNameHexWidth suffixes minted session names — wide enough that
	// two conversations can't plausibly collide onto one addressee.
	// supervisorCorrHexWidth suffixes the fallback correlation id a
	// supervisor terminal mints when the orphan carried none.
	sessionNameHexWidth    = 4
	supervisorCorrHexWidth = 6

	labelPartOf = "app.kubernetes.io/part-of"
	partOfValue = "a2a-next"
	labelRole   = "app.kubernetes.io/component"
	sessionRole = "a2a-session"
	annoTask    = "a2a.kubeagents.dev/task-id"
	annoContext = "a2a.kubeagents.dev/context-id"
	annoCorr    = "a2a.kubeagents.dev/correlation-id"
	annoAddr    = "a2a.kubeagents.dev/addressee"
	annoConvo   = "a2a.kubeagents.dev/session-key"
	annoPrimer  = "a2a.kubeagents.dev/rehydration-primer"
)

// sessionNameAnimals seeds minted session names. W5 owns the canonical
// animal list (stolen from the demo); this short one keeps the dark path
// honest until integration.
var sessionNameAnimals = []string{"otter", "badger", "heron", "lynx", "marten", "puffin", "stoat", "vole"}

// spawner is the session-pod half of the lifecycle. It stays dark behind
// SpawnSessions until W4's worker image exists; the gateway pod gets its
// service-account token with that change, not before.
type spawner interface {
	// Spawn creates the session pod for a task and returns the pod name.
	Spawn(ctx context.Context, rec *SessionRecord, taskID, primer string) (string, error)
	// Delete removes a pod (reap).
	Delete(ctx context.Context, podName string) error
	// TerminalOrphans lists pods in a terminal phase, with the task identity
	// their annotations carry (sweep's scan).
	TerminalOrphans(ctx context.Context) ([]orphanPod, error)
	// LiveSessions counts session pods not yet in a terminal phase - the
	// session cap's denominator.
	LiveSessions(ctx context.Context) (int, error)
}

type orphanPod struct {
	PodName       string
	SessionKey    string
	Addressee     string
	TaskID        string
	ContextID     string
	CorrelationID string
}

// mintSessionName gives an incarnation its bus session name,
// <profile>-<animal> per the house ruling, minted FRESH per incarnation —
// reaping and respawning changes the pod and the bus session name;
// contextId is what persists (gateway design).
func mintSessionName(profile string) string {
	return fmt.Sprintf("%s-%s-%s", profile,
		sessionNameAnimals[int(time.Now().UnixNano())%len(sessionNameAnimals)], randHex(sessionNameHexWidth))
}

func activeCorrelation(rec *SessionRecord) string {
	if rec.ActiveTask != nil {
		return rec.ActiveTask.CorrelationID
	}
	return ""
}

// podSpawner is the client-go implementation.
type podSpawner struct {
	cfg    *Config
	client kubernetes.Interface
	log    *slog.Logger
	// owner is the ownerReference every spawned pod carries — the gateway's
	// own Deployment, resolved once at construction. Nil when no owner is
	// configured (playground).
	owner *metav1.OwnerReference
}

func newPodSpawner(cfg *Config, log *slog.Logger) (*podSpawner, error) {
	rc, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	cs, err := kubernetes.NewForConfig(rc)
	if err != nil {
		return nil, err
	}
	s := &podSpawner{cfg: cfg, client: cs, log: log}
	rctx, cancel := context.WithTimeout(context.Background(), ownerResolveTimeout)
	defer cancel()
	if err := s.resolveOwner(rctx); err != nil {
		// Refuse to boot rather than quietly spawn unowned pods: an owner
		// was configured, so the orphaned-session window is supposed to be
		// closed, and a GET that fails here is a misconfig (name, RBAC) the
		// operator render owns.
		return nil, fmt.Errorf("resolving owner deployment %q: %w", cfg.OwnerDeployment, err)
	}
	return s, nil
}

// resolveOwner fetches the configured Deployment's UID and builds the
// ownerReference spawned pods carry. An ownerReference is name+UID, and the
// UID exists only server-side, so this is a read the gateway's Role grants
// on exactly this one Deployment (resourceNames).
func (s *podSpawner) resolveOwner(ctx context.Context) error {
	if s.cfg.OwnerDeployment == "" {
		return nil
	}
	dep, err := s.client.AppsV1().Deployments(s.cfg.Namespace).Get(ctx, s.cfg.OwnerDeployment, metav1.GetOptions{})
	if err != nil {
		return err
	}
	s.owner = &metav1.OwnerReference{
		APIVersion: "apps/v1",
		Kind:       "Deployment",
		Name:       dep.Name,
		UID:        dep.UID,
		// Controller marks this as the pod's managing owner for tooling
		// ("Controlled By"); no other controller claims session pods, and
		// the Deployment controller only manages ReplicaSets, so nothing
		// competes. BlockOwnerDeletion stays unset: GC does not need it and
		// setting it would require finalizer permissions on the owner.
		Controller: ptr.To(true),
	}
	return nil
}

// Spawn creates the session pod: the demo's reference worker shape — no
// ambient k8s credentials, scratch on emptyDir, 250m/512Mi requests —
// running the headless harness behind the worker adapter (W4's image).
// Model auth, as shipped (spec-chatops-gateway.md, amended 8/31): the
// worker talks to the install's own LiteLLM, in-namespace, with no per-pod
// credential at all — no ServiceAccount, no Workload Identity. Its bus
// credential is the static worker user, injected as env, until the
// deployment spec's auth callout arms; direct Vertex via WI stays the
// target, and arming it is a policy change as well as an IAM one (the
// session egress fence encodes the shipped path).
func (s *podSpawner) Spawn(ctx context.Context, rec *SessionRecord, taskID, primer string) (string, error) {
	name := rec.BusSession
	// The gateway Deployment owns its sessions: when it goes — cleanupA2A on
	// a mode flip, or any other deletion — Kubernetes GC reaps the pods it
	// spawned, closing the orphaned-session window without an operator
	// exception to the IsControlledBy refusal. Empty on playground installs
	// that never configured an owner.
	var owners []metav1.OwnerReference
	if s.owner != nil {
		owners = []metav1.OwnerReference{*s.owner}
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:            name,
			Namespace:       s.cfg.Namespace,
			OwnerReferences: owners,
			Labels: map[string]string{
				labelPartOf: partOfValue,
				labelRole:   sessionRole,
			},
			Annotations: map[string]string{
				annoTask:    taskID,
				annoContext: rec.ContextID,
				annoCorr:    activeCorrelation(rec),
				annoAddr:    rec.Addressee,
				annoConvo:   rec.Key,
				// The rehydration primer rides the pod until W4's adapter
				// grows a first-input path for it; bounded well under the
				// object annotation budget.
				annoPrimer: truncateRunes(primer, primerCap),
			},
		},
		Spec: corev1.PodSpec{
			RestartPolicy:                corev1.RestartPolicyNever,
			AutomountServiceAccountToken: ptr.To(false),
			// The adapter's deadline sits below this by construction (its
			// contract, and podDeadlineGrace's comment): a healthy adapter
			// always publishes the terminal and exits first, so this fires
			// only for a wedged adapter — the one end Session lifecycle
			// used to name as unowned.
			ActiveDeadlineSeconds: ptr.To(int64((s.cfg.TaskDeadline + podDeadlineGrace) / time.Second)),
			SecurityContext: &corev1.PodSecurityContext{
				RunAsNonRoot:   ptr.To(true),
				RunAsUser:      ptr.To(int64(workerRunAsUser)),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Containers: []corev1.Container{{
				Name:  "worker",
				Image: s.cfg.WorkerImage,
				Env: []corev1.EnvVar{
					// The worker env contract (launch-card constants):
					// TASK_ID/PROFILE/NATS_URL. PROFILE names the
					// AgentProfile — the addressee is the bus session name,
					// which is not a profile. Bus creds ride alongside; how
					// W4 wants them delivered is its call to revise.
					{Name: "TASK_ID", Value: taskID},
					{Name: "PROFILE", Value: rec.Profile},
					{Name: "NATS_URL", Value: s.cfg.NATSURL},
					{Name: "NATS_USER", Value: "worker"},
					{Name: "NATS_PASSWORD", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
						LocalObjectReference: corev1.LocalObjectReference{Name: s.cfg.NATSCredsSecret},
						Key:                  "worker-password",
					}}},
					{Name: "A2A_SESSION", Value: rec.BusSession},
					// The adapter's half of the deadline contract, rendered
					// from the same config the pod deadline above is sized
					// from — one number, two enforcement layers, no drift.
					{Name: "A2A_TASK_DEADLINE_SECONDS", Value: strconv.Itoa(int(s.cfg.TaskDeadline / time.Second))},
				},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{
						corev1.ResourceCPU:    resource.MustParse(workerCPURequest),
						corev1.ResourceMemory: resource.MustParse(workerMemoryRequest),
					},
				},
				VolumeMounts: []corev1.VolumeMount{{Name: "scratch", MountPath: "/scratch"}},
				SecurityContext: &corev1.SecurityContext{
					AllowPrivilegeEscalation: ptr.To(false),
					Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
					SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
				},
			}},
			Volumes: []corev1.Volume{{
				Name:         "scratch",
				VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
			}},
		},
	}
	created, err := s.client.CoreV1().Pods(s.cfg.Namespace).Create(ctx, pod, metav1.CreateOptions{})
	if err != nil {
		// AlreadyExists included: session names are minted per incarnation,
		// so a name collision means the mint raced a terminating predecessor
		// — surface it rather than adopt a pod that is about to vanish.
		return "", err
	}
	return created.Name, nil
}

func (s *podSpawner) Delete(ctx context.Context, podName string) error {
	err := s.client.CoreV1().Pods(s.cfg.Namespace).Delete(ctx, podName, metav1.DeleteOptions{})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}

// LiveSessions counts through the k8s API rather than the gateway's own
// registry: the API is authoritative, survives a gateway restart, and sees
// orphans the registry has forgotten. Terminal pods are sweep's inventory,
// not load, so they don't count - matching what a `pods` ResourceQuota
// counts, which is the layer that backstops this number.
func (s *podSpawner) LiveSessions(ctx context.Context) (int, error) {
	pods, err := s.client.CoreV1().Pods(s.cfg.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s,%s=%s", labelPartOf, partOfValue, labelRole, sessionRole),
	})
	if err != nil {
		return 0, err
	}
	n := 0
	for _, p := range pods.Items {
		if p.Status.Phase == corev1.PodSucceeded || p.Status.Phase == corev1.PodFailed {
			continue
		}
		n++
	}
	return n, nil
}

func (s *podSpawner) TerminalOrphans(ctx context.Context) ([]orphanPod, error) {
	pods, err := s.client.CoreV1().Pods(s.cfg.Namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("%s=%s,%s=%s", labelPartOf, partOfValue, labelRole, sessionRole),
	})
	if err != nil {
		return nil, err
	}
	var out []orphanPod
	for _, p := range pods.Items {
		if p.Status.Phase != corev1.PodSucceeded && p.Status.Phase != corev1.PodFailed {
			continue
		}
		out = append(out, orphanPod{
			PodName:       p.Name,
			SessionKey:    p.Annotations[annoConvo],
			Addressee:     p.Annotations[annoAddr],
			TaskID:        p.Annotations[annoTask],
			ContextID:     p.Annotations[annoContext],
			CorrelationID: p.Annotations[annoCorr],
		})
	}
	return out, nil
}

// refuseAtSessionCap is the usability half of the session-pod bound, checked
// before any route mutation that will need a fresh pod. At the cap the turn
// is refused with a reply naming the numbers - never silently queued, never
// dropped. replacing marks a turn that retires its previous incarnation in
// the same breath: the doomed pod is usually still live at count time, so
// it hands its slot to its successor rather than double-counting - and if
// it already went terminal, the extra slot is a transient overshoot the
// quota headroom absorbs.
//
// The count-then-create race is real, and wider than one: conversations run
// on distinct queue workers, so every delegation in flight inside the
// count-to-create window can pass this check together - the true bound on
// the overshoot is the namespace ResourceQuota the operator renders above
// this number, not cap+1 and not a lock here. A lock would only serialize
// this process, while the quota also holds against a gateway that has
// stopped honoring its own cap.
func (g *Gateway) refuseAtSessionCap(ctx context.Context, rec *SessionRecord, replacing bool) bool {
	// SessionRouted persists in the KV record; the spawner is a setting. A
	// W4 rollback leaves session-routed records this check still reaches -
	// with nothing to cap, let the turn degrade the way it always did
	// (publish toward an addressee nothing owns) rather than panic.
	if g.spawner == nil {
		return false
	}
	live, err := g.spawner.LiveSessions(ctx)
	if err != nil {
		// Proceeding blind would make the cap advisory exactly when the API
		// is misbehaving; say so instead of dropping the turn silently.
		g.log.Error("session cap: live count failed", "conversation", rec.Key, "err", err)
		g.post(rec.Key, "⚠️ not started: can't count the running session workers right now — try again in a moment")
		return true
	}
	limit := g.cfg.MaxSessions
	if replacing {
		limit++
	}
	if live < limit {
		return false
	}
	workers := fmt.Sprintf("%d session workers are", live)
	if live == 1 {
		workers = "1 session worker is"
	}
	g.log.Warn("session cap: delegation refused", "conversation", rec.Key, "live", live, "cap", g.cfg.MaxSessions)
	g.post(rec.Key, fmt.Sprintf(
		"🚦 not started: %s already running (cap %d). Wait for one to finish or `stop` one you started; an operator can raise the cap (A2A_MAX_SESSIONS / spec.harness.tuning.maxSessions).",
		workers, g.cfg.MaxSessions))
	return true
}

// ensureSessionPod spawns (or rehydrates) the session's incarnation for a
// new task. Only called on session-addressed routes.
func (g *Gateway) ensureSessionPod(ctx context.Context, rec *SessionRecord, taskID string) {
	if rec.PodName != "" {
		return
	}
	primer := g.buildRehydrationPrimer(ctx, rec)
	podName, err := g.spawner.Spawn(ctx, rec, taskID, primer)
	if err != nil {
		g.log.Error("session pod spawn failed", "session", rec.Key, "err", err)
		// The task is already on the stream and no pod will ever run it.
		// Left alone it is invisible to Sweep (which watches pods) and
		// non-terminal for the whole retention window while the
		// conversation steers into it — so the supervisor rule applies
		// here exactly as at deletion: publish the terminal and let the
		// relay render it onto the rolling line, the same way it renders
		// Sweep's. `failed`, not `canceled`: nothing was stopped, the
		// executor never existed.
		if perr := g.publishSupervisorTerminal(ctx, rec.Addressee, taskID, rec.ContextID, activeCorrelation(rec),
			lib.StateFailed, "session pod could not be created; declared failed by its supervisor"); perr != nil {
			// The residue: the task publish just succeeded, so failing
			// here means the bus dropped between the two publishes. The
			// task stays non-terminal until the user's stop detaches it
			// and retirement completes the cancel.
			g.log.Error("spawn-failure supervisor terminal failed; task left non-terminal", "task", taskID, "err", perr)
			g.post(rec.Key, "⚠️ the session worker could not be started and the task could not be closed — `stop` it, then try again")
		}
		return
	}
	rec.PodName = podName
	g.log.Info("spawned session pod", "session", rec.Key, "pod", podName, "task", taskID)
}

// sweepLoop is the gateway's half of the orphaned-task answer: it is the
// supervisor for sessions it spawned. A pod in a terminal phase whose task
// never emitted a final event gets a terminal event published by the
// gateway, then the pod is deleted. The state follows the supervisor rule:
// `failed` for an executor that died mid-work, `canceled` where the task
// had detached — a worker that exits or wedges after a `stop` reaches here
// routinely, and an unconditional `failed` would report broken for every
// task a user stopped. The synthesized event carries the gateway's identity
// in from, so replay always distinguishes "the worker said failed" from
// "the supervisor declared it dead". (The dispatcher's janitor is the other
// half, for profile-addressed tasks — stage 3.)
func (g *Gateway) sweepLoop(ctx context.Context) {
	ticker := time.NewTicker(sweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.sweepOnce(ctx)
		}
	}
}

// isTaskNotFound reports whether a TasksGet failure means "no events yet"
// (the library's TaskNotFound A2AError) rather than the stream failing to
// answer. The supervisor paths may only proceed on the former.
func isTaskNotFound(err error) bool {
	var aerr *lib.A2AError
	return errors.As(err, &aerr) && aerr.Code == lib.CodeTaskNotFound
}

func (g *Gateway) sweepOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, sweepPassTimeout)
	defer cancel()
	orphans, err := g.spawner.TerminalOrphans(ctx)
	if err != nil {
		g.log.Error("sweep: pod list failed", "err", err)
		return
	}
	for _, o := range orphans {
		if o.TaskID == "" || o.Addressee == "" {
			g.log.Warn("sweep: terminal pod without task annotations; deleting", "pod", o.PodName)
			_ = g.spawner.Delete(ctx, o.PodName)
			g.releaseIncarnation(ctx, o)
			continue
		}
		task, err := g.client.TasksGet(ctx, o.Addressee, o.TaskID)
		if err == nil && task.Final {
			_ = g.spawner.Delete(ctx, o.PodName) // clean exit; nothing owed
			g.releaseIncarnation(ctx, o)
			continue
		}
		if err != nil && !isTaskNotFound(err) {
			// TaskNotFound says "no events", which is the orphan shape.
			// Anything else is the stream not answering — and a terminal we
			// cannot rule out is a reason to wait a cycle, not to author
			// what could be the second final (assertion 10).
			g.log.Error("sweep: replay failed; retrying next cycle", "task", o.TaskID, "err", err)
			continue
		}
		// The supervisor writes what happened: `canceled` if a cancel for
		// this task is on the stream (the session record's task history
		// carries that mark durably — ActiveTask may long since have moved
		// on), `failed` otherwise. A record we cannot read right now is a
		// reason to wait a cycle, not to guess a state.
		state := lib.StateFailed
		note := "session pod exited without a terminal event; declared failed by its supervisor"
		if o.SessionKey != "" {
			rec, err := g.reg.Get(ctx, o.SessionKey)
			if err != nil {
				g.log.Error("sweep: record read failed; retrying next cycle", "session", o.SessionKey, "err", err)
				continue
			}
			if rec != nil && rec.TaskCanceled(o.TaskID) {
				state = lib.StateCanceled
				note = "the requester's stop, completed by the supervisor: the worker exited without publishing its terminal"
			}
		}
		if err := g.publishSupervisorTerminal(ctx, o.Addressee, o.TaskID, o.ContextID, o.CorrelationID, state, note); err != nil {
			g.log.Error("sweep: supervisor terminal publish failed", "task", o.TaskID, "err", err)
			continue // keep the pod as evidence until the event lands
		}
		g.log.Warn("sweep: closed orphaned task", "task", o.TaskID, "state", state, "pod", o.PodName)
		_ = g.spawner.Delete(ctx, o.PodName)
		g.releaseIncarnation(ctx, o)
	}
}

// closeDetachedBeforeDelete is the one rule for every pod the gateway
// deletes itself (Session lifecycle states it once; reap, Sweep, Delegate,
// and the pre-flip retirement all reach it): a pod running a DETACHED task
// gets that task's terminal event published before the delete — deleting
// first would strand the task non-terminal for the whole retention window,
// since the adapter's deadline dies with the process and a deleted pod
// never reaches the phase Sweep watches. The state is `canceled`, not
// `failed`: detached means a stop already published a cancel, so the
// gateway is finishing the cancel the requester asked for (assertion 13's
// enumeration). Returns false when the terminal could not be published; the
// caller must keep the pod.
func (g *Gateway) closeDetachedBeforeDelete(ctx context.Context, rec *SessionRecord) bool {
	active := rec.ActiveTask
	if active == nil || !active.Detached {
		return true // idle pod, or a healed task: nothing owed
	}
	// The task ran under the addressee its own subjects carried — after a
	// Delegate re-home rec.Addressee is already the successor's.
	addressee := rec.AddresseeFor(active.TaskID)
	// Sweep's guard, for the same reason: Detached means the terminal has
	// not been RELAYED, not that it does not exist. A worker that confirmed
	// the cancel before the relay clears the flag — relay lag, a restart
	// with the delete already done — already put the one final on the
	// stream, and a second would be the protocol error assertion 10 makes
	// every consumer surface.
	task, err := g.client.TasksGet(ctx, addressee, active.TaskID)
	if err == nil && task.Final {
		return true
	}
	if err != nil && !isTaskNotFound(err) {
		// Same rule as Sweep: a replay failure means an existing final
		// could not be ruled out, and guessing would author the duplicate
		// the guard above exists to prevent. Keep the pod.
		g.log.Error("pre-delete replay failed; keeping the pod", "task", active.TaskID, "err", err)
		return false
	}
	err = g.publishSupervisorTerminal(ctx, addressee, active.TaskID, rec.ContextID, active.CorrelationID,
		lib.StateCanceled, "the requester's stop, completed by the supervisor at pod retirement")
	if err != nil {
		g.log.Error("supervisor terminal before delete failed", "task", active.TaskID, "err", err)
		return false
	}
	return true
}

// releaseIncarnation clears the session record's pod binding after sweep
// removes a dead pod — otherwise ensureSessionPod sees a PodName forever
// and an active conversation (which keeps resetting the idle clock, so reap
// never fires) has no executor and no way to get one.
func (g *Gateway) releaseIncarnation(ctx context.Context, o orphanPod) {
	if o.SessionKey == "" {
		return
	}
	l := g.lockSession(o.SessionKey)
	l.Lock()
	defer l.Unlock()
	rec, err := g.reg.Get(ctx, o.SessionKey)
	if err != nil || rec == nil || rec.PodName != o.PodName {
		return
	}
	rec.PodName = ""
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("sweep: record release failed", "session", o.SessionKey, "err", err)
	}
}

// publishSupervisorTerminal writes a task's terminal event on behalf of the
// gateway as supervisor — `failed` for an executor that died mid-work,
// `canceled` where the supervisor is finishing a cancel already on the
// stream (the callers own that choice).
func (g *Gateway) publishSupervisorTerminal(ctx context.Context, addressee, taskID, contextID, correlationID string, state lib.TaskState, note string) error {
	if correlationID == "" {
		// The record should always carry it; a missing one still gets a
		// terminal event, honestly labeled.
		correlationID = "corr-supervisor-" + randHex(supervisorCorrHexWidth)
	}
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    taskID,
		ContextID: contextID,
		Status: lib.TaskStatus{
			State: state,
			Message: &lib.Message{
				Role:      "agent",
				MessageID: "msg-" + randHex(messageIDHexWidth),
				Parts:     []lib.Part{{Kind: "text", Text: note}},
			},
		},
		Final: true,
	})
	if err != nil {
		return err
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, taskID, contextID, correlationID, payload)
	if err != nil {
		return err
	}
	return g.client.Publish(ctx, lib.TaskEventsSubject(addressee, taskID), env)
}
