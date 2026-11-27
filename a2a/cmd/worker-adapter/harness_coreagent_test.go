package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// The builder's default path is where the worker image puts the binary.
func TestTheCoreAgentHarnessPathIsWhereTheImagePutsIt(t *testing.T) {
	if p := (coreAgentBuilder{}).defaultPath(); p != coreAgentHarnessPath {
		t.Fatalf("defaultPath %q, want %q", p, coreAgentHarnessPath)
	}
	df, err := os.ReadFile(filepath.Join("..", "..", "Dockerfile.worker"))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"-o /out/core-agent-harness ./cmd/core-agent-harness", "COPY --from=build /out/core-agent-harness " + coreAgentHarnessPath,
		// The session persona, at the path a session pod reads, with no write bit.
		"-core-agent-out /out/core-agent-session", "COPY --from=build /out/core-agent-session/ " + coreAgentSessionPersonaDir + "/",
		"RUN chmod -R a-w " + coreAgentSessionPersonaDir,
	} {
		if !strings.Contains(string(df), want) {
			t.Errorf("Dockerfile.worker lacks %q", want)
		}
	}
	// The tree has to stay root-owned with no write bit: copied and sealed
	// after the runtime stage's last chown, and before it drops to the
	// harness's user.
	text := string(df)
	runtime := strings.LastIndex(text, "\nFROM ")
	chown := strings.LastIndex(text, "chown -R")
	cp := strings.Index(text, "COPY --from=build /out/core-agent-session/")
	seal := strings.Index(text, "RUN chmod -R a-w "+coreAgentSessionPersonaDir)
	user := strings.LastIndex(text, "\nUSER ")
	if !(runtime < chown && chown < cp && cp < seal && seal < user) {
		t.Errorf("Dockerfile.worker order: last FROM %d, chown %d, copy %d, chmod %d, USER %d; want the tree copied and sealed after the chown and before USER", runtime, chown, cp, seal, user)
	}
}

