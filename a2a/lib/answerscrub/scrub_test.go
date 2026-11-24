package answerscrub

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
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
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{
		"password": synthSecretPlain, "api-token": synthSecretPlain2 + "\n", "pin": synthShortSecret,
	})))
	answer := fmt.Sprintf("The Secret holds %s and %s (stored as %s); the pin is short: %s.",
		synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain), synthShortSecret)
	got := Scrub(answer, &s)
	for _, leaked := range []string{synthSecretPlain, synthSecretPlain2, b64(synthSecretPlain)} {
		if strings.Contains(got, leaked) {
			t.Errorf("answer leaked %q: %q", leaked, got)
		}
	}
	if !strings.Contains(got, synthShortSecret) {
		t.Errorf("a value under %d bytes was redacted: %q", seenSecretMinBytes, got)
	}
	if s.Has(synthShortSecret) || s.Has(b64(synthShortSecret)) {
		t.Errorf("a value under %d bytes is tracked", seenSecretMinBytes)
	}
}

// The same from YAML output, from stringData, and from JSON cut short.
func TestSeenSecrets_YAMLStringDataAndTruncatedJSON(t *testing.T) {
	var s Seen
	yaml := "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n  password: " + b64(synthSecretYAMLVal) + "\nstringData:\n  token: \"" + synthSecretPlain2 + "\"\n"
	s.CollectToolResult(terminalResult(t, yaml))
	cut := `{"apiVersion": "v1", "kind": "Secret", "data": {"password": "` + b64(synthSecretPlain) + `"}, "metadata": {"name": "app", "annot`
	s.CollectToolResult(terminalResult(t, cut))
	for _, v := range []string{synthSecretYAMLVal, synthSecretPlain2, synthSecretPlain, b64(synthSecretPlain), b64(synthSecretPlain2)} {
		if !s.Has(v) {
			t.Errorf("%q not tracked", v)
		}
	}
}

// A service account token Secret's namespace is a name, not a credential:
// it is not tracked, so the answer keeps it.
func TestSeenSecrets_AServiceAccountTokensNamespaceIsNotTracked(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "kubernetes.io/service-account-token", map[string]string{
		"namespace": synthSANamespace, "token": synthSecretPlain,
	})))
	got := Scrub("The token in "+synthSANamespace+" is "+synthSecretPlain+".", &s)
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
	var s Seen
	for _, r := range results {
		s.CollectToolResult(terminalResult(t, r))
	}
	if n := s.Size(); n != 0 {
		t.Fatalf("set holds %d values from results with no credential in them: %q", n, s.Values())
	}
	answer := "Pull secret regcred-missing is absent; default-token-x7k2p is mounted. password (14 bytes). " +
		"Postgres uses postgres-creds and TLS uses shop-tls-cert, image sha256:4f1c9a0b2e3d4c5f6a7b8c9d0e1f2a3b4c5d6e7f. " +
		"alice-operator was denied; oidc-provider-x is the issuer. Location (`locations`)."
	if got := Scrub(answer, &s); got != answer {
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
	var s Seen
	s.CollectToolResult(terminalResult(t, "export DB_PASSWORD="+envWord+"\nok"))
	s.CollectToolResult(terminalResult(t, `{"client": {"token": "`+cfgTok+`"}}`))
	s.CollectToolResult(json.RawMessage(mustJSONString(t, "DATABASE_URL=postgres://app:"+urlPass+"@db.internal:5432/orders")))
	s.CollectToolResult(terminalResult(t, "app --db-password "+flagVal+" --port 5432"))
	answer := "Use " + envWord + ", then " + cfgTok + "; the URL carries " + urlPass + " and the flag " + flagVal + "."
	got := Scrub(answer, &s)
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
		var s Seen
		s.CollectToolResult(result)
		if !s.Has(plainWord) {
			t.Errorf("%s: the password's plain-word value is not tracked", name)
		}
		for _, v := range []string{user, host, port, dbName} {
			if s.Has(v) || s.Has(b64(v)) {
				t.Errorf("%s: a connection setting %q is tracked", name, v)
			}
		}
	}
}

