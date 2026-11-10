package hermesbridge

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
)

// Synthetic values only: none of these is, or was copied from, a real
// credential.
const (
	synthSecretPlain   = "synthetic-pass-0001"
	synthSecretPlain2  = "synthetic-token-0002"
	synthCardToken     = "quietpasture-synthetic-0003"
	synthShortSecret   = "abc1234" // seven bytes: under the floor
	synthSANamespace   = "kube-system"
	synthSecretYAMLVal = "synthetic-yaml-0004"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// secretJSON is a Secret as kubectl's -o json prints it.
func secretJSON(t *testing.T, typ string, data map[string]string) string {
	t.Helper()
	enc := map[string]string{}
	for k, v := range data {
		enc[k] = b64(v)
	}
	raw, err := json.MarshalIndent(map[string]any{
		"apiVersion": "v1", "kind": "Secret", "type": typ,
		"metadata": map[string]any{"name": "app-db", "namespace": "prod"},
		"data":     enc,
	}, "", "    ")
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// terminalResult is a terminal tool's result as hermes's post_tool_call
// carries it: a JSON string whose content is JSON with the command's
// output in it.
func terminalResult(t *testing.T, output string) json.RawMessage {
	t.Helper()
	inner, _ := json.Marshal(map[string]any{"output": output, "exit_code": 0, "error": nil})
	outer, _ := json.Marshal(string(inner))
	return outer
}

// A Secret's data values (decoded and as given), read from a terminal
// result's nested JSON, are redacted from an answer by exact match, in both
// forms; a value under eight bytes is not tracked.
func TestSeenSecrets_SecretDataRedactedDecodedAndBase64(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{
		"password": synthSecretPlain, "api-token": synthSecretPlain2 + "\n", "pin": synthShortSecret,
	})))
	answer := fmt.Sprintf("The Secret holds %s and %s (stored as %s); the pin is short: %s.",
		synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain), synthShortSecret)
	got := scrubAnswerText(answer, &s)
	for _, leaked := range []string{synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain)} {
		if strings.Contains(got, leaked) {
			t.Errorf("answer leaked %q: %q", leaked, got)
		}
	}
	if !strings.Contains(got, synthShortSecret) {
		t.Errorf("a value under %d bytes was redacted: %q", seenSecretMinBytes, got)
	}
	if s.has(synthShortSecret) || s.has(b64(synthShortSecret)) {
		t.Errorf("a value under %d bytes is tracked", seenSecretMinBytes)
	}
}

// The same from YAML output, from stringData, and from JSON cut short.
func TestSeenSecrets_YAMLStringDataAndTruncatedJSON(t *testing.T) {
	var s seenSecrets
	yaml := "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n  password: " + b64(synthSecretYAMLVal) + "\nstringData:\n  token: \"" + synthSecretPlain2 + "\"\n"
	s.collectToolResult(terminalResult(t, yaml))
	cut := `{"apiVersion": "v1", "kind": "Secret", "data": {"password": "` + b64(synthSecretPlain) + `"}, "metadata": {"name": "app", "annot`
	s.collectToolResult(terminalResult(t, cut))
	for _, v := range []string{synthSecretYAMLVal, synthSecretPlain2, synthSecretPlain, b64(synthSecretPlain), b64(synthSecretPlain2)} {
		if !s.has(v) {
			t.Errorf("%q not tracked", v)
		}
	}
}

// A service account token Secret's namespace is a name, not a credential:
// it is not tracked, so the answer keeps it.
func TestSeenSecrets_AServiceAccountTokensNamespaceIsNotTracked(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "kubernetes.io/service-account-token", map[string]string{
		"namespace": synthSANamespace, "token": synthSecretPlain,
	})))
	got := scrubAnswerText("The token in "+synthSANamespace+" is "+synthSecretPlain+".", &s)
	if !strings.Contains(got, synthSANamespace) || strings.Contains(got, synthSecretPlain) {
		t.Errorf("answer = %q, want the namespace kept and the token gone", got)
	}
}

