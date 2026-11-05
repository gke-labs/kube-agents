package busmigrate

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The snapshot leg stages every stream under its own directory, and bus-hash
// writes their digests to the termination log as the operator reads them:
// each the digest of exactly the staged data.
func TestTheSnapshotLegStagesWhatBusHashDigests(t *testing.T) {
	s := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(s)
	seedBus(t, s.ClientURL(), 1)
	dir := t.TempDir()
	var stderr bytes.Buffer
	if code := Main(context.Background(), []string{"snapshot", "--server", s.ClientURL(), "--streams", "TASKS,KV_cap", "--dir", dir}, &stderr); code != 0 {
		t.Fatalf("snapshot leg exited %d: %s", code, stderr.String())
	}
	log := filepath.Join(t.TempDir(), "termination-log")
	if code := HashMain([]string{"--dir", dir, "--streams", "TASKS,KV_cap", "--termination-log", log}, &stderr); code != 0 {
		t.Fatalf("bus-hash exited %d: %s", code, stderr.String())
	}
	b, err := os.ReadFile(log)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]StagedDigest
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"TASKS", "KV_cap"} {
		data, err := os.ReadFile(filepath.Join(dir, n, SnapshotDataFile))
		if err != nil {
			t.Fatal(err)
		}
		meta, err := os.ReadFile(filepath.Join(dir, n, SnapshotMetaFile))
		if err != nil {
			t.Errorf("%s: no metadata staged: %v", n, err)
		}
		if got[n].Data != Digest(data) || len(data) == 0 {
			t.Errorf("%s: termination-log data digest %q, want %q of %d staged bytes", n, got[n].Data, Digest(data), len(data))
		}
		if got[n].Meta != Digest(meta) || len(meta) == 0 {
			t.Errorf("%s: termination-log meta digest %q, want %q of %d staged bytes", n, got[n].Meta, Digest(meta), len(meta))
		}
	}
}

// Missing flags are a usage error, not a run against nothing.
func TestBusMigrateRefusesAnIncompleteInvocation(t *testing.T) {
	var stderr bytes.Buffer
	for _, args := range [][]string{nil, {"pin", "--server", "nats://127.0.0.1:1", "--streams", "TASKS"}, {"snapshot", "--server", "x"}, {"restore", "--server", "x", "--streams", "A", "--dir", "/tmp"}, {"bogus", "--streams", "A"}} {
		if code := Main(context.Background(), args, &stderr); code != 2 {
			t.Errorf("bus-migrate %v exited %d, want 2", args, code)
		}
	}
	if code := HashMain([]string{"--dir", t.TempDir()}, &stderr); code != 2 {
		t.Errorf("bus-hash without --streams exited %d, want 2", code)
	}
}

// stageAndHash runs the snapshot leg and bus-hash against url and returns
// the staging dir, the digests and the staged metadata.
func stageAndHash(t *testing.T, url string, streams string) (string, map[string]StagedDigest, map[string]Metadata) {
	t.Helper()
	dir := t.TempDir()
	var stderr bytes.Buffer
	if code := Main(context.Background(), []string{"snapshot", "--server", url, "--streams", streams, "--dir", dir}, &stderr); code != 0 {
		t.Fatalf("snapshot leg exited %d: %s", code, stderr.String())
	}
	log := filepath.Join(t.TempDir(), "hash-log")
	if code := HashMain([]string{"--dir", dir, "--streams", streams, "--termination-log", log}, &stderr); code != 0 {
		t.Fatalf("bus-hash exited %d: %s", code, stderr.String())
	}
	b, _ := os.ReadFile(log)
	var digests map[string]StagedDigest
	if err := json.Unmarshal(b, &digests); err != nil {
		t.Fatal(err)
	}
	metas := map[string]Metadata{}
	for _, n := range strings.Split(streams, ",") {
		mb, _ := os.ReadFile(filepath.Join(dir, n, SnapshotMetaFile))
		var m Metadata
		if err := json.Unmarshal(mb, &m); err != nil {
			t.Fatal(err)
		}
		metas[n] = m
	}
	return dir, digests, metas
}

