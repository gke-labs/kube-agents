package authcallout

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

// The provision Job's authentication path, run with the real nats CLI.
//
// This is the one product client that authenticates through the callout in this
// change, and its credential handling is not our code: the operator renders a
// shell script that hands the CLI a ServiceAccount token in the password field,
// and whether that works is a fact about the CLI's flag handling rather than
// about the callout. Everything else in this package tests our own client
// library, which would agree with us whether or not the CLI does.
//
// So the shape asserted here is the shape the render emits:
//
//	nats --server S --user <serviceaccount> --password <token> \
//	     --inbox-prefix=_INBOX.provision ...
//
// The CLI comes from `go install github.com/nats-io/natscli/nats`, which needs
// no container; the a2a CI job installs it for exactly this test.
func natsCLI(t *testing.T) string {
	t.Helper()
	if path := os.Getenv("NATS_CLI"); path != "" {
		return path
	}
	path, err := exec.LookPath("nats")
	if err != nil {
		t.Skip("the nats CLI is not on PATH; set NATS_CLI or `go install github.com/nats-io/natscli/nats@latest`")
	}
	return path
}

func TestTheProvisionJobsCredentialShapeWorksWithTheRealCLI(t *testing.T) {
	cli := natsCLI(t)
	h := startLiveHarness(t)

	token := h.mintToken(t, provisionSAName, busAudience)
	serviceAccount := "system:serviceaccount:" + h.namespace + ":" + provisionSAName
	server := strings.TrimPrefix(h.nats.ClientURL(), "nats://")

	run := func(extra ...string) (string, error) {
		args := append([]string{
			"--server", server,
			"--user", serviceAccount,
			"--password", token,
			"--inbox-prefix=_INBOX.provision",
		}, extra...)
		out, err := exec.Command(cli, args...).CombinedOutput()
		return string(out), err
	}

	// Every object the provisioning script creates, with the two calls it
	// makes per object: info-then-add, idempotently.
	//
	// Enumerated rather than sampled, and that is the point. The JetStream API
	// grant is now per object rather than $JS.API.>, so a stream or bucket the
	// script provisions but the grant list forgot is a Job that fails on a real
	// install and nowhere else. An earlier version of this test called
	// `stream ls`, which the script never does — it passed while granting
	// nothing the script needs, and then failed when the grant was narrowed
	// correctly. These are the calls the render actually emits.
	for _, stream := range []string{"TASKS", "DIRECTORY", "TOPICS-STATE", "TOPICS-JOURNAL"} {
		// info first: on a fresh bus this is a legitimate not-found, which is
		// what the script's `info || add` relies on. A permissions failure
		// would surface as a timeout instead, since the reply cannot land.
		if out, err := run("stream", "info", stream); err != nil && strings.Contains(out, "deadline exceeded") {
			t.Errorf("stream info %s timed out, which is what a missing grant looks like: %s", stream, out)
		}
		if out, err := run("stream", "add", stream,
			"--subjects", "probe."+stream+".>", "--storage", "file",
			"--retention", "limits", "--replicas", "1", "--defaults",
		); err != nil {
			t.Errorf("the provision credential cannot create stream %s: %v\n%s", stream, err, out)
		}
		if out, err := run("stream", "info", stream); err != nil {
			t.Errorf("the provision credential cannot info stream %s: %v\n%s", stream, err, out)
		}
	}
	for _, bucket := range []string{"runtime-state", "session-state", "cap"} {
		if out, err := run("kv", "add", bucket, "--history", "1"); err != nil {
			t.Errorf("the provision credential cannot create bucket %s: %v\n%s", bucket, err, out)
		}
		if out, err := run("kv", "info", bucket); err != nil {
			t.Errorf("the provision credential cannot info bucket %s: %v\n%s", bucket, err, out)
		}
	}

	// A provisioned topic, which the script writes starter entries to.
	if out, err := run("pub", "a2a.topics.shared.blueprint", "hello"); err != nil {
		t.Errorf("the provision credential could not publish a granted topic: %v\n%s", err, out)
	}

	// The task plane, which this principal deliberately does not hold: a
	// provisioner that can publish tasks can impersonate the fabric.
	out, err := run("pub", "a2a.tasks.platform.t1.in", "nope")
	if err == nil {
		t.Error("the provision credential reached the task plane")
	}
	if !strings.Contains(out, "Permissions Violation") {
		t.Errorf("refusal did not come from the server as a permissions violation:\n%s", out)
	}
}

