package answerscrub

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
)

var (
	inlineTokenValuePattern = regexp.MustCompile(`(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{16,}|ya29\.[A-Za-z0-9._-]{16,}|1//[0-9A-Za-z_-]{20,}|AIza[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{12,}|glpat-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|r8_[A-Za-z0-9]{20,}|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})`)
	// A private-key armour header ends in "PRIVATE KEY-----" (PEM, OpenSSH,
	// PKCS#8, EC, RSA, DSA) or "PRIVATE KEY BLOCK-----" (PGP).
	pemPrivateKeyBlockPattern     = regexp.MustCompile(`(?is)-----BEGIN [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----`)
	pemPrivateKeyOpenPattern      = regexp.MustCompile(`(?is)-----BEGIN [A-Z0-9 _-]*PRIVATE KEY(?: BLOCK)?-----.*`)
	inlineAuthSchemePrefixPattern = regexp.MustCompile(`(?i)^(Bearer|Basic|Token|ApiKey|Api-Key|Digest|Negotiate|AWS4-HMAC-SHA256|Mutual|OAuth)([ \t]+)`)
	// A credential after an HTTP auth scheme word wherever the word stands
	// (a header value, a curl -H argument, a JSON map, a header list's
	// value, a table row, prose), as authSchemeCredentialRedacted says.
	// Where an Authorization key stands in front, the key=value rule owns
	// the pair and redacts the value whatever its shape
	// (authHeaderKVLeadPattern).
	authSchemeCredentialPattern = regexp.MustCompile(`(?i)\b(Bearer|Basic|Token|ApiKey|Api-Key|Digest|Negotiate|NTLM|OAuth|Hawk|AWS4-HMAC-SHA256|Mutual)[ \t]+([^\s"'\x60,;)\]}>|]+)`)
	authHeaderKVLeadPattern     = regexp.MustCompile(`(?i)authorization\\?["']?[ \t]*[:=][ \t]*\\?["']?[ \t]*$`)
	// "Authorization", "<scheme> <credential>": a header setter call or a
	// header pair in code, the credential redacted whatever its shape.
	authHeaderPairPattern = regexp.MustCompile(`(?i)\b(?:proxy-)?authorization\\?["'][ \t]*,[ \t]*(?:\r?\n[ \t]*)?\\?["'](?:(?:Bearer|Basic|Token|ApiKey|Api-Key|Digest|Negotiate|NTLM|OAuth|Hawk|AWS4-HMAC-SHA256|Mutual)[ \t]+)?([^"'\\\r\n]+)`)
	// A URL's password is the userinfo by shape: everything between
	// scheme://user: and the @ a host follows, whatever bytes it holds (a
	// sub-delimiter, a path character, a raw @), to the last such @ in the
	// unbroken run. A URL with an @ later in its path after a port is
	// over-redacted, the safe side.
	inlineURLCredPattern = regexp.MustCompile(`([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@"'\x60]*:)([^\s"\x60<>]+)@(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._~%-]*[A-Za-z0-9])?)`)
	// A libpq password file entry, host:port:database:user:password, a
	// colon in a field escaped with a backslash. pgpassEntryRedacted tells
	// one from a timestamp or an IPv6 address.
	pgpassEntryPattern = regexp.MustCompile(`(?:^|[\s"'\x60=(])(\*|\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9_.-]+):(?:\*|[0-9]{1,5}):((?:[A-Za-z0-9_.$*-]|\\.)+):((?:[A-Za-z0-9_.$*-]|\\.)+):((?:[^\s"'\x60\\]|\\.)+)`)
	// The host field of an RFC 3339 timestamp, which a time range in an
	// answer (2026-10-28T03:56:05Z-03:59:15Z) puts where a host would be.
	timestampHostPattern    = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}$`)
	cliUserPassPattern      = regexp.MustCompile(`(?i)(\b(?:curl|wget|mysql|mysqldump|mariadb|mysqladmin|(?:docker|helm)\s+login)\b[^\r\n]*?\s(?:-u|--user)(?:=|[ \t]+)?)(["']?)([^"'\s:]+):([^"'\s]+)(["']?)`)
	cliAttachedPassPattern  = regexp.MustCompile(`(?i)(\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo)\b[^\r\n]*?\s-p)("[^"\r\n]*"|'[^'\r\n]*'|[^\s"';|&]+)`)
	cliSpacedPassPattern    = regexp.MustCompile(`(?i)(\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo|redis-cli|az\s+login|(?:docker|helm)\s+login)\b[^\r\n]*?\s-[pa][ \t]+)("[^"\r\n]*"|'[^'\r\n]*'|[^-\s"';|&][^\s"';|&]*)`)
	cliLDAPPassPattern      = regexp.MustCompile(`(?i)(\bldap(?:search|modify|add|delete|whoami|passwd|compare|exop|modrdn)\b[^\r\n]*?\s-w[ \t]+)("[^"\r\n]*"|'[^'\r\n]*'|[^-\s"';|&][^\s"';|&]*)`)
	cliPassphraseArgPattern = regexp.MustCompile(`(?i)(\s-pass(?:in|out|word)?[ \t]+pass:)([^\s"';|&]+)`)
	// A flag's unquoted value never starts with a dash, so a flag with no
	// value of its own does not take the next flag as its value and hide
	// that flag's; a value after = or : that does start with one is read
	// by the Dashed patterns, which take no spaced value.
	cliLongSecretFlagPattern       = regexp.MustCompile(`(--([A-Za-z0-9_.-]+)(=|[ \t]+))("[^"\r\n]*"|'[^'\r\n]*'|[^-\s"';|&][^\s"';|&]*)`)
	cliLongSecretFlagDashedPattern = regexp.MustCompile(`(--([A-Za-z0-9_.-]+)(=))(-[^\s"';|&]*)`)
	// A single-dash long flag (-password x, -Password x, -token=x), the
	// spelling Go's flag package, Java tools and PowerShell take.
	cliSingleDashSecretFlagPattern       = regexp.MustCompile(`(?:^|[\s"'(\[,])(-([A-Za-z][A-Za-z0-9_.-]{2,})(=|:|[ \t]+))("[^"\r\n]*"|'[^'\r\n]*'|[^-\s"';|&][^\s"';|&]*)`)
	cliSingleDashSecretFlagDashedPattern = regexp.MustCompile(`(?:^|[\s"'(\[,])(-([A-Za-z][A-Za-z0-9_.-]{2,})(=|:))(-[^\s"';|&]*)`)
	// A flag and its value as separate items of an argument list: JSON or
	// Python strings ("--db-password", "x", on one line or on two as
	// kubectl's -o json prints a list) or YAML items (- --db-password
	// then - x on the next line), as a container's args carry them. A value
	// never starts with a dash, so a flag with no value of its own does not
	// take the next flag as its value and hide that flag's.
	cliArgListJSONPattern = regexp.MustCompile(`["']--?([A-Za-z][A-Za-z0-9_.-]*)["'][ \t]*,[ \t]*(?:\r?\n[ \t]*)?["']((?:[^-"'\r\n][^"'\r\n]*)?)["']`)
	cliArgListYAMLPattern = regexp.MustCompile(`(?m)^[ \t]*-[ \t]+["']?--?([A-Za-z][A-Za-z0-9_.-]*)["']?[ \t]*\r?\n[ \t]*-[ \t]+["']?([^-"'\r\n#\s](?:[^"'\r\n#]*[^"'\r\n#\s])?)`)
	// Prose that states a secret: "the password is hunter2", "password for
	// admin is hunter2", "the password in secret app-db is hunter2", "the
	// password key decodes to hunter2", "the token was abc", "the password
	// is now hunter2", "the secret is set to abc", "the password is:
	// hunter2", "the password for user admin is hunter2". The verb, and each
	// qualifier after it (now, currently, still, just, simply, set to, equal
	// to), may be followed by a colon or by whitespace, none of which is the
	// value. proseValueRedacted keeps
	// a state word: "the token is expired" states no secret.
	proseSecretPattern          = regexp.MustCompile(`(?i)\b((?:the\s+)?(?:[A-Za-z0-9_.-]+\s+)?(?:password|passwd|passphrase|secret|token|api[ _-]?key|credential)[*_]*\s+(?:(?:key|value|field|entry)\s+)?(?:(?:for|in|of|from)\s+(?:(?:the|secret|kubernetes)\s+){0,2}[^\s,.;:!?)'"\]]+(?:\s+[^\s,.;:!?)'"\]]+)?\s+)?(?:is|was|=|will\s+be|should\s+be|has\s+been\s+set\s+to|decodes\s+(?:to|as)|decoded\s+(?:to|as)|is\s+decoded\s+as)(?:\s*:\s*|\s+)(?:(?:now|currently|still|just|simply|also|again)(?:\s*:\s*|\s+))*(?:(?:set|equal|changed|updated|reset)\s+to(?:\s*:\s*|\s+))?)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,.;:!?)'"\]]+)`)
	inlineKVSecretPattern       = regexp.MustCompile(`((?:\\?["'])?)([A-Za-z0-9_.-]+)((?:\\?["'])?)([ \t*_]*([=:])[ \t]*)`) // a * or _ left as text may precede the separator (#6182)
	inlineExtraSecretKeyPattern = regexp.MustCompile(`(?i)(?:^|[_.-])(?:pass|pwd|auth|apikey|access[_-]?key|secret[_-]?key|privatekey|id_rsa|id_ed25519|id_ecdsa|dockercfg|dockerconfigjson|client[_-]?certificate[_-]?data)(?:$|[_.-])|\.(?:key|pem|crt|p12|pfx|dockercfg|dockerconfigjson)$`)
	// An all-caps env-style key whose last component says it holds a key,
	// credentials, a DSN or private material (ENCRYPTION_KEY, STRIPE_KEY,
	// AWS_CREDS, DB_DSN, RSA_PRIVATE): the spelling is the credential's,
	// whatever the first component names. Case-sensitive on purpose, so a
	// prose "primary key" or a JSON "sort_key" is not blanked.
	allCapsSecretSuffixKeyPattern = regexp.MustCompile(`^[A-Z][A-Z0-9_]*_(?:KEY|KEYS|CREDS|DSN|PRIVATE)$`)
	leadingEqAssignmentPattern    = regexp.MustCompile(`^([A-Za-z_][A-Za-z0-9_.-]*)=[^\s=]`)
	allCapsEnvKeyPattern          = regexp.MustCompile(`^[A-Z_][A-Z0-9_]+$`)
	base64PaddingTokenPattern     = regexp.MustCompile(`^[A-Za-z0-9+/_-]+={1,2}(?:[\s,;)\]}>]|\\?["'](?:\s|$)|$)`)
	yamlSecretBlockHeaderPattern  = regexp.MustCompile(`^([ \t]*)(?:data|stringData)[ \t]*:[ \t]*(?:#.*)?$`)
	yamlBlockEntryPattern         = regexp.MustCompile(`^([ \t]+[-A-Za-z0-9_./]+[ \t]*:[ \t]*)([^\r\n#]+?)([ \t]*(?:#.*)?)$`)
)

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

// SecretLookingKey says whether a value under this key never goes on the
// bus: a secret word as a whole component in any spelling, or a camelCase
// key that opens with the word and goes on in something other than a name.
func SecretLookingKey(k string) bool {
	if redactedKeyPattern.MatchString(k) || redactedCamelKeyPattern.MatchString(k) || redactedUpperSuffixPattern.MatchString(k) {
		return true
	}
	return redactedCamelHeadPattern.MatchString(k) && !camelHeadNamePattern.MatchString(k)
}

// inlineSecretKey says whether a key seen inside text (a key=value pair, a
// --flag, a YAML entry) names a credential: the bus-wide SecretLookingKey
// rule, the extra spellings text uses (pass, pwd, auth, a key file's
// extension), and the all-caps env-style suffixes.
func inlineSecretKey(k string) bool {
	return SecretLookingKey(k) || inlineExtraSecretKeyPattern.MatchString(k) || allCapsSecretSuffixKeyPattern.MatchString(k) || qualifiedSecretKey(k)
}

// qualifiedSecretKey says whether a key's credential word is followed only
// by a digit suffix (password_2, PASSWORD2, password1) or by qualifiers
// that still name the credential's own value (password_hash,
// DB_PASSWORD_VALUE, tokenValue, passwordConfirm, db_password_b64): the
// value under it is the credential, or a form of it. An _id, _name, _file,
// _ttl or _ref after the word names something else and is not one.
func qualifiedSecretKey(k string) bool {
	return qualifiedSecretKeyPattern.MatchString(snakeKey(k))
}

var qualifiedSecretKeyPattern = regexp.MustCompile(`(?i)(?:^|_)(?:token|secret|password|passwd|passphrase|api_?key|private_?key|access_?key|secret_?key|credential)s?(?:_?[0-9]+|_(?:value|val|hash|hashed|confirm|confirmation|b64|base64|enc|encoded|plain|plaintext|raw|hex|str|string|text|bytes|data|old|new|current|prev|previous|literal))+$`)

// snakeKey spells a key with underscores between its components: a dot or
// hyphen becomes one, and one goes between a camelCase key's words
// (tokenValue, APIKeyHash). A digit stays joined to the word before it.
func snakeKey(k string) string {
	var sb strings.Builder
	for i := 0; i < len(k); i++ {
		c := k[i]
		if c == '.' || c == '-' {
			sb.WriteByte('_')
			continue
		}
		if i > 0 && c >= 'A' && c <= 'Z' {
			p := k[i-1]
			nextLower := i+1 < len(k) && k[i+1] >= 'a' && k[i+1] <= 'z'
			if p >= 'a' && p <= 'z' || p >= '0' && p <= '9' || (p >= 'A' && p <= 'Z' && nextLower) {
				sb.WriteByte('_')
			}
		}
		sb.WriteByte(c)
	}
	return sb.String()
}

// The value policy under a key inlineSecretKey accepts depends on what the
// key names (secretKeyClassOf):
//
//   - a strong key names the credential itself: its last component is the
//     credential word (db_password, accessToken, X-Api-Key, Authorization,
//     PGPASSWORD, AWS_CREDS, tls.key), or the word is followed only by a
//     digit suffix or a qualifier naming its value (qualifiedSecretKey:
//     password_2, passwordConfirm, DB_PASSWORD_VALUE). Its value is redacted
//     unless it is plainly not a value (valueNotASecret), a passphrase or
//     any other plain word included;
//   - an ambiguous key is one of the short words ordinary text also uses as
//     a label (pass, pwd, auth). Its value is redacted unless it is plainly
//     not a value, a path, prose (more than one word, or a lower-case word
//     the sentence runs on from: "Fix auth: redirect loop"); one of six or
//     more bytes holding a digit is redacted whatever follows it;
//   - any other accepted key carries the credential word somewhere other
//     than last (token-minter, credential-proxy,
//     binaryAuthorization.evaluationMode, token-ttl): it names something
//     else, a container, a proxy, a field, a setting, and its value is
//     redacted only when token-shaped (valueTokenShaped).
type secretKeyClass int

const (
	otherSecretKey secretKeyClass = iota
	ambiguousSecretKey
	strongSecretKeyClass
)

func secretKeyClassOf(k string) secretKeyClass {
	switch {
	case strongSecretKey(k):
		return strongSecretKeyClass
	case ambiguousSecretKeyPattern.MatchString(k) && !SecretLookingKey(k):
		return ambiguousSecretKey
	}
	return otherSecretKey
}

// strongSecretKey says whether a key's last component is the credential
// word (secretKeyClass).
func strongSecretKey(k string) bool {
	return strongSecretKeyEndPattern.MatchString(k) || strongSecretCamelEndPattern.MatchString(k) ||
		redactedUpperSuffixPattern.MatchString(k) || allCapsSecretSuffixKeyPattern.MatchString(k) || qualifiedSecretKey(k)
}

var (
	strongSecretKeyEndPattern   = regexp.MustCompile(`(?i)(?:^|[_.-])(?:token|secret|password|passwd|passphrase|authorization|cookie|api[_-]?key|apikey|private[_-]?key|privatekey|ssh[_-]?key|signing[_-]?key|key[_-]?data|access[_-]?key|secret[_-]?key|credential|id_rsa|id_ed25519|id_ecdsa|dockercfg|dockerconfigjson|client[_-]?certificate[_-]?data)s?$|\.(?:key|pem|crt|p12|pfx|dockercfg|dockerconfigjson)$`)
	strongSecretCamelEndPattern = regexp.MustCompile(`[A-Za-z0-9](?:Token|Secret|Password|Passwd|Passphrase|Authorization|Cookie|Api[_-]?Key|APIKey|Private[_-]?Key|Ssh[_-]?Key|SSHKey|Signing[_-]?Key|Key[_-]?Data|Access[_-]?Key|Secret[_-]?Key|Credential)s?$`)
	ambiguousSecretKeyPattern   = regexp.MustCompile(`(?i)(?:^|[_.-])(?:pass|pwd|auth)(?:$|[_.-])`)
	// A value that stands for a secret rather than being one: an
	// environment reference, a command substitution, a <placeholder>.
	secretPlaceholderPattern = regexp.MustCompile(`^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(.*\)|<[^<>]+>)$`)
)

// credentialStateWords are what text says a credential is, or what a
// credential key is set to, that is not a value: a literal, a switch, a
// state. A short list on purpose: after an explicit credential word any
// other value is redacted.
var credentialStateWords = map[string]bool{
	"true": true, "false": true, "null": true, "nil": true, "none": true, "yes": true, "no": true,
	"on": true, "off": true, "enabled": true, "disabled": true, "set": true, "unset": true,
	"empty": true, "blank": true, "required": true, "optional": true, "n/a": true,
	// A progressive auxiliary, never a value: "the password is currently
	// being reset".
	"being":   true,
	"mounted": true, "rotated": true, "expired": true, "revoked": true, "valid": true, "invalid": true,
	"stored": true, "missing": true, "present": true, "absent": true, "configured": true,
	"unconfigured": true, "unchanged": true, "changed": true, "correct": true, "incorrect": true,
	"wrong": true, "encoded": true, "encrypted": true, "hashed": true, "redacted": true,
	"masked": true, "hidden": true, "available": true, "unavailable": true, "unknown": true,
	"stale": true, "used": true, "unused": true, "needed": true, "not": true, "a": true, "an": true,
	"the": true, "managed": true, "generated": true, "provided": true, "injected": true,
	"exposed": true, "leaked": true, "compromised": true, "only": true, "same": true, "different": true,
	"declared": true,
}

// credentialWholeValueWords are more values that are not a value, matched
// only as the whole value, never as part of a phrase.
var credentialWholeValueWords = map[string]bool{
	"base64": true, "base64-encoded": true, "plaintext": true, "read-only": true, "write-only": true,
	"not-set": true,
}

// statePhraseQualifiers are the words a plain hyphenated state phrase may
// open with (plainHyphenatedStatePhrase).
var statePhraseQualifiers = map[string]bool{
	"now": true, "still": true, "already": true, "not": true, "never": true,
}

// trimValueEdges strips what wraps a value in text and is not part of it:
// quotes, backticks, and the sentence punctuation or closing bracket that
// follows it.
func trimValueEdges(v string) string {
	v = strings.TrimSpace(v)
	v = strings.TrimRight(v, ".,;:!?)]}`*")
	if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
		v = v[1 : len(v)-1]
	}
	return strings.Trim(v, "`\"'")
}