// A Secret's name (the pull secret and token Secret a service account
// lists, a Helm existingSecret reference, a TLS secretName), describe
// secret's byte counts, an image digest, a phrase after a credential key,
// and a word under a weak or ambiguous key are not tracked, so the answer
// keeps every one of them where it names them.
func TestSeenSecrets_NamesSizesDigestsAndPhrasesAreNotTracked(t *testing.T) {
	results := []string{
		"Name:                default\nNamespace:           shop\nImage pull secrets:  regcred-missing\nMountable secrets:   default-token-x7k2p\nTokens:              <none>\n",
		"Name:         db-creds\nType:  Opaque\n\nData\n====\npassword:  14 bytes\nusername:  5 bytes\n",
		"auth:\n  existingSecret: postgres-creds\n  imagePullSecret: regcred-missing\nspec:\n  tls:\n  - hosts: [a]\n    secretName: shop-tls-cert\n    secret: shop-tls-cert\n",
		"docker pull nginx\nDigest: sha256:4f1c9a0b2e3d4c5f6a7b8c9d0e1f2a3b4c5d6e7f\nStatus: Downloaded\n",
		"The secret is referenced by the deployment; the pull secret is regcred-missing.",
		"Authorization: denied for user alice-operator\nauth: oidc-provider-x\n",
		"32|NODE_LOCATIONS_KEY = \"locations\"\n33|ZONE_KEY = \"zone\"",
	}
	var s seenSecrets
	for _, r := range results {
		s.collectToolResult(terminalResult(t, r))
	}
	if n := s.size(); n != 0 {
		t.Fatalf("set holds %d values from results with no credential in them: %q", n, s.values())
	}
	answer := "Pull secret regcred-missing is absent; default-token-x7k2p is mounted. password (14 bytes). " +
		"Postgres uses postgres-creds and TLS uses shop-tls-cert, image sha256:4f1c9a0b2e3d4c5f6a7b8c9d0e1f2a3b4c5d6e7f. " +
		"alice-operator was denied; oidc-provider-x is the issuer. Location (`locations`)."
	if got := scrubAnswerText(answer, &s); got != answer {
		t.Errorf("answer = %q, want it unchanged", got)
	}
}

// A credential a shape rule catches in a tool result that is not a Secret
// object (an env dump, a config file, a connection string, a password
// flag), under a key, flag or noun that names it outright, is tracked, so
// its echo in the answer is redacted where it has no shape of its own.
func TestSeenSecrets_AStrongKeyCredentialInAnyResultIsTracked(t *testing.T) {
	const (
		envWord = "quietpasture"
		cfgTok  = "meadowlark-synthetic-12"
		urlPass = "lanternfield-synthetic"
		flagVal = "harborstone-synthetic"
	)
	var s seenSecrets
	s.collectToolResult(terminalResult(t, "export DB_PASSWORD="+envWord+"\nok"))
	s.collectToolResult(terminalResult(t, `{"client": {"token": "`+cfgTok+`"}}`))
	s.collectToolResult(json.RawMessage(mustJSONString(t, "DATABASE_URL=postgres://app:"+urlPass+"@db.internal:5432/orders")))
	s.collectToolResult(terminalResult(t, "app --db-password "+flagVal+" --port 5432"))
	answer := "Use " + envWord + ", then " + cfgTok + "; the URL carries " + urlPass + " and the flag " + flagVal + "."
	got := scrubAnswerText(answer, &s)
	for _, v := range []string{envWord, cfgTok, urlPass, flagVal} {
		if strings.Contains(got, v) {
			t.Errorf("answer leaked %q: %q", v, got)
		}
	}
}

func mustJSONString(t *testing.T, v string) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// A Secret's value under a credential-named key is tracked whatever its
// shape, a plain word included; the connection settings
// (seenSecretSkippedKeys) are not, in JSON, in YAML or cut short.
func TestSeenSecrets_ACredentialKeysPlainWordIsTrackedAndSettingsAreNot(t *testing.T) {
	const (
		plainWord = "quietpasture"
		user      = "orders-app-user"
		host      = "db.orders.internal"
		port      = "543210000"
		dbName    = "orders-main-db"
	)
	data := map[string]string{"password": plainWord, "username": user, "Host": host, "port": port, "database": dbName}
	yaml := "apiVersion: v1\nkind: Secret\ndata:\n  password: " + b64(plainWord) + "\n  username: " + b64(user) + "\nstringData:\n  host: " + host + "\n  database: \"" + dbName + "\"\n"
	cut := `{"kind": "Secret", "data": {"password": "` + b64(plainWord) + `", "user": "` + b64(user) + `", "port": "` + b64(port) + `", "x`
	for name, result := range map[string]json.RawMessage{
		"json":      terminalResult(t, secretJSON(t, "Opaque", data)),
		"yaml":      terminalResult(t, yaml),
		"truncated": terminalResult(t, cut),
	} {
		var s seenSecrets
		s.collectToolResult(result)
		if !s.has(plainWord) {
			t.Errorf("%s: the password's plain-word value is not tracked", name)
		}
		for _, v := range []string{user, host, port, dbName} {
			if s.has(v) || s.has(b64(v)) {
				t.Errorf("%s: a connection setting %q is tracked", name, v)
			}
		}
	}
}