// Prefixed connection settings (DB_HOST, POSTGRES_USER, db.name, hostname)
// are settings too, matched on the key's last component, and the answer
// that names them stays readable; a prefixed password stays tracked
// (#5311 review: Fuxiao-Gao, bradhoekstra).
func TestSeenSecrets_PrefixedConnectionSettingsAreNotTracked(t *testing.T) {
	const (
		host = "postgres.prod.svc.cluster.local"
		user = "orders_app"
		db   = "orders_db"
		pass = "Synth3tic-DbPass-0042"
	)
	data := map[string]string{"DB_HOST": host, "POSTGRES_USER": user, "db.name": db, "hostname": "db-replica-1", "DB_PASSWORD": pass}
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", data)))
	for _, v := range []string{host, user, db, "db-replica-1"} {
		if s.Has(v) {
			t.Errorf("the setting %q is tracked", v)
		}
	}
	if !s.Has(pass) {
		t.Error("the prefixed password is not tracked")
	}
	answer := "DB_HOST is " + host + "; user " + user + " on " + db + "."
	if got := Scrub(answer, &s); got != answer {
		t.Errorf("the answer naming the settings was changed: %q", got)
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
	var s Seen
	s.CollectToolResult(terminalResult(t, list))
	s.CollectToolResult(terminalResult(t, "apiVersion: v1\ndata:\n  password: "+b64(commentd)+"\nkind: Secret # the db creds\n"))
	for _, v := range []string{first, second, b64(first), commentd} {
		if !s.Has(v) {
			t.Errorf("%q not tracked", v)
		}
	}
	if s.Has(cmValue) || s.Has(b64(cmValue)) {
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
	var s Seen
	s.CollectToolResult(terminalResult(t, cut))
	if !s.Has(whole) || !s.Has(inBlock) {
		t.Errorf("a pair read whole is not tracked: %v %v", s.Has(whole), s.Has(inBlock))
	}
	if s.Size() != 4 {
		t.Errorf("size = %d, want 4 (two values, each as given and decoded): the cut pair was tracked", s.Size())
	}
	mixed := `{"items":[{"data":{"a":"` + b64(whole) + `"},"kind":"ConfigMap"},{"data":{"b":"` + b64(inBlock) + `"},"kind":"Secret"},{"data":{"c":"` + b64(cutOff) + `"`
	var m Seen
	m.CollectToolResult(terminalResult(t, mixed))
	if m.Has(whole) || m.Has(cutOff) || !m.Has(inBlock) {
		t.Errorf("mixed list: ConfigMap read %v, unknown cut item read %v, Secret item read %v; want false, false, true",
			m.Has(whole), m.Has(cutOff), m.Has(inBlock))
	}
}

// A Secret cut short after its kind, whose metadata names another kind (an
// owner reference), is still read for its own data.
func TestSeenSecrets_ACutSecretWithAnOwnerReferenceIsRead(t *testing.T) {
	const value = "synthetic-owned-0012"
	cut := `{"apiVersion":"v1","data":{"password":"` + b64(value) + `"},"kind":"Secret","metadata":{"name":"app-db",` +
		`"ownerReferences":[{"apiVersion":"apps/v1","kind":"Deployment","name":"web","uid":"1"}],"resourceVersion":"12`
	var s Seen
	s.CollectToolResult(terminalResult(t, cut))
	if !s.Has(value) {
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
	var s Seen
	s.CollectToolResult(terminalResult(t, string(list)))
	if !s.Has(value) {
		t.Error("a Secret value past the scan cap was not tracked")
	}
}

// Once the task has finalized the set is closed: a tool result that was on
// its way when finalize ran adds nothing.
func TestSeenSecrets_NothingIsAddedAfterClear(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain})))
	if !s.Has(synthSecretPlain) {
		t.Fatal("not tracked before clear")
	}
	s.Clear()
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain2})))
	s.Add(synthSecretPlain2 + "-x")
	if s.Size() != 0 {
		t.Errorf("size after clear = %d, want 0", s.Size())
	}
}

// Collect, redact and clear run on different goroutines (an executor's
// result handler, its finalizer); under -race they must not trip.
func TestSeenSecrets_CollectRedactAndClearConcurrently(t *testing.T) {
	var s Seen
	result := terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain}))
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(3)
		go func() { defer wg.Done(); s.CollectToolResult(result) }()
		go func() { defer wg.Done(); _ = Scrub("x "+synthSecretPlain+" y", &s) }()
		go func() { defer wg.Done(); _ = s.Size() }()
	}
	wg.Wait()
	if got := Scrub("x "+synthSecretPlain+" y", &s); got != "x [redacted] y" {
		t.Errorf("redact = %q", got)
	}
	s.Clear()
	if s.Size() != 0 {
		t.Error("clear left values behind")
	}
}

