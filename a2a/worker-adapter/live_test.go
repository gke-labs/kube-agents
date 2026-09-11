package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestLiveWorkerPodOnInstall drives the whole worker side on a real mode:next
// install: it publishes a task the way the gateway does (real gateway user,
// real deny-by-default grants), creates a session pod with the spawner's
// exact spec from the real worker-next image, and asserts the lifecycle -
// submitted/working/progress/result/completed, a mid-run steer absorbed at
// the turn boundary, and a cancel landing terminal canceled with the pod
// dead. Discord itself is the only thing not exercised.
//
// Env-gated:
//
//	A2A_LIVE_NATS_URL          e.g. nats://127.0.0.1:4222 via port-forward
//	A2A_LIVE_GATEWAY_PASSWORD  from the install's creds Secret
//	A2A_LIVE_KUBE_CONTEXT      pin --context on every kubectl call
//	A2A_LIVE_NAMESPACE         default kubeagents-system
//	A2A_LIVE_WORKER_IMAGE      default worker-next:latest in the a2a-demo registry
func TestLiveWorkerPodOnInstall(t *testing.T) {
	url := os.Getenv("A2A_LIVE_NATS_URL")
	if url == "" {
		t.Skip("A2A_LIVE_NATS_URL not set; live test skipped")
	}
	gwPass := os.Getenv("A2A_LIVE_GATEWAY_PASSWORD")
	kubeContext := os.Getenv("A2A_LIVE_KUBE_CONTEXT")
	if gwPass == "" || kubeContext == "" {
		t.Fatal("live test needs A2A_LIVE_GATEWAY_PASSWORD and A2A_LIVE_KUBE_CONTEXT")
	}
	namespace := envOr("A2A_LIVE_NAMESPACE", "kubeagents-system")
	image := envOr("A2A_LIVE_WORKER_IMAGE",
		"northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/worker-next:latest")

	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	c, err := lib.Connect(ctx, url,
		lib.WithName("w4-live-test"),
		lib.WithNATSOptions(nats.UserInfo("gateway", gwPass), nats.CustomInboxPrefix("_INBOX.gateway")))
	if err != nil {
		t.Fatalf("connect as gateway: %v", err)
	}
	defer c.Close()

	t.Run("HappyPathWithSteer", func(t *testing.T) {
		session := "chat-live-" + liveSuffix(6)
		taskID := "task-live-" + liveSuffix(8)
		origin := liveSubmit(t, ctx, c, session, taskID,
			"Write a haiku about message buses. Output only the haiku.")
		livePod(t, kubeContext, namespace, image, session, taskID)

		liveWaitState(t, ctx, c, session, taskID, lib.StateWorking, 3*time.Minute)

		steerPayload, _ := json.Marshal(lib.Message{
			Role:      "user",
			Parts:     []lib.Part{{Kind: "text", Text: "Steering update: you MUST include the word tangerine in the haiku."}},
			MessageID: "msg-live-steer-" + taskID,
			TaskID:    taskID, ContextID: origin.ContextID,
		})
		steer, err := lib.NewFollowUpEnvelope(origin, lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
			steerPayload, lib.WithTo(lib.Party{Session: session}))
		if err != nil {
			t.Fatalf("steer envelope: %v", err)
		}
		if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), steer); err != nil {
			t.Fatalf("steer publish: %v", err)
		}

		task := liveWaitFinal(t, ctx, c, session, taskID, 4*time.Minute)
		if task.State != lib.StateCompleted {
			t.Fatalf("terminal %s, want completed", task.State)
		}
		if err := task.ValidateArtifacts(); err != nil {
			t.Fatalf("artifacts: %v", err)
		}
		result := liveArtifactText(task, lib.ArtifactResult)
		t.Logf("result artifact:\n%s", result)
		if result == "" {
			t.Fatal("empty result artifact")
		}
		// The steer is absorbed at the next turn boundary; the deliverable
		// is the steered turn's result (assertion 21 live).
		if !strings.Contains(strings.ToLower(result), "tangerine") {
			t.Errorf("steer not reflected in the deliverable: %q", result)
		}
	})

	// LateSteerRefusedVisibly: the refusal reaches the bus on the real
	// install, ahead of the terminal event, where the gateway's relay is what
	// turns it into a chat message.
	//
	// The harness is scripted here and only here. The window under test opens
	// when the adapter reads the harness's result and closes when the harness
	// exits; behind the real model that is a fraction of a second and cannot
	// be aimed at from outside. The script emits a sentinel through the
	// assistant channel (which the adapter republishes as a progress artifact,
	// so the test can see the window open on the stream), then its result,
	// then lingers - holding the window open long enough to publish a steer
	// into it as the real gateway user.
	t.Run("LateSteerRefusedVisibly", func(t *testing.T) {
		session := "chat-live-" + liveSuffix(6)
		taskID := "task-live-" + liveSuffix(8)
		const sentinel = "DELIVERABLE-INCOMING"
		const deliverable = "the pre-steer answer"
		script := fmt.Sprintf(`echo '{"type":"system","subtype":"init","session_id":"live-late-steer"}'
read first || exit 1
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"%s"}]}}\n'
printf '{"type":"result","subtype":"success","result":"%s"}\n'
sleep 90
`, sentinel, deliverable)

		origin := liveSubmit(t, ctx, c, session, taskID, "opening prompt")
		livePodWithScript(t, kubeContext, namespace, image, session, taskID, script)
		liveWaitState(t, ctx, c, session, taskID, lib.StateWorking, 3*time.Minute)

		// The sentinel on the stream means the harness has reached the line
		// before its result; the adapter chooses the deliverable immediately
		// after. Wait past that, then steer.
		liveWaitProgress(t, ctx, c, kubeContext, namespace, session, taskID, sentinel, 3*time.Minute)
		time.Sleep(3 * time.Second)

		const steerText = "Steering update: you MUST include the word tangerine."
		steerPayload, _ := json.Marshal(lib.Message{
			Role: "user", Parts: []lib.Part{{Kind: "text", Text: steerText}},
			MessageID: "msg-live-late-steer-" + taskID,
			TaskID:    taskID, ContextID: origin.ContextID,
		})
		steer, err := lib.NewFollowUpEnvelope(origin, lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
			steerPayload, lib.WithTo(lib.Party{Session: session}))
		if err != nil {
			t.Fatalf("steer envelope: %v", err)
		}
		if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), steer); err != nil {
			t.Fatalf("steer publish: %v", err)
		}

		task := liveWaitFinal(t, ctx, c, session, taskID, 4*time.Minute)
		if task.State != lib.StateCompleted {
			t.Fatalf("terminal %s, want completed", task.State)
		}
		// The deliverable is the pre-steer answer: the refusal did not
		// displace it, and the steer was not absorbed as another turn.
		if got := liveArtifactText(task, lib.ArtifactResult); got != deliverable {
			t.Errorf("result artifact %q, want %q", got, deliverable)
		}

		// The refusal is on the stream, carries working, names the refused
		// message, and precedes the terminal.
		events := liveReplayEvents(t, ctx, url, gwPass, session, taskID)
		refusals, refusalAt, terminalAt := 0, -1, -1
		for i, env := range events {
			if env.Kind != lib.KindStatusUpdate {
				continue
			}
			var s lib.StatusUpdate
			if err := json.Unmarshal(env.Payload, &s); err != nil {
				t.Fatalf("status payload: %v", err)
			}
			if s.Final {
				terminalAt = i
				continue
			}
			if s.Status.Message == nil {
				continue
			}
			text := joinParts(s.Status.Message.Parts)
			if !strings.Contains(text, "steer refused") {
				continue
			}
			refusals++
			refusalAt = i
			t.Logf("refusal on the live bus (event %d): state=%s\n%s", i, s.Status.State, text)
			if s.Status.State != lib.StateWorking {
				t.Errorf("refusal carries %q, want %q", s.Status.State, lib.StateWorking)
			}
			if !strings.Contains(text, steerText) {
				t.Errorf("refusal does not identify the refused message: %q", text)
			}
		}
		if refusals != 1 {
			t.Fatalf("refusal events on the live bus: %d, want 1", refusals)
		}
		if terminalAt < 0 || refusalAt > terminalAt {
			t.Errorf("refusal at %d, terminal at %d - the refusal must precede the terminal", refusalAt, terminalAt)
		}
	})

	// The bus credential must not reach the harness, demonstrated rather than
	// read off the code. The harness here is a script that reports what its
	// own environment holds, through the same result channel a real harness
	// uses, so the assertion runs against what the subprocess actually got.
	//
	// A marker rides in the pod env beside the password, which is what makes
	// this a demonstration rather than an absence: the marker arriving proves
	// the pod env reached the harness at all, so the password being missing
	// is the filter working and not an empty environment.
	t.Run("TheBusCredentialDoesNotReachTheHarness", func(t *testing.T) {
		session := "chat-live-" + liveSuffix(6)
		taskID := "task-live-" + liveSuffix(8)
		script := `echo '{"type":"system","subtype":"init","session_id":"live-env"}'
read first || exit 1
SAW_PASSWORD=absent
if [ -n "${NATS_PASSWORD:-}" ]; then SAW_PASSWORD=PRESENT; fi
SAW_MARKER=absent
if [ -n "${A2A_LIVE_ENV_MARKER:-}" ]; then SAW_MARKER="$A2A_LIVE_ENV_MARKER"; fi
printf '{"type":"result","subtype":"success","result":"password=%s marker=%s"}\n' "$SAW_PASSWORD" "$SAW_MARKER"
`

		liveSubmit(t, ctx, c, session, taskID, "report your environment")
		livePodWithScript(t, kubeContext, namespace, image, session, taskID, script)

		task := liveWaitFinal(t, ctx, c, session, taskID, 4*time.Minute)
		result := liveArtifactText(task, "result")
		t.Logf("the harness reported: %s", result)

		if !strings.Contains(result, "marker=live-marker-ok") {
			t.Errorf("the marker did not reach the harness (%q), so this run proves nothing about the password", result)
		}
		if !strings.Contains(result, "password=absent") {
			t.Errorf("the bus credential reached the harness: %q", result)
		}
	})

	t.Run("Cancel", func(t *testing.T) {
		session := "chat-live-" + liveSuffix(6)
		taskID := "task-live-" + liveSuffix(8)
		origin := liveSubmit(t, ctx, c, session, taskID,
			"Use the TodoWrite tool to plan 20 research subtasks about NATS JetStream, "+
				"then work through each one in a separate turn, writing detailed notes to "+
				"a file per subtask with the Write tool. Do not stop until all 20 are done.")
		livePod(t, kubeContext, namespace, image, session, taskID)
		liveWaitState(t, ctx, c, session, taskID, lib.StateWorking, 3*time.Minute)
		// Let the harness get properly into the long tool loop before the
		// hard interrupt, so the live path exercised is a real mid-run kill.
		time.Sleep(8 * time.Second)

		cancelEnv, err := lib.NewCancelEnvelope(lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
			taskID, origin.ContextID, origin.CorrelationID, lib.WithTo(lib.Party{Session: session}))
		if err != nil {
			t.Fatalf("cancel envelope: %v", err)
		}
		if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), cancelEnv); err != nil {
			t.Fatalf("cancel publish: %v", err)
		}
		task := liveWaitFinal(t, ctx, c, session, taskID, 2*time.Minute)
		// canceled, or completed if the model won the race - both legal.
		if task.State != lib.StateCanceled && task.State != lib.StateCompleted {
			t.Fatalf("terminal %s after cancel", task.State)
		}
		t.Logf("cancel landed terminal %s", task.State)
	})
}

