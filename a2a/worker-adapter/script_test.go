package workeradapter

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

// scriptRoot is a script root in a temp dir, owned by the test's uid,
// with a check that takes that uid as the scripts' owner and some other uid
// as the worker's.
func scriptRoot(t *testing.T) (string, scriptFileCheck) {
	t.Helper()
	tmp, err := filepath.EvalSymlinks(t.TempDir()) // macOS: /var is /private/var
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(tmp, "scripts")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	return root, scriptFileCheck{root: root + "/", ownerUID: uint32(os.Getuid()), workerUID: os.Getuid() + 4242, set: true}
}

func writeScript(t *testing.T, path, body string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil { // past the umask
		t.Fatal(err)
	}
}

// A script's file is resolved and checked before it runs: under the root
// after every symlink, a regular file, owned by the scripts' owner and
// writable by no one else, in directories that are the same, and never
// owned by the worker itself.
func TestAScriptFileIsCheckedBeforeItRuns(t *testing.T) {
	root, check := scriptRoot(t)
	good := filepath.Join(root, "helpers", "watch.sh")
	writeScript(t, good, "#!/bin/sh\necho ok\n", 0o755)
	if got, err := check.resolve(lib.ScriptRoot + "helpers/watch.sh"); err != nil || got != good {
		t.Fatalf("a good script: %q, %v", got, err)
	}
	inside := filepath.Join(root, "alias.sh")
	if err := os.Symlink(good, inside); err != nil {
		t.Fatal(err)
	}
	if got, err := check.resolve(lib.ScriptRoot + "alias.sh"); err != nil || got != good {
		t.Errorf("a symlink to a script inside the root: %q, %v; want the target", got, err)
	}

	outside := filepath.Join(t.TempDir(), "evil.sh")
	writeScript(t, outside, "#!/bin/sh\nid\n", 0o755)
	escape := filepath.Join(root, "escape.sh")
	if err := os.Symlink(outside, escape); err != nil {
		t.Fatal(err)
	}
	groupWritable := filepath.Join(root, "gw.sh")
	writeScript(t, groupWritable, "#!/bin/sh\n", 0o775)
	worldWritableDir := filepath.Join(root, "open", "a.sh")
	writeScript(t, worldWritableDir, "#!/bin/sh\n", 0o755)
	if err := os.Chmod(filepath.Dir(worldWritableDir), 0o777); err != nil {
		t.Fatal(err)
	}
	notRegular := filepath.Join(root, "adir")
	if err := os.Mkdir(notRegular, 0o755); err != nil {
		t.Fatal(err)
	}
	fifo := filepath.Join(root, "fifo")
	if err := syscall.Mkfifo(fifo, 0o644); err != nil {
		t.Fatal(err)
	}
	// sp is a file under the test's root as the confirm names it.
	sp := func(fsPath string) string { return lib.ScriptRoot + strings.TrimPrefix(fsPath, root+"/") }
	for name, tc := range map[string]struct {
		path  string
		check scriptFileCheck
	}{
		"a path outside the root":        {outside, check},
		"a symlink escaping the root":    {sp(escape), check},
		"a group-writable file":          {sp(groupWritable), check},
		"a world-writable directory":     {sp(worldWritableDir), check},
		"a directory":                    {sp(notRegular), check},
		"a fifo":                         {sp(fifo), check},
		"a missing file":                 {lib.ScriptRoot + "nope.sh", check},
		"a file another uid owns":        {sp(good), scriptFileCheck{root: check.root, ownerUID: check.ownerUID + 1, workerUID: check.workerUID, set: true}},
		"a worker that owns the scripts": {sp(good), scriptFileCheck{root: check.root, ownerUID: check.ownerUID, workerUID: int(check.ownerUID), set: true}},
	} {
		if got, err := tc.check.resolve(tc.path); err == nil {
			t.Errorf("%s: resolved to %q; want a refusal", name, got)
		}
	}

	// The image's own check holds the path to the script root's shape
	// before it touches the file system.
	for _, p := range []string{lib.ScriptRoot + "../../bin/sh", "/bin/sh", lib.ScriptRoot + "a//b", lib.ScriptRoot + "a;id"} {
		if _, err := (scriptFileCheck{}).resolve(p); err == nil || !strings.Contains(err.Error(), "not a script path") {
			t.Errorf("%q: %v; want it refused as not a script path", p, err)
		}
	}
}