// valueNotASecret says a value after an explicit credential word is plainly
// not a value: no letter or digit in it (a line continuation, "{",
// markdown), a marker already placed, a placeholder, a state word, a plain
// hyphenated phrase ending in one (plainHyphenatedStatePhrase), or a short
// number (max_tokens: 4096).
func valueNotASecret(v string) bool {
	if !hasAlnum(v) || strings.Contains(v, Redacted) || secretPlaceholderPattern.MatchString(v) {
		return true
	}
	lower := strings.ToLower(v)
	if credentialStateWords[lower] || credentialWholeValueWords[lower] {
		return true
	}
	if plainHyphenatedStatePhrase(v) {
		return true
	}
	return len(v) <= 5 && isAllDigits(v)
}

// plainHyphenatedStatePhrase says whether v is a qualifier and a state word
// joined by hyphens, at most three words, each lower-case letters only: one
// of statePhraseQualifiers first, a state word of four letters or more last
// (now-expired, still-mounted, not-yet-rotated). Anything else hyphenated,
// a lower-case passphrase or a value with a digit, a capital or a symbol in
// it, is not one and stays redacted.
func plainHyphenatedStatePhrase(v string) bool {
	parts := strings.Split(v, "-")
	if len(parts) < 2 || len(parts) > 3 || !statePhraseQualifiers[parts[0]] {
		return false
	}
	for _, p := range parts {
		if !isLowerWord(p) {
			return false
		}
	}
	last := parts[len(parts)-1]
	return len(last) > 3 && credentialStateWords[last] && !valueTokenShaped(v)
}

// keyedValueRedacted says whether the value under a key inlineSecretKey
// accepts is redacted, as its secretKeyClass says. followedByWord says the
// text after the value runs on as a sentence (a space, then a lower-case
// word), which only the ambiguous keys read.
func keyedValueRedacted(key, value string, followedByWord bool) bool {
	return classValueRedacted(secretKeyClassOf(key), value, followedByWord)
}

func classValueRedacted(class secretKeyClass, value string, followedByWord bool) bool {
	v := trimValueEdges(value)
	if valueNotASecret(v) {
		return false
	}
	switch class {
	case strongSecretKeyClass:
		return true
	case ambiguousSecretKey:
		if strings.HasPrefix(v, "/") || strings.HasPrefix(v, "~/") || strings.HasPrefix(v, "./") || strings.Contains(v, "://") {
			return false
		}
		if len(v) >= 6 && strings.ContainsAny(v, "0123456789") {
			return true
		}
		if strings.ContainsAny(v, " \t") {
			return false
		}
		return !(followedByWord && isLowerWord(v))
	}
	return valueTokenShaped(v)
}

// proseValueRedacted is the prose rule's test: after an explicit credential
// word and a verb ("the password is ..."), the value is redacted unless it
// is plainly not one (valueNotASecret) or a word too short to be one.
func proseValueRedacted(value string) bool {
	v := trimValueEdges(value)
	return !valueNotASecret(v) && len(v) >= 3
}

// runsOnAsSentence says whether rest (the text after a value) continues a
// sentence: a space, then a lower-case letter.
func runsOnAsSentence(rest string) bool {
	return len(rest) >= 2 && rest[0] == ' ' && rest[1] >= 'a' && rest[1] <= 'z'
}

