package hermesbridge

import (
	"strings"
	"testing"
)

// The shape classes scrubInlineSecrets must catch, and the ordinary ops text
// it must leave alone. Every value is synthetic. A positive names the values
// that must not survive and the text that must; a negative must come back
// unchanged.
func TestScrubInlineSecretsShapeClasses(t *testing.T) {
	positives := []struct {
		name      string
		in        string
		forbidden []string
		wantSub   []string
	}{
		// (a) a password passed as a CLI flag, in the spellings a CLI accepts.
		{
			name:      "cli flag: short form, attached single-quoted value",
			in:        `mysql -u root -p'synthetic-pass-0001' appdb`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"mysql -u root -p", "appdb"},
		},
		{
			name:      "cli flag: short form, attached double-quoted value",
			in:        `mysqldump -u root -p"synthetic pass 0001" appdb`,
			forbidden: []string{"synthetic pass 0001"},
			wantSub:   []string{"mysqldump -u root -p", "appdb"},
		},
		{
			name:      "cli flag: single-dash long form, spaced value",
			in:        `app -password synthetic-pass-0001 -v`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"app -password [redacted] -v"},
		},
		{
			name:      "cli flag: single-dash long form, capitalised name",
			in:        `Connect-Db -Server db.internal -Password synthetic-pass-0001`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"-Server db.internal -Password [redacted]"},
		},
		{
			name:      "cli flag: single-dash abbreviated name, token-shaped value",
			in:        `app -pwd Synth3tic-Pass-0001`,
			forbidden: []string{"Synth3tic-Pass-0001"},
			wantSub:   []string{"app -pwd [redacted]"},
		},
		{
			name:      "cli flag: user:password attached to the short user flag",
			in:        `curl -uadmin:synthetic-pass-0001 https://api.example.test/v1`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"curl -uadmin:[redacted] https://api.example.test/v1"},
		},
		{
			name:      "cli flag: argument list, flag and value as separate JSON strings",
			in:        `"args": ["--db-password", "synthetic-pass-0001", "--port", "5432"]`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{`["--db-password", "[redacted]", "--port", "5432"]`},
		},
		{
			name:      "cli flag: argument list, flag and value as separate YAML items",
			in:        "args:\n  - --db-password\n  - synthetic-pass-0001\n  - --port\n  - \"5432\"",
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"  - --db-password\n  - [redacted]\n  - --port\n  - \"5432\""},
		},
		{
			name:      "cli flag: directory tool password short flag",
			in:        `ldapsearch -x -D cn=admin,dc=example,dc=test -w synthetic-pass-0001 -b dc=example,dc=test`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"-w [redacted] -b dc=example,dc=test"},
		},
		{
			name:      "cli flag: pass-phrase source argument and long forms stay caught",
			in:        `openssl pkcs12 -in a.p12 -passin pass:synthetic-pass-0001 && app --api-key=synthetic-key-0001 --secret 'synthetic secret 0001'`,
			forbidden: []string{"synthetic-pass-0001", "synthetic-key-0001", "synthetic secret 0001"},
			wantSub:   []string{"--api-key=[redacted]", "--secret [redacted]"},
		},
		// (b) an HTTP auth header whose credential follows the scheme word.
		{
			name:      "auth header: header setter call, non-bearer scheme",
			in:        `req.Header.Set("Authorization", "Token synthetic-token-0001")`,
			forbidden: []string{"synthetic-token-0001"},
			wantSub:   []string{`req.Header.Set("Authorization", "Token [redacted]")`},
		},
		{
			name:      "auth header: name/value header list",
			in:        "httpHeaders:\n- name: Authorization\n  value: ApiKey Synth3ticT0kenValue0001",
			forbidden: []string{"Synth3ticT0kenValue0001"},
			wantSub:   []string{"value: ApiKey [redacted]"},
		},
		{
			name:      "auth header: header line, curl argument, JSON map and table row stay caught",
			in:        "Authorization: Bearer synthetic-token-0001\ncurl -H \"Authorization: Basic c3ludGhldGljOnBhc3MtMDAwMQ==\" x\n{\"Authorization\": \"Token synthetic-token-0002\"}\n| Authorization | Bearer Synth3ticT0kenValue0003 |",
			forbidden: []string{"synthetic-token-0001", "c3ludGhldGljOnBhc3MtMDAwMQ==", "synthetic-token-0002", "Synth3ticT0kenValue0003"},
			wantSub:   []string{"Authorization: Bearer [redacted]", `"Authorization: Basic [redacted]"`, `"Authorization": "Token [redacted]"`, "| Authorization | Bearer [redacted] |"},
		},
		// (c) a host:port:database:user:password entry.
		{
			name:      "pgpass entry: bare line",
			in:        "db.internal:5432:appdb:appuser:synthetic-pass-0001",
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{"db.internal:5432:appdb:appuser:[redacted]"},
		},
		{
			name:      "pgpass entry: quoted, wildcard database",
			in:        `echo 'db.internal:5432:*:postgres:synthetic-pass-0001' >> ~/.pgpass`,
			forbidden: []string{"synthetic-pass-0001"},
			wantSub:   []string{`'db.internal:5432:*:postgres:[redacted]' >> ~/.pgpass`},
		},
		{
			name:      "pgpass entry: escaped colon in the password, wildcard host",
			in:        `*:*:*:postgres:synthetic\:pass\:0001`,
			forbidden: []string{"synthetic", "0001"},
			wantSub:   []string{`*:*:*:postgres:[redacted]`},
		},
		// URL userinfo, by shape: everything between scheme://user: and the
		// @ a host follows.
		{
			name:      "url userinfo: password holding sub-delimiters",
			in:        `postgres://app:synth'pass(0001)*@db.internal:5432/app`,
			forbidden: []string{"synth", "pass(0001)"},
			wantSub:   []string{"postgres://app:[redacted]@db.internal:5432/app"},
		},
		{
			name:      "url userinfo: password holding a raw @",
			in:        `redis://:synthetic@pass-0001@cache.internal:6379/0`,
			forbidden: []string{"synthetic", "pass-0001"},
			wantSub:   []string{"redis://:[redacted]@cache.internal:6379/0"},
		},
		{
			name:      "url userinfo: other sub-delimiters stay caught",
			in:        `amqp://user:synthetic!pass$0001&x;y=z,w+@mq.internal/vhost`,
			forbidden: []string{"synthetic!pass"},
			wantSub:   []string{"amqp://user:[redacted]@mq.internal/vhost"},
		},
		// The keyword-led rules still catch a token-shaped value.
		{
			name:      "keyword rules: a token-shaped value after a scheme word, in prose, under an ambiguous key",
			in:        "Basic c3ludGhldGljOnBhc3MtMDAwMQ== was sent. The token is Synth3ticT0kenValue0001. pass=Synth3tic-Pass-0001 auth: Synth3ticT0kenValue0002",
			forbidden: []string{"c3ludGhldGljOnBhc3MtMDAwMQ==", "Synth3ticT0kenValue0001", "Synth3tic-Pass-0001", "Synth3ticT0kenValue0002"},
			wantSub:   []string{"Basic [redacted] was sent.", "The token is [redacted].", "pass=[redacted]", "auth: [redacted]"},
		},
		// After an explicit credential word the rules fail closed: a plain
		// word is a value unless it is a state word.
		{
			name:      "fail closed: a plain-word password after a credential noun and verb",
			in:        "The password is quietpasture. The secret was meadowlark, and the token is expired.",
			forbidden: []string{"quietpasture", "meadowlark"},
			wantSub:   []string{"The password is [redacted].", "The secret was [redacted],", "the token is expired."},
		},
		{
			name:      "fail closed: a lower-case word under a strong key, mid-sentence",
			in:        "Log in with password: quietpasture and then token: meadowlark today",
			forbidden: []string{"quietpasture", "meadowlark"},
			wantSub:   []string{"password: [redacted] and", "token: [redacted] today"},
		},
		{
			name:      "fail closed: an ambiguous key's word value, and one with a digit before more words",
			in:        "auth: quietpasture\nUse pass: synth3t1c and more",
			forbidden: []string{"quietpasture", "synth3t1c"},
			wantSub:   []string{"auth: [redacted]", "pass: [redacted] and more"},
		},
		{
			name:      "fail closed: a letters-only mixed-case credential after Bearer",
			in:        "the client sent Bearer SynThEtIcToKeNvAlUe to the API",
			forbidden: []string{"SynThEtIcToKeNvAlUe"},
			wantSub:   []string{"sent Bearer [redacted] to the API"},
		},
		{
			name:      "fail closed: a token-like value ending in a state word",
			in:        "The password is Synth3tic-expired. The token was synth-9f3c2a-valid, the secret is SynthPass-mounted and api_key=synth-0001-set",
			forbidden: []string{"Synth3tic-expired", "synth-9f3c2a-valid", "SynthPass-mounted", "synth-0001-set"},
			wantSub:   []string{"The password is [redacted].", "The token was [redacted],", "the secret is [redacted]", "api_key=[redacted]"},
		},
		{
			name:      "fail closed: a lower-case hyphenated passphrase, whatever word it ends in",
			in:        "The passphrase is quiet-meadow-lantern-expired and the password is meadow-lantern-plaintext; the secret is meadow-lantern-expired.",
			forbidden: []string{"quiet-meadow-lantern-expired", "meadow-lantern-plaintext", "meadow-lantern-expired"},
			wantSub:   []string{"The passphrase is [redacted] and the password is [redacted]; the secret is [redacted]."},
		},
		{
			name:      "cli flag: pass-phrase source argument followed by a word",
			in:        "openssl pkcs12 -export -passout pass:quietpasture outfile.p12",
			forbidden: []string{"quietpasture"},
			wantSub:   []string{"-passout pass:[redacted] outfile.p12"},
		},
	}
	for _, tc := range positives {
		t.Run(tc.name, func(t *testing.T) {
			got := scrubInlineSecrets(tc.in)
			for _, f := range tc.forbidden {
				if strings.Contains(got, f) {
					t.Errorf("scrubInlineSecrets(%q) = %q: leaked %q", tc.in, got, f)
				}
			}
			for _, w := range tc.wantSub {
				if !strings.Contains(got, w) {
					t.Errorf("scrubInlineSecrets(%q) = %q: missing %q", tc.in, got, w)
				}
			}
		})
	}

	// Ordinary ops text: a keyword with no value shape after it.
	negatives := []string{
		"Enable basic monitoring and basic authentication on the cluster.",
		"Bearer tokens are rotated hourly.",
		"Fix auth: redirect loop on login",
		"pass: all checks green",
		"pwd: /home/user",
		"The token is expired and the secret is mounted.",
		"The credential is stored in Secret Manager.",
		"The secret is base64-encoded and the token is valid for 1h.",
		"Set `automountServiceAccountToken: false` on the pod template.",
		"Binary Authorization is disabled (`binaryAuthorization: {}`) on `seeded-a`.",
		"`secure-boot=NONE`, `binary-authorization=OFF`, `database-encryption=DECRYPTED`",
		"set `spec.binaryAuthorization.evaluationMode: PROJECT_SINGLETON_POLICY_ENFORCE`.",
		"platform-agent-host/kube-system/Deployment/platform-agent-credential-proxy: entry 4",
		"`Deployment/github-token-minter` (`token-minter: httpGet` probe on /version)",
		"gcloud backup-dr plans create p \\\n  --include-secrets \\\n  --cron-schedule=\"0 2 * * *\"",
		"Raise max_tokens: 4096 for the summariser.",
		"Use --password-file /etc/app/db-pass and --token-ttl 30m.",
		"The node fe80:0:0:0:200:f8ff:fe21:67cf answered at 10:15:30:123:456 UTC.",
		"See http://localhost:8080/healthz and https://example.test/docs/page.",
		"Enable Basic Authentication on the ingress; the token is valid and the password is unchanged.",
		"The secret is now-expired, the token is not-set and the credential is still-mounted.",
		"`UPGRADE_MASTER` (`2026-10-28T03:10:37Z`–`03:17:08Z`, `6 min`) and (2026-10-28T03:59:19Z–04:02:33Z) on default-pool",
		`{"state":{"running":{"startedAt":"2026-11-09T06:53:15Z"}},"name":"api","ready":false,"restartCount":4}`,
		"The upgrade ran 2026-10-28T03:56:05Z-03:59:15Z on default-pool.",
	}
	for _, in := range negatives {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// Markdown around a credential key (bold or italic, a code span, a table
// row or column, a parenthetical before the colon, an arrow) and the
// "decodes to" and "in secret X is" phrasings: the layouts an answer uses
// for "here is the credential". Each value is synthetic, one token-shaped
// and one a plain word under a key that names the credential.
func TestScrubInlineSecretsMarkdownLayouts(t *testing.T) {
	const tok, word = "Synth3tic-Pass-0001", "quietpasture"
	positives := []struct{ in, want string }{
		{"**Password:** `" + tok + "`", "**Password:** `[redacted]`"},
		{"**Password**: `" + word + "`", "**Password**: `[redacted]`"},
		{"- **password**: " + tok, "- **password**: [redacted]"},
		{"- __password__: " + word, "- __password__: [redacted]"},
		{"*password*: " + word, "*password*: [redacted]"},
		{"- `password`: `" + tok + "`", "- `password`: `[redacted]`"},
		{"- `password` → `" + word + "`", "- `password` → `[redacted]`"},
		{"password -> " + word, "password -> [redacted]"},
		{"'password' => '" + word + "'", "'password' => '[redacted]'"},
		{"Decoded `password`: `" + tok + "`", "Decoded `password`: `[redacted]`"},
		{"Password (decoded): " + word, "Password (decoded): [redacted]"},
		{"password (base64-decoded): " + tok, "password (base64-decoded): [redacted]"},
		{"The password decodes to " + tok + ".", "The password decodes to [redacted]."},
		{"The `password` key decodes to `" + word + "`.", "The `password` key decodes to `[redacted]`."},
		{"The password in secret `app-db` is `" + word + "`.", "The password in secret `app-db` is `[redacted]`."},
		{"The **password** in the secret app-db is **" + word + "**.", "The **password** in the secret app-db is **[redacted]**."},
		{"| password | " + tok + " |", "| password | [redacted] |"},
		{"| `password` | `" + word + "` |", "| `password` | `[redacted]` |"},
		{"| Key | Value |\n|---|---|\n| username | app |\n| **password** | " + word + " |",
			"| Key | Value |\n|---|---|\n| username | app |\n| **password** | [redacted] |"},
		{"| User | Password |\n| --- | --- |\n| app | " + word + " |\n| admin | `" + tok + "` |",
			"| User | Password |\n| --- | --- |\n| app | [redacted] |\n| admin | `[redacted]` |"},
		{"| Name | Token |\n|:--|:--|\n| ci | " + tok + " |", "| Name | Token |\n|:--|:--|\n| ci | [redacted] |"},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
	// Markdown with no credential value in it, and a lone mark that may be
	// part of a value, stay as they are.
	negatives := []string{
		"| Secret | Namespace | Age |\n|---|---|---|\n| app-tls | prod | 3d |\n| regcred | shop | 1d |",
		"| Name | Type |\n|---|---|\n| db-creds | Opaque |",
		"| Secret | db-credentials |",
		"| Token TTL | 1h |",
		"- **Secret name**: `db-credentials`",
		"**Status:** `Running` (the token is valid)",
		"Use `kubectl get secret app-db -o yaml` to read it.",
		"The **token** is expired and the `secret` is mounted.",
		"Set `DB_PASSWORD_FILE` to `/etc/db/password` -> mounted read-only.",
		"a*b and c_d_e stay; ptr->next is code.",
		"| Field | Meaning |\n|---|---|\n| `password` | the DB user's password |",
		// The nightly answers' shapes: a resource named after the
		// credential word in a table's first column, a feature name ending
		// in one, a note in parentheses before a label's colon, and a name
		// ending in the word before "is" in prose.
		"| Namespace | Status | Age |\n|---|---|---|\n| `seeded-token` | `Active` | `34d` |",
		"| Check | Result | Notes |\n|---|---|---|\n| Binary Authorization | `WARN` | Disabled |",
		"- **Binary Authorization (`WARN`):** `Binary Authorization is DISABLED`",
		"**ServiceAccount tokens (`5` major, `25` minor):** Apply the patch.",
		"`Deployment/token-reader` in `seeded-token` is declared intent.",
	}
	for _, in := range negatives {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// The strictness the scrubber had before value shapes, on every surface
// (titles, card reports, the debug error line, answers): a key whose
// credential word is followed by a digit suffix or a value qualifier is a
// strong key; a passphrase or other plain word under a strong key is
// redacted; an all-letter credential after Bearer or Basic is redacted
// unless it is a dictionary word prose puts there. Every value is
// synthetic.
func TestScrubInlineSecretsKeepsTheStrictnessOfBefore(t *testing.T) {
	positives := []struct{ in, want string }{
		{"password_hash=lanternmeadowx", "password_hash=[redacted]"},
		{"DB_PASSWORD_VALUE=lanternmeadowfield", "DB_PASSWORD_VALUE=[redacted]"},
		{"db_password_b64: bGFudGVybm1lYWRvdw", "db_password_b64: [redacted]"},
		{"tokenValue: lanternmeadow", "tokenValue: [redacted]"},
		{"passwordConfirm: lanternmeadow", "passwordConfirm: [redacted]"},
		{"password_2: lanternmeadow", "password_2: [redacted]"},
		{"REDIS_PASSWORD_2=lantern-meadow-field", "REDIS_PASSWORD_2=[redacted]"},
		{"password1: lanternmeadow", "password1: [redacted]"},
		{"PASSWORD2=lanternmeadow", "PASSWORD2=[redacted]"},
		{"DB_PASSWORD=lanternmeadowfield", "DB_PASSWORD=[redacted]"},
		{"password: lantern-meadow-field", "password: [redacted]"},
		{"api_key: lanternmeadow1", "api_key: [redacted]"},
		{"Bearer lanternmeadowfield", "Bearer [redacted]"},
		{"BEARER lanternmeadowfield", "BEARER [redacted]"},
		{"Bearer\tLanternmeadow", "Bearer\t[redacted]"},
		{"Basic lanternmeadowfield", "Basic [redacted]"},
		{"authorization:\n  Bearer lanternmeadowfield", "authorization:\n  Bearer [redacted]"},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
	negatives := []string{
		"Enable Basic Authentication and bearer credentials on the ingress.",
		"Run basic troubleshooting, then basic connectivity checks.",
		"token_count: 1234, token_ttl: 30m, password_file: /etc/app/db-pass, private_key_id_ref: x",
		"Bearer tokens are short-lived.",
	}
	for _, in := range negatives {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// The markdown rules stay deny-by-default; each false positive the
// nightlies showed is kept out by an exclusion tied to its own non-secret
// shape (a status word or a tally in the parenthetical, a listing's header
// words, a feature-name qualifier, a state word after "is"), so the
// near-identical credential layouts are still redacted. Synthetic values.
func TestScrubInlineSecretsMarkdownExclusionsStayNarrow(t *testing.T) {
	const word = "quietpasture"
	positives := []struct{ in, want string }{
		// Any other parenthetical before the colon is read past.
		{"Password (prod): " + word, "Password (prod): [redacted]"},
		{"- **DB password (current):** `" + word + "`", "- **DB password (current):** `[redacted]`"},
		// A table whose headers are not a listing's: its first cells are keys.
		{"| Item | Setting |\n|---|---|\n| password | " + word + " |", "| Item | Setting |\n|---|---|\n| password | [redacted] |"},
		{"| Variable | Current |\n| --- | --- |\n| `DB_PASSWORD` | `" + word + "` |", "| Variable | Current |\n| --- | --- |\n| `DB_PASSWORD` | `[redacted]` |"},
		// A compound credential name ending in a bare noun is strong.
		{"| App | Client Secret |\n|---|---|\n| web | " + word + " |", "| App | Client Secret |\n|---|---|\n| web | [redacted] |"},
		{"| api key | " + word + " |", "| api key | [redacted] |"},
		{"| DB Token | " + word + " |", "| DB Token | [redacted] |"},
		// A hyphenated or dotted name before the noun is still read.
		{"The app-db password is " + word + ".", "The app-db password is [redacted]."},
		{"spring.datasource.password is " + word + ".", "spring.datasource.password is [redacted]."},
		{"`app-db-password` is `" + word + "`.", "`app-db-password` is `[redacted]`."},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
	negatives := []string{
		"- **Password rotation (`WARN`):** `rotation is overdue`",
		"**Password (status):** enabled",
	}
	for _, in := range negatives {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// A key/value table with a status or notes column still redacts its rows'
// values, and a decode parenthetical before a value that merely opens with
// a status word is read past. Synthetic values.
func TestScrubInlineSecretsValueColumnsAndDecodeParens(t *testing.T) {
	positives := []struct{ in, want string }{
		{"| Name | Value | Notes |\n|---|---|---|\n| password | quietpasture | rotated monthly |",
			"| Name | Value | Notes |\n|---|---|---|\n| password | [redacted] | rotated monthly |"},
		{"| Key | Status | Value |\n|---|---|---|\n| api_key | set | quietpasture |",
			"| Key | Status | Value |\n|---|---|---|\n| api_key | set | [redacted] |"},
		{"Password (decoded): ready2go-synthetic", "Password (decoded): [redacted]"},
		{"Password (prod): readyvalue123", "Password (prod): [redacted]"},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
}

// Every value column of a key/value table is redacted, encoded columns
// included, and a short row's last cell; a parenthetical is kept only when
// the whole rest of the line is a status word. Synthetic values.
func TestScrubInlineSecretsAllValueColumnsAndWholeStatusValue(t *testing.T) {
	positives := []struct{ in, want string }{
		{"| Name | Decoded | Encoded |\n|---|---|---|\n| password | quietpasture | cXVpZXRwYXN0dXJl |",
			"| Name | Decoded | Encoded |\n|---|---|---|\n| password | [redacted] | [redacted] |"},
		{"| Name | Decoded | Encoded |\n|---|---|---|\n| password | quietpasture |",
			"| Name | Decoded | Encoded |\n|---|---|---|\n| password | [redacted] |"},
		{"| Key | Status | Value |\n|---|---|---|\n| api_key | quietpasture |",
			"| Key | Status | Value |\n|---|---|---|\n| api_key | [redacted] |"},
		{"Password (decoded): enabled quietpasture word", "Password (decoded): [redacted]"},
		{"Password (prod): ready quietpasture", "Password (prod): [redacted]"},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
}

// A Kubernetes env entry whose name is a credential key and whose value is
// a literal sibling, in YAML, JSON (compact, indented, value first) or a
// flow mapping, loses the value; a non-credential name, a valueFrom
// reference, a path under a *_FILE name and a TTL under a token setting
// stay. Synthetic values.
func TestScrubInlineSecretsEnvNameValuePairs(t *testing.T) {
	const word, tok = "quietpasture", "Synth3tic-Pass-0001"
	positives := []struct{ in, want string }{
		{"env:\n- name: DB_PASSWORD\n  value: " + word + "\n- name: LOG_LEVEL\n  value: debug",
			"env:\n- name: DB_PASSWORD\n  value: [redacted]\n- name: LOG_LEVEL\n  value: debug"},
		{"        - name: API_TOKEN\n          value: \"" + tok + "\"", "        - name: API_TOKEN\n          value: \"[redacted]\""},
		{`{"name":"DB_PASSWORD","value":"` + word + `"}`, `{"name":"DB_PASSWORD","value":"[redacted]"}`},
		{"{\n  \"name\": \"REDIS_PASSWORD\",\n  \"value\": \"" + word + "\"\n}", "{\n  \"name\": \"REDIS_PASSWORD\",\n  \"value\": \"[redacted]\"\n}"},
		{`{"value":"` + tok + `","name":"CLIENT_SECRET"}`, `{"value":"[redacted]","name":"CLIENT_SECRET"}`},
		{"env: [{name: DB_PASSWORD, value: " + word + "}]", "env: [{name: DB_PASSWORD, value: [redacted]}]"},
	}
	for _, tc := range positives {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
	negatives := []string{
		"- name: LOG_LEVEL\n  value: debug",
		"- name: DB_PASSWORD\n  valueFrom:\n    secretKeyRef:\n      name: db-creds\n      key: password",
		"- name: DB_PASSWORD_FILE\n  value: /etc/secret/db-password",
		`{"name":"TOKEN_TTL","value":"30m"}`,
		"- name: DB_PASSWORD\n  value: \"\"",
	}
	for _, in := range negatives {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// A boolean or valueless flag just before a credential flag in an argument
// list does not take the credential flag as its value: the credential's
// value is redacted, in a JSON list and in YAML items.
func TestScrubInlineSecretsArgListFlagBeforeACredentialFlag(t *testing.T) {
	cases := []struct{ in, want string }{
		{`"args": ["--verbose", "--db-password", "synthetic-pass-0001"]`,
			`"args": ["--verbose", "--db-password", "[redacted]"]`},
		{`"args": ["--include-secrets", "--db-password", "synthetic-pass-0001", "--port", "5432"]`,
			`"args": ["--include-secrets", "--db-password", "[redacted]", "--port", "5432"]`},
		{"args:\n  - --verbose\n  - --db-password\n  - synthetic-pass-0001\n",
			"args:\n  - --verbose\n  - --db-password\n  - [redacted]\n"},
	}
	for _, tc := range cases {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
}

// A line holding two parenthetical labels before their colons reads both:
// a credential label after an earlier note on the same line still reads as
// its key, and a status note stays.
func TestScrubInlineSecretsTwoParentheticalLabelsOnALine(t *testing.T) {
	const word = "quietpasture"
	cases := []struct{ in, want string }{
		{"User (default): app, Password (decoded): " + word, "User (default): app, Password (decoded): [redacted]"},
		{"Rotated (2026-11-01): yes; Password (prod): " + word, "Rotated (2026-11-01): yes; Password (prod): [redacted]"},
		{"Check (WARN): DB_PASSWORD (prod): " + word, "Check (WARN): DB_PASSWORD (prod): [redacted]"},
		{"| Password (decoded): " + word + " | Token (prod): " + word + " |", "| Password (decoded): [redacted] | Token (prod): [redacted] |"},
	}
	for _, tc := range cases {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
	for _, in := range []string{
		"- **Binary Authorization (`WARN`):** `Binary Authorization is DISABLED`, Shielded Nodes (`OK`): enabled",
	} {
		if got := scrubInlineSecrets(in); got != in {
			t.Errorf("scrubInlineSecrets(%q) = %q, want it unchanged", in, got)
		}
	}
}

// A code-spanned value is bounded by its backticks, not by the punctuation
// or spaces inside it, in prose and after a key, and its whole span is
// redacted; emphasis marks inside a code span are part of the value.
func TestScrubInlineSecretsCodeSpannedValueIsBoundedByItsBackticks(t *testing.T) {
	cases := []struct{ in, want string }{
		{"The password is `Synth2026!prod`.", "The password is `[redacted]`."},
		{"The password is `a!synthetic123`.", "The password is `[redacted]`."},
		{"The password is `2026!SynthPass`.", "The password is `[redacted]`."},
		{"The token is `v1.synthetic123`.", "The token is `[redacted]`."},
		{"Decoded `password`: `correct horse battery`", "Decoded `password`: `[redacted]`"},
		{"**password**: `p@ss{1}synthetic`", "**password**: `[redacted]`"},
		{"**password**: `p@ss;word,2`", "**password**: `[redacted]`"},
		{"**password**: `*synthetic123*`", "**password**: `[redacted]`"},
		{"- `password` → `synth pass, 0001`", "- `password` → `[redacted]`"},
	}
	for _, tc := range cases {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
	// The whole code-spanned value reaches the seen-values sink.
	var got []string
	scrubInlineSecretsTo("The password is `Synth2026!prod`.", func(v string) { got = append(got, v) })
	if len(got) != 1 || got[0] != "Synth2026!prod" {
		t.Errorf("sink got %q, want the whole value", got)
	}
}

// A code span holding a credential token and more words after it, under a
// key whose class needs a token-shaped value, is redacted whole: the span
// is read for its first word when the whole span is not a value.
func TestScrubInlineSecretsCodeSpanWithACredentialAndMoreWords(t *testing.T) {
	cases := []struct{ in, want string }{
		{"- **github_token_id**: `Synth3t1cT0kenValue01 rotated daily`", "- **github_token_id**: `[redacted]`"},
		{"note github_token_id: `Synth3t1cT0kenValue01 (prod)` here", "note github_token_id: `[redacted]` here"},
	}
	for _, tc := range cases {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
}

// A value column headed Base64 or B64, or a header word with a digit after
// it (Value1), reads the table as a key/value table.
func TestScrubInlineSecretsTableHeaderWithADigit(t *testing.T) {
	const v = "quietpasture"
	for _, h := range []string{"Base64", "B64", "Value1"} {
		in := "| Key | Status | " + h + " |\n|---|---|---|\n| password | set | " + v + " |\n"
		want := "| Key | Status | " + h + " |\n|---|---|---|\n| password | set | [redacted] |\n"
		if got := scrubInlineSecrets(in); got != want {
			t.Errorf("header %s: scrubInlineSecrets(%q)\n got %q\nwant %q", h, in, got, want)
		}
	}
}

// A valueless flag just before a credential flag does not take the
// credential flag as its value, in the long-flag, single-dash, spaced -p/-a
// and LDAP -w rules; a value after = may still start with a dash.
func TestScrubInlineSecretsValuelessFlagBeforeACredentialFlag(t *testing.T) {
	cases := []struct{ in, want string }{
		{"app --verbose --password synthetic-pass-0001", "app --verbose --password [redacted]"},
		{"curl --silent --token synthetic-token-0001 https://api.example.test", "curl --silent --token [redacted] https://api.example.test"},
		{"java -server -password synthetic-pass-0001", "java -server -password [redacted]"},
		{"mysql -h db -A -p synthetic-pass-0001", "mysql -h db -A -p [redacted]"},
		{"ldapsearch -x -W -w synthetic-pass-0001", "ldapsearch -x -W -w [redacted]"},
		{"app --password=-synthetic-pass-0001", "app --password=[redacted]"},
		{"app -password=-synthetic-pass-0001", "app -password=[redacted]"},
	}
	for _, tc := range cases {
		if got := scrubInlineSecrets(tc.in); got != tc.want {
			t.Errorf("scrubInlineSecrets(%q)\n got %q\nwant %q", tc.in, got, tc.want)
		}
	}
}