// A script file with the setuid or setgid bit is refused, and the same file without either
// resolves.
func TestAScriptFileThatIsSetuidOrSetgidIsRefused(t *testing.T) {
	root, check := scriptRoot(t)
	for name, bit := range map[string]os.FileMode{"setuid": os.ModeSetuid, "setgid": os.ModeSetgid} {
		p := filepath.Join(root, name+".sh")
		writeScript(t, p, "#!/bin/sh\necho ok\n", 0o755)
		if got, err := check.resolve(lib.ScriptRoot + name + ".sh"); err != nil || got != p {
			t.Fatalf("%s: without the bit: %q, %v; want it resolved", name, got, err)
		}
		if err := os.Chmod(p, 0o755|bit); err != nil {
			t.Fatal(err)
		}
		fi, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if fi.Mode()&bit == 0 {
			// A setgid bit for a group the test's user isn't in is cleared by the kernel.
			t.Logf("%s: the file system cleared the bit (%v), so this case can't be checked here", name, fi.Mode())
			continue
		}
		if got, err := check.resolve(lib.ScriptRoot + name + ".sh"); err == nil || !strings.Contains(err.Error(), "setuid or setgid") {
			t.Errorf("%s: resolved to %q, %v; want it refused as setuid or setgid", name, got, err)
		}
	}
}