// valueTokenShaped says whether a value looks like a credential rather than
// a word: at least 8 bytes with no space, not a URL or a path, and holding
// a symbol a word does not (!@#$%^&*+=~\|?<>), all three of lower case,
// upper case and digits, a run of 8 or more letters and digits holding two
// digits (hunter301, AKIA...), or the case churn of base62 (three or more
// lower-to-upper or letter-digit changes). A dictionary word, a DNS name
// (kube-agents-evals-31, us-central1), an enum (PROJECT_SINGLETON_POLICY)
// and a camelCase field (httpGet) are not.
func valueTokenShaped(v string) bool {
	if len(v) < 8 || strings.ContainsAny(v, " \t\r\n") || strings.Contains(v, "://") ||
		strings.HasPrefix(v, "/") || strings.HasPrefix(v, "~/") || strings.HasPrefix(v, "./") {
		return false
	}
	if strings.ContainsAny(v, "!@#$%^&*+=~\\|?<>") && hasAlnum(v) {
		return true
	}
	var lower, upper, digit bool
	changes := 0
	prev := byte(0)
	for i := 0; i < len(v); i++ {
		c := v[i]
		switch {
		case c >= 'a' && c <= 'z':
			lower = true
			if prev == 'd' {
				changes++
			}
			prev = 'l'
		case c >= 'A' && c <= 'Z':
			upper = true
			if prev == 'l' || prev == 'd' {
				changes++
			}
			prev = 'u'
		case c >= '0' && c <= '9':
			digit = true
			if prev == 'l' || prev == 'u' {
				changes++
			}
			prev = 'd'
		default:
			prev = 0
		}
	}
	if lower && upper && digit || changes >= 3 {
		return true
	}
	for _, seg := range strings.FieldsFunc(v, func(r rune) bool { return r == '-' || r == '.' || r == ':' || r == '/' }) {
		digits, letters := 0, 0
		for i := 0; i < len(seg); i++ {
			switch c := seg[i]; {
			case c >= '0' && c <= '9':
				digits++
			case c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z':
				letters++
			}
		}
		if len(seg) >= 8 && letters > 0 && digits >= 2 {
			return true
		}
	}
	return false
}

func hasAlnum(s string) bool {
	for i := 0; i < len(s); i++ {
		if c := s[i]; c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' {
			return true
		}
	}
	return false
}

func isAllDigits(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return s != ""
}

// secretSink receives a value a shape rule redacts when the text in front
// of it names a credential outright (keyClearlyNamesSecret, a password,
// passphrase, token or api key noun) or the rule's own shape is a
// credential's (a token prefix, a URL's password, a password flag, an
// Authorization header pair), so the seen-values set can track what a
// tool's result held; nil drops them. The other rules hand it nothing: a
// value caught after an auth scheme word, a pgpass line, a YAML data block
// or a weak or ambiguous key may be an ordinary word, which the set would
// redact everywhere in the answer.
type secretSink func(value string)

func (sink secretSink) emit(v string) {
	if sink != nil && v != "" && !strings.Contains(v, Redacted) {
		sink(v)
	}
}

var (
	clearSecretKeyEndPattern   = regexp.MustCompile(`(?i)(?:^|[_.-])(?:password|passwd|passphrase|secret|token|api[_-]?key|apikey)$`)
	clearSecretCamelEndPattern = regexp.MustCompile(`[A-Za-z0-9](?:Password|Passwd|Passphrase|Secret|Token|Api[_-]?Key|APIKey)$`)
	clearSecretUpperEndPattern = regexp.MustCompile(`[A-Z0-9](?:PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN)$`)
	// secretReferenceKeyPattern is a key that names a Secret object, not a
	// credential: existingSecret, imagePullSecret, tls_secret.
	secretReferenceKeyPattern = regexp.MustCompile(`(?i)(?:existing|external|pull|tls|ref|reference|source|target|mountable|default)[_.-]?secret$`)
	clearSecretNounPattern    = regexp.MustCompile(`(?i)\b(?:password|passwd|passphrase|token|api[ _-]?key)_*\b`)
)

// keyClearlyNamesSecret says whether a key's last component is password,
// passwd, passphrase, secret, token or api key, singular: the value under
// it is a credential whatever it looks like. A *_KEY or *_ID name, a key
// carrying the word elsewhere (but for qualifiedSecretKey's digit and value
// qualifiers), the ambiguous pass, pwd and auth, a plural
// (Mountable secrets, Tokens), the bare word secret and a key naming a
// Secret object (existingSecret) do not.
func keyClearlyNamesSecret(k string) bool {
	if strings.EqualFold(k, "secret") || secretReferenceKeyPattern.MatchString(k) {
		return false
	}
	return clearSecretKeyEndPattern.MatchString(k) || clearSecretCamelEndPattern.MatchString(k) || clearSecretUpperEndPattern.MatchString(k) ||
		qualifiedSecretKey(k)
}

// inlineScrubRule is one step of ScrubInline, in order.
type inlineScrubRule struct {
	name string
	fn   func(s string, sink secretSink) string
}

// inlineScrubRules are ScrubInline's steps, in the order they run.
// The names are for tests and the evidence tooling, which attribute a
// redaction to the step that made it.
var inlineScrubRules = []inlineScrubRule{
	{"pem-private-key", func(s string, _ secretSink) string {
		s = redactSubmatch(pemPrivateKeyBlockPattern, s, 0, nil)
		return redactSubmatch(pemPrivateKeyOpenPattern, s, 0, nil)
	}},
	{"yaml-secret-block", func(s string, _ secretSink) string { return scrubYAMLSecretBlocks(s) }},
	{"token-prefix", func(s string, sink secretSink) string {
		return redactSubmatchTo(inlineTokenValuePattern, s, 0, nil, sink)
	}},
	{"auth-header-pair", func(s string, sink secretSink) string {
		return redactSubmatchTo(authHeaderPairPattern, s, 1, nil, sink)
	}},
	{"auth-scheme", func(s string, _ secretSink) string {
		return redactSubmatch(authSchemeCredentialPattern, s, 2, authSchemeCredentialRedacted)
	}},
	{"url-userinfo", func(s string, sink secretSink) string {
		return redactSubmatchTo(inlineURLCredPattern, s, 2, nil, sink)
	}},
	{"pgpass-entry", func(s string, _ secretSink) string {
		return redactSubmatch(pgpassEntryPattern, s, 4, pgpassEntryRedacted)
	}},
	{"cli-flag", scrubCLISecretFlags},
	{"prose", scrubProseSecrets},
	{"env-name-value", scrubEnvNameValuePairs},
	{"markdown-table", scrubMarkdownTables},
	{"key-value", scrubInlineKVSecrets},
}

// ScrubInline is the defence in depth on the text an executor
// publishes that a model wrote: the identifier a step title names (a
// cluster, a file's tail, a search query, a card's title), a settled card's
// report, and the answer itself (Scrub). Tool arguments, results,
// error messages and model reasoning never reach the bus, because they are
// not published at all (the executor note in the hermes-bridge's
// activity.go); a scrubber is a net, not a contract.
func ScrubInline(s string) string {
	if s == "" || s == Redacted {
		return s
	}
	return scrubInlineTo(s, nil)
}

// scrubInlineTo is ScrubInline, handing the values it redacts to sink as
// secretSink says.
func scrubInlineTo(s string, sink secretSink) string {
	if s == "" || s == Redacted {
		return s
	}
	for _, r := range inlineScrubRules {
		s = r.fn(s, sink)
	}
	return s
}

// redactSubmatch replaces group of each match of re in s with the marker,
// where keep (nil for always) says so.
func redactSubmatch(re *regexp.Regexp, s string, group int, keep func(s string, m []int) bool) string {
	return redactSubmatchTo(re, s, group, keep, nil)
}

// redactSubmatchTo is redactSubmatch, handing each value it redacts to
// sink.
func redactSubmatchTo(re *regexp.Regexp, s string, group int, keep func(s string, m []int) bool, sink secretSink) string {
	return redactNamedSubmatch(re, s, group, -1, keep, sink)
}

// redactNamedSubmatch is redactSubmatchTo handing a value to sink only
// where the key in keyGroup names a credential outright
// (keyClearlyNamesSecret); keyGroup -1 hands every value.
func redactNamedSubmatch(re *regexp.Regexp, s string, group, keyGroup int, keep func(s string, m []int) bool, sink secretSink) string {
	return redactNamedSubmatchSpan(re, s, group, keyGroup, keep, sink, false)
}

// redactNamedSubmatchSpan is redactNamedSubmatch that, with codeSpan, gives
// a code span's closing backtick back to the text (closingBacktickOutside).
// Only the command-line flag rules pass it: their value runs to whitespace,
// so it alone takes a span's closing backtick.
func redactNamedSubmatchSpan(re *regexp.Regexp, s string, group, keyGroup int, keep func(s string, m []int) bool, sink secretSink, codeSpan bool) string {
	ms := re.FindAllStringSubmatchIndex(s, -1)
	if ms == nil {
		return s
	}
	var sb strings.Builder
	last := 0
	for _, m := range ms {
		lo, hi := m[2*group], m[2*group+1]
		if lo < last || hi <= lo || s[lo:hi] == Redacted || (keep != nil && !keep(s, m)) {
			continue
		}
		if codeSpan {
			hi = closingBacktickOutside(s, lo, hi)
		}
		sb.WriteString(s[last:lo])
		sb.WriteString(Redacted)
		if keyGroup < 0 || keyClearlyNamesSecret(s[m[2*keyGroup]:m[2*keyGroup+1]]) {
			sink.emit(s[lo:hi])
		}
		last = hi
	}
	if last == 0 {
		return s
	}
	sb.WriteString(s[last:])
	return sb.String()
}

// closingPunctuation is what may follow a code span's closing backtick and
// still be handed back to the text with it (closingBacktickOutside).
const closingPunctuation = ").,;:!?]}"

// closingBacktickOutside gives the text back from the backtick that closes
// a markdown code span the value sits in: an unquoted value inside an open
// span (an odd count of backticks earlier on its line) ends at its last
// backtick, and that backtick and whatever the value rule took after it
// (closingPunctuation only) stay in the text. Everything before it is still
// redacted, a backtick inside the value included. Outside a code span, or
// for a quoted value, the span is unchanged.
func closingBacktickOutside(s string, lo, hi int) int {
	if hi-lo < 2 || s[lo] == '"' || s[lo] == '\'' {
		return hi
	}
	k := strings.LastIndexByte(s[lo:hi], '`')
	if k < 1 || strings.Trim(s[lo+k+1:hi], closingPunctuation) != "" {
		// No backtick, or one with more of the value after it than
		// closing punctuation: the value's own, redacted whole.
		return hi
	}
	// The closing delimiter is the whole run of backticks the value ends
	// in (a span may open with two, "``--password=x``"), and a span is open
	// before the value when an odd number of backtick runs precede it on
	// the line.
	for k > 0 && s[lo+k-1] == '`' {
		k--
	}
	if k < 1 {
		return hi
	}
	line := strings.LastIndexByte(s[:lo], '\n') + 1
	if backtickRuns(s[line:lo])%2 == 1 {
		return lo + k
	}
	return hi
}

// backtickRuns counts the maximal runs of backticks in s.
func backtickRuns(s string) int {
	runs := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '`' && (i == 0 || s[i-1] != '`') {
			runs++
		}
	}
	return runs
}