// The argv carries the model and the persona dir, and no session or resume
// flag: the conversation travels in the stdin envelope.
func TestTheCoreAgentArgvCarriesNoSession(t *testing.T) {
	t.Setenv(lib.EnvProfileWorker, "true")
	argv := (coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"})
	got := strings.Join(argv, " ")
	if !strings.Contains(got, "--model m") || !strings.Contains(got, "--persona-dir "+coreAgentProfilePersonaDir) || strings.Contains(got, "--session") || strings.Contains(got, "--resume") {
		t.Errorf("argv %q", got)
	}
}

// A profile pod the operator rendered a persona for passes
// --persona-required; one it didn't, doesn't.
func TestACoreAgentProfilePodRequiresItsPersonaWhenTold(t *testing.T) {
	t.Setenv(lib.EnvProfileWorker, "true")
	argv := strings.Join((coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"}), " ")
	if strings.Contains(argv, "--persona-required") {
		t.Errorf("argv %q requires a persona nothing said was rendered", argv)
	}
	t.Setenv(coreAgentPersonaRequiredEnv, "true")
	argv = strings.Join((coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"}), " ")
	if !strings.Contains(argv, "--persona-required") {
		t.Errorf("argv %q", argv)
	}
}

// A session pod reads the persona the image ships for sessions, and always
// requires it: the tree is in every worker image, so a missing or changed
// one fails the turn rather than run the session without its skills.
func TestACoreAgentSessionPodRequiresTheShippedSessionPersona(t *testing.T) {
	t.Setenv(lib.EnvProfileWorker, "")
	t.Setenv(coreAgentPersonaRequiredEnv, "")
	argv := strings.Join((coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"}), " ")
	if !strings.Contains(argv, "--persona-dir "+coreAgentSessionPersonaDir) || !strings.Contains(argv, "--persona-required") {
		t.Errorf("argv %q, want the session persona, required", argv)
	}
	if strings.Contains(argv, coreAgentProfilePersonaDir) {
		t.Errorf("argv %q names the profile persona", argv)
	}
}

// Which harness a session pod runs decides whether it reads the core-agent
// session persona at all: unset is Claude Code, today's default, with its
// own config directory; core-agent named explicitly reads the shipped
// session tree, required.
func TestTheSessionPersonaFollowsTheSessionsHarness(t *testing.T) {
	for _, tc := range []struct {
		name, harness string
		wantCoreAgent bool
	}{
		{name: "unset", harness: ""},
		{name: "explicit core-agent", harness: string(persona.CoreAgent), wantCoreAgent: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := map[string]string{lib.EnvHarness: tc.harness, lib.EnvHarnessUnisolated: lib.HarnessUnisolatedAllow}
			getenv := func(k string) string { return env[k] }
			h, _, b, err := selectHarness(getenv)
			if err != nil {
				t.Fatal(err)
			}
			if got := h == persona.CoreAgent; got != tc.wantCoreAgent {
				t.Fatalf("harness %q, want core-agent %v", h, tc.wantCoreAgent)
			}
			dir := b.configDir(getenv)
			if tc.wantCoreAgent != (dir == coreAgentSessionPersonaDir) {
				t.Errorf("persona dir %q for harness %q", dir, h)
			}
		})
	}
}

// A profile pod whose rendered persona is missing refuses, even though the
// image carries the session's tree: it reads only its own path, required.
func TestAProfilePodWithoutItsRenderedPersonaRefusesDespiteTheSessionTree(t *testing.T) {
	session := writeCoreAgentTree(t)
	profile := filepath.Join(t.TempDir(), "agent-persona")
	prevS, prevP := coreAgentSessionPersonaDir, coreAgentProfilePersonaDir
	t.Cleanup(func() { coreAgentSessionPersonaDir, coreAgentProfilePersonaDir = prevS, prevP })
	coreAgentSessionPersonaDir, coreAgentProfilePersonaDir = session, profile
	if err := coreagent.VerifyRendering(session, -1); err != nil {
		t.Fatalf("the session tree should verify: %v", err)
	}
	t.Setenv(lib.EnvProfileWorker, "true")
	t.Setenv(coreAgentPersonaRequiredEnv, "true")
	argv := (coreAgentBuilder{}).command(harnessSpec{path: coreAgentHarnessPath, model: "m"})
	dir := argAfter(argv, "--persona-dir")
	if dir != profile || !slices.Contains(argv, "--persona-required") {
		t.Fatalf("argv %q, want the profile's own persona, required", argv)
	}
	if err := coreagent.VerifyRendering(dir, -1); err == nil {
		t.Error("the profile pod's missing persona verified")
	}
}

// A session pod's check finds the shipped session tree and verifies it.
func TestASessionPodVerifiesTheShippedSessionPersona(t *testing.T) {
	session := writeCoreAgentTree(t)
	prev := coreAgentSessionPersonaDir
	t.Cleanup(func() { coreAgentSessionPersonaDir = prev })
	coreAgentSessionPersonaDir = session
	t.Setenv(lib.EnvProfileWorker, "")
	var buf bytes.Buffer
	b := coreAgentBuilder{}
	if err := checkSessionPersona(b, b.configDir(os.Getenv), slog.New(slog.NewTextHandler(&buf, nil))); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "session skills match their build-time manifest") {
		t.Errorf("log %q, want the session persona verified", buf.String())
	}
}

// writeCoreAgentTree writes a core-agent session tree, manifest included,
// with every write bit cleared, as the worker image ships it.
func writeCoreAgentTree(t *testing.T) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "session")
	files := map[string][]byte{
		coreagent.InstructionsFile:               []byte("You are the session agent.\n"),
		coreagent.SkillsDir + "/triage/SKILL.md": []byte("---\nname: triage\ndescription: d\n---\nbody\n"),
	}
	for rel, data := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(root, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(files), 0o644); err != nil {
		t.Fatal(err)
	}
	var dirs []string
	_ = filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			dirs = append(dirs, p)
			return nil
		}
		return os.Chmod(p, 0o444)
	})
	slices.Reverse(dirs)
	for _, d := range dirs {
		if err := os.Chmod(d, 0o555); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		for _, d := range dirs {
			_ = os.Chmod(d, 0o755)
		}
	})
	return root
}