// mintProxy is a credential-proxy task credential from kp, for task on pod,
// with a trigger naming script (none when script is "").
func mintProxy(t *testing.T, kp nkeys.KeyPair, task, pod, script string, aud taskcred.Audience) string {
	t.Helper()
	c := taskcred.Claims{Task: task, Profile: pwProfile, Tier: "unattended", Pod: pod,
		PodUID: "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", Audience: aud, Expires: time.Now().Add(10 * time.Minute)}
	if aud == taskcred.AudienceCredentialProxy {
		c.Trigger = &taskcred.Trigger{Name: "schedule/stall-watch", Origin: lib.TriggerOriginBuiltin, Script: script}
	}
	tok, err := taskcred.Mint(kp, c)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func signingKey(t *testing.T) (nkeys.KeyPair, string) {
	t.Helper()
	kp, err := taskcred.NewSigningKey()
	if err != nil {
		t.Fatal(err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		t.Fatal(err)
	}
	return kp, pub
}

// The confirm's script runs only when the task credential verifies against
// the install's keys, is this task's on this pod, and names the same
// script. A credential signed with a retired key is refused; one signed
// with the key a rotation replaces is taken while both are trusted.
func TestAScriptRunsOnlyWithAVerifiedCredentialNamingIt(t *testing.T) {
	kp, pub := signingKey(t)
	old, oldPub := signingKey(t)
	now := time.Now()
	good := mintProxy(t, kp, "t1", pwPod, "stall-watch", taskcred.AudienceCredentialProxy)
	if err := verifyScriptCredential(good, []string{pub}, now, "t1", pwPod, "stall-watch"); err != nil {
		t.Fatalf("a good credential: %v", err)
	}
	byOld := mintProxy(t, old, "t1", pwPod, "stall-watch", taskcred.AudienceCredentialProxy)
	if err := verifyScriptCredential(byOld, []string{pub, oldPub}, now, "t1", pwPod, "stall-watch"); err != nil {
		t.Errorf("a credential from the key a rotation replaces, while both are trusted: %v", err)
	}
	for name, tc := range map[string]struct {
		token  string
		keys   []string
		script string
	}{
		"no keys":                  {good, nil, "stall-watch"},
		"no credential":            {"", []string{pub}, "stall-watch"},
		"a retired key":            {byOld, []string{pub}, "stall-watch"},
		"another task":             {mintProxy(t, kp, "t2", pwPod, "stall-watch", taskcred.AudienceCredentialProxy), []string{pub}, "stall-watch"},
		"another pod":              {mintProxy(t, kp, "t1", "other-pod", "stall-watch", taskcred.AudienceCredentialProxy), []string{pub}, "stall-watch"},
		"no script in the trigger": {mintProxy(t, kp, "t1", pwPod, "", taskcred.AudienceCredentialProxy), []string{pub}, "stall-watch"},
		"another script":           {good, []string{pub}, "leeway-watch"},
		"the nats credential":      {mintProxy(t, kp, "t1", pwPod, "", taskcred.AudienceNATS), []string{pub}, "stall-watch"},
		"a tampered credential":    {good[:len(good)-2] + "xx", []string{pub}, "stall-watch"},
	} {
		if err := verifyScriptCredential(tc.token, tc.keys, now, "t1", pwPod, tc.script); err == nil {
			t.Errorf("%s: verified; want a refusal", name)
		}
	}
}

// A script's environment is the worker's rendered hand-off and the task's
// parameters as KA_PARAM_<NAME>, nothing else: a parameter named for a
// loader or search path lands under the prefix, and the worker's other
// variables stay out.
func TestAScriptsEnvironmentIsTheHandOffAndItsParametersOnly(t *testing.T) {
	worker := map[string]string{"PATH": "/opt/tools/kubectl/bin:/usr/bin", "HOME": "/home/worker",
		"CREDENTIAL_PROXY_URL": "http://broker", "CREDENTIAL_PROXY_TOKEN_FILE": "/var/run/broker/token",
		"CREDENTIAL_PROXY_TASK_TOKEN_FILE": "/var/run/task/token", "ANTHROPIC_API_KEY": "secret", "NATS_URL": "nats://bus"}
	origin := profileSubmission(t, "t1", "ignored", map[string]string{"ld_preload": "x.so", "path": "tmp", "pythonpath": "tmp", "cluster": "prod"})
	env, err := scriptEnv(origin, func(k string) string { return worker[k] })
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"PATH=/opt/tools/kubectl/bin:/usr/bin", "HOME=/home/worker", "CREDENTIAL_PROXY_URL=http://broker",
		"CREDENTIAL_PROXY_TOKEN_FILE=/var/run/broker/token", "CREDENTIAL_PROXY_TASK_TOKEN_FILE=/var/run/task/token",
		"KA_PARAM_CLUSTER=prod", "KA_PARAM_LD_PRELOAD=x.so", "KA_PARAM_PATH=tmp", "KA_PARAM_PYTHONPATH=tmp"}
	if !slices.Equal(env, want) {
		t.Errorf("env %q\nwant %q", env, want)
	}
	for _, e := range env {
		for _, name := range []string{"LD_PRELOAD=", "PYTHONPATH=", "ANTHROPIC_API_KEY=", "NATS_URL="} {
			if strings.HasPrefix(e, name) {
				t.Errorf("%s reached the script's environment", e)
			}
		}
	}
	long := profileSubmission(t, "t1", "x", map[string]string{strings.Repeat("a", 33): "v"})
	if _, err := scriptEnv(long, func(string) string { return "" }); err == nil {
		t.Error("a parameter name too long for KA_PARAM_<NAME> was taken")
	}
}

// The conformance row: no field of the task's payload reaches the exec'd
// path or the process's environment, except a parameter as KA_PARAM_*. The
// script here is env itself, so what it prints is exactly what it got.
func TestNoTaskPayloadFieldReachesTheScriptButItsParameters(t *testing.T) {
	const sentinel = "SENTINEL-7d1f"
	origin := profileSubmission(t, "t1", "text "+sentinel, map[string]string{"cluster": sentinel})
	origin.ContextID, origin.CorrelationID = "ctx-"+sentinel, "corr-"+sentinel
	env, err := scriptEnv(origin, func(k string) string {
		if k == "PATH" {
			return "/usr/bin:/bin"
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	res := execScript(context.Background(), &scriptRun{name: "env", path: "/usr/bin/env", env: env, timeout: 10 * time.Second}, scriptSignals{})
	if res.exitCode != 0 || res.startErr != nil {
		t.Fatalf("env: exit %d, %v", res.exitCode, res.startErr)
	}
	for _, line := range strings.Split(strings.TrimSpace(res.stdout), "\n") {
		if strings.Contains(line, sentinel) && line != "KA_PARAM_CLUSTER="+sentinel {
			t.Errorf("the payload reached the script outside KA_PARAM_*: %q", line)
		}
	}
	if !strings.Contains(res.stdout, "KA_PARAM_CLUSTER="+sentinel) {
		t.Errorf("the parameter didn't reach the script: %q", res.stdout)
	}
}

// A script's stdout, capped and marked when cut, is its result; its stderr
// is capped; its exit code and its timeout are reported, and a timeout
// kills its whole process group.
func TestAScriptsOutputIsCappedAndItsEndReported(t *testing.T) {
	root, _ := scriptRoot(t)
	run := func(name, body string, timeout time.Duration) scriptResult {
		t.Helper()
		p := filepath.Join(root, name)
		writeScript(t, p, "#!/bin/sh\n"+body, 0o755)
		return execScript(context.Background(), &scriptRun{name: name, path: p, env: []string{"PATH=/usr/bin:/bin"}, timeout: timeout}, scriptSignals{})
	}
	big := run("big.sh", "head -c 200000 /dev/zero | tr '\\0' a\nhead -c 50000 /dev/zero | tr '\\0' e >&2\n", 10*time.Second)
	if want := scriptStdoutCap + len(scriptTruncated); len(big.stdout) != want || !strings.HasSuffix(big.stdout, scriptTruncated) {
		t.Errorf("stdout of %d bytes, want %d ending in the truncation mark", len(big.stdout), want)
	}
	if len(big.stderr) > scriptStderrCap {
		t.Errorf("stderr of %d bytes, over the %d cap", len(big.stderr), scriptStderrCap)
	}
	if r := run("fail.sh", "echo partial\nexit 3\n", 10*time.Second); r.exitCode != 3 || r.timedOut {
		t.Errorf("exit %d timedOut %v; want 3", r.exitCode, r.timedOut)
	}
	marker := filepath.Join(root, "child-alive")
	start := time.Now()
	r := run("slow.sh", "(sleep 3; touch "+marker+") &\nsleep 30\n", 300*time.Millisecond)
	if !r.timedOut || time.Since(start) > 10*time.Second {
		t.Fatalf("timedOut %v after %s; want a timeout at its bound", r.timedOut, time.Since(start))
	}
	time.Sleep(4 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Error("the script's child outlived its timeout: the process group wasn't killed")
	}
}

// startScriptWorker is startProfileWorker with a script check and the
// task-signing keys.
func startScriptWorker(t *testing.T, ctx context.Context, cfg Config, pw ProfileWorkerConfig) <-chan error {
	t.Helper()
	ln, addr := readyListener(t)
	pw.Tier, pw.ReadyAddr, pw.readyListener, pw.deadlineMargin = pwTier, addr, ln, time.Second
	done := make(chan error, 1)
	go func() { done <- RunProfileWorker(ctx, cfg, pw) }()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get("http://" + addr + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return done
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("the worker never became ready")
	return done
}

// End to end on a one-shot worker: a confirm naming a script, with a
// credential naming the same one, runs the script in place of the harness;
// its stdout is the result. A mismatched credential, or a pool worker,
// rejects the task with script-refused and runs nothing.
func TestAScriptRunTaskRunsItsScriptOrIsRefused(t *testing.T) {
	kp, pub := signingKey(t)
	root, check := scriptRoot(t)
	marker := filepath.Join(t.TempDir(), "ran")
	script := filepath.Join(root, "watch.sh")
	writeScript(t, script, "#!/bin/sh\ntouch "+marker+"\necho \"watched $KA_PARAM_CLUSTER\"\n", 0o755)
	sc := &lib.DispatchScript{Name: "stall-watch", Path: lib.ScriptRoot + "watch.sh", TimeoutSeconds: 30}
	// The harness must never run for a script task.
	harness := stub(t, "echo harness-ran >&2\nexit 7\n")

	for _, tc := range []struct {
		name       string
		oneShot    bool
		credScript string
		want       lib.TaskState
	}{
		{"runs", true, "stall-watch", lib.StateCompleted},
		{"another script in the credential", true, "leeway-watch", lib.StateRejected},
		{"a pool worker", false, "stall-watch", lib.StateRejected},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_ = os.Remove(marker)
			url := startServer(t)
			d := newFakeDispatcher(t, url)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			startScriptWorker(t, ctx, profileWorkerConfig(t, url, harness), ProfileWorkerConfig{
				OneShot: tc.oneShot, TaskSigningKeys: pub, scriptCheck: check})
			task := "task-script-1"
			d.offer(task, profileSubmission(t, task, "run the watch", map[string]string{"cluster": "prod-east"}), 5)
			if a := d.nextAccept(5 * time.Second); !a.Accepted {
				t.Fatalf("answer %+v, want an accept", a)
			}
			d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task, Script: sc,
				ProxyToken: mintProxy(t, kp, task, pwPod, tc.credScript, taskcred.AudienceCredentialProxy)})
			if _, final := drainLeases(d, task, 20*time.Second); !final {
				t.Fatal("no final renewal")
			}
			got := foldTask(t, testClient(t, url), pwProfile, task)
			if got.State != tc.want {
				t.Fatalf("state %q, want %q", got.State, tc.want)
			}
			_, err := os.Stat(marker)
			switch tc.want {
			case lib.StateCompleted:
				if err != nil {
					t.Error("the script didn't run")
				}
				if text := artifactText(got, lib.ArtifactResult); text != "watched prod-east\n" {
					t.Errorf("result %q, want the script's stdout", text)
				}
			default:
				if err == nil {
					t.Error("a refused script ran")
				}
				if msg := terminalText(t, url, pwProfile, task); !strings.Contains(msg, "script-refused") {
					t.Errorf("terminal %q, want script-refused", msg)
				}
			}
		})
	}
}

// scriptTaskRig is a one-shot worker with a script check and keys, offered
// and accepted one task; the caller sends the confirm.
func scriptTaskRig(t *testing.T, harness []string, pw ProfileWorkerConfig) (url string, d *fakeDispatcher, task string) {
	t.Helper()
	url = startServer(t)
	d = newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	startScriptWorker(t, ctx, profileWorkerConfig(t, url, harness), pw)
	task = "task-script-2"
	d.offer(task, profileSubmission(t, task, "run the watch", map[string]string{"cluster": "prod-east"}), 5)
	if a := d.nextAccept(5 * time.Second); !a.Accepted {
		t.Fatalf("answer %+v, want an accept", a)
	}
	return url, d, task
}

// A cancel on the task's .in stops a running script (its process group
// killed) and ends the task canceled; an abandon stops it and publishes no
// terminal, the dispatcher's being the one.
func TestARunningScriptIsStoppedByACancelOrAnAbandon(t *testing.T) {
	kp, pub := signingKey(t)
	root, check := scriptRoot(t)
	started := filepath.Join(t.TempDir(), "started")
	writeScript(t, filepath.Join(root, "slow.sh"), "#!/bin/sh\ntouch "+started+"\nsleep 30\necho never\n", 0o755)
	sc := &lib.DispatchScript{Name: "stall-watch", Path: lib.ScriptRoot + "slow.sh", TimeoutSeconds: 60}
	harness := stub(t, "exit 7\n")
	waitStarted := func() {
		t.Helper()
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			if _, err := os.Stat(started); err == nil {
				return
			}
			time.Sleep(50 * time.Millisecond)
		}
		t.Fatal("the script never started")
	}

	t.Run("cancel", func(t *testing.T) {
		_ = os.Remove(started)
		url, d, task := scriptTaskRig(t, harness, ProfileWorkerConfig{OneShot: true, TaskSigningKeys: pub, scriptCheck: check})
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task, Script: sc,
			ProxyToken: mintProxy(t, kp, task, pwPod, "stall-watch", taskcred.AudienceCredentialProxy)})
		waitStarted()
		env, err := lib.NewCancelEnvelope(gatewayParty, task, "ctx-"+task, "corr-"+task, lib.WithTo(lib.Party{Session: pwProfile}))
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		start := time.Now()
		if err := testClient(t, url).Publish(ctx, lib.TaskInSubject(pwProfile, task), env); err != nil {
			t.Fatal(err)
		}
		if _, final := drainLeases(d, task, 20*time.Second); !final {
			t.Fatal("no final renewal")
		}
		if took := time.Since(start); took > 15*time.Second {
			t.Errorf("the cancel took %s; the script wasn't stopped", took)
		}
		if got := foldTask(t, testClient(t, url), pwProfile, task); got.State != lib.StateCanceled {
			t.Errorf("state %q, want canceled", got.State)
		}
	})

	t.Run("abandon", func(t *testing.T) {
		_ = os.Remove(started)
		url, d, task := scriptTaskRig(t, harness, ProfileWorkerConfig{OneShot: true, TaskSigningKeys: pub, scriptCheck: check})
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task, Script: sc,
			ProxyToken: mintProxy(t, kp, task, pwPod, "stall-watch", taskcred.AudienceCredentialProxy)})
		waitStarted()
		d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: task, Reason: "deadline-exceeded"})
		time.Sleep(3 * time.Second)
		if got := foldTask(t, testClient(t, url), pwProfile, task); got.Final {
			t.Errorf("an abandoned script task published terminal %q; the dispatcher writes it", got.State)
		}
	})
}