// authSchemeCredentialRedacted says whether a credential after a scheme
// word is redacted here: not where an Authorization key leads it on its
// line (the key=value rule takes the whole value there, a Digest's
// parameters included). After Bearer or Basic, as before this rule had
// value shapes, any credential of 8 bytes or more is redacted unless it is
// one of the dictionary words prose puts after either, in lower case or
// capitalised ("basic monitoring", "Basic Authentication",
// authSchemeProseWords): an all-letter credential is redacted. After the
// other scheme words, which are ordinary English too (a token, a digest),
// only a token-shaped one.
func authSchemeCredentialRedacted(s string, m []int) bool {
	// The lead pattern ends at the match and is short ("authorization", an
	// optional quote, a separator, spaces), so only a bounded tail of the
	// line is read: reading back to the line's start for every match made a
	// long single line (minified JSON) cost the square of its length.
	tail := s[max(0, m[0]-authHeaderLeadWindow):m[0]]
	if i := strings.LastIndexByte(tail, '\n'); i >= 0 {
		tail = tail[i+1:]
	}
	if authHeaderKVLeadPattern.MatchString(tail) {
		return false
	}
	cred := trimValueEdges(s[m[4]:m[5]])
	switch strings.ToLower(s[m[2]:m[3]]) {
	case "bearer", "basic":
		return len(cred) >= 8 && !(isPlainWord(cred) && authSchemeProseWords[strings.ToLower(cred)])
	}
	return valueTokenShaped(cred)
}

// authHeaderLeadWindow bounds how far before a scheme word the
// authorization key is looked for: far more than the key, its quotes and
// separator take.
const authHeaderLeadWindow = 256

// authSchemeProseWords are the dictionary words of eight letters or more
// that prose puts after Bearer or Basic as an adjective. A shorter word is
// never redacted there (the eight-byte floor).
var authSchemeProseWords = map[string]bool{
	"authentication": true, "authenticated": true, "authenticating": true, "authorization": true,
	"authorisation": true, "authorized": true, "credential": true, "credentials": true, "password": true,
	"passwords": true, "username": true, "usernames": true, "security": true, "protocol": true,
	"protocols": true, "challenge": true, "challenges": true, "settings": true, "configuration": true,
	"configured": true, "information": true, "knowledge": true, "understanding": true,
	"troubleshooting": true, "monitoring": true, "networking": true, "functionality": true,
	"connectivity": true, "operations": true, "operation": true, "commands": true, "workloads": true,
	"workflow": true, "workflows": true, "validation": true, "verification": true, "scheduling": true,
	"observability": true, "permissions": true, "principal": true, "principals": true, "requests": true,
	"response": true, "responses": true, "required": true, "optional": true, "enabled": true,
	"disabled": true, "supported": true, "features": true, "endpoint": true, "endpoints": true,
	"mechanism": true, "mechanisms": true, "strategy": true, "approach": true, "examples": true,
	"scenario": true, "scenarios": true, "guidance": true, "overview": true, "concepts": true,
	"metrics": true, "logging": true, "alerting": true, "policies": true, "template": true,
	"templates": true, "structure": true, "standard": true, "requirements": true, "properties": true,
	"principle": true, "principles": true, "checklist": true, "constraints": true, "privileges": true,
	"identity": true, "identities": true, "accounts": true, "resources": true, "services": true,
}

// isPlainWord says whether w is letters only, in lower case after an
// optional capital: a dictionary word's spelling, not a token's.
func isPlainWord(w string) bool {
	if w == "" {
		return false
	}
	start := 0
	if w[0] >= 'A' && w[0] <= 'Z' {
		start = 1
	}
	return start < len(w) && isLowerWord(w[start:])
}

// pgpassEntryRedacted tells a password file entry from a timestamp or an
// IPv6 address with as many colons: the host names a host (a letter or a
// dot in it, or the * wildcard, and not a timestamp's date and hour), and
// the database or user field holds something a run of hex digits does not.
func pgpassEntryRedacted(s string, m []int) bool {
	host := s[m[2]:m[3]]
	if timestampHostPattern.MatchString(host) {
		return false
	}
	if host != "*" && !strings.ContainsAny(host, ".abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") {
		return false
	}
	for _, f := range []string{s[m[4]:m[5]], s[m[6]:m[7]]} {
		for i := 0; i < len(f); i++ {
			c := f[i]
			if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F') {
				return true
			}
		}
	}
	return false
}

func scrubCLISecretFlags(s string, sink secretSink) string {
	notAFlag := func(group int) func(s string, m []int) bool {
		return func(s string, m []int) bool { return !strings.HasPrefix(s[m[2*group]:m[2*group+1]], "-") }
	}
	s = redactNamedSubmatchSpan(cliUserPassPattern, s, 4, -1, nil, sink, true)
	s = redactNamedSubmatchSpan(cliSpacedPassPattern, s, 2, -1, notAFlag(2), sink, true)
	s = redactNamedSubmatchSpan(cliAttachedPassPattern, s, 2, -1, nil, sink, true)
	s = redactNamedSubmatchSpan(cliLDAPPassPattern, s, 2, -1, notAFlag(2), sink, true)
	s = redactNamedSubmatchSpan(cliPassphraseArgPattern, s, 2, -1, nil, sink, true)
	// A flag whose name is a credential key (flagValueRedacted).
	flagValue := func(s string, m []int) bool {
		return flagValueRedacted(s[m[4]:m[5]], s[m[6]:m[7]], s[m[8]:m[9]])
	}
	// A flag's value is handed to sink only under a name that names a
	// credential outright.
	s = redactNamedSubmatchSpan(cliLongSecretFlagPattern, s, 4, 2, flagValue, sink, true)
	s = redactNamedSubmatchSpan(cliLongSecretFlagDashedPattern, s, 4, 2, flagValue, sink, true)
	s = redactNamedSubmatchSpan(cliSingleDashSecretFlagPattern, s, 4, 2, flagValue, sink, true)
	s = redactNamedSubmatchSpan(cliSingleDashSecretFlagDashedPattern, s, 4, 2, flagValue, sink, true)
	argListValue := func(s string, m []int) bool {
		return flagValueRedacted(s[m[2]:m[3]], " ", s[m[4]:m[5]])
	}
	s = redactNamedSubmatchSpan(cliArgListJSONPattern, s, 2, 1, argListValue, sink, true)
	return redactNamedSubmatchSpan(cliArgListYAMLPattern, s, 2, 1, argListValue, sink, true)
}

// flagValueRedacted says whether a flag's value is redacted: the flag's
// name is a credential key, a spaced value is not the next flag, and the
// value passes keyedValueRedacted (so --token-ttl 30m and --password-file
// /path stay, and --include-secrets followed by a line continuation does).
func flagValueRedacted(name, sep, value string) bool {
	if !inlineSecretKey(name) {
		return false
	}
	if sep != "=" && sep != ":" && strings.HasPrefix(value, "-") {
		return false
	}
	return keyedValueRedacted(name, value, false)
}

func scrubYAMLSecretBlocks(s string) string {
	if !strings.Contains(s, "data:") && !strings.Contains(s, "stringData:") {
		return s
	}
	lines := strings.Split(s, "\n")
	inSecretBlock := false
	blockIndent := 0
	for i, line := range lines {
		trimmedLine := strings.TrimRight(line, "\r")
		if m := yamlSecretBlockHeaderPattern.FindStringSubmatch(trimmedLine); m != nil {
			inSecretBlock = true
			blockIndent = len(m[1])
			continue
		}
		if !inSecretBlock {
			continue
		}
		if strings.TrimSpace(trimmedLine) == "" || strings.HasPrefix(strings.TrimSpace(trimmedLine), "#") {
			continue
		}
		indent := len(trimmedLine) - len(strings.TrimLeft(trimmedLine, " \t"))
		if indent <= blockIndent {
			inSecretBlock = false
			if m := yamlSecretBlockHeaderPattern.FindStringSubmatch(trimmedLine); m != nil {
				inSecretBlock = true
				blockIndent = len(m[1])
			}
			continue
		}
		if em := yamlBlockEntryPattern.FindStringSubmatch(trimmedLine); em != nil {
			val := strings.TrimSpace(em[2])
			if val != "" && val != "{}" && val != "[]" && val != "|" && val != ">" && val != "|-" && val != ">-" && val != Redacted {
				cr := ""
				if strings.HasSuffix(line, "\r") {
					cr = "\r"
				}
				lines[i] = em[1] + Redacted + em[3] + cr
			}
		}
	}
	return strings.Join(lines, "\n")
}

// scrubProseSecrets redacts the value of prose that states a secret
// (proseSecretPattern), as proseValueRedacted says, matched with the
// markdown around the noun and the value normalised away ("the
// **password** is `x`").
// proseMarkedAssignmentPattern is a prose label that ends in a key, a mark
// left against it, and "=": an assignment. Its group is the key.
var proseMarkedAssignmentPattern = regexp.MustCompile(`([A-Za-z0-9_.-]+)[*_]+\s*=\s*$`)

// keyValueOwnsProseLabel says whether the key/value rule takes a prose
// label's value instead: an assignment whose key it classifies as a secret
// key redacts whole there, while the prose value stops at punctuation
// (#6201). Any other label keeps the prose redaction.
func keyValueOwnsProseLabel(label string) bool {
	m := proseMarkedAssignmentPattern.FindStringSubmatch(label)
	return m != nil && inlineSecretKey(strings.TrimRight(m[1], "_"))
}

func scrubProseSecrets(s string, sink secretSink) string {
	return scrubNormalized(s, sink, func(n string, code [][2]int, rec func(lo, hi int, named bool)) {
		for _, m := range proseSecretPattern.FindAllStringSubmatchIndex(n, -1) {
			lo, hi := m[4], m[5]
			if end, ok := codeSpanEnd(code, lo); ok && n[lo:end] != Redacted && proseValueRedacted(n[lo:end]) {
				hi = end // a code-spanned value ends at its closing backtick
			}
			if keyValueOwnsProseLabel(n[m[2]:m[3]]) {
				continue
			}
			if n[lo:hi] != Redacted && proseValueRedacted(n[lo:hi]) {
				rec(lo, hi, clearSecretNounPattern.MatchString(n[m[2]:m[3]]))
			}
		}
	})
}

// scrubInlineKVSecrets redacts the value after a credential key and a = or
// :, as keyedValueRedacted says. It matches on the text with the markdown
// around keys and values normalised away (normalizeMarkdown), so a bold or
// code-spanned key, a code-spanned value, a parenthetical before the colon
// or an arrow for a separator reads as the plain pair, and it redacts the
// value where it stands in s. A bare lower-case key used as a label in the
// middle of a sentence ("rotate the token: run ...") keeps a value that is
// one plain word.
func scrubInlineKVSecrets(s string, sink secretSink) string {
	return scrubNormalized(s, sink, kvSecretSpans)
}