// The inbox trap, reproduced against the real CLI because this is the failure
// mode the render's --inbox-prefix flag exists to prevent, and it is the one
// that does not look like an authorization failure.
//
// Every JetStream API call is answered on an inbox subject. This principal is
// granted only _INBOX.provision.>, and the CLI's default is _INBOX.<nuid> — so
// without the flag the request goes out, the reply is refused by the caller's
// own grant, and the CLI reports a TIMEOUT. W6 lost a provisioning Job to
// exactly this: the Job could never succeed and nothing said why.
func TestWithoutTheInboxPrefixTheProvisionCredentialTimesOutRatherThanFailing(t *testing.T) {
	cli := natsCLI(t)
	h := startLiveHarness(t)

	token := h.mintToken(t, provisionSAName, busAudience)
	serviceAccount := "system:serviceaccount:" + h.namespace + ":" + provisionSAName
	server := strings.TrimPrefix(h.nats.ClientURL(), "nats://")

	out, err := exec.Command(cli,
		"--server", server,
		"--user", serviceAccount,
		"--password", token,
		"stream", "ls",
	).CombinedOutput()

	if err == nil {
		t.Fatal("a call on an ungranted inbox prefix succeeded; the per-user inbox grant is not being enforced")
	}
	// The point of the assertion: it is a deadline, not a permissions error.
	// Anyone debugging this on a cluster will go looking at the network.
	if !strings.Contains(string(out), "deadline exceeded") && !strings.Contains(string(out), "timeout") {
		t.Errorf("expected a timeout, got:\n%s", out)
	}
}

// The narrowing, verified where it matters: against a real server, with the
// real CLI, using the operator's real rendered grants.
//
// The claim the agent principal exists to make is that it cannot reach the task
// plane. Withholding `a2a.tasks.>` from its subject lists does not achieve that
// on its own — for JetStream a grant list is a capability surface rather than a
// read/write distinction, because subject permissions cannot see a request body
// and a consumer's target stream and delivery subject are both body fields. So
// `$JS.API.>` would hand the whole task plane back through a consumer that
// delivers TASKS into an inbox the principal *can* subscribe to.
//
// This asserts both halves: the reads the principal genuinely needs still work,
// and the two escapes do not.
func TestTheProvisionPrincipalCannotEscapeThroughTheJetStreamAPI(t *testing.T) {
	cli := natsCLI(t)
	h := startLiveHarness(t)

	token := h.mintToken(t, provisionSAName, busAudience)
	serviceAccount := "system:serviceaccount:" + h.namespace + ":" + provisionSAName
	server := strings.TrimPrefix(h.nats.ClientURL(), "nats://")

	run := func(extra ...string) (string, error) {
		args := append([]string{
			"--server", server,
			"--user", serviceAccount,
			"--password", token,
			"--inbox-prefix=_INBOX.provision",
		}, extra...)
		out, err := exec.Command(cli, args...).CombinedOutput()
		return string(out), err
	}

	// What it must still be able to do: create the streams it provisions.
	if out, err := run("stream", "add", "TASKS",
		"--subjects", "a2a.tasks.>", "--storage", "file", "--retention", "limits",
		"--replicas", "1", "--defaults",
	); err != nil {
		t.Fatalf("the provision principal cannot create the stream it provisions: %v\n%s", err, out)
	}
	if out, err := run("stream", "info", "TASKS"); err != nil {
		t.Errorf("the provision principal cannot read back the stream it created: %v\n%s", err, out)
	}

	// What it must not: deleting the audit substrate it provisioned.
	if out, err := run("stream", "rm", "TASKS", "--force"); err == nil {
		t.Errorf("the provision principal deleted a stream:\n%s", out)
	}

	// And the consumer escape — a push consumer on TASKS delivering into an
	// inbox this principal can subscribe to would read the whole task plane.
	out, err := run("consumer", "add", "TASKS", "pwn",
		"--target", "_INBOX.provision.steal", "--deliver", "all", "--ack", "none",
		"--replay", "instant", "--filter", "", "--defaults")
	if err == nil {
		t.Errorf("the provision principal created a push consumer on TASKS:\n%s", out)
	}
}