// No script runs without keys to verify its credential, and a confirm
// with no script whose credential names one is refused rather than run as
// a model turn: either way the task is rejected script-refused and neither
// the script nor the harness runs.
func TestAScriptTaskWithoutKeysOrWithoutAScriptConfirmRunsNothing(t *testing.T) {
	kp, pub := signingKey(t)
	root, check := scriptRoot(t)
	ran := filepath.Join(t.TempDir(), "ran")
	writeScript(t, filepath.Join(root, "watch.sh"), "#!/bin/sh\ntouch "+ran+"\n", 0o755)
	harness := stub(t, "touch "+ran+"\nexit 7\n")
	sc := &lib.DispatchScript{Name: "stall-watch", Path: lib.ScriptRoot + "watch.sh", TimeoutSeconds: 30}
	for name, tc := range map[string]struct {
		keys   string
		script *lib.DispatchScript
	}{
		"no keys on the worker":        {"", sc},
		"malformed keys":               {"not-a-key", sc},
		"no script on the confirm":     {pub, nil},
		"no script and no keys either": {"", nil},
	} {
		t.Run(name, func(t *testing.T) {
			_ = os.Remove(ran)
			url, d, task := scriptTaskRig(t, harness, ProfileWorkerConfig{OneShot: true, TaskSigningKeys: tc.keys, scriptCheck: check})
			d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task, Script: tc.script,
				ProxyToken: mintProxy(t, kp, task, pwPod, "stall-watch", taskcred.AudienceCredentialProxy)})
			if _, final := drainLeases(d, task, 20*time.Second); !final {
				t.Fatal("no final renewal")
			}
			if got := foldTask(t, testClient(t, url), pwProfile, task); got.State != lib.StateRejected {
				t.Errorf("state %q, want rejected", got.State)
			}
			if msg := terminalText(t, url, pwProfile, task); !strings.Contains(msg, "script-refused") {
				t.Errorf("terminal %q, want script-refused", msg)
			}
			if _, err := os.Stat(ran); err == nil {
				t.Error("the script or the harness ran")
			}
		})
	}
}