// kvSecretSpans reports, through rec, the span of each value
// scrubInlineKVSecrets redacts in s.
func kvSecretSpans(s string, code [][2]int, rec func(lo, hi int, named bool)) {
	pos := 0
	for pos < len(s) {
		loc := inlineKVSecretPattern.FindStringSubmatchIndex(s[pos:])
		if loc == nil {
			return
		}
		matchStart := pos + loc[0]
		matchEnd := pos + loc[1]
		openQuote := s[pos+loc[2] : pos+loc[3]]
		key := strings.TrimRight(s[pos+loc[4]:pos+loc[5]], "_")
		closeQuote := s[pos+loc[6] : pos+loc[7]]
		sep := s[pos+loc[10]]
		// Only a blank counts here, not a mark the separator group allows.
		hadPreSepSpace := strings.ContainsAny(s[pos+loc[8]:pos+loc[10]], " \t")
		hadPostSepSpace := loc[9] > loc[11]
		pos = matchEnd
		if !inlineSecretKey(key) {
			continue
		}
		rest := s[matchEnd:]
		if rest == "" || rest[0] == '\r' || rest[0] == '\n' {
			continue
		}
		if sep == '=' && !hadPreSepSpace && hadPostSepSpace && isFollowingKVAssignment(rest) {
			continue
		}
		codeEnd, coded := codeSpanEnd(code, matchEnd)
		if sloc := inlineAuthSchemePrefixPattern.FindStringSubmatchIndex(rest); sloc != nil {
			matchEnd += sloc[1]
			rest = rest[sloc[1]:]
			pos = matchEnd
		}
		lineLeading := isLineLeadingKVKey(s, matchStart)
		class := secretKeyClassOf(key)
		named := keyClearlyNamesSecret(key)
		redacts := func(v, after string) bool {
			return classValueRedacted(class, v, sep == ':' && runsOnAsSentence(after))
		}
		// A code-spanned value is bounded by its backticks, like a quoted
		// one: the span is redacted whole when it reads as the value, or
		// when its first word does (a credential and a note after it).
		if coded && codeEnd > matchEnd {
			span := s[matchEnd:codeEnd]
			first := span
			if ws := strings.IndexAny(span, " \t"); ws >= 0 {
				first = span[:ws]
			}
			if span != Redacted && (redacts(span, "") || first != span && redacts(first, "")) {
				rec(matchEnd, codeEnd, named)
				pos = codeEnd
			}
			continue
		}
		if openQuote != "" && closeQuote == "" && sep == ':' && !strings.HasPrefix(rest, `"`) && !strings.HasPrefix(rest, `'`) && !strings.HasPrefix(rest, `\"`) && !strings.HasPrefix(rest, `\'`) {
			var closeIdx int
			if len(openQuote) == 2 {
				closeIdx = findUnescapedEscapedQuote(rest, openQuote[1])
			} else {
				closeIdx = findUnescapedQuote(rest, openQuote[0])
			}
			if closeIdx >= 0 {
				if redacts(rest[:closeIdx], "") {
					rec(matchEnd, matchEnd+closeIdx, named)
					pos = matchEnd + closeIdx
				}
				continue
			}
		}
		lineEndIn := func(t string) int {
			if nl := strings.IndexAny(t, "\r\n"); nl >= 0 {
				return nl
			}
			return len(t)
		}
		switch {
		case strings.HasPrefix(rest, `\"`), strings.HasPrefix(rest, `\'`):
			q := rest[:2]
			afterOpen := rest[2:]
			closeIdx := findUnescapedEscapedQuote(afterOpen, q[1])
			end := closeIdx
			if closeIdx < 0 {
				end = lineEndIn(afterOpen)
			}
			if !redacts(afterOpen[:end], "") {
				continue
			}
			rec(matchEnd+2, matchEnd+2+end, named)
			pos = matchEnd + 2 + end
			if closeIdx >= 0 {
				pos += 2
			}
		case strings.HasPrefix(rest, `"`), strings.HasPrefix(rest, `'`):
			q := rest[0]
			afterOpen := rest[1:]
			closeIdx := findUnescapedQuote(afterOpen, q)
			end := closeIdx
			if closeIdx < 0 {
				end = lineEndIn(afterOpen)
			}
			val := afterOpen[:end]
			schemeEnd := 0
			if sloc := inlineAuthSchemePrefixPattern.FindStringSubmatchIndex(val); sloc != nil && closeIdx >= 0 {
				schemeEnd = sloc[1]
			}
			if !redacts(val[schemeEnd:], "") {
				continue
			}
			rec(matchEnd+1+schemeEnd, matchEnd+1+end, named)
			pos = matchEnd + 1 + end
			if closeIdx >= 0 {
				pos++
			}
		case strings.HasPrefix(rest, Redacted):
			pos = matchEnd + len(Redacted)
		default:
			if sep == ':' && openQuote == "" && closeQuote == "" && lineLeading {
				lineRest := strings.TrimRight(rest[:lineEndIn(rest)], " \t")
				if n, ok := firstTokenBeforeLaterPair(lineRest); ok && !strings.ContainsAny(lineRest, "{}") {
					if redacts(lineRest[:n], "") {
						rec(matchEnd, matchEnd+n, named)
						pos = matchEnd + n
					}
					continue
				}
				if lineRest != "" && !strings.ContainsAny(lineRest, "{};") && !hasLaterInlineKVAssignment(lineRest) {
					if redacts(lineRest, "") {
						rec(matchEnd, matchEnd+len(lineRest), named)
						pos = matchEnd + len(lineRest)
					}
					continue
				}
			}
			valLen := 0
			for valLen < len(rest) {
				if strings.HasPrefix(rest[valLen:], `\"`) || strings.HasPrefix(rest[valLen:], `\'`) {
					break
				}
				c := rest[valLen]
				if c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '"' || c == '\'' || c == ',' || c == ')' || c == '}' || c == ']' || c == '>' {
					break
				}
				if c == ';' && (sep == '=' || valLen+1 >= len(rest) || rest[valLen+1] == ' ' || rest[valLen+1] == '\t') {
					break
				}
				valLen++
			}
			if valLen == 0 || !redacts(rest[:valLen], rest[valLen:]) {
				continue
			}
			rec(matchEnd, matchEnd+valLen, named)
			pos = matchEnd + valLen
		}
	}
}

var (
	// envNameValueFlowPattern is a Kubernetes env entry, a name followed by
	// a literal value sibling, in JSON or a flow mapping (a comma between),
	// an unquoted value ending at , } or ]; envNameValueBlockPattern is the
	// same in block YAML (- name: X, then value: y on the next line, a
	// comment after the name allowed), an unquoted value running to its
	// line's end, comment and all, for envUnquotedValue to cut;
	// envValueNamePattern is the flow one with the value first. valueFrom
	// is not a value. Each value match ends within its own line or at a
	// flow separator, so a scan of one long line stays linear.
	envNameValueFlowPattern  = regexp.MustCompile(`(?:^|[\s{,\[-])["']?name["']?[ \t]*:[ \t]*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?[ \t]*,[ \t]*(?:\r?\n[ \t]*)?["']?value["']?[ \t]*:[ \t]*("(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*'|[^,}\]\r\n]*[^,}\]\r\n \t])`)
	envNameValueBlockPattern = regexp.MustCompile(`(?:^|[\s{,\[-])["']?name["']?[ \t]*:[ \t]*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?[ \t]*(?:[ \t]+#[^\r\n]*)?\r?\n[ \t]*["']?value["']?[ \t]*:[ \t]*("(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*'|[^\r\n]*[^\r\n \t])`)
	envValueNamePattern      = regexp.MustCompile(`(?:^|[\s{,\[-])["']?value["']?[ \t]*:[ \t]*("(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*'|[^,}\]\r\n]*[^,}\]\r\n \t])[ \t]*,[ \t]*(?:\r?\n[ \t]*)?["']?name["']?[ \t]*:[ \t]*["']?([A-Za-z_][A-Za-z0-9_.-]*)["']?`)
)

// scrubEnvNameValuePairs redacts the literal value of a Kubernetes env
// entry whose name is a credential key (DB_PASSWORD), as the name's class
// says (classValueRedacted): the name and the value are separate fields,
// so the key-value rule never links them.
func scrubEnvNameValuePairs(s string, sink secretSink) string {
	if !strings.Contains(s, "name") || !strings.Contains(s, "value") {
		return s
	}
	var spans [][2]int
	pair := func(re *regexp.Regexp, nameG, valueG int) {
		for _, m := range re.FindAllStringSubmatchIndex(s, -1) {
			name := s[m[2*nameG]:m[2*nameG+1]]
			if !inlineSecretKey(name) {
				continue
			}
			lo, hi := m[2*valueG], m[2*valueG+1]
			var decoded string
			// The value's candidate ends, the first that reads as a value
			// taken: a quoted value's closing quote; an unquoted one's
			// line end, then its first , } or ] (a note, a URL or another
			// value after it on the line).
			var ends []int
			// segs[i] is where ends[i]'s line text stops, a comment after the
			// value included: on a credential key's line a comment that
			// reads as a value is redacted with it (commentReadsAsValue).
			var segs []int
			cutAt, rest := -1, "" // an unquoted value's first , } or ], and the text after it
			if hi-lo >= 2 && (s[lo] == '"' || s[lo] == '\'') && s[hi-1] == s[lo] {
				decoded, _, _ = yamlQuoted(s[lo:hi])
				lo, hi = lo+1, hi-1
				ends = []int{hi}
				segs = []int{hi}
			} else {
				if strings.HasPrefix(s[lo:hi], "#") {
					// YAML reads no value here, but a commented-out value on
					// a credential key's line is still redacted, as a
					// line-end read redacted it. Nothing is tracked: it is
					// not the value YAML reads.
					if commentReadsAsValue(name, s[lo:hi]) {
						spans = append(spans, [2]int{lo, hi})
					}
					continue
				}
				ends = []int{lo + envUnquotedValue(s[lo:hi])}
				segs = []int{lo + len(strings.TrimRight(s[lo:hi], " \t"))}
				if cut := strings.IndexAny(s[lo:hi], ",}]"); cut >= 0 {
					ends = append(ends, lo+envUnquotedValue(s[lo:lo+cut]))
					segs = append(segs, lo+len(strings.TrimRight(s[lo:lo+cut], " \t")))
					cutAt, rest = lo+cut, s[lo+cut+1:hi]
				}
			}
			if sloc := inlineAuthSchemePrefixPattern.FindStringIndex(s[lo:hi]); sloc != nil {
				lo += sloc[1] // the scheme word stays, as under a key
			}
			var reads, readSegs []int // the candidate ends that read as a value, in order, and their line text's stops
			for i, end := range ends {
				if end > lo && !strings.Contains(s[lo:end], Redacted[:len(Redacted)-1]) && classValueRedacted(secretKeyClassOf(name), s[lo:end], false) {
					reads = append(reads, end)
					readSegs = append(readSegs, segs[i])
				}
			}
			if len(reads) == 0 {
				continue
			}
			hi = reads[0]
			v := s[lo:hi]
			redactHi := hi
			if after := strings.TrimLeft(s[hi:readSegs[0]], " \t"); strings.HasPrefix(after, "#") && commentReadsAsValue(name, after) {
				redactHi = readSegs[0]
			}
			spans = append(spans, [2]int{lo, redactHi})
			if keyClearlyNamesSecret(name) {
				sink.emit(v)
				// The value cut at its first , } or ] is tracked too when
				// it also reads as one: the line's whole text may hold a
				// note after it, which an answer leaves off.
				for _, end := range reads[1:] {
					if end != hi {
						sink.emit(s[lo:end])
					}
				}
				// A quoted value is also tracked as its escapes read
				// (a JSON or YAML "\"" is the value's own quote), past a
				// scheme word as the value itself is.
				if sloc := inlineAuthSchemePrefixPattern.FindStringIndex(decoded); sloc != nil {
					decoded = decoded[sloc[1]:]
				}
				if decoded != "" && decoded != v {
					sink.emit(decoded)
				}
			}
			if cutAt >= 0 && hi > cutAt && sink != nil {
				// The whole line went as the value: what other rules would
				// have found after the cut (", password: x", a URL's
				// credential) is still tracked, as it is when the value
				// ends at the cut.
				// In its line context ("value: <redacted>, ..."), so a key
				// after the cut is not read as starting a line of its own,
				// which would read its value to the line's end.
				scrubEnvRest("value: "+Redacted+s[cutAt:cutAt+1]+rest, sink)
			}
		}
	}
	pair(envNameValueFlowPattern, 1, 2)
	pair(envNameValueBlockPattern, 1, 2)
	pair(envValueNamePattern, 2, 1)
	return redactSpans(s, spans)
}

