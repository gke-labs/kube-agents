package hermesbridge

// The activity door: how the persona's tool calls become the task's
// `activity` artifact, and its heartbeat the `progress` artifact.
//
// Under -Q hermes writes nothing to stdout until the final response, so the
// bridge cannot learn about tool calls from the pipe it already holds. What
// hermes does offer is its outbound webhooks (agent/outbound_webhooks.py): a
// `hooks.outbound` config entry POSTs every pre_tool_call and post_tool_call
// to a URL, fire-and-forget through a bounded queue, signed with HMAC-SHA256
// when the variable named by `secret_env` is set. The bridge listens on a
// loopback address (the sidecar shares the pod's network namespace), and
// hands each child the entry through hermes's managed scope: a per-task
// directory holding the operator's managed config.yaml and .env with a
// hooks.outbound entry added, named by HERMES_MANAGED_DIR in the child's
// environment. Only the bridge's children carry the hook, so a kanban
// worker or cron tick under the same profile never POSTs anywhere, and a
// pod with no bridge has nothing to POST at.
//
// Correlation is the signature. Nothing in the delivery names the A2A task:
// hermes's own task_id is the kanban card or a fresh UUID, cwd and profile
// are shared by every process under the profile, and the URL does not expand
// environment variables. So each child gets a random key in its environment
// under ActivitySecretEnv, and a delivery belongs to whichever in-flight task's
// key verifies its signature - at most Concurrency keys to try. Only the
// bridge's children carry the hook (it rides each child's own managed
// scope), so a kanban worker or cron tick under the same profile never
// delivers; an unsigned or unmatched delivery that does arrive is answered
// 204 and dropped.
//
// Trust boundary, stated: everything in the pod is reachable from the
// persona's own terminal tool, its environment included. The trace is "as
// reported by the executor's process", the worker adapter's posture too; the
// key rejects cross-talk, not adversaries.

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"sigs.k8s.io/yaml"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// DefaultActivityListen is the loopback address the door binds; the
	// URL each child is handed is whatever the door actually bound. The
	// pod's other listeners are hermes's API server
	// on 8642 and the agent-api-auth container on 8643 (bound on every
	// interface, so a loopback bind there fails too), the dashboard on 9119;
	// 8651 is clear of all of them.
	DefaultActivityListen = "127.0.0.1:8651"
	// ActivityPath is the door's one route.
	ActivityPath = "/hermes/tool-events"
	// ActivitySecretEnv is the variable hermes reads the signing key from
	// (the `secret_env` of the hooks.outbound entry); the bridge sets it per
	// child. In a single-profile CLI process hermes's secret scope falls
	// through to the process environment, which is what makes this work.
	ActivitySecretEnv = "A2A_ACTIVITY_SECRET"
	// ActivityURLEnv tells the child where its deliveries go. hermes reads
	// the URL from the managed config the bridge writes; the variable is for
	// the record and for test stubs standing in for hermes.
	ActivityURLEnv = "A2A_ACTIVITY_URL"
	// ManagedDirEnv is hermes's managed-scope override: a directory whose
	// config.yaml is deep-merged per leaf over the profile's and whose .env
	// is loaded last. The operator sets it on the agent container (and so
	// on the sidecar) to /etc/hermes; the bridge points each child at its
	// own copy with the hook added.
	ManagedDirEnv = "HERMES_MANAGED_DIR"
	// DefaultManagedDir is hermes's managed-scope default when the variable
	// is unset, read only when the directory exists.
	DefaultManagedDir = "/etc/hermes"
	managedConfigFile = "config.yaml"
	// scopeMarkerFile is written first into every child scope and is what
	// the start-time sweep keys on: a directory in the scratch dir is the
	// bridge's to remove when it carries this file, whatever its name.
	scopeMarkerFile = ".a2a-bridge-scope"
	managedEnvFile  = ".env"
	// The hooks.outbound entry the bridge writes for its child. The timeout
	// is longer than activityPublishTimeout: the door publishes on the
	// delivery, and a timed-out delivery is retried, which would be a
	// duplicate call in the trace.
	hookEntryName      = "a2a-bridge-activity"
	hookTimeoutSeconds = 10
	hooksKey           = "hooks"
	hooksOutboundKey   = "outbound"
	// childScopeDirMode: the copy carries the managed .env, credentials
	// included, so it is the bridge's alone.
	childScopeDirMode  = 0o700
	childScopeFileMode = 0o600
	// DefaultProgressInterval is the heartbeat cadence. The relay renders
	// progress as one edited chat line, so this is one edit per minute.
	DefaultProgressInterval = 60 * time.Second

	// activityInputCap bounds one call's input on the bus: two KiB, enough
	// for any argument object a grader would read and small beside the
	// stream's per-message ceiling.
	activityInputCap = 2048
	// The entry's other fields, copied from the delivery, are bounded too:
	// a part is small beside the stream's ceiling whatever the delivery
	// carried (the door reads up to activityBodyCap).
	activityToolNameCap = 256
	activityCallIDCap   = 128
	activityWordCap     = 64
	// wrapperCallsDepth is where hermes's tool_call wrapper carries its
	// calls array: the root object's own "calls" value (the root map is
	// depth 0, its values depth 1). Only the elements of that array are
	// calls whose "name" is a tool name.
	wrapperCallsDepth = 1
	// unparseableInput is what an input the door could not decode or
	// re-encode becomes on the bus.
	unparseableInput = `{"unparseable":true}`
	// activityInputHead is how much of an over-cap input survives, as text.
	activityInputHead = 1024
	// hermesToolCallWrapper is hermes's batching tool: one call whose input
	// is {"calls":[{"name":...,"arguments":{...}},...]}. The verifier
	// unwraps the nested names, so an over-cap wrapper is capped per nested
	// call - each arguments object over activityInputCallHead becomes its
	// own stand-in with that much text, smaller ones stay - and the names
	// survive; only a wrapper still over the cap with every head dropped
	// falls back to the whole stand-in.
	hermesToolCallWrapper = "tool_call"
	wrapperCallsKey       = "calls"
	// wrapperCallNameKey is the key under which an element of the wrapper's
	// calls carries the nested tool's name.
	wrapperCallNameKey    = "name"
	wrapperCallArgsKey    = "arguments"
	activityInputCallHead = 256
	// activityBodyCap bounds one delivery read. hermes's payloads carry the
	// tool input and result whole, so a file write of a large manifest is a
	// few MiB; the cap is set well above that, since a delivery over it
	// cannot be attributed (the cut body verifies against no key), so
	// nothing counts it: the call it was for ends interrupted when its pre
	// arrived, and is absent when the pre was the large one, since the post
	// carries the same input and is larger still.
	activityBodyCap = 8 << 20
	// activityPublishTimeout bounds one artifact publish; the trace is
	// telemetry and must never stall the run or the terminal. hermes waits
	// on the delivery for the hook entry's timeout, which is longer, so a
	// slow publish is not a retried (duplicated) delivery.
	activityPublishTimeout = 5 * time.Second
	// activityDrainTimeout bounds finalize's flush of the calls still open,
	// so a slow bus cannot spend the terminal's budget.
	activityDrainTimeout = 5 * time.Second // activityDrainBudget below is its variable
	// activitySeenCap is how many delivery ids a task remembers for
	// dedupe, the most recent kept: hermes retries a delivery once, right
	// after it, so an id older than the newest 8192 has no retry coming.
	activitySeenCap  = 8192
	activityKeyBytes = 32
	// activityTruncatedTool names the one entry published in place of the
	// calls past the budget, with Dropped saying how many.
	activityTruncatedTool   = "activity-budget"
	ActivityStatusTruncated = "truncated"
	// The door's HTTP timeouts: a client on loopback that has not sent its
	// headers or body in these is broken, and the response is one status
	// line. The write deadline is armed when the headers are read, not when
	// the handler writes, and the handler publishes on the delivery under
	// run.mu, behind another delivery's publish or finalize's drain, result
	// and terminal; so it is longer than all of that put together, else a
	// slow publish closes the connection with no status and hermes retries
	// the delivery that the hook's own timeout was set long to avoid.
	// activityShutdownTimeout bounds Serve's drain on bridge exit.
	activityReadHeaderTimeout = 5 * time.Second
	activityReadTimeout       = 10 * time.Second
	activityWriteTimeout      = 60 * time.Second
	activityShutdownTimeout   = 2 * time.Second

	hookPreToolCall     = "pre_tool_call"
	hookPostToolCall    = "post_tool_call"
	hookSignatureHeader = "X-Hermes-Signature-256"
	hookSignaturePrefix = "sha256="
	hookStatusOK        = "ok"

	// Statuses on the activity entry. completed and error are the api path's
	// vocabulary; interrupted is a call whose end never arrived before the
	// task's terminal - deadline, cancel, or a crash mid-tool.
	ActivityStatusCompleted   = "completed"
	ActivityStatusError       = "error"
	ActivityStatusInterrupted = "interrupted"

	redactedValue = "[redacted]"
)