// A script with nothing to pass gets an empty environment, never the
// worker's; and one that exits 0 with a child still holding its output is
// a success, with the child killed.
func TestAScriptGetsNoInheritedEnvironmentAndLeavesNoChild(t *testing.T) {
	t.Setenv("KA_TEST_WORKER_SECRET", "leaked")
	env, err := scriptEnv(profileSubmission(t, "t1", "x", nil), func(string) string { return "" })
	if err != nil || env == nil || len(env) != 0 {
		t.Fatalf("env %q (nil %v), %v; want empty and not nil", env, env == nil, err)
	}
	res := execScript(context.Background(), &scriptRun{name: "env", path: "/usr/bin/env", env: env, timeout: 10 * time.Second}, scriptSignals{})
	if strings.Contains(res.stdout, "KA_TEST_WORKER_SECRET") {
		t.Errorf("the worker's environment reached the script: %q", res.stdout)
	}

	root, _ := scriptRoot(t)
	marker := filepath.Join(root, "child-alive")
	p := filepath.Join(root, "bg.sh")
	writeScript(t, p, "#!/bin/sh\n(sleep 8; touch "+marker+") &\necho done\nexit 0\n", 0o755)
	r := execScript(context.Background(), &scriptRun{name: "bg", path: p, env: []string{"PATH=/usr/bin:/bin"}, timeout: 30 * time.Second}, scriptSignals{})
	if r.exitCode != 0 || r.timedOut || !strings.HasPrefix(r.stdout, "done") {
		t.Errorf("exit %d timedOut %v stdout %q; want a success", r.exitCode, r.timedOut, r.stdout)
	}
	time.Sleep(9 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Error("the script's child outlived a successful exit")
	}
}