// scrubEnvRest is scrubInlineTo, run over the text after an env value's
// first , } or ] for what it tracks; set in init, since the rules include
// scrubEnvNameValuePairs itself.
var scrubEnvRest func(s string, sink secretSink) string

func init() { scrubEnvRest = scrubInlineTo }

// commentReadsAsValue reports whether comment, a YAML comment ("# ...") on
// credential key name's value line, reads as a value under that key: its
// text past the # is what the key's class redacts. A commented-out or
// annotated value is then redacted, as it is when the line is read to its
// end; a bare marker ("# none") is not.
func commentReadsAsValue(name, comment string) bool {
	text := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(comment), "#"))
	return text != "" && classValueRedacted(secretKeyClassOf(name), text, false)
}

// envUnquotedValue is the length of an unquoted env value with a trailing
// comment (a # after a blank, or one opening the value) and the blanks
// before it taken off.
func envUnquotedValue(v string) int {
	if strings.HasPrefix(v, "#") {
		return 0
	}
	for _, c := range []string{" #", "\t#"} {
		if i := strings.Index(v, c); i >= 0 {
			v = v[:i]
		}
	}
	return len(strings.TrimRight(v, " \t"))
}

// scrubMarkdownTables redacts the value cells of a markdown table that
// names a credential key: in a row whose first cell is one, every cell
// after it but those under a listing or description header (a row shorter
// than its header: its last cell), unless the table has a header with no
// value column (tableValueHeaderWords) whose second column's header shows
// a listing of resources or checks (| Namespace | Status | Age |) or a
// description of each key (tableHeaderIsListing); under
// a header cell that is one, that column's cells in every row below the
// delimiter row. A cell is read with its markdown normalised away
// and redacted where it stands. Only a key whose class is strong counts
// (tableSecretKeyClass); a bare credential noun heads a column of names as
// often as of values, so under one a value must be token-shaped.
//
// Rows are split twice, once with a pipe inside a paired code span read as
// cell content and once at every pipe, and the spans of both are redacted:
// backticks that pair across cells can merge columns, so neither split
// alone decides which cell is under which header.
func scrubMarkdownTables(s string, sink secretSink) string {
	if !strings.Contains(s, "|") {
		return s
	}
	spans := append(markdownTableSpans(s, sink, true), markdownTableSpans(s, sink, false)...)
	return redactSpans(s, mergeSpans(spans))
}

// mergeSpans is spans with every overlapping pair joined, so redactSpans
// skips none of a span's bytes.
func mergeSpans(spans [][2]int) [][2]int {
	if len(spans) < 2 {
		return spans
	}
	sort.Slice(spans, func(i, j int) bool { return spans[i][0] < spans[j][0] })
	out := [][2]int{spans[0]}
	for _, sp := range spans[1:] {
		if last := &out[len(out)-1]; sp[0] < last[1] {
			last[1] = max(last[1], sp[1])
			continue
		}
		out = append(out, sp)
	}
	return out
}

// markdownTableSpans is the value-cell spans scrubMarkdownTables redacts,
// with rows split as markdownTableCells does under codeAware.
func markdownTableSpans(s string, sink secretSink, codeAware bool) [][2]int {
	var spans [][2]int
	type row struct{ cells [][2]int }
	var block []row
	flush := func() {
		if len(block) == 0 {
			return
		}
		cols := map[int]secretKeyClass{}
		start, firstCellKeys := 0, true
		var valueCols []int // nil: every cell after the first
		if len(block) >= 2 && markdownDelimiterRow(s, block[1].cells) {
			for c, cell := range block[0].cells {
				if class, ok := tableSecretKeyClass(s[cell[0]:cell[1]]); ok {
					cols[c] = class
				}
			}
			if h := block[0].cells; kubectlSecretListingHeader(s, h) && kubectlSecretListingRows(s, func() (cells [][][2]int) {
				for _, r := range block[2:] {
					cells = append(cells, r.cells)
				}
				return cells
			}()) {
				// `kubectl get secrets` as a table lists Secrets, never their
				// values: its DATA is a key count. Only when every row reads
				// as kubectl's own, so the header alone can't carry a value
				// past the scrub.
				block = block[:0]
				return
			}
			if h := block[0].cells; len(h) >= 2 {
				hasValue := false
				for c := 1; c < len(h); c++ {
					switch {
					case tableHeaderIsValue(s, h[c]):
						hasValue = true
						valueCols = append(valueCols, c)
					case !tableHeaderIsListing(s, h[c]):
						valueCols = append(valueCols, c)
					}
				}
				if !hasValue {
					firstCellKeys = !tableHeaderIsListing(s, h[1])
				}
			}
			start = 2
		}
		for _, r := range block[start:] {
			if markdownDelimiterRow(s, r.cells) {
				continue
			}
			if len(r.cells) >= 2 && firstCellKeys {
				if class, ok := tableSecretKeyClass(s[r.cells[0][0]:r.cells[0][1]]); ok {
					named := tableKeyNamed(s, r.cells[0])
					cols := valueCols
					if cols == nil {
						for c := 1; c < len(r.cells); c++ {
							cols = append(cols, c)
						}
					}
					short := true
					for _, c := range cols {
						if c < len(r.cells) {
							short = false
							spans = append(spans, tableCellSpans(s, r.cells[c], class, named, sink)...)
						}
					}
					if short { // a row shorter than its header: its last cell
						spans = append(spans, tableCellSpans(s, r.cells[len(r.cells)-1], class, named, sink)...)
					}
				}
			}
			for c, class := range cols {
				if c < len(r.cells) {
					spans = append(spans, tableCellSpans(s, r.cells[c], class, tableKeyNamed(s, block[0].cells[c]), sink)...)
				}
			}
		}
		block = block[:0]
	}
	for lineStart := 0; lineStart < len(s); {
		lineEnd := strings.IndexByte(s[lineStart:], '\n')
		if lineEnd < 0 {
			lineEnd = len(s)
		} else {
			lineEnd += lineStart
		}
		if cells := markdownTableCells(s, lineStart, lineEnd, codeAware); cells != nil {
			block = append(block, row{cells})
		} else {
			flush()
		}
		lineStart = lineEnd + 1
	}
	flush()
	return spans
}

// markdownTableCells is the content span of each cell of a table row (a
// line that opens with a pipe and holds another), trimmed of spaces, or nil
// for a line that is not one. A pipe escaped with a backslash is content.
func markdownTableCells(s string, lo, hi int, codeAware bool) [][2]int {
	line := strings.TrimRight(s[lo:hi], "\r")
	trimmed := strings.TrimLeft(line, " \t")
	if !strings.HasPrefix(trimmed, "|") || strings.Count(trimmed, "|") < 2 {
		return nil
	}
	// Under codeAware, a pipe inside a paired code span is the cell's
	// content: the answer is published as written, so a value it pushes
	// past is still there.
	var inSpan []bool
	if codeAware {
		inSpan = make([]bool, len(line))
		for _, c := range markPairedMarkdownRuns(line, 0, len(line), make([]bool, len(line)), nil) {
			for i := c[0]; i < c[1]; i++ {
				inSpan[i] = true
			}
		}
	}
	inCode := func(i int) bool { return inSpan != nil && inSpan[i] }
	var pipes []int
	for i := 0; i < len(line); i++ {
		if line[i] == '\\' {
			i++
			continue
		}
		if line[i] == '|' && !inCode(i) {
			pipes = append(pipes, lo+i)
		}
	}
	end := lo + len(line)
	if strings.TrimSpace(s[pipes[len(pipes)-1]+1:end]) != "" {
		pipes = append(pipes, end)
	}
	var cells [][2]int
	for k := 0; k+1 < len(pipes); k++ {
		a, b := pipes[k]+1, pipes[k+1]
		for a < b && (s[a] == ' ' || s[a] == '\t') {
			a++
		}
		for b > a && (s[b-1] == ' ' || s[b-1] == '\t') {
			b--
		}
		cells = append(cells, [2]int{a, b})
	}
	return cells
}

var markdownDelimiterCellPattern = regexp.MustCompile(`^:?-+:?$`)

func markdownDelimiterRow(s string, cells [][2]int) bool {
	for _, c := range cells {
		if !markdownDelimiterCellPattern.MatchString(s[c[0]:c[1]]) {
			return false
		}
	}
	return len(cells) > 0
}

var (
	// tableValueHeaderWords name a column of values: a table with one is a
	// key/value table whatever its other headers say.
	tableValueHeaderWords = map[string]bool{
		"value": true, "values": true, "decoded": true, "content": true, "contents": true,
		"plaintext": true, "secret": true, "password": true, "credential": true, "encoded": true,
		"base64": true, "b64": true, "data": true,
	}
	// tableListingHeaderWords are header words of a listing whose first
	// column names resources or checks, not keys (| Namespace | Status |
	// Age |, | Check | Status | Detail |), and of a table whose second
	// column describes the key in the first (| Field | Meaning |).
	tableListingHeaderWords = map[string]bool{
		"status": true, "state": true, "phase": true, "age": true, "namespace": true, "namespaces": true,
		"ready": true, "restarts": true, "node": true, "nodes": true, "check": true, "checks": true,
		"result": true, "severity": true, "cluster": true, "clusters": true, "created": true, "health": true,
		"finding": true, "findings": true, "pod": true, "pods": true, "zone": true, "region": true,
		"description": true, "meaning": true, "purpose": true, "notes": true, "note": true, "usage": true,
		"explanation": true, "comment": true, "comments": true,
	}
	tableKeyCellPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]*(?: [A-Za-z0-9_.-]+){0,3}$`)
	// bareCredentialNouns head a table column of names as often as one of
	// values (| Secret | Namespace |).
	// nonCredentialQualifiers are words that, before a bare credential
	// noun, name a feature or a setting, not a credential.
	nonCredentialQualifiers = map[string]bool{
		"binary": true, "rbac": true, "rotation": true, "type": true, "name": true, "count": true,
		"ttl": true, "expiry": true, "header": true, "scheme": true, "mode": true, "policy": true,
	}
	bareCredentialNouns = map[string]bool{
		"secret": true, "secrets": true, "token": true, "tokens": true, "credential": true,
		"credentials": true, "key": true, "keys": true, "cookie": true, "authorization": true,
	}
)

// tableHeaderWords is a header cell's words, the markdown normalised away,
// in lower case: split on anything but a letter or a digit (Base64, B64),
// and each word with a digit in it again with its digits taken as
// separators too (Value1, Password2).
func tableHeaderWords(s string, cell [2]int) []string {
	n, _ := normalizeMarkdown(s[cell[0]:cell[1]])
	lower := strings.ToLower(n)
	isLetter := func(r rune) bool { return r >= 'a' && r <= 'z' }
	words := strings.FieldsFunc(lower, func(r rune) bool { return !isLetter(r) && !(r >= '0' && r <= '9') })
	for _, w := range words {
		if strings.IndexFunc(w, func(r rune) bool { return !isLetter(r) }) >= 0 {
			words = append(words, strings.FieldsFunc(w, func(r rune) bool { return !isLetter(r) })...)
		}
	}
	return words
}

// kubectlSecretListingHeader says whether a table's header is kubectl's
// listing of Secrets, NAME | TYPE | DATA | AGE, in any case and markdown.
func kubectlSecretListingHeader(s string, h [][2]int) bool {
	want := []string{"name", "type", "data", "age"}
	if len(h) != len(want) {
		return false
	}
	for i, cell := range h {
		if words := tableHeaderWords(s, cell); len(words) != 1 || words[0] != want[i] {
			return false
		}
	}
	return true
}

// kubectlSecretListingRows says whether every row reads as kubectl's
// listing of Secrets: a name, a Secret type (one token, no spaces), a whole
// number of keys, and an age (5d, 3h12m, <unknown>).
func kubectlSecretListingRows(s string, rows [][][2]int) bool {
	for _, r := range rows {
		if len(r) != 4 {
			return false
		}
		cell := func(i int) string { n, _ := normalizeMarkdown(s[r[i][0]:r[i][1]]); return strings.TrimSpace(n) }
		if !kubectlSecretTypePattern.MatchString(cell(1)) || !isAllDigits(cell(2)) || cell(2) == "" || !kubectlAgePattern.MatchString(cell(3)) {
			return false
		}
	}
	return true
}

var (
	kubectlSecretTypePattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9._/-]*$`)
	kubectlAgePattern        = regexp.MustCompile(`^(?:(?:\d+[smhdy])+|<unknown>|<invalid>)$`)
)