// liveSuffix returns n lowercase characters that differ between calls.
//
// It exists because taking them off the FRONT of a NUID does not: a NUID is a
// 12-character per-process prefix followed by a sequential tail, so
// `nuid.Next()[:6]` is the same six characters for every call in a process.
// Every subtest here therefore built the same pod name, and the second one to
// run hit `pod updates may not change fields other than image` against the
// first one's pod - a failure that only appears when the subtests run
// together, which is why it survived until a third subtest landed between
// them.
func liveSuffix(n int) string {
	s := nuid.Next()
	return strings.ToLower(s[len(s)-n:])
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func liveSubmit(t *testing.T, ctx context.Context, c *lib.Client, session, taskID, text string) *lib.Envelope {
	t.Helper()
	payload, _ := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}},
		MessageID: "msg-" + taskID, TaskID: taskID, ContextID: "ctx-" + taskID,
	})
	env, err := lib.NewMessageEnvelope(lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
		taskID, "ctx-"+taskID, "corr-live-"+taskID, payload, lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish submission: %v", err)
	}
	return env
}

// livePod applies a pod with the gateway spawner's exact spec (spawn.go) and
// registers cleanup.
func livePod(t *testing.T, kubeContext, namespace, image, session, taskID string) {
	t.Helper()
	livePodWithScript(t, kubeContext, namespace, image, session, taskID, "")
}

