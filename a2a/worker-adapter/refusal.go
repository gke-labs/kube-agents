package workeradapter

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"regexp"
	"syscall"
)

// A profile task's harness is stopped at its first definitive write refusal
// (spec-subagent-profiles.md, "Stopped at a write refusal"). The signal is
// the credential broker's refusal as the in-pod broker client saw it, never
// a tool's output: tool output carries cluster data (pod logs, ConfigMap
// values, events), and text planted there must not be able to end a task.
//
// The broker marks a definitive write refusal in its 403 body
// (credential_proxy.WRITE_REFUSAL_KEY): a policy rule refusing a mutating
// kubectl verb that is not a local render, either the kubernetes.read-only
// rule or the profile caller's flag rule (caller.kubectl-flag), which reads a
// profile write's flags first. A busy, rate or timeout answer is never
// marked. Nor is a version-control refusal: the version-control skill's
// answer to a missing permit is the patch, which the task still gives. The client
// (agents/platform/scripts/credential_proxy_client.py, record_write_refusal)
// appends one JSON line per such refusal to the file named by
// EnvWriteRefusalFile:
//
//	{"write_refusal":true,"rule":"kubernetes.read-only","verb":"scale","route":"/v1/exec","time":"2026-11-21T10:00:00Z","task":"<id>"}
//
// The profile worker makes that file empty on the task's tmpfs, beside the
// task token, before the harness starts, and removes it after. The adapter
// reads it after each tool-result line the harness prints, which comes after
// the call that wrote it has exited, and stops at the first valid record: one
// naming this task, one of those two rules (stopRules), and a verb of one or
// two lowercase words.
//
// Every route by which a definitive write refusal can reach the model runs
// through that client:
//   - kubectl is /usr/local/bin/kubectl, which on a profile worker (no
//     KUBE_PROXY_URL) execs credential-proxy-exec, and gcloud is the shim
//     itself (a2a/Dockerfile.worker; the operator's profile PATH puts
//     /opt/credential-proxy/bin first). A persona script that runs either
//     goes the same way.
//
// The paths that do not go through it carry no definitive write refusal:
//   - The version-control skill's vcs.py goes through the client
//     (credential_proxy_client.vcs_call), but its refusals are never recorded:
//     a missing permit is answered with the patch.
//   - The MCP relay (mcprelay.go) carries mcp_policy's refusals. A
//     gcp.mcp.read-only refusal answers a write tool and a tool the table
//     does not list alike, so it is not a definitive write refusal.
//   - The stock kubectl binary (/usr/local/libexec/kubectl) run directly, and
//     curl or Python at the cluster API, hold no credential on a profile
//     worker: what they meet is the API server's own 401 or 403.
//   - curl or Python posting to the broker directly gets the broker's JSON
//     refusal, which no part of the worker reads as a halt.
//   - git is local-only (deploy/shared/worker-gitconfig), and the harness's
//     file, search and web tools reach no write the broker governs.
//
// So no tool output is read for refusals at all. A record lost or never
// written fails safe: the task runs on, and the broker refuses every further
// write regardless. The stop is for an honest, early report, not enforcement.
// The file is writable by the harness's own user, so a record is no stronger
// than the model's own choice to stop; what this rules out is text in a
// tool's output stopping it.

const (
	// EnvWriteRefusalFile names the record file to the broker client.
	EnvWriteRefusalFile = "CREDENTIAL_PROXY_REFUSAL_FILE"
	// EnvWriteRefusalTask is the task id the client puts in each record.
	EnvWriteRefusalTask = "CREDENTIAL_PROXY_REFUSAL_TASK"
	// writeRefusalFileName is the record file's name beside the task token.
	writeRefusalFileName = "write-refusals"
	// maxWriteRefusalBytes bounds what is read of the file: a record is a
	// line of well under a KiB, and only the first valid one counts.
	maxWriteRefusalBytes = 64 << 10
)