// The longest value wins where two overlap, so no fragment is left over.
func TestSeenSecrets_TheLongestValueWins(t *testing.T) {
	var s Seen
	s.Add(synthSecretPlain)
	s.Add(synthSecretPlain + "-suffix")
	if got := Scrub("x "+synthSecretPlain+"-suffix y", &s); got != "x [redacted] y" {
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
	var s Seen
	per := seenSecretResultEntries / 2 // two entries (as given, decoded) per value
	results := seenSecretPoolEntries / seenSecretResultEntries
	for r := 0; r < results; r++ {
		s.CollectToolResult(secretYAMLResult(t, synthVals(fmt.Sprintf("r%d", r), per)))
	}
	if n := len(s.secret.entries); n != seenSecretPoolEntries {
		t.Fatalf("Secret pool holds %d, want its reserve %d", n, seenSecretPoolEntries)
	}
	late := synthVals("late", 3)
	s.CollectToolResult(secretYAMLResult(t, late))
	if s.Has(late[0]) {
		t.Error("a later result's value got into a pool full of earlier results'")
	}
	if !s.Has(synthVals("r0", 1)[0]) {
		t.Error("a later result evicted an earlier result's value")
	}
	if got := s.Clear(); got != 2*len(late) {
		t.Errorf("refused = %d, want %d (each value in two forms)", got, 2*len(late))
	}

	// One large result keeps its newest values within its allowance.
	var one Seen
	vals := synthVals("big", per+40)
	one.CollectToolResult(secretYAMLResult(t, vals))
	if !one.Has(vals[len(vals)-1]) || one.Has(vals[0]) || one.Size() != seenSecretResultEntries {
		t.Errorf("large result: newest kept %v, oldest kept %v, size %d; want true, false, %d",
			one.Has(vals[len(vals)-1]), one.Has(vals[0]), one.Size(), seenSecretResultEntries)
	}

	// Bytes: values near the per-value cap fill a result's byte allowance.
	var big Seen
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
		big.CollectToolResult(res)
	}
	big.mu.Lock()
	held := big.secret.bytes
	big.mu.Unlock()
	if held > seenSecretPoolBytes || !big.Has(val(0, perBytes+1)) || big.Has(val(0, 0)) {
		t.Errorf("bytes = %d (reserve %d), first result's newest kept %v, its oldest kept %v",
			held, seenSecretPoolBytes, big.Has(val(0, perBytes+1)), big.Has(val(0, 0)))
	}
	big.Add(strings.Repeat("x", seenSecretMaxValueBytes+1))
	if big.Has(strings.Repeat("x", seenSecretMaxValueBytes+1)) {
		t.Error("a value over the per-value cap is tracked")
	}
	big.Clear()
	if big.Size() != 0 {
		t.Error("clear left values behind")
	}
}