// tableHeaderIsValue says whether a header names a column of values
// (tableValueHeaderWords).
func tableHeaderIsValue(s string, cell [2]int) bool {
	for _, w := range tableHeaderWords(s, cell) {
		if tableValueHeaderWords[w] {
			return true
		}
	}
	return false
}

// tableHeaderIsListing says whether the second column's header shows a
// listing or a description (tableListingHeaderWords).
func tableHeaderIsListing(s string, cell [2]int) bool {
	for _, w := range tableHeaderWords(s, cell) {
		if tableListingHeaderWords[w] {
			return true
		}
	}
	return false
}

// tableSecretKeyClass says whether a table cell (a header or a row's first
// cell) names a credential key, and the value policy under it: a key of
// the strong class (Password, DB password, Client Secret, API key), but a
// bare credential noun (Secret, Token), alone or after a word that names
// something else (nonCredentialQualifiers: Binary Authorization), only as
// the other class. Words joined by spaces read as one key.
func tableSecretKeyClass(cell string) (secretKeyClass, bool) {
	n, _ := normalizeMarkdown(cell)
	n = strings.TrimSpace(strings.TrimRight(strings.TrimSpace(n), ":"))
	if !tableKeyCellPattern.MatchString(n) {
		return 0, false
	}
	key := strings.ReplaceAll(n, " ", "_")
	if !inlineSecretKey(key) || secretKeyClassOf(key) != strongSecretKeyClass {
		return 0, false
	}
	words := strings.Fields(strings.ToLower(n))
	if bareCredentialNouns[words[len(words)-1]] {
		qualified := false
		for _, w := range words[:len(words)-1] {
			if !nonCredentialQualifiers[w] {
				qualified = true
			}
		}
		if !qualified {
			return otherSecretKey, true
		}
	}
	return strongSecretKeyClass, true
}

// tableCellSpans is the span to redact in one value cell, its markdown
// normalised away for the test and kept around the marker, or none.
func tableCellSpans(s string, cell [2]int, class secretKeyClass, named bool, sink secretSink) [][2]int {
	text := s[cell[0]:cell[1]]
	n, idx, code := normalizeMarkdownSpans(text)
	cellLo := len(n) - len(strings.TrimLeft(n, " \t"))
	cellHi := len(strings.TrimRight(n, " \t"))
	if cellHi <= cellLo || strings.Contains(n, Redacted) {
		return nil
	}
	// As under a key: an auth-scheme word stays, and a code-spanned value
	// is bounded by its backticks. Both the span and the whole cell are
	// candidates, and the first that reads as a value is redacted: the span
	// first when a blank or the cell's end follows it (a note after it),
	// the whole cell first when text runs on straight after it.
	valueLo := cellLo
	if sloc := inlineAuthSchemePrefixPattern.FindStringIndex(n[cellLo:cellHi]); sloc != nil {
		valueLo += sloc[1]
	}
	cands := [][2]int{{valueLo, cellHi}, {cellLo, cellHi}}
	if end, ok := codeSpanEnd(code, valueLo); ok {
		span := [2]int{valueLo, end}
		// Another code span after it in the cell: the value is not the
		// first span alone.
		// The whole cell is read past the scheme word first, and with it
		// only as a last resort, so a scheme word never joins the value.
		// A note after it is left only when no word of it may be a value
		// (tableNoteAfterSpan); else the rest of the cell goes with it.
		// A span that ends at or past the cell's trimmed end (its content
		// may end in a blank) has nothing after it: the slice is not taken.
		if len(code) == 1 && (end >= cellHi || (n[end] == ' ' || n[end] == '\t') && tableNoteAfterSpan(class, n[end:cellHi])) {
			cands = [][2]int{span, {valueLo, cellHi}, {cellLo, cellHi}}
		} else {
			cands = [][2]int{{valueLo, cellHi}, span, {cellLo, cellHi}}
		}
	}
	lo, hi := 0, 0
	for _, c := range cands {
		if c[1] > c[0] && classValueRedacted(class, n[c[0]:c[1]], false) {
			lo, hi = c[0], c[1]
			break
		}
	}
	if hi <= lo {
		return nil
	}
	value := n[lo:hi]
	// After an auth-scheme word, text plainly not a value, or a word that
	// names the scheme's own use (Basic authentication), is prose: not
	// tracked. Anything else there is.
	track := named && (lo < valueLo || valueLo == cellLo || !schemeProse(value))
	if idx != nil {
		lo, hi = idx[lo], idx[hi-1]+1
	}
	if track {
		// As written, and with its markdown taken out when that differs
		// (a code span inside the value): an answer may echo either.
		sink.emit(text[lo:hi])
		if value != text[lo:hi] {
			sink.emit(value)
		}
	}
	return [][2]int{{cell[0] + lo, cell[0] + hi}}
}

// schemeUseWords are the words that follow an auth-scheme word in prose
// about the scheme rather than in front of a credential ("Basic
// authentication", "Bearer token").
var schemeUseWords = map[string]bool{
	"auth": true, "authentication": true, "authorization": true, "authorisation": true,
	"credential": true, "credentials": true, "token": true, "tokens": true,
	"scheme": true, "header": true, "headers": true, "login": true,
}

// schemeProse says whether v, the text after an auth-scheme word, is
// plainly not a value (valueNotASecret) or one of schemeUseWords.
func schemeProse(v string) bool {
	v = trimValueEdges(v)
	return valueNotASecret(v) || schemeUseWords[strings.ToLower(v)]
}

// tableNoteAfterSpan says whether rest, the text of a table cell after
// the code span holding its value, is a note that may stay: no word of it
// token-shaped and, under a key of the strong class (where every other
// word reads as a value), every word plainly not a value (valueNotASecret:
// "(rotated)", "/"). Anything else may be a second value or the value
// itself (`appuser` synthtoken0001abcd).
func tableNoteAfterSpan(class secretKeyClass, rest string) bool {
	for _, w := range strings.Fields(rest) {
		w = trimValueEdges(strings.TrimLeft(trimValueEdges(w), "([{"))
		if valueTokenShaped(w) || class == strongSecretKeyClass && !tableNoteWordUnderStrongKey(w) {
			return false
		}
	}
	return true
}

// tableNoteWordUnderStrongKey says whether a word after a code span, under a
// strong key, is plainly a note: a state word ("rotated", "(none)") or one
// with no letter or digit ("/", "-"). A short number or a placeholder, which
// valueNotASecret lets pass elsewhere, may be the rest of the value here.
func tableNoteWordUnderStrongKey(w string) bool {
	lower := strings.ToLower(w)
	if credentialStateWords[lower] || credentialWholeValueWords[lower] || plainHyphenatedStatePhrase(w) {
		return true
	}
	return !strings.ContainsFunc(w, func(r rune) bool { return unicode.IsLetter(r) || unicode.IsDigit(r) })
}

// tableKeyNamed says whether a table's key cell names a credential
// outright (keyClearlyNamesSecret), its words read as one key.
func tableKeyNamed(s string, cell [2]int) bool {
	n, _ := normalizeMarkdown(s[cell[0]:cell[1]])
	return keyClearlyNamesSecret(strings.ReplaceAll(strings.TrimSpace(strings.TrimRight(strings.TrimSpace(n), ":")), " ", "_"))
}

// normalizeMarkdown is s with the markdown that wraps a key or a value in
// an answer taken out, for matching only: emphasis runs (* or _, not inside
// a word) and code-span backticks, each only where a run of the same mark
// and length pairs with it on the line, a short parenthetical between a
// word and its colon ("Password (decoded):"), and an arrow used as a
// separator, read as a colon. A lone mark stays, since it may be part of a
// value. idx maps each byte of the result to its byte in s; it is nil when
// nothing changed.
func normalizeMarkdown(s string) (string, []int) {
	n, idx, _ := normalizeMarkdownSpans(s)
	return n, idx
}

// normalizeMarkdownSpans is normalizeMarkdown, also saying where each code
// span's content stands in the result (code, in order), so a value read
// there is bounded by its backticks.
func normalizeMarkdownSpans(s string) (string, []int, [][2]int) {
	if !strings.ContainsAny(s, "*_`(\u2192\u21d2") && !strings.Contains(s, "->") && !strings.Contains(s, "=>") {
		return s, nil, nil
	}
	drop := make([]bool, len(s))
	colon := make([]bool, len(s))
	var code [][2]int // in s
	for lineStart := 0; lineStart <= len(s); {
		lineEnd := strings.IndexByte(s[lineStart:], '\n')
		if lineEnd < 0 {
			lineEnd = len(s)
		} else {
			lineEnd += lineStart
		}
		code = markPairedMarkdownRuns(s, lineStart, lineEnd, drop, code)
		lineStart = lineEnd + 1
	}
	for _, m := range markdownKeyParenPattern.FindAllStringSubmatchIndex(s, -1) {
		// The text after the colon, to the cell or line end, read but not
		// consumed: a later parenthetical label on the line is matched too.
		after := s[m[1]:]
		if end := strings.IndexAny(after, "|\r\n"); end >= 0 {
			after = after[:end]
		}
		if markdownParenKept(s[m[4]:m[5]], after) {
			continue
		}
		if m[2] > 0 && (isWordByte(s[m[2]-1]) || strings.IndexByte("*_`", s[m[2]-1]) >= 0) {
			for i := m[2]; i < m[3]; i++ {
				drop[i] = true
			}
		}
	}
	for _, m := range markdownArrowPattern.FindAllStringSubmatchIndex(s, -1) {
		colon[m[2]] = true
		for i := m[2] + 1; i < m[3]; i++ {
			drop[i] = true
		}
	}
	var sb strings.Builder
	idx := make([]int, 0, len(s))
	at := make([]int, len(s)+1) // each byte of s's place in the result
	changed := false
	for i := 0; i < len(s); i++ {
		at[i] = sb.Len()
		switch {
		case drop[i]:
			changed = true
			continue
		case colon[i]:
			changed = true
			sb.WriteByte(':')
		default:
			sb.WriteByte(s[i])
		}
		idx = append(idx, i)
	}
	if !changed {
		return s, nil, nil
	}
	at[len(s)] = sb.Len()
	for k := range code {
		code[k] = [2]int{at[code[k][0]], at[code[k][1]]}
	}
	return sb.String(), idx, code
}