// A cap that falls inside a multi-byte character drops that character's leading bytes, so the
// truncated stdout is valid UTF-8 before its marker.
func TestACappedOutputIsCutOnACharacterBoundary(t *testing.T) {
	b := &cappedBuffer{max: 5}
	_, _ = b.Write([]byte("abcd\u00e9xyz")) // "é" is two bytes, the 5th and 6th
	got := b.text()
	if !utf8.ValidString(got) {
		t.Fatalf("text %q is not valid UTF-8", got)
	}
	if want := "abcd" + scriptTruncated; got != want {
		t.Errorf("text = %q, want %q", got, want)
	}
}

// Only an incomplete character at the cut is trimmed, by at most three bytes, whatever its width
// and wherever the cut falls inside it.
func TestACappedOutputTrimsAtMostThreeBytesAtTheCut(t *testing.T) {
	for _, r := range []string{"\u00e9", "\u20ac", "\U0001F600"} { // two, three and four bytes
		for split := 1; split < len(r); split++ {
			prefix := strings.Repeat("a", 10)
			b := &cappedBuffer{max: len(prefix) + split}
			_, _ = b.Write([]byte(prefix + r + "tail"))
			got := strings.TrimSuffix(b.text(), scriptTruncated)
			if got != prefix {
				t.Errorf("%q cut after %d of its %d bytes: kept %q, want %q", r, split, len(r), got, prefix)
			}
			if trimmed := b.max - len(got); trimmed > utf8.UTFMax-1 {
				t.Errorf("%q cut after %d bytes: trimmed %d bytes, more than %d", r, split, trimmed, utf8.UTFMax-1)
			}
		}
		// A whole character at the cut is kept.
		b := &cappedBuffer{max: len(r)}
		_, _ = b.Write([]byte(r + "tail"))
		if got := b.text(); got != r+scriptTruncated {
			t.Errorf("%q whole at the cut: text %q, want it kept", r, got)
		}
	}
}

// An invalid byte early in a capped output doesn't cost the output after it: the result keeps
// the full cap.
func TestAnInvalidByteEarlyInACappedOutputKeepsTheRest(t *testing.T) {
	b := &cappedBuffer{max: scriptStdoutCap}
	_, _ = b.Write(append([]byte("ok\xff"), bytes.Repeat([]byte("a"), 2*scriptStdoutCap)...))
	got := b.text()
	if want := scriptStdoutCap + len(scriptTruncated); len(got) != want {
		t.Errorf("text of %d bytes, want %d (the cap and the marker)", len(got), want)
	}
	if !strings.HasPrefix(got, "ok\xffaaa") {
		t.Errorf("text starts %q, want the script's bytes as written", got[:8])
	}
}

// A script that exits 0 while a child it started still holds its stdout, with its bound passing
// before the worker stops waiting on the pipe, is reported by its exit, not as a timeout: the
// script finished inside its bound.
func TestAScriptThatExitsZeroWithAChildHoldingItsOutputIsNotATimeout(t *testing.T) {
	root, _ := scriptRoot(t)
	p := filepath.Join(root, "linger.sh")
	writeScript(t, p, "#!/bin/sh\nsleep 30 &\necho done\nexit 0\n", 0o755)
	// The bound is shorter than the grace the worker gives a held pipe, so it passes while the
	// child holds stdout, and long enough for the script to start and exit on a loaded machine.
	timeout := scriptKillGrace * 4 / 5
	r := execScript(context.Background(), &scriptRun{name: "linger", path: p, env: []string{"PATH=/usr/bin:/bin"}, timeout: timeout}, scriptSignals{})
	if r.timedOut || r.exitCode != 0 || r.startErr != nil || !strings.HasPrefix(r.stdout, "done") {
		t.Errorf("timedOut %v exit %d startErr %v stdout %q; want a success by its exit", r.timedOut, r.exitCode, r.startErr, r.stdout)
	}
}