// YAML lists: kubectl's -o yaml of several Secrets is a List whose items
// are indented, a kind line may carry a comment, and each item is read for
// its own lines only, so a ConfigMap item beside a Secret is not read.
func TestSeenSecrets_YAMLSecretListItemsAreReadEachInItsOwnScope(t *testing.T) {
	const (
		first    = "synthetic-list-0005"
		second   = "synthetic-list-0006"
		cmValue  = "configmap-value-0007"
		commentd = "synthetic-cmt-0008"
	)
	list := "apiVersion: v1\nitems:\n- apiVersion: v1\n  data:\n    password: " + b64(first) + "\n  kind: Secret\n  metadata:\n    name: a\n" +
		"- apiVersion: v1\n  data:\n    setting: " + b64(cmValue) + "\n  kind: ConfigMap\n  metadata:\n    name: b\n" +
		"- apiVersion: v1\n  data:\n    token: " + b64(second) + "\n  kind: Secret\n  metadata:\n    name: c\nkind: List\n"
	var s seenSecrets
	s.collectToolResult(terminalResult(t, list))
	s.collectToolResult(terminalResult(t, "apiVersion: v1\ndata:\n  password: "+b64(commentd)+"\nkind: Secret # the db creds\n"))
	for _, v := range []string{first, second, b64(first), commentd} {
		if !s.has(v) {
			t.Errorf("%q not tracked", v)
		}
	}
	if s.has(cmValue) || s.has(b64(cmValue)) {
		t.Error("a ConfigMap item's data in the same list is tracked")
	}
}

// JSON cut inside a data block: every pair read whole is tracked, the cut
// one is not. Each block is read for its own object's kind: a ConfigMap
// item is not read, a Secret item beside it is, and an item cut before its
// kind is printed counts as a Secret only when every item before it was.
func TestSeenSecrets_JSONCutInsideDataTracksWhatWasReadWhole(t *testing.T) {
	const (
		whole   = "synthetic-whole-0009"
		inBlock = "synthetic-block-0010"
		cutOff  = "synthetic-cutoff-0011"
	)
	cut := `{"apiVersion":"v1","items":[{"apiVersion":"v1","data":{"a":"` + b64(whole) + `"},"kind":"Secret"},` +
		`{"apiVersion":"v1","data":{"b":"` + b64(inBlock) + `","c":"` + b64(cutOff)[:10]
	var s seenSecrets
	s.collectToolResult(terminalResult(t, cut))
	if !s.has(whole) || !s.has(inBlock) {
		t.Errorf("a pair read whole is not tracked: %v %v", s.has(whole), s.has(inBlock))
	}
	if s.size() != 4 {
		t.Errorf("size = %d, want 4 (two values, each as given and decoded): the cut pair was tracked", s.size())
	}
	mixed := `{"items":[{"data":{"a":"` + b64(whole) + `"},"kind":"ConfigMap"},{"data":{"b":"` + b64(inBlock) + `"},"kind":"Secret"},{"data":{"c":"` + b64(cutOff) + `"`
	var m seenSecrets
	m.collectToolResult(terminalResult(t, mixed))
	if m.has(whole) || m.has(cutOff) || !m.has(inBlock) {
		t.Errorf("mixed list: ConfigMap read %v, unknown cut item read %v, Secret item read %v; want false, false, true",
			m.has(whole), m.has(cutOff), m.has(inBlock))
	}
}

// A Secret cut short after its kind, whose metadata names another kind (an
// owner reference), is still read for its own data.
func TestSeenSecrets_ACutSecretWithAnOwnerReferenceIsRead(t *testing.T) {
	const value = "synthetic-owned-0012"
	cut := `{"apiVersion":"v1","data":{"password":"` + b64(value) + `"},"kind":"Secret","metadata":{"name":"app-db",` +
		`"ownerReferences":[{"apiVersion":"apps/v1","kind":"Deployment","name":"web","uid":"1"}],"resourceVersion":"12`
	var s seenSecrets
	s.collectToolResult(terminalResult(t, cut))
	if !s.has(value) {
		t.Error("a cut Secret with an owner reference of another kind was not read")
	}
}