// The pools keep their reserves: shape-caught values leave a Secret value
// tracked and redacted, and a result holding many of them keeps its newest
// within its shape allowance; a forged Secret with many values adds at most
// its Secret allowance and evicts no shape-caught value and no earlier
// Secret value.
func TestSeenSecrets_EachPoolKeepsItsShare(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain})))
	flood := func(n int) string {
		var lines strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&lines, "svc%d_token: synthetic-flood-%06d\n", i, i)
		}
		return lines.String()
	}
	// The Secret's own result may add to the shape pool too (the shape
	// rules read JSON text): the flood is measured from after it.
	shapeFromSecret := len(s.shape.entries)
	s.CollectToolResult(terminalResult(t, flood(200)))
	if !s.Has(synthSecretPlain) || !s.Has(b64(synthSecretPlain)) {
		t.Fatal("shape-caught values evicted the Secret's value")
	}
	if got := Scrub("It is "+synthSecretPlain+".", &s); got != "It is [redacted]." {
		t.Errorf("answer = %q, want the Secret value redacted", got)
	}
	newest, oldest := fmt.Sprintf("synthetic-flood-%06d", 199), fmt.Sprintf("synthetic-flood-%06d", 0)
	if !s.Has(newest) || s.Has(oldest) {
		t.Errorf("shape pool: newest kept %v, oldest kept %v; want true, false", s.Has(newest), s.Has(oldest))
	}
	if n := len(s.shape.entries) - shapeFromSecret; n != seenShapeResultEntries {
		t.Errorf("shape pool holds %d, want one result's allowance %d", n, seenShapeResultEntries)
	}

	forged := map[string]string{}
	for i := 0; i < 1000; i++ {
		forged[fmt.Sprintf("k%04d", i)] = fmt.Sprintf("synthetic-forged-%06d", i)
	}
	shapeBefore := s.Values()[len(s.secret.entries):]
	secretBefore := len(s.secret.entries)
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", forged)))
	if added := len(s.secret.entries) - secretBefore; added > seenSecretResultEntries {
		t.Errorf("one result added %d Secret entries, over the allowance %d", added, seenSecretResultEntries)
	}
	for _, v := range shapeBefore {
		if !s.Has(v) {
			t.Fatalf("a forged Secret evicted a shape-caught value")
		}
	}
	if !s.Has(synthSecretPlain) {
		t.Error("a forged Secret evicted the earlier Secret value")
	}
}

// An env entry's literal credential in a tool result (kubectl get deploy
// -o yaml) is tracked, so the answer's bare echo of it is redacted.
func TestSeenSecrets_AnEnvEntrysCredentialIsTracked(t *testing.T) {
	const value = "harborlantern-synthetic"
	var s Seen
	s.CollectToolResult(terminalResult(t, "spec:\n  containers:\n  - name: app\n    env:\n    - name: DB_PASSWORD\n      value: "+value+"\n"))
	if got := Scrub("The app connects with "+value+".", &s); got != "The app connects with [redacted]." {
		t.Errorf("answer = %q, want the env value redacted", got)
	}
}