// The directories on the path the confirm names are checked as well as the resolved file's: a
// symlinked directory, or a group-writable one, on the named path is refused even when the file
// it leads to passes every check.
func TestAScriptsNamedPathsDirectoriesAreCheckedToo(t *testing.T) {
	root, check := scriptRoot(t)
	good := filepath.Join(root, "helpers", "watch.sh")
	writeScript(t, good, "#!/bin/sh\necho ok\n", 0o755)
	if err := os.Symlink(filepath.Join(root, "helpers"), filepath.Join(root, "linked")); err != nil {
		t.Fatal(err)
	}
	openDir := filepath.Join(root, "open")
	if err := os.Mkdir(openDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(openDir, 0o775); err != nil { // past the umask
		t.Fatal(err)
	}
	if err := os.Symlink(good, filepath.Join(openDir, "watch.sh")); err != nil {
		t.Fatal(err)
	}
	// A named path whose own directories pass, through a symlinked file, still resolves.
	tidy := filepath.Join(root, "tidy")
	if err := os.Mkdir(tidy, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(good, filepath.Join(tidy, "watch.sh")); err != nil {
		t.Fatal(err)
	}
	if got, err := check.resolve(lib.ScriptRoot + "tidy/watch.sh"); err != nil || got != good {
		t.Fatalf("a symlinked file in a sound directory: %q, %v; want the target", got, err)
	}
	for name, p := range map[string]string{
		"a symlinked directory on the named path":      lib.ScriptRoot + "linked/watch.sh",
		"a group-writable directory on the named path": lib.ScriptRoot + "open/watch.sh",
	} {
		if got, err := check.resolve(p); err == nil {
			t.Errorf("%s: resolved to %q; want a refusal", name, got)
		}
	}
}

// A submission whose own trigger names a script is a script task's even when the confirm carries
// no script and the task credential names none (or there is no credential at all): it is
// rejected script-refused and never falls through to a model turn.
func TestASubmissionNamingAScriptNeverRunsAsAModelTurn(t *testing.T) {
	kp, pub := signingKey(t)
	_, check := scriptRoot(t)
	ran := filepath.Join(t.TempDir(), "ran")
	harness := stub(t, "touch "+ran+"\nexit 7\n")
	const scriptAuthority = `{"grants":{"capability":{"key":"cap-a","revision":1}},"trigger":{"name":"schedule/stall-watch","origin":"builtin","script":"stall-watch"}}`
	for name, tc := range map[string]struct {
		token     func(task string) string
		authority string
		want      string
	}{
		"no task credential":                    {func(string) string { return "" }, scriptAuthority, "submission's trigger"},
		"a credential whose trigger names none": {func(task string) string { return mintProxy(t, kp, task, pwPod, "", taskcred.AudienceCredentialProxy) }, scriptAuthority, "submission's trigger"},
		"an authority block that can't be read": {func(string) string { return "" }, `[1]`, "authority block can't be read"},
	} {
		token := tc.token
		t.Run(name, func(t *testing.T) {
			_ = os.Remove(ran)
			url := startServer(t)
			d := newFakeDispatcher(t, url)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			startScriptWorker(t, ctx, profileWorkerConfig(t, url, harness), ProfileWorkerConfig{OneShot: true, TaskSigningKeys: pub, scriptCheck: check})
			task := "task-script-3"
			origin := profileSubmission(t, task, "run the watch", map[string]string{"cluster": "prod-east"})
			origin.Authority = []byte(tc.authority)
			d.offer(task, origin, 5)
			if a := d.nextAccept(5 * time.Second); !a.Accepted {
				t.Fatalf("answer %+v, want an accept", a)
			}
			d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task, ProxyToken: token(task)})
			if _, final := drainLeases(d, task, 20*time.Second); !final {
				t.Fatal("no final renewal")
			}
			if got := foldTask(t, testClient(t, url), pwProfile, task); got.State != lib.StateRejected {
				t.Errorf("state %q, want rejected", got.State)
			}
			if msg := terminalText(t, url, pwProfile, task); !strings.Contains(msg, "script-refused") || !strings.Contains(msg, tc.want) {
				t.Errorf("terminal %q, want script-refused naming %q", msg, tc.want)
			}
			if _, err := os.Stat(ran); err == nil {
				t.Error("the harness ran a model turn for a script task")
			}
		})
	}
}