// What of an input reaches the bus is decided twice, because this stream
// is retained for days and copied into eval records. redactedKeyPattern
// and its siblings name input keys whose values never go on the bus, so
// the trace still says a secret-looking key was there. Then every string
// value becomes its shape (shapeValue): the graders on the inject lane
// read tool names (an entry's tool, a tool_call wrapper's calls[].name)
// and nothing of the arguments, and no grammar tells a resource name from
// a credential under the same key, so no string value leaves the pod
// whatever its key, and no value scrub is attempted. A credential a model
// pastes into a terminal command is one string under "command", and ships
// as a length.
// activityEntryBudget bounds the parts one task publishes on its trace and
// its heartbeat together. Both ride the task's own events subject, which
// the TASKS stream caps at 4096 messages per subject with discard-old, so a
// run that published without bound - a looping persona, or a short
// heartbeat interval under a long deadline - would evict its own submitted
// and working events and read as never started. One counter for both
// artifacts keeps the sum under the cap whatever the knobs say; 3000 leaves
// room for the four lifecycle events, a chunked result and the truncation
// marker. Trace parts stop activityHeartbeatReserve short of it, so the
// heartbeat keeps going to the terminal on exactly the looping run the
// budget exists for: 120 is the default interval under the default
// deadline (one a minute for two hours). A heartbeat interval set short
// enough to spend that alone still goes quiet at the budget, which the doc
// says. Variables only so a test can lower them.
var (
	activityEntryBudget      = 3000
	activityHeartbeatReserve = 120
	// activityDrainBudget is activityDrainTimeout as a variable, so a test
	// can spend the drain's budget before it starts.
	activityDrainBudget = activityDrainTimeout
)

// taskIDPattern is what a task id may look like before it becomes a path
// segment under ScratchDir: the gateway mints task-<hex>, tests use words.
// The id arrives on a bus envelope, and the lib checks it against the
// subject token it rode in on - which cannot hold a dot, so no ".." - but
// the sink that writes files does not lean on that.
var taskIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// writeScopeFile writes one file of a child scope; a variable only so a
// test can observe the order the two files are written in.
var writeScopeFile = os.WriteFile

var (
	// A key is secret-looking when one of the words is a whole component of
	// it (access_token, AWS_SECRET_ACCESS_KEY, api-key, private_key, and in camelCase
	// accessToken, clientSecret, dbPassword), not a substring (tokenizer,
	// secretName): the latter are names, and blanking them would put
	// "[redacted]" where the worker adapter's trace carries the value.
	// A count or a path that ends in the word (max_tokens, credentials_file,
	// accessTokenExpiry) is blanked too; that is the accepted price of
	// catching SECRET_KEY. The camelCase form is case-sensitive: the word
	// starts a component when any letter or digit precedes its capital (so
	// an acronym prefix counts: AWSSecretAccessKey, DBPassword, IDToken), and
	// ends one at the end, a separator or the next capital. A key that opens
	// with the word is handled apart (redactedCamelHeadPattern below).
	redactedKeyPattern = regexp.MustCompile(`(?i)(?:^|[_.-])(?:token|secret|password|passwd|passphrase|authorization|cookie|set-cookie|api[_-]?key|private[_-]?key|ssh[_-]?key|signing[_-]?key|key[_-]?data|credential)s?(?:$|[_.-])`)
	// An all-caps key with the word as an unseparated suffix (PGPASSWORD,
	// DBPASSWORD, MYSQLPASSWORD): the libpq spelling, not an exotic one.
	redactedUpperSuffixPattern = regexp.MustCompile(`[A-Z0-9](?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|COOKIE|CREDENTIAL)S?$`)
	redactedCamelKeyPattern    = regexp.MustCompile(`[A-Za-z0-9](?:Token|Secret|Password|Passwd|Passphrase|Authorization|Cookie|Api[_-]?Key|APIKey|Private[_-]?Key|Ssh[_-]?Key|SSHKey|Signing[_-]?Key|Key[_-]?Data|Credential)s?(?:$|[_.-]|[A-Z])`)
	// A camelCase key that opens with the word and goes on in another
	// component (secretAccessKey, SecretKey, tokenValue, passwordHash) is a
	// credential unless the next component says it is a name, a reference
	// or a location of one: secretName, SecretRef, tokenPath, credentialsFile,
	// passwordId stay.
	redactedCamelHeadPattern = regexp.MustCompile(`^(?i:token|secret|password|passwd|passphrase|authorization|cookie|api[_-]?key|private[_-]?key|ssh[_-]?key|signing[_-]?key|key[_-]?data|credential)s?[A-Z]`)
	camelHeadNamePattern     = regexp.MustCompile(`^(?i:token|secret|password|passwd|passphrase|authorization|cookie|api[_-]?key|private[_-]?key|ssh[_-]?key|signing[_-]?key|key[_-]?data|credential)s?(?:Name|Names|Ref|Refs|Path|Paths|File|Files|Id|Ids)?$`)
)