// A value the shape pool holds that a later result shows in a Secret ends
// up in exactly one pool: when earlier results fill the Secret pool, it
// stays in the shape pool and is still redacted. A value no own entry can
// make room for is refused without first evicting the entries its result
// already added.
func TestSeenSecrets_AMoveToAFullSecretPoolKeepsTheValue(t *testing.T) {
	var s Seen
	for i := 0; i < seenSecretPoolEntries/2; i++ {
		s.Add(fmt.Sprintf("synthetic-fill-%06d", i)) // each its own result, two forms each
	}
	if n := len(s.secret.entries); n != seenSecretPoolEntries {
		t.Fatalf("Secret pool holds %d, want its reserve %d", n, seenSecretPoolEntries)
	}
	s.AddShape(synthCardToken)
	s.Add(synthCardToken)
	if !s.Has(synthCardToken) || !s.Has(b64(synthCardToken)) {
		t.Fatal("a value moving to a full Secret pool was dropped from both pools")
	}
	if got := Scrub("It is "+synthCardToken+".", &s); got != "It is [redacted]." {
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
	var b Seen
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
	if !b.Has(small) {
		t.Error("a refused value evicted its own result's earlier entry for nothing")
	}
	if b.Has(strings.Repeat("h", seenSecretMaxValueBytes)) {
		t.Error("a value over the room left was tracked")
	}
}

// A SecretList as the API server sends it (kubectl get --raw, a curl
// through the API proxy) has kind on the list only; its items are Secrets,
// read whole and cut short, under any data key.
func TestSeenSecrets_RawSecretListItemsAreSecrets(t *testing.T) {
	list := fmt.Sprintf(`{"kind":"SecretList","apiVersion":"v1","metadata":{"resourceVersion":"1"},"items":[`+
		`{"metadata":{"name":"tls-a","namespace":"prod"},"type":"kubernetes.io/tls","data":{"tls.key":%q}},`+
		`{"metadata":{"name":"app-db","namespace":"prod"},"type":"Opaque","data":{"password":%q}}]}`,
		b64(synthSecretPlain), b64(synthSecretPlain2))
	var whole Seen
	whole.CollectToolResult(terminalResult(t, list))
	for _, v := range []string{synthSecretPlain, synthSecretPlain2} {
		if !whole.Has(v) {
			t.Errorf("a raw SecretList item's decoded value %q is not tracked", v)
		}
	}
	cut := list[:strings.Index(list, "app-db")]
	var c Seen
	c.CollectToolResult(terminalResult(t, cut))
	if !c.Has(synthSecretPlain) {
		t.Error("a raw SecretList cut short: its whole first item's decoded value is not tracked")
	}
}

// A raw SecretList whose keys are sorted (jq -S), so its kind comes after
// its items, still has its items read as Secrets when the text it starts
// does not parse whole (output after it); a token Secret cut before its type has its
// namespace left out, since it may be a service-account token's.
func TestSeenSecrets_JSONKeyOrderDoesNotDecideWhatIsRead(t *testing.T) {
	sorted := fmt.Sprintf(`{"apiVersion":"v1","items":[`+
		`{"data":{"password":%q},"metadata":{"name":"app-db","namespace":"prod"},"type":"Opaque"}],`+
		`"kind":"SecretList","metadata":{"resourceVersion":"1"}}`, b64(synthSecretPlain))
	var s Seen
	s.CollectToolResult(terminalResult(t, sorted+"\nsecret/app-db listed"))
	if !s.Has(synthSecretPlain) {
		t.Error("a sorted SecretList's kind-less item was not read as a Secret")
	}

	list := fmt.Sprintf(`{"apiVersion":"v1","items":[`+
		`{"apiVersion":"v1","data":{"password":%q},"kind":"Secret","metadata":{"name":"app-db"},"type":"Opaque"},`+
		`{"apiVersion":"v1","data":{"ca.crt":%q,"namespace":%q,"token":"c3ludGhldGlj`,
		b64(synthSecretPlain), b64(synthSecretPlain2), b64(synthSANamespace))
	var c Seen
	c.CollectToolResult(terminalResult(t, list))
	if !c.Has(synthSecretPlain2) {
		t.Error("the cut item's whole pairs were not read")
	}
	if c.Has(synthSANamespace) {
		t.Error("a Secret cut before its type had its namespace tracked")
	}
}

// A comment line inside a YAML list item, at any column, does not end the
// item: its data block after the comment is still read.
func TestSeenSecrets_ACommentLineDoesNotEndAYAMLListItem(t *testing.T) {
	doc := "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: Secret\n# the app's database login\n  metadata:\n    name: app-db\n  data:\n    password: " + b64(synthSecretPlain) + "\n"
	var s Seen
	s.CollectToolResult(terminalResult(t, doc))
	if !s.Has(synthSecretPlain) {
		t.Error("a comment line ended the list item before its data block")
	}
}

// A data value with a trailing newline (a file's, echo's) is tracked as
// its trimmed value's base64 too: an answer re-encoding the value has it.
func TestSeenSecrets_ATrimmedDataValuesOwnBase64IsTracked(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, secretJSON(t, "Opaque", map[string]string{"password": synthSecretPlain + "\n"})))
	for _, v := range []string{b64(synthSecretPlain + "\n"), synthSecretPlain, b64(synthSecretPlain)} {
		if !s.Has(v) {
			t.Errorf("%q is not tracked", v)
		}
	}
}

// A JSON tool result's lines are not read as one run of text: a value at
// a line's end does not run on into the next line, and a key does not take
// the escape's n, so nothing tracked holds an escaped line break.
func TestSeenSecrets_RawJSONIsNotReadAsOneLine(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, "status: ok\nsvc1_token: "+synthSecretPlain+"\nregion: us-east4\n"))
	if !s.Has(synthSecretPlain) {
		t.Fatal("the line's own value is not tracked")
	}
	for _, v := range s.Values() {
		if strings.Contains(v, `\n`) || strings.Contains(v, "us-east4") {
			t.Errorf("tracked a run-on value %q", v)
		}
	}
}