// End to end: a script that exits non-zero ends the task failed script-failed with its exit code,
// one that runs past its bound ends it failed script-timeout, and the worker's log line for a run
// carries the stderr's size, never its text.
func TestAScriptsFailureAndTimeoutAreItsTerminalsAndOnlyItsStderrSizeIsLogged(t *testing.T) {
	const sentinel = "STDERR-SENTINEL-41c9"
	kp, pub := signingKey(t)
	root, check := scriptRoot(t)
	writeScript(t, filepath.Join(root, "fail.sh"), "#!/bin/sh\necho "+sentinel+" >&2\nexit 3\n", 0o755)
	writeScript(t, filepath.Join(root, "slow.sh"), "#!/bin/sh\nsleep 30\n", 0o755)
	harness := stub(t, "exit 7\n")
	for _, tc := range []struct {
		name, file string
		want       []string
	}{
		{"fails", "fail.sh", []string{"reason: script-failed", "exited 3"}},
		{"times out", "slow.sh", []string{"reason: script-timeout", "1s bound"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs := &syncBuffer{}
			url := startServer(t)
			d := newFakeDispatcher(t, url)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			cfg := profileWorkerConfig(t, url, harness)
			cfg.Logger = slog.New(slog.NewTextHandler(logs, nil))
			startScriptWorker(t, ctx, cfg, ProfileWorkerConfig{OneShot: true, TaskSigningKeys: pub, scriptCheck: check})
			task := "task-script-4"
			d.offer(task, profileSubmission(t, task, "run the watch", nil), 5)
			if a := d.nextAccept(5 * time.Second); !a.Accepted {
				t.Fatalf("answer %+v, want an accept", a)
			}
			d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task,
				Script:     &lib.DispatchScript{Name: "stall-watch", Path: lib.ScriptRoot + tc.file, TimeoutSeconds: 1},
				ProxyToken: mintProxy(t, kp, task, pwPod, "stall-watch", taskcred.AudienceCredentialProxy)})
			if _, final := drainLeases(d, task, 20*time.Second); !final {
				t.Fatal("no final renewal")
			}
			if got := foldTask(t, testClient(t, url), pwProfile, task); got.State != lib.StateFailed {
				t.Errorf("state %q, want failed", got.State)
			}
			msg := terminalText(t, url, pwProfile, task)
			for _, w := range tc.want {
				if !strings.Contains(msg, w) {
					t.Errorf("terminal %q, want %q in it", msg, w)
				}
			}
			out := logs.String()
			if !strings.Contains(out, "script ran") || !strings.Contains(out, "stderrBytes=") {
				t.Errorf("no script-ran log line with the stderr's size in:\n%s", out)
			}
			if tc.file == "fail.sh" && !strings.Contains(out, fmt.Sprintf("stderrBytes=%d", len(sentinel)+1)) {
				t.Errorf("the log line doesn't carry the stderr's %d bytes:\n%s", len(sentinel)+1, out)
			}
			if strings.Contains(out, sentinel) {
				t.Errorf("the script's stderr text reached the log:\n%s", out)
			}
		})
	}
}

// prepareScript bounds a script by its own timeout within what is left of the task's deadline,
// and refuses one with no time left.
func TestAScriptsBoundIsItsTimeoutWithinTheTasksDeadline(t *testing.T) {
	kp, pub := signingKey(t)
	root, check := scriptRoot(t)
	writeScript(t, filepath.Join(root, "watch.sh"), "#!/bin/sh\necho ok\n", 0o755)
	w := &profileWorker{cfg: Config{PodName: pwPod}, pw: ProfileWorkerConfig{OneShot: true, TaskSigningKeys: pub, scriptCheck: check}}
	heldFor := func(timeoutSeconds int) *held {
		return &held{offer: lib.DispatchMessage{TaskID: "t1"}, origin: profileSubmission(t, "t1", "x", nil),
			proxyToken: mintProxy(t, kp, "t1", pwPod, "stall-watch", taskcred.AudienceCredentialProxy),
			script:     &lib.DispatchScript{Name: "stall-watch", Path: lib.ScriptRoot + "watch.sh", TimeoutSeconds: timeoutSeconds}}
	}
	for name, tc := range map[string]struct {
		timeoutSeconds int
		taskDeadline   time.Duration
		want           time.Duration
	}{
		"the script's timeout, inside the deadline": {2, 10 * time.Second, 2 * time.Second},
		"the deadline, inside the script's timeout": {60, 10 * time.Second, 10 * time.Second},
	} {
		sr, why := w.prepareScript(heldFor(tc.timeoutSeconds), tc.taskDeadline)
		if why != "" || sr == nil || sr.timeout != tc.want {
			t.Errorf("%s: bound %v, refusal %q; want a bound of %s", name, boundOf(sr), why, tc.want)
		}
	}
	for name, deadline := range map[string]time.Duration{"no time left": 0, "past the deadline": -time.Second} {
		if sr, why := w.prepareScript(heldFor(60), deadline); sr != nil || !strings.Contains(why, "no time is left") {
			t.Errorf("%s: bound %v, refusal %q; want it refused for the deadline", name, boundOf(sr), why)
		}
	}
}

// boundOf is a prepared run's bound, or nil for none: what a failure prints, not the run's
// environment.
func boundOf(sr *scriptRun) any {
	if sr == nil {
		return nil
	}
	return sr.timeout
}