// schemaKeyPattern is what an object key must look like to be published
// under shape mode: a tool schema's key, letters, digits, underscore or dash,
// starting with a letter or underscore, at most schemaKeyMax characters. A
// key is model-written text like a value is (a map keyed by user data, a
// malformed call with the value in the key slot), so anything else, and
// anything token-shaped (tokenShapedKey), becomes "<key n, N chars>".
var (
	schemaKeyPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_-]*$`)
	// keyTokenRunPattern is the token look a schema-shaped key can still
	// have: a long digit run, an all-hex body, an all-caps-and-digits body
	// with digits in it, a long lower-case alphanumeric run.
	keyTokenRunPattern = regexp.MustCompile(`[0-9]{6,}|^[a-f0-9]{20,}$|^[A-Z0-9_]*[0-9][A-Z0-9_]*$|[a-z0-9]{24,}|[a-z]{20,}|[A-Z]{20,}`)
	// keyTokenPrefixPattern is the credential prefixes a key can start with
	// (GitHub, Google, Slack, AWS, GitLab, OpenAI tokens, a JWT); a key so
	// shaped is a token in the key slot whatever its body looks like.
	keyTokenPrefixPattern = regexp.MustCompile(`^(?:gh[pousr]_|github_pat_|ya29\.|AIza|xox[abposr]-|AKIA|ASIA|glpat-|sk-|eyJ)`)
)

const (
	schemaKeyMax = 48
	// keyMaxCaseChanges is how many camelCase humps and letter-digit
	// boundaries a key may have before it reads as base62 churn rather than
	// camelCase: a 36-character random body has about fifteen, a schema
	// key's words a handful.
	keyMaxCaseChanges = 5
)

// capRunes cuts s to at most n bytes at a rune boundary.
func capRunes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return chunkString(s, n)[0]
}

// tokenShapedKey says whether a key that fits the schema grammar still
// reads as a token: too long, a known credential prefix, a long digit,
// hex or single-case letter run, an all-caps-and-digits body with digits
// in it, or character classes that churn like base62. No grammar tells
// every token from every key; this names the shapes, and a key it refuses
// costs its spelling, not the trace.
func tokenShapedKey(k string) bool {
	if len(k) > schemaKeyMax || keyTokenRunPattern.MatchString(k) || keyTokenPrefixPattern.MatchString(k) {
		return true
	}
	changes := 0
	prev := 0
	for _, r := range k {
		class := 0
		switch {
		case r >= 'a' && r <= 'z':
			class = 1
		case r >= 'A' && r <= 'Z':
			class = 2
		case r >= '0' && r <= '9':
			class = 3
		}
		// A camelCase hump (lower to upper) and a letter-digit boundary
		// each count once; the fall back to lower case after a capital is
		// the hump's own end, not a change.
		if class != 0 && prev != 0 && class != prev && !(prev == 2 && class == 1) {
			changes++
		}
		prev = class
	}
	return changes > keyMaxCaseChanges
}

// shapeOf is what a free-text value becomes under shape mode.
func shapeOf(s string) string {
	return fmt.Sprintf("<string, %d chars>", utf8.RuneCountInString(s))
}

// ActivityEntry is one data part of the activity artifact: one tool
// invocation. tool and input are the worker adapter's shape; the rest is
// what hermes's hook adds. Results are deliberately absent - no check reads
// them and they are the riskiest payload in the pod.
type ActivityEntry struct {
	Tool   string          `json:"tool"`
	Input  json.RawMessage `json:"input,omitempty"`
	CallID string          `json:"callId,omitempty"`
	Status string          `json:"status,omitempty"`
	// ErrorType keeps hermes's own verdict when Status is error: its
	// status word (blocked, cancelled, timeout, error) or error_type
	// (tool_error), so a guardrail refusal stays distinguishable from a
	// tool failure in the trace.
	ErrorType  string `json:"errorType,omitempty"`
	DurationMs int64  `json:"durationMs,omitempty"`
	At         string `json:"at,omitempty"`
	// Dropped is set only on the activityTruncatedTool entry: how many
	// calls are missing from the trace, past the budget, failed to publish,
	// or left unreported by the drain's deadline.
	Dropped int `json:"dropped,omitempty"`
}

// hookDelivery is the subset of hermes's outbound webhook body the door
// reads (agent/shell_hooks.py _payload_fields plus the delivery metadata).
type hookDelivery struct {
	Event      string          `json:"hook_event_name"`
	ToolName   string          `json:"tool_name"`
	ToolInput  json.RawMessage `json:"tool_input"`
	Timestamp  string          `json:"timestamp"`
	DeliveryID string          `json:"delivery_id"`
	// SessionID is the hermes session the call ran in, top-level in the
	// payload; the door attributes an API-executor delivery by it.
	SessionID string `json:"session_id"`
	Extra     struct {
		ToolCallID string      `json:"tool_call_id"`
		DurationMs json.Number `json:"duration_ms"`
		Status     string      `json:"status"`
		ErrorType  string      `json:"error_type"`
	} `json:"extra"`
}

// UnmarshalJSON reads a delivery leniently: the body has to be a JSON
// object, and each field is taken when it has the expected type and left
// zero when it does not, so one field of a surprising type on a
// post_tool_call (a duration_ms that is a word, a tool_call_id that is a
// number) does not drop the delivery and leave its call to end
// interrupted. A string field also takes a number's or boolean's literal.
func (d *hookDelivery) UnmarshalJSON(b []byte) error {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(b, &top); err != nil {
		return err
	}
	d.Event = lenientString(top["hook_event_name"])
	d.ToolName = lenientString(top["tool_name"])
	d.ToolInput = top["tool_input"]
	d.Timestamp = lenientString(top["timestamp"])
	d.DeliveryID = lenientString(top["delivery_id"])
	d.SessionID = lenientString(top["session_id"])
	var extra map[string]json.RawMessage
	if raw, ok := top["extra"]; ok {
		_ = json.Unmarshal(raw, &extra)
	}
	d.Extra.ToolCallID = lenientString(extra["tool_call_id"])
	d.Extra.DurationMs = lenientNumber(extra["duration_ms"])
	d.Extra.Status = lenientString(extra["status"])
	d.Extra.ErrorType = lenientString(extra["error_type"])
	return nil
}

// useNumber is the yaml decoder option that keeps numbers as literals.
func useNumber(d *json.Decoder) *json.Decoder {
	d.UseNumber()
	return d
}

// lenientString is a JSON string's value, a number's or boolean's literal,
// and "" for anything else (absent, null, an object, an array).
func lenientString(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	t := strings.TrimSpace(string(raw))
	if t == "" || t == "null" || t[0] == '{' || t[0] == '[' {
		return ""
	}
	return t
}

// lenientNumber is a JSON number, or a string holding one; "" otherwise.
func lenientNumber(raw json.RawMessage) json.Number {
	var n json.Number
	if json.Unmarshal(raw, &n) == nil {
		return n
	}
	return ""
}

// activityState is one task's side of the door.
type activityState struct {

	// key signs this task's deliveries: the child's env value, verbatim.
	// hermes HMACs with the secret's text bytes (target.secret.encode()),
	// so the hex string is the key, not the bytes it spells.
	key string

	// sessionID is the API executor's attribution in place of a key: the
	// delivery is signed with the pod's shared ActivitySecret and names its
	// session, and inTurn says this task holds that session's turn, so of
	// two tasks in one conversation only the one whose turn is running
	// claims the call.
	sessionID string
	inTurn    atomic.Bool

	mu         sync.Mutex
	open       map[string]ActivityEntry // calls started and not yet ended
	openOrder  []string                 // their ids, in start order
	seen       map[string]struct{}      // delivery ids, so a hermes retry is one call
	seenRing   []string                 // the ids in arrival order, the oldest forgotten past activitySeenCap
	seenNext   int
	calls      int
	published  int // trace parts sent, against the budget less the reserve
	heartbeats int // progress parts sent, against the reserve
	dropped    int // calls past the trace's share, reported once at the terminal
	lastTool   string
	startedAt  time.Time
	appended   map[string]bool // artifact name -> a first part went out

	// The heartbeat goroutine's lifecycle. Entries are not queued: the door
	// publishes each one on the delivering request, under run.mu, so no
	// entry can sit between a queue and a drain when finalize runs.
	stop     chan struct{}
	stopOnce sync.Once
	done     chan struct{}
}

func newActivityState(withKey bool) *activityState {
	a := &activityState{
		open:      make(map[string]ActivityEntry),
		seen:      make(map[string]struct{}),
		startedAt: time.Now(),
		appended:  make(map[string]bool),
		stop:      make(chan struct{}),
		done:      make(chan struct{}),
	}
	if withKey {
		raw := make([]byte, activityKeyBytes)
		// crypto/rand.Read does not fail (it crashes the program if the
		// source is unusable), so there is no error to carry.
		_, _ = rand.Read(raw)
		a.key = hex.EncodeToString(raw)
	}
	return a
}

// newSessionActivityState is the API executor's side of the door: no key of
// its own (the pod's hook signs with the shared secret), the session id to
// claim deliveries by.
func newSessionActivityState(sessionID string) *activityState {
	a := newActivityState(false)
	a.sessionID = sessionID
	return a
}

// childEnv is what the door adds to the hermes child's environment: the
// signing key, the door's URL for the record, and the managed scope that
// carries the hook. Last wins among duplicates in exec.Cmd.Env, so the
// scope override replaces the sidecar's inherited one.
func (a *activityState) childEnv(url, managedDir string) []string {
	if a.key == "" {
		return nil
	}
	return []string{
		ActivitySecretEnv + "=" + a.key,
		ActivityURLEnv + "=" + url,
		ManagedDirEnv + "=" + managedDir,
	}
}

// signed reports whether sig (the X-Hermes-Signature-256 header) is this
// task's HMAC over body.
func (a *activityState) signed(sig string, body []byte) bool {
	return hookSigned(a.key, sig, body)
}

// hookSigned reports whether sig is key's HMAC over body, the way hermes
// signs a delivery. An empty key signs nothing.
func hookSigned(key, sig string, body []byte) bool {
	if key == "" || !strings.HasPrefix(sig, hookSignaturePrefix) {
		return false
	}
	got, err := hex.DecodeString(strings.TrimPrefix(sig, hookSignaturePrefix))
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(body)
	return hmac.Equal(got, mac.Sum(nil))
}

// observe records one delivery. A start opens a call; an end closes it and
// yields the entry to publish. Ends are the entries (one per invocation, the
// spec's rule); starts exist so an interrupted call can still be reported.
func (a *activityState) observe(d hookDelivery) (ActivityEntry, bool) {
	id := d.Extra.ToolCallID
	if id == "" {
		// Inline executors carry no call id; the tool name pairs start to
		// end well enough for the interrupted flush, which is all the id
		// is for.
		id = "tool:" + d.ToolName
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if d.DeliveryID != "" {
		if _, dup := a.seen[d.DeliveryID]; dup {
			return ActivityEntry{}, false
		}
		// The most recent activitySeenCap ids: a retry follows its first
		// delivery closely, so the oldest id is the one safe to forget.
		if len(a.seenRing) < activitySeenCap {
			a.seenRing = append(a.seenRing, d.DeliveryID)
		} else {
			delete(a.seen, a.seenRing[a.seenNext])
			a.seenRing[a.seenNext] = d.DeliveryID
			a.seenNext = (a.seenNext + 1) % activitySeenCap
		}
		a.seen[d.DeliveryID] = struct{}{}
	}
	a.lastTool = capRunes(d.ToolName, activityToolNameCap)
	switch d.Event {
	case hookPreToolCall:
		if _, seen := a.open[id]; !seen {
			a.openOrder = append(a.openOrder, id)
		}
		a.open[id] = ActivityEntry{
			Tool:   capRunes(d.ToolName, activityToolNameCap),
			Input:  redactInput(d.ToolName, d.ToolInput),
			CallID: capRunes(d.Extra.ToolCallID, activityCallIDCap),
			At:     capRunes(d.Timestamp, activityWordCap),
		}
		return ActivityEntry{}, false
	case hookPostToolCall:
		if _, seen := a.open[id]; seen {
			delete(a.open, id)
			a.openOrder = slices.DeleteFunc(a.openOrder, func(v string) bool { return v == id })
		}
		a.calls++
		status := activityStatus(d)
		e := ActivityEntry{
			Tool:       capRunes(d.ToolName, activityToolNameCap),
			Input:      redactInput(d.ToolName, d.ToolInput),
			CallID:     capRunes(d.Extra.ToolCallID, activityCallIDCap),
			Status:     status,
			DurationMs: durationMillis(d.Extra.DurationMs),
			At:         capRunes(d.Timestamp, activityWordCap),
		}
		if status == ActivityStatusError {
			e.ErrorType = capRunes(activityErrorType(d), activityWordCap)
		}
		return e, true
	}
	return ActivityEntry{}, false
}

// interrupted returns the calls still open, in start order, as entries, and
// forgets them. Called once, from finalize.
func (a *activityState) interrupted() []ActivityEntry {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]ActivityEntry, 0, len(a.openOrder))
	for _, id := range a.openOrder {
		e := a.open[id]
		e.Status = ActivityStatusInterrupted
		out = append(out, e)
	}
	a.open = make(map[string]ActivityEntry)
	a.openOrder = nil
	return out
}

// progressLine is the heartbeat text: what a reader of the rolling line, or
// of a stalled task's probe, needs to tell slow from stuck. The count only
// moves on a delivery through the door, so with the door closed the line
// says the trace is off rather than reporting zero calls from a persona
// that may be making them.
func (a *activityState) progressLine(now time.Time) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	elapsed := now.Sub(a.startedAt).Round(time.Second)
	if a.key == "" {
		return fmt.Sprintf("running %s, tool trace off", elapsed)
	}
	line := fmt.Sprintf("running %s, %d tool call(s)", elapsed, a.calls)
	if a.lastTool != "" {
		line += ", last " + a.lastTool
	}
	return line
}

// underBudget says whether the next trace part may go out, counting it
// either way: past the trace's share of the budget (the whole minus the
// heartbeat's reserve) the call is counted as dropped and reported once by
// the marker finalize publishes. Caller holds run.mu, which is what orders
// this against finalize's drain.
func (a *activityState) underBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.published < activityEntryBudget-activityHeartbeatReserve {
		a.published++
		return true
	}
	a.dropped++
	return false
}

// countDropped adds calls the drain could not report to the dropped count
// the marker publishes.
func (a *activityState) countDropped(n int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.dropped += n
}

// heartbeatUnderBudget is underBudget for a progress part, against the
// heartbeat's own share: the reserve, and nothing of the trace's, so a short
// interval under a long deadline silences the heartbeat and never the trace.
// Past it the heartbeat stops, uncounted - the marker counts calls.
func (a *activityState) heartbeatUnderBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.heartbeats < activityHeartbeatReserve {
		a.heartbeats++
		return true
	}
	return false
}

// truncationMarker is the entry that stands for the calls the budget cut,
// or false when nothing was cut.
func (a *activityState) truncationMarker() (ActivityEntry, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.dropped == 0 {
		return ActivityEntry{}, false
	}
	return ActivityEntry{Tool: activityTruncatedTool, Status: ActivityStatusTruncated, Dropped: a.dropped, At: time.Now().UTC().Format(time.RFC3339)}, true
}

func (a *activityState) signalStop() {
	a.stopOnce.Do(func() { close(a.stop) })
}

// activityErrorType is hermes's own verdict for a failed call: its status
// word when that is not the generic "error", else its error_type.
func activityErrorType(d hookDelivery) string {
	if d.Extra.Status != "" && d.Extra.Status != hookStatusOK && d.Extra.Status != ActivityStatusError {
		return d.Extra.Status
	}
	return d.Extra.ErrorType
}

func activityStatus(d hookDelivery) string {
	if d.Extra.ErrorType != "" || (d.Extra.Status != "" && d.Extra.Status != hookStatusOK) {
		return ActivityStatusError
	}
	return ActivityStatusCompleted
}

// durationMillis reads hermes's duration_ms whatever its number shape: an
// integer, or a float a Python emitter may write (12.5). One field's shape
// is not a reason to drop the delivery.
func durationMillis(n json.Number) int64 {
	if i, err := n.Int64(); err == nil {
		if i < 0 {
			return 0
		}
		return i
	}
	// Strict on the upper side: math.MaxInt64 rounds to 2^63 as a float,
	// which int64 cannot hold and would wrap negative.
	if f, err := n.Float64(); err == nil && f >= 0 && f < math.MaxInt64 {
		return int64(f)
	}
	return 0
}

// secretLookingKey says whether a value under this key never goes on the
// bus: a secret word as a whole component in any spelling, or a camelCase
// key that opens with the word and goes on in something other than a name.
func secretLookingKey(k string) bool {
	if redactedKeyPattern.MatchString(k) || redactedCamelKeyPattern.MatchString(k) || redactedUpperSuffixPattern.MatchString(k) {
		return true
	}
	return redactedCamelHeadPattern.MatchString(k) && !camelHeadNamePattern.MatchString(k)
}

// redactInput returns the tool input fit for the bus: the structure with
// the values under secret-looking keys blanked and every other string
// replaced by its shape, at every depth, and the whole thing capped. Over
// the cap the entry carries the size and a rune-safe head rather than a
// JSON fragment; for hermes's tool_call wrapper the cap is applied to each
// nested call's arguments instead, so the nested tool names stay readable.
func redactInput(tool string, raw json.RawMessage) json.RawMessage {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil
	}
	// UseNumber: a number decoded into float64 and written back loses the
	// low digits of a 64-bit id, and the worker adapter publishes the same
	// argument verbatim; json.Number falls through redactKeys and shapeValue
	// and marshals as its literal.
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return json.RawMessage(unparseableInput)
	}
	red := shapeValue(redactKeys(v), tool)
	out, err := json.Marshal(red)
	if err != nil {
		return json.RawMessage(unparseableInput)
	}
	if len(out) <= activityInputCap {
		return out
	}
	if tool == hermesToolCallWrapper {
		if capped, ok := capWrapperCalls(red); ok {
			return capped
		}
	}
	out, _ = json.Marshal(truncatedStandIn(out, activityInputHead))
	return out
}

// truncatedStandIn is what replaces an input over the cap: the size it had
// and headLen bytes of it, cut at a rune boundary.
func truncatedStandIn(out []byte, headLen int) map[string]any {
	head := ""
	if headLen > 0 {
		head = chunkString(string(out), headLen)[0]
	}
	return map[string]any{"truncated": true, "bytes": len(out), "head": head}
}

// capWrapperCalls caps a tool_call wrapper per nested call in two passes:
// first the large arguments objects become stand-ins with a short head,
// then every arguments object becomes a head-less one, so a wrapper of many
// small calls still keeps its names. False when the wrapper still does not
// fit, or when the input is not the wrapper's shape, and the caller falls
// back to the whole stand-in.
func capWrapperCalls(red any) (json.RawMessage, bool) {
	m, ok := red.(map[string]any)
	if !ok {
		return nil, false
	}
	calls, ok := m[wrapperCallsKey].([]any)
	if !ok || len(calls) == 0 {
		return nil, false
	}
	type nested struct {
		call map[string]any
		raw  []byte
	}
	var args []nested
	for _, c := range calls {
		call, ok := c.(map[string]any)
		if !ok {
			return nil, false
		}
		a, has := call[wrapperCallArgsKey]
		if !has {
			continue
		}
		raw, err := json.Marshal(a)
		if err != nil {
			return nil, false
		}
		args = append(args, nested{call: call, raw: raw})
	}
	for _, headLen := range []int{activityInputCallHead, 0} {
		for _, n := range args {
			if headLen == 0 || len(n.raw) > activityInputCallHead {
				n.call[wrapperCallArgsKey] = truncatedStandIn(n.raw, headLen)
			}
		}
		out, err := json.Marshal(m)
		if err != nil {
			return nil, false
		}
		if len(out) <= activityInputCap {
			return out, true
		}
	}
	return nil, false
}

// shapeValue is the pass over a key-blanked value: every string becomes
// its shape, with the structure, numbers, booleans and nulls left as they
// are. A "[redacted]" marker stays a marker, so the trace still says a
// secret-looking key was there.
func shapeValue(v any, tool string) any {
	return shapeValueIn(v, "", tool == hermesToolCallWrapper, 0, false)
}

// shapeValueIn is shapeValue with the enclosing key, the container depth,
// whether this input is hermes's tool_call wrapper and whether v is an
// element of the wrapper's own calls array (the array that is the root
// object's "calls" value), so a tool name is known as one by its path: the
// "name" string of such an element is what a tool_called check reads and
// is kept whatever its spelling, bounded like the entry's own tool. Every
// other string, under any key, at any depth and under any tool, is shaped,
// a "calls" that is a map or sits deeper included.
func shapeValueIn(v any, key string, wrapper bool, depth int, callElem bool) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		shaped := 0
		for k, val := range t {
			nk := k
			if !schemaKeyPattern.MatchString(k) || tokenShapedKey(k) {
				shaped++
				nk = fmt.Sprintf("<key %d, %d chars>", shaped, utf8.RuneCountInString(k))
			}
			if name, ok := val.(string); ok && callElem && k == wrapperCallNameKey && name != redactedValue {
				out[nk] = capRunes(name, activityToolNameCap)
				continue
			}
			out[nk] = shapeValueIn(val, k, wrapper, depth+1, false)
		}
		return out
	case []any:
		elem := wrapper && depth == wrapperCallsDepth && key == wrapperCallsKey
		for i := range t {
			t[i] = shapeValueIn(t[i], key, wrapper, depth+1, elem)
		}
		return t
	case string:
		if t == redactedValue {
			return t
		}
		return shapeOf(t)
	}
	return v
}

// redactKeys blanks the values under secret-looking keys at every depth;
// shapeValue replaces every other string right after, so no value is read
// for what it carries.
func redactKeys(v any) any {
	switch t := v.(type) {
	case map[string]any:
		for k, val := range t {
			if secretLookingKey(k) {
				t[k] = redactedValue
			} else {
				t[k] = redactKeys(val)
			}
		}
		return t
	case []any:
		for i := range t {
			t[i] = redactKeys(t[i])
		}
		return t
	}
	return v
}

// --- the child's managed scope ---

// childManagedScope writes the per-task managed directory: the source scope's
// config.yaml with the door's hooks.outbound entry added (appended to any the
// source already carries, less a pod-wide entry of the door's own name) and
// its .env verbatim. Returns the directory; the
// caller removes it once the child has exited.
func (b *Bridge) childManagedScope(taskID string) (dir string, err error) {
	if !taskIDPattern.MatchString(taskID) {
		return "", fmt.Errorf("task id %q is not a path segment", taskID)
	}
	scratch, err := filepath.Abs(b.cfg.ScratchDir)
	if err != nil {
		return "", fmt.Errorf("scratch dir: %w", err)
	}
	dir = filepath.Join(scratch, taskID)
	if err := os.MkdirAll(scratch, childScopeDirMode); err != nil {
		return "", fmt.Errorf("scratch dir: %w", err)
	}
	// Mkdir, not MkdirAll: the scope is made fresh or not at all. A task id
	// that names something already in the scratch dir (a shared mount's
	// cache, a symlink planted by a same-uid process) must not be adopted
	// as a scope, written into and removed at exit; it is refused with the
	// reason and the task fails at spawn.
	if err := os.Mkdir(dir, childScopeDirMode); err != nil {
		return "", fmt.Errorf("child scope dir %s: %w", dir, err)
	}
	// Nothing half-written survives a failure: the copy carries the
	// managed .env, and a directory left behind would hold it for the
	// pod's lifetime.
	made := dir
	defer func() {
		if err != nil {
			_ = os.RemoveAll(made)
		}
	}()
	// A source file that exists but cannot be read is a fault, not an
	// absence: a child started on a hook-only scope would run without the
	// operator's pins, silently. Only a missing file means nothing to copy.
	cfg := map[string]any{}
	var env []byte
	src := b.cfg.ManagedScopeDir
	if src != "" {
		raw, rerr := os.ReadFile(filepath.Join(src, managedConfigFile))
		switch {
		case rerr == nil:
			// UseNumber: the copy has to carry the operator's pins as written,
			// and a float64 round trip rewrites an id above 2^53 (a chat
			// channel's) into a neighbouring integer with nothing logging it.
			if err := yaml.Unmarshal(raw, &cfg, useNumber); err != nil {
				return "", fmt.Errorf("managed config %s: %w", src, err)
			}
			if cfg == nil {
				cfg = map[string]any{}
			}
		case !errors.Is(rerr, os.ErrNotExist):
			return "", fmt.Errorf("managed config %s: %w", src, rerr)
		}
		raw, rerr = os.ReadFile(filepath.Join(src, managedEnvFile))
		switch {
		case rerr == nil:
			env = raw
		case !errors.Is(rerr, os.ErrNotExist):
			return "", fmt.Errorf("managed env %s: %w", src, rerr)
		}
	}
	// The operator's hooks are kept and the door's entry appended. A hooks
	// or hooks.outbound of another shape (a mapping where a list belongs)
	// is the same fault as an unreadable file: silently replacing it would
	// start the child without the operator's hooks.
	hooks := map[string]any{}
	if v, present := cfg[hooksKey]; present && v != nil {
		m, ok := v.(map[string]any)
		if !ok {
			return "", fmt.Errorf("managed config %s: %s is %T, not a mapping", src, hooksKey, v)
		}
		hooks = m
	}
	var outbound []any
	if v, present := hooks[hooksOutboundKey]; present && v != nil {
		l, ok := v.([]any)
		if !ok {
			return "", fmt.Errorf("managed config %s: %s.%s is %T, not a list", src, hooksKey, hooksOutboundKey, v)
		}
		// The pod's own entry for the API executor carries this name and the
		// same secret_env, which the child's environment rebinds to its
		// per-task key: kept, it would deliver each call twice.
		for _, e := range l {
			if m, ok := e.(map[string]any); ok && m["name"] == hookEntryName {
				continue
			}
			outbound = append(outbound, e)
		}
	}
	outbound = append(outbound, map[string]any{
		"name":       hookEntryName,
		"url":        b.ActivityURL(),
		"events":     []any{hookPreToolCall, hookPostToolCall},
		"secret_env": ActivitySecretEnv,
		"timeout":    hookTimeoutSeconds,
	})
	hooks[hooksOutboundKey] = outbound
	cfg[hooksKey] = hooks
	out, err := yaml.Marshal(cfg)
	if err != nil {
		return "", fmt.Errorf("child scope config: %w", err)
	}
	// The marker first, then config.yaml, then .env: the start-time sweep
	// takes a scope by its marker, so a kill between any two writes leaves a
	// scope the next start removes rather than a credential copy it never
	// sees.
	if err := writeScopeFile(filepath.Join(dir, scopeMarkerFile), nil, childScopeFileMode); err != nil {
		return "", fmt.Errorf("child scope marker: %w", err)
	}
	if err := writeScopeFile(filepath.Join(dir, managedConfigFile), out, childScopeFileMode); err != nil {
		return "", fmt.Errorf("child scope config: %w", err)
	}
	if env != nil {
		if err := writeScopeFile(filepath.Join(dir, managedEnvFile), env, childScopeFileMode); err != nil {
			return "", fmt.Errorf("child scope env: %w", err)
		}
	}
	return dir, nil
}

// --- the door ---

// listenActivity binds the door and claims the scratch dir. Called from New
// so the address is known before Run; Run serves it. The scratch dir is
// made if absent and swept, never replaced: a scope a previous incarnation
// left behind (killed mid-task, its defers never run) held a copy of the
// managed .env, and the sweep that finalizes that incarnation's tasks does
// not know about files. Only that incarnation's leftovers go with it, the
// direct subdirectories carrying the bridge's marker file; the directory
// itself and anything else in it are left alone, since BRIDGE_SCRATCH_DIR
// is whatever the manifest says and may name a mount the bridge does not
// own.
func (b *Bridge) listenActivity() error {
	// The sweep runs whether or not the door opens: a previous incarnation
	// with the door open may have left a scope, and this one closing the
	// door is not a reason to leave its credential copy behind. With the
	// door closed nothing is written, so a scratch dir that is missing or
	// unusable is said in the log and the executor starts; with it open
	// every task needs a scope there, so the same is a start-time failure.
	if b.cfg.ActivityListen == "" {
		if err := sweepTaskScopes(b.cfg.ScratchDir, b.cfg.Logger); err != nil && !os.IsNotExist(err) {
			b.cfg.Logger.Warn("scratch dir not swept; the door is closed and nothing is written there", "dir", b.cfg.ScratchDir, "err", err)
		}
		return nil
	}
	if err := os.MkdirAll(b.cfg.ScratchDir, childScopeDirMode); err != nil {
		return fmt.Errorf("scratch dir %s: %w", b.cfg.ScratchDir, err)
	}
	if err := sweepTaskScopes(b.cfg.ScratchDir, b.cfg.Logger); err != nil {
		return fmt.Errorf("scratch dir %s: %w", b.cfg.ScratchDir, err)
	}
	ln, err := net.Listen("tcp", b.cfg.ActivityListen)
	if err != nil {
		return fmt.Errorf("activity door listen %s: %w", b.cfg.ActivityListen, err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc(ActivityPath, b.handleActivity)
	b.activityLn = ln
	b.activityDone = make(chan struct{})
	b.activitySrv = &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: activityReadHeaderTimeout,
		ReadTimeout:       activityReadTimeout,
		WriteTimeout:      activityWriteTimeout,
	}
	return nil
}

// sweepTaskScopes removes the direct children of dir that are the bridge's
// own scopes: a directory that carries scopeMarkerFile, which only
// childManagedScope writes, and which it writes first. The marker rather
// than a name shape, because BRIDGE_SCRATCH_DIR may name a directory the
// bridge shares (cache, data, work are the shape of most directories) and
// because the bus accepts any DNS-1123 label as a task id, so no name
// shape is the bridge's to require. A file, or a directory without the
// marker, is not the bridge's and stays. A scope that cannot be removed
// is logged and left: the scratch dir is a shared emptyDir, any same-uid
// process in the pod can plant an unremovable directory in it, and the
// executor must not stay down for that. Only an unreadable scratch dir is
// an error.
func sweepTaskScopes(dir string, log *slog.Logger) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		scope := filepath.Join(dir, e.Name())
		if _, err := os.Stat(filepath.Join(scope, scopeMarkerFile)); err != nil {
			continue
		}
		if err := os.RemoveAll(scope); err != nil && log != nil {
			log.Warn("leftover task scope not removed", "scope", scope, "err", err)
		}
	}
	return nil
}

// ActivityURL is where this bridge's children deliver; "" when the door is
// off.
func (b *Bridge) ActivityURL() string {
	if b.activityLn == nil {
		return ""
	}
	return "http://" + b.activityLn.Addr().String() + ActivityPath
}

func (b *Bridge) serveActivity(ctx context.Context) {
	if b.activitySrv == nil {
		return
	}
	go func() {
		select {
		case <-ctx.Done():
		case <-b.activityDone:
		}
		b.closeActivity()
	}()
	go func() {
		if err := b.activitySrv.Serve(b.activityLn); err != nil && err != http.ErrServerClosed {
			b.cfg.Logger.Error("activity door stopped", "err", err)
		}
	}()
	b.cfg.Logger.Info("activity door listening", "url", b.ActivityURL())
}

// closeActivity shuts the door once, from whichever side ends first: the
// context (a shutdown) or the bridge's own close (a Run that returned
// early, say on a refused subscribe). The server is asked to shut down
// before the listener is closed, so Serve ends with ErrServerClosed and
// nothing logs a stopped door beside the error that really ended the run.
func (b *Bridge) closeActivity() {
	if b.activityLn == nil {
		return
	}
	b.activityOnce.Do(func() {
		close(b.activityDone)
		if b.activitySrv != nil {
			sctx, cancel := context.WithTimeout(context.Background(), activityShutdownTimeout)
			defer cancel()
			_ = b.activitySrv.Shutdown(sctx)
		}
		_ = b.activityLn.Close()
	})
}

// handleActivity is the door. Every answer past the method check is 204:
// hermes retries connection errors and 5xx and warns on 4xx, and a delivery
// the door cannot use (unsigned, unmatched, unparseable) is not the sender's
// problem to hear about per call. An entry is published on this request,
// under run.mu: hermes delivers from one background thread in order, so
// the trace keeps call order, a publish costs the tool call nothing, and
// there is no queue for finalize to race.
func (b *Bridge) handleActivity(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, activityBodyCap+1))
	if err != nil || len(body) > activityBodyCap {
		// Cut at the cap, so the signature cannot verify and the task is
		// not known here. The usual over-size delivery is a post_tool_call
		// (it carries the result), whose small pre arrived and opened the
		// call, so the call ends interrupted at the terminal though the
		// tool finished; an over-size pre leaves the call absent. Said in
		// the log, since nothing else can say it.
		b.cfg.Logger.Warn("activity delivery refused: over the body cap or unreadable", "bytes", len(body), "cap", activityBodyCap, "err", err)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	run := b.runForSignature(r.Header.Get(hookSignatureHeader), body)
	if run == nil {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	var d hookDelivery
	if err := json.Unmarshal(body, &d); err != nil {
		// Signed for this task and unreadable, and which event it was
		// cannot be known: an unreadable pre costs the trace nothing (the
		// post carries the record whole), an unreadable post leaves its
		// call open to end interrupted at the drain, so the call is in the
		// trace either way and the marker does not count it. Said in the
		// log.
		b.cfg.Logger.Warn("activity delivery unparseable", "task", run.origin.TaskID, "err", err)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	// observe under run.mu: it closes the call, and a finalize slipping in
	// between the close and the publish would find nothing open to report
	// and the entry would then be dropped as post-terminal - present in the
	// trace neither as completed nor as interrupted.
	act := run.act.Load()
	run.mu.Lock()
	if entry, ok := act.observe(d); ok {
		if run.state == stateRunning {
			if act.underBudget() {
				if !b.publishActivityEntry(run, entry) {
					act.countDropped(1)
				}
			}
		} else {
			b.cfg.Logger.Warn("activity delivery after the terminal; dropped", "task", run.origin.TaskID, "tool", entry.Tool)
		}
	}
	run.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

// runForSignature finds the in-flight task a delivery belongs to: the CLI
// task whose own key signed body, else, when the pod's shared secret signed
// it, the API task holding the turn in the session the payload names. The
// session id is read only after the signature verifies, so an unsigned body
// cannot pick a task.
func (b *Bridge) runForSignature(sig string, body []byte) *taskRun {
	if sig == "" {
		return nil
	}
	b.mu.Lock()
	runs := make([]*taskRun, 0, len(b.tasks))
	for _, r := range b.tasks {
		runs = append(runs, r)
	}
	b.mu.Unlock()
	for _, r := range runs {
		if a := r.act.Load(); a != nil && a.signed(sig, body) {
			return r
		}
	}
	if !hookSigned(b.cfg.ActivitySecret, sig, body) {
		return nil
	}
	var d hookDelivery
	if err := json.Unmarshal(body, &d); err != nil || d.SessionID == "" {
		return nil
	}
	for _, r := range runs {
		if a := r.act.Load(); a != nil && a.sessionID == d.SessionID && a.inTurn.Load() {
			return r
		}
	}
	return nil
}

// --- the heartbeat ---

// runActivity is one task's heartbeat: a progress part on the interval
// until finalize stops it. Each publish takes run.mu and checks the state,
// so nothing lands after the terminal.
func (b *Bridge) runActivity(run *taskRun) {
	a := run.act.Load()
	defer close(a.done)
	var tick <-chan time.Time
	if b.cfg.ProgressInterval > 0 {
		t := time.NewTicker(b.cfg.ProgressInterval)
		defer t.Stop()
		tick = t.C
	}
	for {
		select {
		case <-a.stop:
			return
		case <-tick:
			run.mu.Lock()
			if run.state == stateRunning && a.heartbeatUnderBudget() {
				b.publishProgress(run, a.progressLine(time.Now()))
			}
			run.mu.Unlock()
		}
	}
}

// drainActivity is finalize's half: with run.mu held and the state still
// running, report every call still open as interrupted, then stop the
// heartbeat. Bounded, because the result and the terminal are the publishes
// that matter.
func (b *Bridge) drainActivity(run *taskRun) {
	a := run.act.Load()
	if a == nil {
		return
	}
	a.signalStop()
	deadline := time.Now().Add(activityDrainBudget)
	open := a.interrupted()
	for i, e := range open {
		if !time.Now().Before(deadline) {
			// The calls left are counted as dropped, so the marker below says
			// the trace is short rather than vouching for it.
			a.countDropped(len(open) - i)
			b.cfg.Logger.Warn("activity drain budget spent; interrupted calls not all reported", "task", run.origin.TaskID, "unreported", len(open)-i)
			break
		}
		if a.underBudget() && !b.publishActivityEntry(run, e) {
			a.countDropped(1)
		}
	}
	// The marker is what tells a reader the trace is short; it goes out
	// after the drain whether or not the drain spent its budget, on its own
	// publish timeout, and a failure is said rather than silent.
	if marker, ok := a.truncationMarker(); ok {
		if !b.publishActivityEntry(run, marker) {
			b.cfg.Logger.Warn("activity truncation marker not published; the trace reads complete and is not", "task", run.origin.TaskID, "dropped", marker.Dropped)
		}
	}
}

// waitActivity joins the publisher after finalize; the goroutine is gone
// before the run is forgotten.
func (b *Bridge) waitActivity(run *taskRun) {
	a := run.act.Load()
	if a == nil {
		return
	}
	a.signalStop()
	select {
	case <-a.done:
	case <-time.After(activityPublishTimeout):
	}
}

// publishActivityEntry publishes one data part onto the activity artifact
// and reports whether it went out. Caller holds run.mu.
func (b *Bridge) publishActivityEntry(run *taskRun, e ActivityEntry) bool {
	data, err := json.Marshal(e)
	if err != nil {
		b.cfg.Logger.Warn("activity entry marshal failed", "task", run.origin.TaskID, "err", err)
		return false
	}
	return b.publishArtifactPart(run, lib.ArtifactActivity, lib.Part{Kind: "data", Data: data})
}

// publishProgress publishes one text part onto the progress artifact.
// Caller holds run.mu.
func (b *Bridge) publishProgress(run *taskRun, text string) {
	b.publishArtifactPart(run, lib.ArtifactProgress, lib.Part{Kind: "text", Text: text})
}

// publishArtifactPart is the worker adapter's publishArtifactChunk: one part
// appended onto the named artifact, artifactId "artifact-<task>-<name>",
// append after the first, never lastChunk, best-effort, and reports whether
// the part went out. The result artifact keeps its own publisher because it
// is chunked and load-bearing.
func (b *Bridge) publishArtifactPart(run *taskRun, name string, part lib.Part) bool {
	a := run.act.Load()
	payload, err := json.Marshal(lib.ArtifactUpdate{
		TaskID:    run.origin.TaskID,
		ContextID: run.origin.ContextID,
		Artifact: lib.Artifact{
			ArtifactID: "artifact-" + run.origin.TaskID + "-" + name,
			Name:       name,
			Parts:      []lib.Part{part},
		},
		Append: a.appended[name],
	})
	if err != nil {
		b.cfg.Logger.Warn("artifact marshal failed", "task", run.origin.TaskID, "name", name, "err", err)
		return false
	}
	env, err := lib.NewArtifactUpdateEnvelope(b.from, run.origin.TaskID, run.origin.ContextID, run.origin.CorrelationID, payload)
	if err != nil {
		b.cfg.Logger.Warn("artifact envelope failed", "task", run.origin.TaskID, "name", name, "err", err)
		return false
	}
	// Marked before the publish, as the worker adapter does: a publish whose
	// ack times out after the server stored it (a reconnect) must not make
	// the next part a non-append that replaces the artifact in every fold.
	// A lost first part costs one entry; a reset costs the whole trace.
	a.appended[name] = true
	ctx, cancel := context.WithTimeout(context.Background(), activityPublishTimeout)
	defer cancel()
	if err := b.c.Publish(ctx, lib.TaskEventsSubject(b.cfg.Profile, run.origin.TaskID), env); err != nil {
		b.cfg.Logger.Warn("artifact publish failed", "task", run.origin.TaskID, "name", name, "err", err)
		return false
	}
	return true
}