// livePodWithScript is livePod with the harness replaced by a shell script
// mounted from a ConfigMap. The late-steer subtest needs it because the
// window it tests - between the adapter choosing its deliverable and the
// harness exiting - is a fraction of a second behind the real model and
// cannot be aimed at from outside the pod. Everything else stays real: the
// install, the bus, the deny-by-default grants, the worker credential, and
// the adapter binary out of the shipped image.
//
// The script arrives by ConfigMap rather than through A2A_HARNESS_CMD
// directly because that variable is split with strings.Fields, which no
// amount of quoting survives.
func livePodWithScript(t *testing.T, kubeContext, namespace, image, session, taskID, script string) {
	t.Helper()
	var extra, scriptObjects, scriptMount, scriptVolume string
	if script != "" {
		var indented strings.Builder
		for _, line := range strings.Split(strings.TrimRight(script, "\n"), "\n") {
			fmt.Fprintf(&indented, "\n    %s", line)
		}
		scriptObjects = fmt.Sprintf(`apiVersion: v1
kind: ConfigMap
metadata:
  name: %s-harness
  namespace: %s
data:
  harness.sh: |%s
---
`, session, namespace, indented.String())
		extra = "\n        - name: A2A_HARNESS_CMD\n          value: /bin/sh /harness/harness.sh"
		scriptMount = "\n        - name: harness\n          mountPath: /harness"
		scriptVolume = fmt.Sprintf("\n    - name: harness\n      configMap:\n        name: %s-harness", session)
		t.Cleanup(func() {
			_ = exec.Command("kubectl", "--context", kubeContext, "-n", namespace,
				"delete", "configmap", session+"-harness", "--wait=false").Run()
		})
	}
	manifest := scriptObjects + fmt.Sprintf(`apiVersion: v1
kind: Pod
metadata:
  name: %s
  namespace: %s
  labels:
    app.kubernetes.io/part-of: a2a-next
    app.kubernetes.io/component: a2a-session
  annotations:
    a2a.kubeagents.dev/task-id: %s
    a2a.kubeagents.dev/addressee: %s
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
  containers:
    - name: worker
      image: %s
      env:
        - name: TASK_ID
          value: %s
        - name: PROFILE
          value: chat
        - name: NATS_URL
          value: nats://platform-agent-a2a-nats.%s.svc:4222
        - name: NATS_USER
          value: worker
        - name: NATS_PASSWORD
          valueFrom:
            secretKeyRef:
              name: platform-agent-a2a-nats-creds
              key: worker-password
        - name: A2A_SESSION
          value: %s
        - name: A2A_LIVE_ENV_MARKER
          value: live-marker-ok%s
      resources:
        requests: { cpu: 250m, memory: 512Mi }
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"] }
      volumeMounts:
        - name: scratch
          mountPath: /scratch%s
  volumes:
    - name: scratch
      emptyDir: {}%s
`, session, namespace, taskID, session, image, taskID, namespace, session, extra, scriptMount, scriptVolume)
	path := filepath.Join(t.TempDir(), "pod.yaml")
	if err := os.WriteFile(path, []byte(manifest), 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command("kubectl", "--context", kubeContext, "-n", namespace, "apply", "-f", path).CombinedOutput()
	if err != nil {
		t.Fatalf("pod apply: %v\n%s", err, out)
	}
	t.Cleanup(func() {
		_ = exec.Command("kubectl", "--context", kubeContext, "-n", namespace,
			"delete", "pod", session, "--wait=false").Run()
	})
}

func liveWaitState(t *testing.T, ctx context.Context, c *lib.Client, session, taskID string, state lib.TaskState, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		task, err := c.TasksGet(ctx, session, taskID)
		if err == nil {
			for _, s := range task.StatusHistory {
				if s == state {
					return
				}
			}
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("task %s never reached %s within %s", taskID, state, within)
}

func liveWaitFinal(t *testing.T, ctx context.Context, c *lib.Client, session, taskID string, within time.Duration) *lib.Task {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		task, err := c.TasksGet(ctx, session, taskID)
		if err == nil && task.Final {
			return task
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("task %s never reached a terminal state within %s", taskID, within)
	return nil
}

// liveWaitProgress blocks until the task's progress artifact carries want.
// On timeout it dumps the pod, because every reason this waits forever is on
// the pod side (unscheduled, image pull, a harness that never started) and
// the cleanup deletes the evidence a moment later.
func liveWaitProgress(t *testing.T, ctx context.Context, c *lib.Client, kubeContext, namespace, session, taskID, want string, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		task, err := c.TasksGet(ctx, session, taskID)
		if err == nil && strings.Contains(liveArtifactText(task, lib.ArtifactProgress), want) {
			return
		}
		time.Sleep(250 * time.Millisecond)
	}
	describe, _ := exec.Command("kubectl", "--context", kubeContext, "-n", namespace,
		"describe", "pod", session).CombinedOutput()
	logs, _ := exec.Command("kubectl", "--context", kubeContext, "-n", namespace,
		"logs", session, "--tail=50").CombinedOutput()
	t.Fatalf("progress artifact never carried %q\n--- describe ---\n%s\n--- logs ---\n%s",
		want, describe, logs)
}

// liveReplayEvents reads the task's raw event envelopes in stream order as
// the gateway user - the fold drops what this test needs to look at (the
// non-final status messages).
func liveReplayEvents(t *testing.T, ctx context.Context, url, gwPass, session, taskID string) []*lib.Envelope {
	t.Helper()
	nc, err := nats.Connect(url,
		nats.UserInfo("gateway", gwPass),
		nats.CustomInboxPrefix("_INBOX.gateway"))
	if err != nil {
		t.Fatalf("replay connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("replay jetstream: %v", err)
	}
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{lib.TaskEventsSubject(session, taskID)},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	if err != nil {
		t.Fatalf("replay consumer: %v", err)
	}
	batch, err := cons.FetchNoWait(1000)
	if err != nil {
		t.Fatalf("replay fetch: %v", err)
	}
	var events []*lib.Envelope
	for msg := range batch.Messages() {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			t.Fatalf("replay parse: %v", err)
		}
		events = append(events, env)
	}
	return events
}

func liveArtifactText(task *lib.Task, name string) string {
	a := task.Artifact(name)
	if a == nil {
		return ""
	}
	var b strings.Builder
	for _, p := range a.Parts {
		b.WriteString(p.Text)
	}
	return b.String()
}