// codeSpanEnd is where the code span whose content starts at p ends, when
// one does.
func codeSpanEnd(code [][2]int, p int) (int, bool) {
	k := sort.Search(len(code), func(k int) bool { return code[k][0] >= p })
	if k < len(code) && code[k][0] == p && code[k][1] > p {
		return code[k][1], true
	}
	return 0, false
}

var (
	// markdownKeyParenPattern is a short parenthetical before a colon
	// ("Password (decoded):", "**password** (prod):"), emphasis or a code
	// span allowed between. normalizeMarkdown keeps one only in the
	// non-secret shapes markdownParenKept names.
	markdownKeyParenPattern = regexp.MustCompile(`([ \t]*\(([^()\r\n:]{1,40})\))[*_\x60 \t]*:`)
	// markdownDecodedParenPattern is a parenthetical that says how the
	// value is written: never kept.
	markdownDecodedParenPattern = regexp.MustCompile(`(?i)\b(?:decoded|encoded|plain[ -]?text|base64|b64|raw|literal)\b`)
	// markdownTallyPattern is a parenthetical that counts ("5 major, 25
	// minor").
	markdownTallyPattern = regexp.MustCompile(`^[0-9]+ [A-Za-z]+(?:, ?[0-9]+ [A-Za-z]+)*$`)
	// markdownArrowPattern is an arrow used as a key's separator, spaced or
	// after a closing quote, code span or emphasis.
	markdownArrowPattern = regexp.MustCompile("[ \\t\"'`*](\u2192|\u21d2|->|=>)[ \\t]")
)

// statusWords are the status a label reports in a check list or a
// listing, not a value: a parenthetical holding one ("Binary Authorization
// (WARN):") notes the label, and one after the colon is the label's
// status.
var statusWords = map[string]bool{
	"pass": true, "passed": true, "fail": true, "failed": true, "warn": true, "warning": true, "ok": true,
	"error": true, "info": true, "critical": true, "high": true, "medium": true, "low": true, "none": true,
	"unknown": true, "enabled": true, "disabled": true, "active": true, "inactive": true, "ready": true,
	"running": true, "pending": true, "true": true, "false": true, "yes": true, "no": true, "on": true,
	"off": true, "n/a": true, "skip": true, "skipped": true,
}

// markdownParenKept says whether a parenthetical before a colon stays when
// normalizeMarkdown reads the text: its content (marks taken off) is a
// status word or a tally, or the whole rest of the line or table cell
// after the colon, trimmed and in lower case, is exactly a status word. A
// parenthetical saying how the value is written
// (decoded, base64, plaintext) is never kept. Any other is dropped, so the
// label reads as the key.
func markdownParenKept(content, after string) bool {
	if markdownDecodedParenPattern.MatchString(content) {
		return false
	}
	c := strings.ToLower(strings.TrimSpace(strings.Trim(strings.TrimSpace(content), "*_`")))
	c = strings.ReplaceAll(strings.ReplaceAll(c, "`", ""), "*", "")
	a := strings.ToLower(strings.Trim(strings.TrimSpace(after), "*_` \t."))
	return statusWords[c] || markdownTallyPattern.MatchString(c) || statusWords[a]
}

// markPairedMarkdownRuns marks in drop the code-span and emphasis runs of
// s[lo:hi] (one line) that pair, and appends each code span's content to
// code. A run of backticks opens a code span the next run of the same
// length closes; what is inside one is literal. Outside them, a run of * or
// _ not inside a word is dropped when a later run of the same mark and
// length closes it.
func markPairedMarkdownRuns(s string, lo, hi int, drop []bool, code [][2]int) [][2]int {
	inCode := func(int) bool { return false }
	first := len(code)
	for i := lo; i < hi; {
		if s[i] != '`' {
			i++
			continue
		}
		j := i
		for j < hi && s[j] == '`' {
			j++
		}
		n := j - i
		closeAt := -1
		for k := j; k < hi; {
			if s[k] != '`' {
				k++
				continue
			}
			m := k
			for m < hi && s[m] == '`' {
				m++
			}
			if m-k == n {
				closeAt = k
				break
			}
			k = m
		}
		if closeAt < 0 {
			i = j // a lone run stays: it may be part of a value
			continue
		}
		for x := i; x < j; x++ {
			drop[x] = true
		}
		for x := closeAt; x < closeAt+n; x++ {
			drop[x] = true
		}
		code = append(code, [2]int{j, closeAt})
		i = closeAt + n
	}
	if spans := code[first:]; len(spans) > 0 {
		inCode = func(i int) bool {
			for _, sp := range spans {
				if i >= sp[0]-1 && i <= sp[1] {
					return true
				}
			}
			return false
		}
	}
	type run struct {
		at, n int
		c     byte
	}
	var open []run
	for i := lo; i < hi; {
		c := s[i]
		if c != '*' && c != '_' || inCode(i) {
			i++
			continue
		}
		j := i
		for j < hi && s[j] == c {
			j++
		}
		r := run{i, j - i, c}
		i = j
		if c != '`' && r.at > lo && j < hi && isWordByte(s[r.at-1]) && isWordByte(s[j]) {
			continue // inside a word: DB_PASSWORD, a*b
		}
		k := len(open) - 1
		for k >= 0 && (open[k].c != r.c || open[k].n != r.n) {
			k--
		}
		if k < 0 {
			open = append(open, r)
			continue
		}
		for _, p := range []run{open[k], r} {
			for x := p.at; x < p.at+p.n; x++ {
				drop[x] = true
			}
		}
		open = open[:k]
	}
	return code
}

func isWordByte(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// scrubNormalized runs find over s with its markdown normalised away
// (normalizeMarkdown) and redacts each span find reports in s itself, so
// the markdown around a redacted value stays.
func scrubNormalized(s string, sink secretSink, find func(n string, code [][2]int, rec func(lo, hi int, named bool))) string {
	n, idx, code := normalizeMarkdownSpans(s)
	var spans [][2]int
	find(n, code, func(lo, hi int, named bool) {
		if hi <= lo {
			return
		}
		if idx != nil {
			lo, hi = idx[lo], idx[hi-1]+1
		}
		spans = append(spans, [2]int{lo, hi})
		if named {
			sink.emit(s[lo:hi])
		}
	})
	return redactSpans(s, spans)
}

// redactSpans replaces each span of s with the marker, a span that
// overlaps an earlier one skipped.
func redactSpans(s string, spans [][2]int) string {
	if len(spans) == 0 {
		return s
	}
	sort.Slice(spans, func(i, j int) bool { return spans[i][0] < spans[j][0] })
	var sb strings.Builder
	last := 0
	for _, sp := range spans {
		if sp[0] < last || s[sp[0]:sp[1]] == Redacted {
			continue
		}
		sb.WriteString(s[last:sp[0]])
		sb.WriteString(Redacted)
		last = sp[1]
	}
	sb.WriteString(s[last:])
	return sb.String()
}

// isLowerWord says whether k is one lower-case word, no separator.
func isLowerWord(k string) bool {
	for i := 0; i < len(k); i++ {
		if k[i] < 'a' || k[i] > 'z' {
			return false
		}
	}
	return k != ""
}

func isLineLeadingKVKey(s string, matchStart int) bool {
	lineStart := 0
	if nl := strings.LastIndexByte(s[:matchStart], '\n'); nl >= 0 {
		lineStart = nl + 1
	}
	prefix := strings.TrimLeft(s[lineStart:matchStart], " \t")
	return prefix == "" || prefix == "-"
}

// inlineKVLaterKeyPattern is inlineKVSecretPattern without marks before the
// separator. A later key that ends a value is looked for with it, so a
// passphrase holding "secret*:" stays one value.
var inlineKVLaterKeyPattern = regexp.MustCompile(`((?:\\?["'])?)([A-Za-z0-9_.-]+)((?:\\?["'])?)([ \t]*([=:])[ \t]*)`)

func hasLaterInlineKVAssignment(lineRest string) bool {
	ws := strings.IndexAny(lineRest, " \t")
	if ws < 0 {
		return false
	}
	afterFirstToken := strings.TrimLeft(lineRest[ws:], " \t")
	if loc := inlineKVLaterKeyPattern.FindStringSubmatchIndex(afterFirstToken); loc != nil {
		return inlineSecretKey(afterFirstToken[loc[4]:loc[5]])
	}
	return false
}

// laterPairPattern is a pair straight after a value's first token: an
// unquoted key starting with a letter, a colon, and the key's own value.
var laterPairPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]*[ \t]*:(.*)$`)

// firstTokenBeforeLaterPair is the length of a line-leading key's value
// when a pair of any kind follows the value's first token on the line
// ("password: x, host: y"): that token, a pair separator after it included,
// so the value is redacted and tracked whole up to the next key. Only when
// the token, its separator aside, holds none of the bytes the value scanner
// stops at, and the later key's value is one token or one ending in a pair
// separator: a passphrase of several words with a colon in it reads as
// one value.
func firstTokenBeforeLaterPair(lineRest string) (int, bool) {
	ws := strings.IndexAny(lineRest, " \t")
	if ws <= 0 {
		return 0, false
	}
	tok := lineRest[:ws]
	if v := strings.TrimSuffix(strings.TrimSuffix(tok, ","), ";"); v == "" || strings.ContainsAny(v, `"',;)}]>`) {
		return 0, false
	}
	m := laterPairPattern.FindStringSubmatch(strings.TrimLeft(lineRest[ws:], " \t"))
	if m == nil {
		return 0, false
	}
	f := strings.Fields(m[1])
	if len(f) == 0 || len(f) > 1 && !strings.HasSuffix(f[0], ",") && !strings.HasSuffix(f[0], ";") {
		return 0, false
	}
	return ws, true
}

func isFollowingKVAssignment(rest string) bool {
	if base64PaddingTokenPattern.MatchString(rest) {
		return false
	}
	m := leadingEqAssignmentPattern.FindStringSubmatch(rest)
	if len(m) < 2 {
		return false
	}
	candKey := m[1]
	return inlineSecretKey(candKey) || allCapsEnvKeyPattern.MatchString(candKey)
}

func findUnescapedQuote(s string, q byte) int {
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' {
			i++
			continue
		}
		if s[i] == q {
			return i
		}
	}
	return -1
}

func findUnescapedEscapedQuote(s string, q byte) int {
	for i := 1; i < len(s); i++ {
		if s[i] != q || s[i-1] != '\\' {
			continue
		}
		bs := 0
		for j := i - 1; j >= 0 && s[j] == '\\'; j-- {
			bs++
		}
		if bs%4 == 1 {
			return i - 1
		}
	}
	return -1
}