// The pairing scan reads JSON in its own key order: an env-like entry
// whose other keys sort between name and value is still paired, and a long
// one-line result is scanned in linear time.
func TestSeenSecrets_JSONPairingKeepsKeyOrderAndScalesLinearly(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"env":[{"name":"DB_PASSWORD","value":"`+synthSecretPlain+`","type":"literal"}],"note":"a\nb"}`))
	if !s.Has(synthSecretPlain) {
		t.Error("an env entry with a key sorting between name and value was not paired")
	}
	var env []map[string]any
	for i := 0; i < 16000; i++ {
		env = append(env, map[string]any{"name": fmt.Sprintf("FLAG_%d", i), "value": i%2 == 0})
	}
	b, _ := json.Marshal(map[string]any{"env": env})
	start := time.Now()
	var big Seen
	big.CollectToolResult(terminalResult(t, string(b)+"\nnote: done"))
	// Linear is a second or two even under -race; the quadratic scan this
	// guards against took 7s on a quarter of this input without -race.
	if d := time.Since(start); d > 30*time.Second {
		t.Errorf("a %d-byte one-line result took %v", len(b), d)
	}
}

// A quoted stringData value is read with its YAML escapes: a double-quoted
// one's backslash escapes, a single-quoted one's doubled quotes.
func TestSeenSecrets_QuotedYAMLStringDataIsUnescaped(t *testing.T) {
	cases := []struct{ yaml, want string }{
		{`"synth\"pass\"0001"`, `synth"pass"0001`},
		{`'synth''pass''0001'`, `synth'pass'0001`},
		{`"synth\\pass\t0001"`, "synth\\pass\t0001"},
		{`"synth-pass-0001" # a comment`, "synth-pass-0001"},
	}
	for _, tc := range cases {
		var s Seen
		s.CollectToolResult(terminalResult(t, "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-db\nstringData:\n  password: "+tc.yaml+"\n"))
		if !s.Has(tc.want) {
			t.Errorf("stringData %s: %q is not tracked; set %q", tc.yaml, tc.want, s.Values())
		}
	}
}

// A quoted stringData value with escapes in it is tracked as written too,
// so an answer that quotes the manifest's raw form has it redacted; a quote
// that never closes is not tracked with it.
func TestSeenSecrets_QuotedYAMLStringDataRawFormIsTracked(t *testing.T) {
	cases := []struct{ yaml, raw string }{
		{`"synth\"pass\"0001"`, `synth\"pass\"0001`},
		{`'synth''pass''0001'`, `synth''pass''0001`},
		{`"synth\qpass-0001"`, `synth\qpass-0001`}, // not a YAML escape: kept as written
	}
	for _, tc := range cases {
		var s Seen
		s.CollectToolResult(terminalResult(t, "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-db\nstringData:\n  password: "+tc.yaml+"\n"))
		answer := "The manifest sets password to " + tc.raw + " here."
		if got := Scrub(answer, &s); strings.Contains(got, tc.raw) {
			t.Errorf("stringData %s: the raw form survived: %q", tc.yaml, got)
		}
	}
	var s Seen
	s.CollectToolResult(terminalResult(t, "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app-db\nstringData:\n  password: \"synthetic-open-0006\n"))
	if !s.Has("synthetic-open-0006") {
		t.Errorf("an unclosed quote: the value is not tracked without its quote; set %q", s.Values())
	}
}

// A line-leading credential key followed on its line by other key: value
// pairs hands its own value to the set, and only that value is redacted.
func TestSeenSecrets_ALineLeadingKeyWithLaterPairsIsTracked(t *testing.T) {
	for _, line := range []string{
		"password: " + synthCardToken + ", host: db.internal",
		"password: " + synthCardToken + ", host: db.internal, port: 5432",
		"password: " + synthCardToken + ", token: " + synthSecretPlain2,
	} {
		var s Seen
		s.CollectToolResult(terminalResult(t, line+"\n"))
		if !s.Has(synthCardToken) {
			t.Errorf("%q: the value is not tracked; set %q", line, s.Values())
		}
	}
	// The value is redacted to the boundary the later pair sets, its pair
	// separator with it; a value holding a byte the value scanner stops at,
	// and a passphrase of several words with a colon in it, are redacted
	// whole.
	for in, want := range map[string]string{
		"password: " + synthCardToken + ", host: db.internal": "password: [redacted] host: db.internal",
		"password: correct horse battery staple":              "password: [redacted]",
		"password: synth,pass(0001) host: db.internal":        "password: [redacted]",
		"password: synth)pass>0001, host: db.internal":        "password: [redacted]",
		"password: correct horse word: battery staple":        "password: [redacted]",
		"password: open sesame: synthetic phrase 0007":        "password: [redacted]",
	} {
		if got := ScrubInline(in); got != want {
			t.Errorf("ScrubInline(%q) = %q, want %q", in, got, want)
		}
	}
}