// A Secret list larger than the shape rules' scan cap is parsed whole, so
// a value past the cap is tracked.
func TestSeenSecrets_ASecretListOverTheScanCapIsReadWhole(t *testing.T) {
	const value = "synthetic-past-cap-0013"
	filler := strings.Repeat("A", toolResultScanCap+1024)
	list, err := json.Marshal(map[string]any{"apiVersion": "v1", "kind": "List", "items": []any{
		map[string]any{"kind": "Secret", "data": map[string]string{"bundle": filler}},
		map[string]any{"kind": "Secret", "data": map[string]string{"password": b64(value)}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var s seenSecrets
	s.collectToolResult(terminalResult(t, string(list)))
	if !s.has(value) {
		t.Error("a Secret value past the scan cap was not tracked")
	}
}

// Once the task has finalized the set is closed: a delivery that was past
// the door when finalize ran adds nothing.
func TestSeenSecrets_NothingIsAddedAfterClear(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain})))
	if !s.has(synthSecretPlain) {
		t.Fatal("not tracked before clear")
	}
	s.clear()
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain2})))
	s.add(synthSecretPlain2 + "-x")
	if s.size() != 0 {
		t.Errorf("size after clear = %d, want 0", s.size())
	}
}

// Collect, redact and clear run on different goroutines (the door, the
// finalizer); under -race they must not trip.
func TestSeenSecrets_CollectRedactAndClearConcurrently(t *testing.T) {
	var s seenSecrets
	result := terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain}))
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(3)
		go func() { defer wg.Done(); s.collectToolResult(result) }()
		go func() { defer wg.Done(); _ = scrubAnswerText("x "+synthSecretPlain+" y", &s) }()
		go func() { defer wg.Done(); _ = s.size() }()
	}
	wg.Wait()
	if got := scrubAnswerText("x "+synthSecretPlain+" y", &s); got != "x [redacted] y" {
		t.Errorf("redact = %q", got)
	}
	s.clear()
	if s.size() != 0 {
		t.Error("clear left values behind")
	}
}

// The longest value wins where two overlap, so no fragment is left over.
func TestSeenSecrets_TheLongestValueWins(t *testing.T) {
	var s seenSecrets
	s.add(synthSecretPlain)
	s.add(synthSecretPlain + "-suffix")
	if got := scrubAnswerText("x "+synthSecretPlain+"-suffix y", &s); got != "x [redacted] y" {
		t.Errorf("redact = %q, want one marker for the longer value", got)
	}
}

// secretYAMLResult is a terminal result printing one Secret as YAML, its
// data in the order given.
func secretYAMLResult(t *testing.T, vals []string) json.RawMessage {
	t.Helper()
	var sb strings.Builder
	sb.WriteString("apiVersion: v1\nkind: Secret\ndata:\n")
	for i, v := range vals {
		fmt.Fprintf(&sb, "  k%04d: %s\n", i, b64(v))
	}
	return terminalResult(t, sb.String())
}

func synthVals(prefix string, n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("synthetic-%s-%06d", prefix, i)
	}
	return out
}

// The caps: each result keeps at most its allowance in a pool, its newest
// values; once earlier results fill a pool's reserve, a later result's
// values are refused (and counted), never let in by evicting an earlier
// result's; the byte allowance and reserve hold the same way; a value over
// seenSecretMaxValueBytes is not tracked.
func TestSeenSecrets_TheCapsKeepEarlierResults(t *testing.T) {
	var s seenSecrets
	per := seenSecretResultEntries / 2 // two entries (as given, decoded) per value
	results := seenSecretPoolEntries / seenSecretResultEntries
	for r := 0; r < results; r++ {
		s.collectToolResult(secretYAMLResult(t, synthVals(fmt.Sprintf("r%d", r), per)))
	}
	if n := len(s.secret.entries); n != seenSecretPoolEntries {
		t.Fatalf("Secret pool holds %d, want its reserve %d", n, seenSecretPoolEntries)
	}
	late := synthVals("late", 3)
	s.collectToolResult(secretYAMLResult(t, late))
	if s.has(late[0]) {
		t.Error("a later result's value got into a pool full of earlier results'")
	}
	if !s.has(synthVals("r0", 1)[0]) {
		t.Error("a later result evicted an earlier result's value")
	}
	if got := s.clear(); got != 2*len(late) {
		t.Errorf("refused = %d, want %d (each value in two forms)", got, 2*len(late))
	}

	// One large result keeps its newest values within its allowance.
	var one seenSecrets
	vals := synthVals("big", per+40)
	one.collectToolResult(secretYAMLResult(t, vals))
	if !one.has(vals[len(vals)-1]) || one.has(vals[0]) || one.size() != seenSecretResultEntries {
		t.Errorf("large result: newest kept %v, oldest kept %v, size %d; want true, false, %d",
			one.has(vals[len(vals)-1]), one.has(vals[0]), one.size(), seenSecretResultEntries)
	}

	// Bytes: values near the per-value cap fill a result's byte allowance.
	var big seenSecrets
	val := func(r, i int) string {
		return fmt.Sprintf("%02d%04d", r, i) + strings.Repeat("s", seenSecretMaxValueBytes-6)
	}
	perBytes := seenSecretResultBytes / seenSecretMaxValueBytes
	for r := 0; r < seenSecretPoolBytes/seenSecretResultBytes+1; r++ {
		var vs []string
		for i := 0; i < perBytes+2; i++ {
			vs = append(vs, val(r, i))
		}
		res, _ := json.Marshal(map[string]any{"kind": "Secret", "stringData": func() map[string]string {
			m := map[string]string{}
			for i, v := range vs {
				m[fmt.Sprintf("k%04d", i)] = v
			}
			return m
		}()})
		big.collectToolResult(res)
	}
	big.mu.Lock()
	held := big.secret.bytes
	big.mu.Unlock()
	if held > seenSecretPoolBytes || !big.has(val(0, perBytes+1)) || big.has(val(0, 0)) {
		t.Errorf("bytes = %d (reserve %d), first result's newest kept %v, its oldest kept %v",
			held, seenSecretPoolBytes, big.has(val(0, perBytes+1)), big.has(val(0, 0)))
	}
	big.add(strings.Repeat("x", seenSecretMaxValueBytes+1))
	if big.has(strings.Repeat("x", seenSecretMaxValueBytes+1)) {
		t.Error("a value over the per-value cap is tracked")
	}
	big.clear()
	if big.size() != 0 {
		t.Error("clear left values behind")
	}
}