func writeRunFile(t *testing.T, run RunFile) string {
	t.Helper()
	b, err := json.Marshal(run)
	if err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(t.TempDir(), "run.json")
	if err := os.WriteFile(p, b, 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func readReport(t *testing.T, path string) Report {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var r Report
	if err := json.Unmarshal(b, &r); err != nil {
		t.Fatalf("report %q: %v", b, err)
	}
	return r
}

// restoreInto starts an empty standalone server (the rollback's one server)
// and runs the restore leg into it.
func restoreInto(t *testing.T, dir, streams string, run RunFile) (code int, report Report, url string, stderr string) {
	t.Helper()
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	t.Cleanup(func() { shutdown(dst) })
	log := filepath.Join(t.TempDir(), "restore-log")
	var e bytes.Buffer
	code = Main(context.Background(), []string{"restore", "--server", dst.ClientURL(), "--streams", streams, "--dir", dir,
		"--run-file", writeRunFile(t, run), "--replicas", "1", "--termination-log", log}, &e)
	return code, readReport(t, log), dst.ClientURL(), e.String()
}

func streamExists(t *testing.T, url, name string) bool {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	_, err = streamInfo(context.Background(), nc, name)
	return err == nil
}

// Snapshot, hash, restore through the subcommands, with the operator's run
// file: the streams come back with their messages, and the report carries
// the warnings and no error.
func TestTheRestoreLegRestoresWhatTheSnapshotLegStaged(t *testing.T) {
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	seedBus(t, src.ClientURL(), 1)
	dir, digests, metas := stageAndHash(t, src.ClientURL(), "TASKS,KV_cap")
	shutdown(src)
	// The operator renders one more TASKS subject than this bus has, as for
	// an install provisioned before a subject was added: the restore goes
	// ahead, and the gap is a warning.
	tasks := policyOf(metas["TASKS"].Config)
	tasks.Subjects = append(append([]string(nil), tasks.Subjects...), "a2a.tasks-added-later")
	run := RunFile{Digests: digests, Policies: []StreamPolicy{tasks, policyOf(metas["KV_cap"].Config)}}
	code, report, url, stderr := restoreInto(t, dir, "TASKS,KV_cap", run)
	if code != 0 || report.Error != "" {
		t.Fatalf("restore leg exited %d, report %+v: %s", code, report, stderr)
	}
	nc, _ := nats.Connect(url)
	defer nc.Close()
	js, _ := nc.JetStream()
	si, err := js.StreamInfo("TASKS")
	if err != nil || si.State.Msgs != 5 || si.State.LastSeq != 5 {
		t.Fatalf("TASKS after the restore leg: %+v %v", si, err)
	}
	// The test bus is not a fresh install (its TASKS is seedBus's), so the
	// fields outside the policy that differ are reported, not refused.
	all := strings.Join(report.Warnings, "\n")
	if !strings.Contains(all, "stream TASKS: max_consumers") {
		t.Errorf("report warnings %v, want TASKS's drift from a fresh install", report.Warnings)
	}
	if !strings.Contains(all, "stream TASKS: subject a2a.tasks-added-later is missing") {
		t.Errorf("report warnings %v, want the missing rendered subject", report.Warnings)
	}
}

// Metadata changed on the staging volume after the hashing pod read it is
// refused before anything is sent, even when its config would pass.
func TestTheRestoreLegRefusesMetadataChangedAfterHashing(t *testing.T) {
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	seedBus(t, src.ClientURL(), 1)
	dir, digests, metas := stageAndHash(t, src.ClientURL(), "TASKS")
	shutdown(src)
	edited := metas["TASKS"]
	edited.Config.Description = "edited after hashing"
	b, _ := json.Marshal(edited)
	if err := os.WriteFile(filepath.Join(dir, "TASKS", SnapshotMetaFile), b, 0o600); err != nil {
		t.Fatal(err)
	}
	code, report, url, _ := restoreInto(t, dir, "TASKS", RunFile{Digests: digests, Policies: []StreamPolicy{policyOf(metas["TASKS"].Config)}})
	if code != 1 || !strings.Contains(report.Error, "metadata") || !strings.Contains(report.Error, "digest") {
		t.Errorf("exit %d, report %+v; want a metadata digest refusal", code, report)
	}
	if streamExists(t, url, "TASKS") {
		t.Error("TASKS was created from metadata that changed after hashing")
	}
}

// A staged config outside the operator's policy (here the policy renders a
// narrower subject list than the stream had) is refused with nothing sent.
func TestTheRestoreLegRefusesAConfigOutsideThePolicy(t *testing.T) {
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	seedBus(t, src.ClientURL(), 1)
	dir, digests, metas := stageAndHash(t, src.ClientURL(), "TASKS")
	shutdown(src)
	policy := policyOf(metas["TASKS"].Config)
	policy.Subjects = []string{"a2a.tasks.only"}
	start := time.Now()
	code, report, url, _ := restoreInto(t, dir, "TASKS", RunFile{Digests: digests, Policies: []StreamPolicy{policy}})
	if code != 1 || !strings.Contains(report.Error, "outside the operator's policy") {
		t.Errorf("exit %d, report %+v; want a policy refusal", code, report)
	}
	// A refusal is final, never retried as if the server were not ready.
	if took := time.Since(start); took > 3*time.Second {
		t.Errorf("the refusal took %v; it was retried", took)
	}
	if streamExists(t, url, "TASKS") {
		t.Error("TASKS was created from a config outside the policy")
	}
}

// The report always fits the kubelet's termination message, and says how
// many warnings it dropped.
func TestTheRestoreReportFitsATerminationMessage(t *testing.T) {
	var many []string
	for i := 0; i < 500; i++ {
		many = append(many, fmt.Sprintf("stream TASKS: field_%d is something; a fresh install has something else", i))
	}
	p := filepath.Join(t.TempDir(), "log")
	if err := writeReport(p, Report{Warnings: many, Error: "boom"}); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(p)
	if len(b) > terminationMessageLimit {
		t.Fatalf("report is %d bytes", len(b))
	}
	r := readReport(t, p)
	last := r.Warnings[len(r.Warnings)-1]
	if r.Error != "boom" || !strings.HasPrefix(last, "and ") || !strings.HasSuffix(last, " more") {
		t.Errorf("truncated report error %q, last warning %q", r.Error, last)
	}
	var n int
	fmt.Sscanf(last, "and %d more", &n)
	if n+len(r.Warnings)-1 != len(many) {
		t.Errorf("kept %d and counted %d more of %d", len(r.Warnings)-1, n, len(many))
	}
}

// A restore pod run a second time over streams it already restored (or one
// whose finish timed out after the server built the stream) takes them as
// done, rather than retrying a name that is in use until it fails.
func TestTheRestoreLegTakesAStreamItAlreadyRestoredAsDone(t *testing.T) {
	src := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	seedBus(t, src.ClientURL(), 1)
	dir, digests, metas := stageAndHash(t, src.ClientURL(), "TASKS")
	shutdown(src)
	run := RunFile{Digests: digests, Policies: []StreamPolicy{policyOf(metas["TASKS"].Config)}}
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	for i := 1; i <= 2; i++ {
		start := time.Now()
		log := filepath.Join(t.TempDir(), "log")
		var e bytes.Buffer
		code := Main(context.Background(), []string{"restore", "--server", dst.ClientURL(), "--streams", "TASKS", "--dir", dir,
			"--run-file", writeRunFile(t, run), "--replicas", "1", "--termination-log", log}, &e)
		if code != 0 {
			t.Fatalf("run %d exited %d: %s %+v", i, code, e.String(), readReport(t, log))
		}
		if took := time.Since(start); took > 5*time.Second {
			t.Errorf("run %d took %v", i, took)
		}
	}
}

// The clear leg deletes every named stream and reports each; a stream
// already gone counts as cleared, so a second clear (a retry) succeeds.
func TestTheClearLegDeletesTheStreamsAndCanRunAgain(t *testing.T) {
	s := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(s)
	seedBus(t, s.ClientURL(), 1)
	for i := 1; i <= 2; i++ {
		log := filepath.Join(t.TempDir(), "log")
		var e bytes.Buffer
		if code := Main(context.Background(), []string{"clear", "--server", s.ClientURL(), "--streams", "TASKS,KV_cap,NEVER_MADE", "--termination-log", log}, &e); code != 0 {
			t.Fatalf("clear run %d exited %d: %s", i, code, e.String())
		}
		r := readReport(t, log)
		if strings.Join(r.Cleared, ",") != "TASKS,KV_cap,NEVER_MADE" || r.Error != "" {
			t.Errorf("clear run %d report %+v", i, r)
		}
	}
	for _, n := range []string{"TASKS", "KV_cap"} {
		if streamExists(t, s.ClientURL(), n) {
			t.Errorf("%s survived the clear", n)
		}
	}
}