// A YAML \x, \u or \U escape names a code point only up to utf8.MaxRune and
// outside the surrogates: one past either is not read, so the quoted value
// is kept as written.
func TestYAMLUnescape_EscapeOutOfRuneRangeIsNotRead(t *testing.T) {
	if got, ok := yamlUnescape(`synth\U0010FFFFx`); !ok || got != "synth\U0010FFFFx" {
		t.Errorf("yamlUnescape(max rune) = %q, %v, want it read", got, ok)
	}
	if got, ok := yamlUnescape(`synth\x41x`); !ok || got != "synthAx" {
		t.Errorf("yamlUnescape(\\x41) = %q, %v, want it read", got, ok)
	}
	for _, raw := range []string{`synth\U00110000x`, `synth\UFFFFFFFFx`, `synth\U80000041x`, `synth\uD800x`} {
		if got, ok := yamlUnescape(raw); ok {
			t.Errorf("yamlUnescape(%q) = %q, true, want it refused", raw, got)
		}
	}
}

// A state word or an upper-case env var reference, the whole of a JSON
// string under a key that names a credential (password: disabled), is not
// tracked, so the answer's ordinary use of the word stays.
func TestSeenSecrets_AStateWordUnderACredentialJSONKeyIsNotTracked(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"password": "disabled", "token": "required", "api_key": "configured", "clientSecret": "Plaintext", "db_password": "${DB_PASSWORD}", "admin_password": "$ADMIN_PASSWORD", "access_token": "`+synthSecretPlain+`"}`))
	for _, w := range []string{"disabled", "required", "configured", "Plaintext", "${DB_PASSWORD}", "$ADMIN_PASSWORD"} {
		if s.Has(w) {
			t.Errorf("the set tracks %q, a state word or placeholder", w)
		}
	}
	if !s.Has(synthSecretPlain) {
		t.Error("the credential under access_token was not tracked")
	}
	const answer = "Basic auth is disabled; a token is required and the key is configured."
	if got := Scrub(answer, &s); got != answer {
		t.Errorf("answer = %q, want it unchanged", got)
	}
}

// A credential under a JSON key is tracked exactly as decoded: trailing
// punctuation in it is part of it, not a sentence's.
func TestSeenSecrets_AnExactJSONValueKeepsItsTrailingPunctuation(t *testing.T) {
	const bang, short = "Synth2026!", "p@ss!12!"
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"password": "`+bang+`", "db_password": "`+short+`"}`))
	for _, v := range []string{bang, b64(bang), short, b64(short)} {
		if !s.Has(v) {
			t.Errorf("the set does not track %q", v)
		}
	}
	if got := Scrub("Encoded it is "+b64(bang)+" and plain "+bang+" works", &s); got != "Encoded it is [redacted] and plain [redacted] works" {
		t.Errorf("answer = %q, want both forms redacted whole", got)
	}
}