// At finalize the bridge logs how many values the set refused, at debug,
// as a count with no value in the line.
func TestSeenSecrets_TheRefusalCountIsLoggedWithoutValues(t *testing.T) {
	var logs lockedBuffer
	b := &Bridge{cfg: Config{Logger: slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))}}
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-refused"}}
	act := newActivityState(false)
	run.act.Store(act)
	per := seenSecretResultEntries / 2
	for r := 0; r < seenSecretPoolEntries/seenSecretResultEntries; r++ {
		act.secrets.collectToolResult(secretYAMLResult(t, synthVals(fmt.Sprintf("r%d", r), per)))
	}
	late := synthVals("late", 2)
	act.secrets.collectToolResult(secretYAMLResult(t, late))
	b.clearSeenSecrets(run)
	out := logs.String()
	if !strings.Contains(out, "seen-set refused 4 values") {
		t.Errorf("log = %q, want the refusal count", out)
	}
	for _, v := range late {
		if strings.Contains(out, v) || strings.Contains(out, b64(v)) {
			t.Errorf("the log carried a refused value")
		}
	}
}

// A card's report that echoes a Secret value a worker's tool result held
// (no shape of its own, the class a shape rule cannot see) loses it in the
// answer the turn folds the card into.
func TestSeenSecrets_ACardReportEchoingAWorkersValueIsRedacted(t *testing.T) {
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-card-echo"}, state: stateRunning}
	act := newSessionActivityState("a2a-ctx-card-echo", false)
	run.act.Store(act)
	// The worker printed the Secret; the door collects it.
	act.secrets.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"token": synthCardToken})))
	card := kanbanCardRow{ID: "t_echo", Title: "Check prod-a", Status: "done", Result: "Log in with " + synthCardToken + " and retry."}
	got := scrubAnswer(run, cardAnswerText(card))
	if strings.Contains(got, synthCardToken) || !strings.Contains(got, "Log in with [redacted] and retry.") {
		t.Errorf("card answer = %q, want the echoed token redacted", got)
	}
}

// postToolResult delivers a post_tool_call carrying a result, signed with
// the pod's shared key.
func postToolResult(t *testing.T, url, key, sessionID, callID string, result json.RawMessage) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostToolCall, "tool_name": "terminal", "tool_input": map[string]any{"command": "kubectl get secret app-db -o json"},
		"session_id": sessionID, "delivery_id": nuid.Next(), "timestamp": "2026-11-09T12:00:00Z",
		"extra": map[string]any{"tool_call_id": callID, "duration_ms": 7, "status": "ok", "result": result},
	})
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(body)
	req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Errorf("delivery: %v", err)
		return
	}
	resp.Body.Close()
}