var (
	// The rule ids and verbs a record may carry: the broker's own vocabulary.
	refusalRuleShape = regexp.MustCompile(`\A[a-z0-9][a-z0-9.-]{0,63}\z`)
	// stopRules are the broker's rules whose refusal of a write stops a task:
	// the read-only rule and the profile caller's flag rule. The record file
	// is the harness's to write, and the reason reaches a delegating parent,
	// so a record naming any other rule, or a verb no kubectl write has, is
	// skipped.
	stopRules = map[string]bool{kubernetesReadOnlyRule: true, callerKubectlFlagRule: true}
	// stopVerbShape is a kubectl write's verb as the broker names it: one
	// word, or two for the verbs whose second word decides.
	stopVerbShape    = regexp.MustCompile(`\A[a-z]+(?: [a-z]+)?\z`)
	refusalVerbShape = regexp.MustCompile(`\A[A-Za-z0-9_.:@/-]{1,64}(?: [A-Za-z0-9_.:@/-]{1,64}){0,2}\z`)
)

// writeRefusal is the first definitive refusal a task met: the broker's rule
// and the verb it refused, and nothing else.
type writeRefusal struct {
	rule string
	verb string
}

// writeRefusalRecord is one line of the record file.
type writeRefusalRecord struct {
	WriteRefusal bool   `json:"write_refusal"`
	Rule         string `json:"rule"`
	Verb         string `json:"verb"`
	Route        string `json:"route"`
	Task         string `json:"task"`
	Time         string `json:"time"`
}

// readWriteRefusal is the first valid record in the file at path for the
// task, or nil. A missing file is nil and no error. The file is opened
// without following a symlink and must be a regular file; a record counts
// only when it is marked a write refusal, its rule and verb have the broker's
// shape, and it names this task. A record naming no task, as from a stray
// earlier process on a pool pod, never counts, and with no task id nothing
// does.
func readWriteRefusal(path, taskID string) (*writeRefusal, error) {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0) // #nosec G304 -- the worker's own per-task file
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	defer func() { _ = f.Close() }()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("the write-refusal record is not a regular file")
	}
	sc := bufio.NewScanner(io.LimitReader(f, maxWriteRefusalBytes))
	sc.Buffer(make([]byte, 0, 4096), maxWriteRefusalBytes)
	for sc.Scan() {
		var rec writeRefusalRecord
		if json.Unmarshal(sc.Bytes(), &rec) != nil {
			continue
		}
		if !rec.WriteRefusal || !refusalRuleShape.MatchString(rec.Rule) || !refusalVerbShape.MatchString(rec.Verb) {
			continue
		}
		if taskID == "" || rec.Task != taskID {
			continue
		}
		if !stopRules[rec.Rule] || !stopVerbShape.MatchString(rec.Verb) {
			continue
		}
		return &writeRefusal{rule: rec.Rule, verb: rec.Verb}, nil
	}
	return nil, sc.Err()
}

// kubernetesReadOnlyRule is the command policy's read-only rule, whose
// refused write can be proposed as a pull request instead.
const kubernetesReadOnlyRule = "kubernetes.read-only"

// callerKubectlFlagRule is the broker's flag rule (credential_proxy
// RULE_CALLER_KUBECTL_FLAG), which refuses most profile writes that carry a
// flag before the read-only rule reads them.
const callerKubectlFlagRule = "caller.kubectl-flag"

// writeRefusedReason is the terminal message of a task stopped at a write
// refusal, in the `reason: <token> - <detail>` shape.
func writeRefusedReason(r *writeRefusal) string {
	// Facts only: the rule, the verb, and that the change was not made. The
	// reason reaches a delegating parent model, so it carries no advice.
	return "reason: write-refused" + reasonDetailSeparator +
		"the task stopped after a write refusal: the credential broker's " + r.rule + " rule refused the verb " + r.verb +
		"; the change was not made."
}