// A JSON credential value that only looks like a placeholder (a command
// substitution, an angle-bracketed word, a mixed-case $name), or a state
// word with more around it, is tracked as it is: its edges are exact.
func TestSeenSecrets_APlaceholderShapedJSONCredentialIsTracked(t *testing.T) {
	vals := []string{"$(synthpass-0001)", "<synthpass-0002>", "$Synth_Pass_0003", "disabled-synth-0004"}
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"password": "`+vals[0]+`", "token": "`+vals[1]+`", "api_key": "`+vals[2]+`", "db_password": "`+vals[3]+`"}`))
	for _, v := range vals {
		if !s.Has(v) {
			t.Errorf("the set does not track %q", v)
		}
	}
}

// A JSON tool result's credential structures that span several fields or
// items (a Kubernetes env entry's name and value, a container's flag and
// value args, a header list's name and value) are tracked from kubectl's
// -o json as they are from -o yaml, so the answer's bare echo is redacted.
func TestSeenSecrets_JSONResultsShapePairsAreTracked(t *testing.T) {
	const envValue, argValue, headerValue = "harborlantern-synthetic", "harborstone-synthetic", "quietpasture-synthetic-hdr"
	deploy, err := json.MarshalIndent(map[string]any{
		"kind": "Deployment",
		"spec": map[string]any{"template": map[string]any{"spec": map[string]any{"containers": []any{map[string]any{
			"name":    "app",
			"env":     []any{map[string]any{"name": "DB_PASSWORD", "value": envValue}},
			"args":    []any{"--verbose", "--db-password", argValue},
			"command": []any{"curl", "-H", "Authorization", "x"},
		}}}}},
		"metadata": map[string]any{"annotations": map[string]any{"headers": "[\n  \"Authorization\",\n  \"" + headerValue + "\"\n]"}},
	}, "", "    ")
	if err != nil {
		t.Fatal(err)
	}
	var s Seen
	s.CollectToolResult(terminalResult(t, string(deploy)))
	for _, v := range []string{envValue, argValue, headerValue} {
		if !s.Has(v) {
			t.Errorf("the set does not track %q from the JSON result", v)
		}
	}
	if got := Scrub("The app connects with "+envValue+".", &s); got != "The app connects with [redacted]." {
		t.Errorf("answer = %q, want the env value redacted", got)
	}
	// The list rules read a pair split over two lines, as -o json prints it.
	for in, want := range map[string]string{
		"\"args\": [\n    \"--db-password\",\n    \"" + synthSecretPlain + "\"\n]": "\"args\": [\n    \"--db-password\",\n    \"[redacted]\"\n]",
		"[\n    \"Authorization\",\n    \"" + synthSecretPlain + "\"\n]":           "[\n    \"Authorization\",\n    \"[redacted]\"\n]",
	} {
		if got := ScrubInline(in); got != want {
			t.Errorf("ScrubInline(%q)\n got %q\nwant %q", in, got, want)
		}
	}
}

// A JSON credential value that trimValueEdges would change (quotes around
// it, a trailing full stop) is tracked in both forms, so an answer that
// echoes either is redacted whole; a trimmed form that is a state word is
// not tracked.
func TestSeenSecrets_AnExactJSONValueAlsoTracksItsTrimmedForm(t *testing.T) {
	const quoted, dotted = "synthpass-0005", "synthpass-0006."
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"password": "\"`+quoted+`\"", "token": "`+dotted+`", "api_key": "disabled."}`))
	for _, v := range []string{`"` + quoted + `"`, quoted, dotted, "synthpass-0006"} {
		if !s.Has(v) {
			t.Errorf("the set does not track %q", v)
		}
	}
	if s.Has("disabled") {
		t.Error("the set tracks a trimmed state word")
	}
	cases := map[string]string{
		"It is " + quoted + " now":     "It is [redacted] now",
		"It is \"" + quoted + "\" now": "It is [redacted] now",
		"Rotate synthpass-0006 today":  "Rotate [redacted] today",
		"Rotate synthpass-0006. Now.":  "Rotate [redacted] Now.",
	}
	for in, want := range cases {
		if got := Scrub(in, &s); got != want {
			t.Errorf("answer %q = %q, want %q", in, got, want)
		}
	}
}

// A table cell under a strong key whose value follows an auth-scheme word
// and opens with a partial code span is tracked without the scheme word
// (the seen set keeps no value with a blank in it, so a value taken with
// "Bearer " in front would never be tracked), and with its markdown taken
// out as well as written.
func TestSeenSecrets_ATableValueAfterASchemeWordIsTrackedWithoutIt(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, "| Key | Value |\n|---|---|\n| api_token | Bearer `sy`nthtoken0001abcd |\n"))
	if !s.Has("synthtoken0001abcd") || !s.Has("sy`nthtoken0001abcd") {
		t.Fatalf("the value was not tracked; tracked: %q", s.Values())
	}
}

// A credential-named env value after an auth-scheme word, with an escape in
// it, is tracked as its escapes read as well as written, without the scheme
// word either way.
func TestSeenSecrets_AnEnvValueAfterASchemeWordIsTrackedAsItsEscapesRead(t *testing.T) {
	var s Seen
	s.CollectToolResult(terminalResult(t, `{"name": "API_TOKEN", "value": "Bearer synth\"token\"0001abcd"}`))
	if !s.Has(`synth"token"0001abcd`) || !s.Has(`synth\"token\"0001abcd`) {
		t.Fatalf("tracked: %q", s.Values())
	}
}