// End to end on the API executor: a tool result through the door carries
// a Secret; both turns' answers repeat its value, plain and base64. The turn
// artifact and the result carry the marker instead, no message on any
// subject and no log line carries either form, and the set is cleared once
// the task has finalized.
func TestAPI_SeenSecretValuesNeverLeaveTheBridge(t *testing.T) {
	_, url := startServer(t)
	wireConn, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer wireConn.Close()
	var wire lockedBuffer
	if _, err := wireConn.Subscribe(">", func(m *nats.Msg) {
		fmt.Fprintf(&wire, "%s %v %s\n", m.Subject, m.Header, m.Data)
	}); err != nil {
		t.Fatal(err)
	}
	var logs lockedBuffer

	const shared = "pod-wide-activity-key"
	var doorURL atomic.Value
	door := func() string { return doorURL.Load().(string) }
	release := make(chan struct{})
	result := terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain}))
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if !strings.Contains(c.idempotency, "/") {
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			postDelivery(t, door(), shared, c.sessionID, hookPreToolCall, "terminal", "c1")
			postToolResult(t, door(), shared, c.sessionID, "c1", result)
			writeCompletion(w, c.sessionID, "Turn one: the Secret holds "+synthSecretPlain+" (stored as "+b64(synthSecretPlain)+").")
			return
		}
		writeCompletion(w, c.sessionID, "Turn two: still "+synthSecretPlain+".")
	})
	var captured atomic.Pointer[taskRun]
	var sizeAtResult atomic.Int64
	b, _ := startAPIBridgeHooked(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
		cfg.Logger = slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	}, func(b *Bridge) {
		orig := b.resultPublish
		b.resultPublish = func(ctx context.Context, run *taskRun, output string, chat []byte) error {
			captured.Store(run)
			if a := run.act.Load(); a != nil {
				sizeAtResult.Store(int64(a.secrets.size()))
			}
			return orig(ctx, run, output, chat)
		}
	}, func(net.Addr) bool { return true })
	doorURL.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-seen-secret", "ctx-seen-secret", "read the db secret")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "and again")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)

	text := func(name string) string {
		var sb strings.Builder
		for _, a := range task.Artifacts {
			if a.Name != name {
				continue
			}
			for _, p := range a.Parts {
				sb.WriteString(p.Text)
			}
		}
		return sb.String()
	}
	if turn := text(lib.ArtifactTurn); !strings.Contains(turn, "Turn one: the Secret holds [redacted] (stored as [redacted]).") {
		t.Errorf("turn artifact = %q, want both forms redacted", turn)
	}
	if res := text(lib.ArtifactResult); !strings.Contains(res, "Turn two: still [redacted].") {
		t.Errorf("result = %q, want the value redacted", res)
	}
	if sizeAtResult.Load() == 0 {
		t.Error("the set was empty when the result was published: the door collected nothing")
	}
	time.Sleep(200 * time.Millisecond) // let the bus deliver the last events to the wire tap
	for _, form := range []string{synthSecretPlain, b64(synthSecretPlain)} {
		if strings.Contains(wire.String(), form) {
			t.Errorf("a message on the bus carried %q", form)
		}
		if strings.Contains(logs.String(), form) {
			t.Errorf("a log line carried %q", form)
		}
	}
	waitFor(t, 5*time.Second, "the set cleared at finalize", func() bool {
		run := captured.Load()
		return run != nil && run.act.Load() != nil && run.act.Load().secrets.size() == 0
	})
}