// argAfter is the argument after flag in argv, or "".
func argAfter(argv []string, flag string) string {
	for i, a := range argv {
		if a == flag && i+1 < len(argv) {
			return argv[i+1]
		}
	}
	return ""
}

// coreAgentInstructions is the system-prompt lines the configured driver
// sends in a turn's envelope.
func coreAgentInstructions(t *testing.T, cfg workeradapter.Config) []string {
	t.Helper()
	b, err := cfg.Driver.EncodeUser("hi")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	return env.Instructions
}

// A core-agent session pod gets the same cluster-access line Claude Code
// gets on its argv, in each turn's envelope: the view's line with the view
// on, the no-cluster line without it, neither when an override leaves the
// view's Bash out. Never the delegate line, with the delegate tool on or
// off: core-agent has no delegate tool yet. A profile worker gets none.
func TestACoreAgentSessionGetsItsClusterPromptInTheEnvelope(t *testing.T) {
	t.Setenv("TASK_ID", "task-1")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://localhost:4222")
	t.Setenv(lib.EnvProfileWorker, "")
	t.Setenv(lib.EnvProfileExecutor, "")
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv(lib.EnvHarness, "core-agent")
	t.Setenv(lib.EnvHarnessUnisolated, lib.HarnessUnisolatedAllow)
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })

	for _, tc := range []struct {
		name, view, allowed, delegate string
		want                          []string
	}{
		{"cluster view", "true", "", "", []string{clusterViewPrompt}},
		{"cluster view, delegate off", "true", "", "off", []string{clusterViewPrompt}},
		{"no cluster view", "", "", "", []string{noClusterPrompt}},
		{"no cluster view, delegate off", "", "", "off", []string{noClusterPrompt}},
		{"view without Bash", "true", "Read,Glob", "", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(lib.EnvClusterView, tc.view)
			t.Setenv("A2A_ALLOWED_TOOLS", tc.allowed)
			t.Setenv("A2A_DELEGATE_TOOL", tc.delegate)
			cfg, ok := configFromEnv(log)
			if !ok {
				t.Fatal("configFromEnv refused the core-agent session")
			}
			got := coreAgentInstructions(t, cfg)
			if !slices.Equal(got, tc.want) {
				t.Errorf("instructions %q, want %q", got, tc.want)
			}
			if slices.Contains(got, delegatePrompt) {
				t.Errorf("a core-agent session was told of the delegate tool it doesn't have")
			}
			if got := coreAgentInstructions(t, profileModeConfig(cfg)); len(got) != 0 {
				t.Errorf("profile worker instructions %q, want none", got)
			}
		})
	}
}

// A profile pod that lost its role signal (A2A_PROFILE_WORKER) can't fall
// back to the session tree: without it the worker takes the session path,
// whose start needs a TASK_ID or the warm-pod flag, and the operator gives a
// profile pod neither. So it exits 1 at start, before any turn or persona
// read, rather than run the profile with the session's persona.
func TestAProfilePodMissingItsRoleSignalRefusesToStart(t *testing.T) {
	session := writeCoreAgentTree(t)
	code, stderr := runChild(t, "",
		"PROFILE=cluster", "NATS_URL=nats://127.0.0.1:1", "A2A_WORKDIR="+t.TempDir(),
		lib.EnvProfileTier+"=read", coreAgentPersonaRequiredEnv+"=true",
		lib.EnvHarness+"="+string(persona.CoreAgent), lib.EnvHarnessUnisolated+"="+lib.HarnessUnisolatedAllow,
		"A2A_HARNESS_PATH=/bin/false", runChildSessionPersonaEnv+"="+session)
	if code != 1 || !strings.Contains(stderr, "TASK_ID, PROFILE, and NATS_URL are required") {
		t.Errorf("exit %d, stderr %q; want 1 and the start-up refusal", code, stderr)
	}
	if strings.Contains(stderr, "session skills match") || strings.Contains(stderr, "adapter run failed") {
		t.Errorf("it went on past the refusal: %q", stderr)
	}
}