// A card's worker can run its tool calls while the coordinator's turn
// still goes on (a card runs as soon as it is filed), before the task waits
// on its cards, so its deliveries match no task's trace. Their Secret
// values still reach the set of the task that filed the card, whose answer
// folds the card's report in; a card no task tracks feeds nothing.
func TestActivity_CardWorkerSecretsReachTheSetBeforeTheWait(t *testing.T) {
	const secret = "pod-wide-shared-secret"
	b := &Bridge{cfg: Config{ActivitySecret: secret, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, tasks: make(map[string]*taskRun)}
	a := newActivityState(false)
	a.sessionID = "a2a-coord-in-turn"
	a.inTurn.Store(true)
	a.SyncKanbanCard("t_card42", "Check prod-a", "")
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-in-turn"}, state: stateRunning}
	run.act.Store(a)
	b.tasks["task-in-turn"] = run

	deliver := func(cwd, value string) {
		body, _ := json.Marshal(map[string]any{
			"hook_event_name": hookPostToolCall, "tool_name": "terminal", "tool_input": map[string]any{"command": "kubectl get secret app-db -o json"},
			"session_id": "20990101_120001_worker", "cwd": cwd, "delivery_id": nuid.Next(), "timestamp": "2026-11-09T12:00:00Z",
			"extra": map[string]any{"tool_call_id": "w1", "status": "ok", "result": terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": value}))},
		})
		mac := hmac.New(sha256.New, []byte(secret))
		mac.Write(body)
		req := httptest.NewRequest(http.MethodPost, "/", bytes.NewReader(body))
		req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
		b.handleActivity(httptest.NewRecorder(), req)
	}
	deliver("/var/lib/hermes/kanban/workspaces/t_card99_other/repo", synthSecretPlain2)
	if a.secrets.size() != 0 {
		t.Fatal("a worker of a card no task tracks fed the set")
	}
	deliver("/var/lib/hermes/kanban/workspaces/t_card42/repo", synthSecretPlain)
	if !a.secrets.has(synthSecretPlain) {
		t.Error("the card's worker's Secret value did not reach the task's set before the wait")
	}
	if a.hasWorkerSession("20990101_120001_worker") {
		t.Error("the secrets path changed the task's card map")
	}
}

// The pools keep their reserves: shape-caught values leave a Secret value
// tracked and redacted, and a result holding many of them keeps its newest
// within its shape allowance; a forged Secret with many values adds at most
// its Secret allowance and evicts no shape-caught value and no earlier
// Secret value.
func TestSeenSecrets_EachPoolKeepsItsShare(t *testing.T) {
	var s seenSecrets
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain})))
	flood := func(n int) string {
		var lines strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&lines, "svc%d_token: synthetic-flood-%06d\n", i, i)
		}
		return lines.String()
	}
	s.collectToolResult(terminalResult(t, flood(200)))
	if !s.has(synthSecretPlain) || !s.has(b64(synthSecretPlain)) {
		t.Fatal("shape-caught values evicted the Secret's value")
	}
	if got := scrubAnswerText("It is "+synthSecretPlain+".", &s); got != "It is [redacted]." {
		t.Errorf("answer = %q, want the Secret value redacted", got)
	}
	newest, oldest := fmt.Sprintf("synthetic-flood-%06d", 199), fmt.Sprintf("synthetic-flood-%06d", 0)
	if !s.has(newest) || s.has(oldest) {
		t.Errorf("shape pool: newest kept %v, oldest kept %v; want true, false", s.has(newest), s.has(oldest))
	}
	if n := len(s.shape.entries); n != seenShapeResultEntries {
		t.Errorf("shape pool holds %d, want one result's allowance %d", n, seenShapeResultEntries)
	}

	forged := map[string]string{}
	for i := 0; i < 1000; i++ {
		forged[fmt.Sprintf("k%04d", i)] = fmt.Sprintf("synthetic-forged-%06d", i)
	}
	shapeBefore := s.values()[len(s.secret.entries):]
	secretBefore := len(s.secret.entries)
	s.collectToolResult(terminalResult(t, secretJSON(t, "Opaque", forged)))
	if added := len(s.secret.entries) - secretBefore; added > seenSecretResultEntries {
		t.Errorf("one result added %d Secret entries, over the allowance %d", added, seenSecretResultEntries)
	}
	for _, v := range shapeBefore {
		if !s.has(v) {
			t.Fatalf("a forged Secret evicted a shape-caught value")
		}
	}
	if !s.has(synthSecretPlain) {
		t.Error("a forged Secret evicted the earlier Secret value")
	}
}

// An env entry's literal credential in a tool result (kubectl get deploy
// -o yaml) is tracked, so the answer's bare echo of it is redacted.
func TestSeenSecrets_AnEnvEntrysCredentialIsTracked(t *testing.T) {
	const value = "harborlantern-synthetic"
	var s seenSecrets
	s.collectToolResult(terminalResult(t, "spec:\n  containers:\n  - name: app\n    env:\n    - name: DB_PASSWORD\n      value: "+value+"\n"))
	if got := scrubAnswerText("The app connects with "+value+".", &s); got != "The app connects with [redacted]." {
		t.Errorf("answer = %q, want the env value redacted", got)
	}
}

// A failure's status tails carry what an answer would: a failed CLI run's
// stdout tail (the answer stream) and an API error's body and error tails
// are scrubbed as an answer is before they go on TASKS.
func TestFailureTailsAreScrubbed(t *testing.T) {
	const word = "quietpasture"
	t.Run("cli stdout tail", func(t *testing.T) {
		_, url := startServer(t)
		startBridge(t, url, script(t, `echo "the password is `+word+`"
echo "export API_TOKEN=Synth3tic-Pass-0001"
echo "session_id: 20260925_181506_ab12cd" >&2
exit 1`))
		c := gatewayClient(t, url)
		submit(t, c, "task-fail-tail", "doomed")
		task := waitTerminal(t, c, "task-fail-tail")
		reason := terminalReason(t, task)
		if strings.Contains(reason, word) || strings.Contains(reason, "Synth3tic-Pass-0001") || !strings.Contains(reason, "session: 20260925_181506_ab12cd") {
			t.Errorf("terminal reason = %q, want the tail scrubbed and the session kept", reason)
		}
	})
	for _, tc := range []struct {
		name   string
		handle func(w http.ResponseWriter, r *http.Request, c apiCall)
	}{
		{"api 5xx body tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "partial answer: the password is "+word)
		}},
		{"api unreadable body tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			_, _ = io.WriteString(w, `{"choices":[],"note":"password: `+word+`"}`)
		}},
		{"api turn error tail", func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
			w.Header().Set("X-Hermes-Completed", "false")
			_, _ = io.WriteString(w, `{"choices":[{"message":{"content":"x"},"finish_reason":"error"}],`+
				`"hermes":{"completed":false,"partial":false,"failed":true,"error":"tool said: the password is `+word+`","error_code":"agent_error"}}`)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			startAPIBridge(t, url, newAPIStub(t, tc.handle), nil)
			c := gatewayClient(t, url)
			taskID := "task-tail-" + strings.ReplaceAll(tc.name, " ", "-")
			submitIn(t, c, taskID, "ctx-tail", "doomed")
			task := waitTerminal(t, c, taskID)
			if reason := terminalReason(t, task); task.State != lib.StateFailed || strings.Contains(reason, word) || !strings.Contains(reason, "[redacted]") {
				t.Errorf("state = %s reason = %q, want failed with the tail scrubbed", task.State, reason)
			}
		})
	}
}

// A value the shape pool holds that a later result shows in a Secret ends
// up in exactly one pool: when earlier results fill the Secret pool, it
// stays in the shape pool and is still redacted. A value no own entry can
// make room for is refused without first evicting the entries its result
// already added.
func TestSeenSecrets_AMoveToAFullSecretPoolKeepsTheValue(t *testing.T) {
	var s seenSecrets
	for i := 0; i < seenSecretPoolEntries/2; i++ {
		s.add(fmt.Sprintf("synthetic-fill-%06d", i)) // each its own result, two forms each
	}
	if n := len(s.secret.entries); n != seenSecretPoolEntries {
		t.Fatalf("Secret pool holds %d, want its reserve %d", n, seenSecretPoolEntries)
	}
	s.addShape(synthCardToken)
	s.add(synthCardToken)
	if !s.has(synthCardToken) || !s.has(b64(synthCardToken)) {
		t.Fatal("a value moving to a full Secret pool was dropped from both pools")
	}
	if got := scrubAnswerText("It is "+synthCardToken+".", &s); got != "It is [redacted]." {
		t.Errorf("answer = %q, want the value still redacted", got)
	}
	s.mu.Lock()
	inShape := 0
	for _, e := range s.shape.entries {
		if e.v == synthCardToken {
			inShape++
		}
	}
	for _, e := range s.secret.entries {
		if e.v == synthCardToken {
			t.Error("the value is in the Secret pool too")
		}
	}
	s.mu.Unlock()
	if inShape != 1 {
		t.Errorf("shape pool holds the value %d times, want 1", inShape)
	}

	// Bytes: earlier results leave less room than a large value needs even
	// with the current result's own small entry gone; that entry stays.
	var b seenSecrets
	for i := 0; i < seenSecretPoolBytes/seenSecretMaxValueBytes-1; i++ {
		b.mu.Lock()
		b.addLocked(fmt.Sprintf("%06d", i)+strings.Repeat("f", seenSecretMaxValueBytes-6), true, uint64(i+100))
		b.mu.Unlock()
	}
	b.mu.Lock()
	b.addLocked(strings.Repeat("g", 100), true, 5000)
	b.mu.Unlock()
	gen := b.newGen() // 1: no earlier result's generation
	const small = "synthetic-small-0005"
	b.mu.Lock()
	b.addLocked(small, true, gen)
	b.addLocked(strings.Repeat("h", seenSecretMaxValueBytes), true, gen)
	b.mu.Unlock()
	if !b.has(small) {
		t.Error("a refused value evicted its own result's earlier entry for nothing")
	}
	if b.has(strings.Repeat("h", seenSecretMaxValueBytes)) {
		t.Error("a value over the room left was tracked")
	}
}
