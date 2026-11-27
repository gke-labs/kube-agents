"""The shared token-shape list refuses and redacts every shape its two callers rely on.

Run: python3 -m unittest test_credential_shapes   (from agents/platform/scripts)
"""

from __future__ import annotations

import json
import time
import tracemalloc
import unittest

import credential_shapes as cs

TOKENS = {
    "a GitHub token": "ghp_" + "a" * 36,
    "a fine-grained GitHub token": "github_pat_11ABCDEFG0123456789abcdefGHIJ",
    "a Google API key": "AIza" + "A" * 35,
    "a Google OAuth token": "ya29.a0AfB_" + "x" * 40,
    "an AWS access key id": "AKIA" + "A" * 16,
    "a provider API key": "sk-" + "a" * 24,
    "a Slack token": "xoxb-1234567890-abcdefghijkl",
    "an authorization header value": "Bearer abc123def456ghi789jkl0",
    "a signed JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    "a password in a URL": "https://user:s3cretpass@example.com/repo.git",
    "private key material": "-----BEGIN RSA PRIVATE KEY-----",
}
PREFIXED_PROVIDER_KEY = "sk-ant-api03-" + "Ab1" * 12
# The secret part of the URL token: its scheme, user and host are not.
URL_PASSWORD = "s3cretpass"
# Each shape's literal lead in TOKENS: what the cut-tail pass keeps when fewer
# than CUT_BODY_MIN_CHARS characters of the body follow it.
CUT_LEADS = {
    "a GitHub token": "ghp_",
    "a fine-grained GitHub token": "github_pat_",
    "a Google API key": "AIza",
    "a Google OAuth token": "ya29.",
    "an AWS access key id": "AKIA",
    "a provider API key": "sk-",
    "a prefixed provider key": "sk-",
    "a Slack token": "xoxb-",
    "an authorization header value": "Bearer ",
    "a signed JWT": "eyJ",
}
BENIGN = (
    "auth failed with 401 and the relay retried",
    "I followed the basic troubleshooting steps and the agent hung",
    "Bearer authentication against the relay returns 401",
    "basic configuration of the installation steps",
    "Basic auth is on for the dashboard",
    "the bearer of bad news",
    "sk-short",
    "ghp_tooShort",
    "https://example.com/path:8080/x",
    "AKIAI is not a key",
)


class CredentialShapesTest(unittest.TestCase):
    def test_every_shape_is_found_and_named(self) -> None:
        for name, token in TOKENS.items():
            with self.subTest(name=name):
                self.assertEqual(name, cs.find_credential_shape(f"log line: {token} then 401"))

    def test_the_prefixed_provider_key_is_found(self) -> None:
        self.assertEqual("a provider API key", cs.find_credential_shape(f"auth failed for {PREFIXED_PROVIDER_KEY}"))

    def test_benign_text_is_not_a_shape(self) -> None:
        for text in BENIGN:
            with self.subTest(text=text):
                self.assertIsNone(cs.find_credential_shape(text))

    def test_the_redactor_still_masks_a_letters_only_header_value(self) -> None:
        # Over-masking a word in a log line is cheap; refusing a report for it
        # is not, so the two forms differ on purpose.
        self.assertIn(cs.REDACTED, cs.redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz"))
        self.assertIsNone(cs.find_credential_shape("Bearer abcdefghijklmnopqrstuvwxyz"))

    def test_redact_blanks_every_shape_and_keeps_the_rest(self) -> None:
        line = "relay said " + " and ".join(TOKENS.values()) + " at 12:00"
        out = cs.redact(line)
        for token in TOKENS.values():
            self.assertNotIn(token, out)
        self.assertTrue(out.startswith("relay said ") and out.endswith(" at 12:00"))
        self.assertIn(cs.REDACTED, out)


    def test_redact_counted_counts_each_replacement(self) -> None:
        line = f"a {TOKENS['a GitHub token']} b {TOKENS['a Slack token']} c"
        self.assertEqual((f"a {cs.REDACTED} b {cs.REDACTED} c", 2), cs.redact_counted(line))
        self.assertEqual(("nothing to see", 0), cs.redact_counted("nothing to see"))

    def test_a_token_cut_at_any_length_leaves_no_fragment(self) -> None:
        # What a byte cap leaves of a token at the end of a text: the whole
        # pass, then the cut-tail pass. A lead with CUT_BODY_MIN_CHARS or more
        # of its body after it goes; with fewer it is kept, so no run of
        # CUT_BODY_MIN_CHARS characters of the body survives at any length.
        for name, token in {**TOKENS, "a prefixed provider key": PREFIXED_PROVIDER_KEY}.items():
            if name == "private key material":
                continue  # an armour line, not key material
            lead = CUT_LEADS.get(name, "")
            body = URL_PASSWORD if name == "a password in a URL" else token[len(lead):]
            for kept in range(1, len(token) + 1):
                cut = "value: " + token[:kept]
                with self.subTest(name=name, kept=kept):
                    out, _ = cs.redact_tool_output_counted(cut)
                    out, _ = cs.redact_cut_tail(out)
                    tail = out[len("value: "):]
                    tail = tail[len(lead):] if tail.startswith(lead) else tail
                    survivors = [body[i:i + cs.CUT_BODY_MIN_CHARS] for i in range(len(body) - cs.CUT_BODY_MIN_CHARS + 1)
                                 if body[i:i + cs.CUT_BODY_MIN_CHARS] in tail]
                    self.assertEqual([], survivors, out)
                    self.assertTrue(out.startswith("value: "), out)

    def test_the_cut_tail_pass_keeps_a_lead_without_a_body(self) -> None:
        # A word that ends in a shape's lead, or carries one with no leading
        # boundary where its whole shape has one, is not a token cut short.
        for text in (
            "workflow uses github_actions",
            "triggered by github_",
            "the laughs_",
            "see ghs_",
            "region ASIA",
            "region=ASIA",
            "region_ASIAEAST",
            "flag sk-a",
            "id xoxb-1",
            "set mode Bearer",
        ):
            with self.subTest(text=text):
                self.assertEqual((text, 0), cs.redact_cut_tail(text))
        # A lead with enough of a body after it goes; a header's name stays.
        for text, expected in (
            ("x ghs_abcd", "x " + cs.REDACTED),
            ("key ASIAABCD", "key " + cs.REDACTED),
            ("Authorization: Bearer abcd", "Authorization: " + cs.REDACTED),
            ("authorization: map[bearer abcd", "authorization: map[" + cs.REDACTED),
            ("token bearer abc1", "token " + cs.REDACTED),
            ("db postgres://admin:hunt\\u00", "db " + cs.REDACTED),
        ):
            with self.subTest(text=text):
                self.assertEqual((expected, 1), cs.redact_cut_tail(text))

    def test_the_cut_tail_pass_leaves_a_text_that_ends_between_words(self) -> None:
        for text in ("pods listed\n", "ready 3/3 ", "sha256:3f1c9a6e\n", 'resourceVersion: "184467"\n'):
            with self.subTest(text=text):
                self.assertEqual((text, 0), cs.redact_cut_tail(text))


    def test_tool_output_matches_a_token_after_a_json_escape(self) -> None:
        # kubectl -o json writes a multi-line value with `\n` escapes, which
        # leave a word character in front of the token.
        for escape in ("\\n", "\\t", '\\"', "\\\\", "\\u0041"):
            for name, token in TOKENS.items():
                if name == "private key material":
                    continue
                text = f'{{"data": {{"notes": "rotated{escape}{token}{escape}done"}}}}'
                with self.subTest(escape=escape, name=name):
                    out, count = cs.redact_tool_output_counted(text)
                    self.assertNotIn(token, out)
                    self.assertGreaterEqual(count, 1)
                    # The escape itself is kept whole: no match opens on its
                    # letter, the URL shape's scheme included.
                    self.assertIn(f"rotated{escape}{cs.REDACTED}", out)

    def test_tool_output_matches_what_the_log_form_matches_after_any_backslash(self) -> None:
        # A token right after a backslash that is no JSON escape (`\x`, `\A`
        # in a Windows path or shell output) is matched by the plain boundary,
        # and the tool-output form keeps it as an alternative.
        for text, token in (
            ("C:\\" + TOKENS["a Slack token"] + " end", TOKENS["a Slack token"]),
            ("C:\\" + TOKENS["an AWS access key id"] + " end", TOKENS["an AWS access key id"]),
            ("dir\\" + TOKENS["a Google OAuth token"], TOKENS["a Google OAuth token"]),
            ("x\\" + PREFIXED_PROVIDER_KEY, PREFIXED_PROVIDER_KEY),
        ):
            with self.subTest(text=text[:12]):
                self.assertNotIn(token, cs.redact(text))
                self.assertNotIn(token, cs.redact_tool_output_counted(text)[0])

    def test_the_log_form_keeps_its_grammar(self) -> None:
        # The JSON-escape boundary is the tool-output form's alone.
        json_escaped = '{"v": "x\\n' + TOKENS["a Google OAuth token"] + '"}'
        self.assertIn(TOKENS["a Google OAuth token"], cs.redact(json_escaped))
        self.assertNotIn(TOKENS["a Google OAuth token"], cs.redact_tool_output_counted(json_escaped)[0])
        for name, pattern in cs.CREDENTIAL_SHAPES:
            with self.subTest(name=name):
                self.assertNotIn("\\\\[ntrbf", pattern.pattern)

    def test_a_password_url_after_a_json_escape_is_redacted(self) -> None:
        out, _ = cs.redact_tool_output_counted("x\\npostgres://app:Synth3ticPassw0rd@db")
        self.assertNotIn("Synth3ticPassw0rd", out)
        self.assertTrue(out.endswith(cs.REDACTED + "db"), out)

    def test_tool_output_redacts_a_whole_private_key_block(self) -> None:
        body = "synthetic_key_body.not-a-key"
        block = "-----BEGIN RSA PRIVATE KEY-----\n" + body + "\n-----END RSA PRIVATE KEY-----"
        self.assertEqual((f"key:\n{cs.REDACTED}\nafter", 1), cs.redact_tool_output_counted(f"key:\n{block}\nafter"))
        # One a cap cut runs to the end, its body with it.
        self.assertEqual((f"key: {cs.REDACTED}", 1), cs.redact_tool_output_counted("key: " + block[:45]))
        # The log form keeps its armour-line grammar.
        self.assertIn(body, cs.redact(block))

    def test_redacted_json_still_parses(self) -> None:
        # kubectl -o json is Go's encoder: `&`, `<`, `>` come out as \\u0026,
        # \\u003c, \\u003e. Whatever a value holds, a redaction leaves JSON
        # that parses, with no token left in it.
        def go_json(document):
            return json.dumps(document, indent=2).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")

        pem = "-----BEGIN PRIVATE KEY-----\nsynthetic_key_body.not-a-key\n-----END PRIVATE KEY-----"
        values = {**TOKENS, "private key material": pem, "a prefixed provider key": PREFIXED_PROVIDER_KEY,
                  "an unclosed private key": pem[:45]}
        for name, token in values.items():
            for before in ("", "line\n", "tab\t", 'q"', "back\\", "\u00e9"):
                text = go_json({"kind": "ConfigMap", "data": {"notes": f"{before}{token}\nnext", "k": "a&b"}})
                with self.subTest(name=name, before=before):
                    out, count = cs.redact_tool_output_counted(text)
                    self.assertGreaterEqual(count, 1)
                    document = json.loads(out)
                    self.assertNotIn(token, document["data"]["notes"])
                    self.assertTrue(document["data"]["notes"].startswith(before), document["data"]["notes"])
                    self.assertEqual("a&b", document["data"]["k"])

    def test_a_url_password_holding_json_escapes_is_redacted_in_tool_output(self) -> None:
        # Go writes `&` in a password as \\u0026; any encoder writes `"` and `\\`
        # as \\" and \\\\. The log form's classes keep a backslash out, and so
        # does the log form still.
        for password in ("hunt&er2xx", 'hunt"er2xx', "hunt\\er2xx", "p<w>rd12"):
            for user in ("admin", "ad&min"):
                url = f"postgres://{user}:{password}@db/x"
                text = json.dumps({"url": url}).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
                with self.subTest(password=password, user=user):
                    out, count = cs.redact_tool_output_counted(text)
                    self.assertEqual(1, count, out)
                    self.assertEqual({"url": f"{cs.REDACTED}db/x"}, json.loads(out))
        self.assertEqual(
            '{"url": "postgres://admin:hunt\\u0026er2xx@db/x"}',
            cs.redact('{"url": "postgres://admin:hunt\\u0026er2xx@db/x"}'),
        )

    def test_a_url_after_a_json_escape_leaves_the_escape_whole(self) -> None:
        text = '{"v": "x\\nhttps://u:pw12345@h", "w": "\\u0041ftp://u:pw12345@h"}'
        out, count = cs.redact_tool_output_counted(text)
        self.assertEqual((f'{{"v": "x\\n{cs.REDACTED}h", "w": "\\u0041{cs.REDACTED}h"}}', 2), (out, count))
        self.assertEqual({"v": f"x\n{cs.REDACTED}h", "w": f"A{cs.REDACTED}h"}, json.loads(out))

    def test_tool_output_keeps_prose_around_bearer_and_basic(self) -> None:
        for text in (
            "the basic troubleshootingsteps",
            "I followed the basic troubleshooting steps and the agent hung",
            "Bearer authentication against the relay returns 401",
            "basic configuration of the installationsteps",
            "the bearer of badnewsforeveryone",
            "Basic auth is on for the dashboard",
            "it failed the basic troubleshooting.",
            "Basic authentication. Bearer authorization.",
        ):
            with self.subTest(text=text):
                self.assertEqual((text, 0), cs.redact_tool_output_counted(text))
                # The log form keeps its any-twelve-letters grammar.
                if "steps" in text:
                    self.assertIn(cs.REDACTED, cs.redact(text))

    def test_tool_output_redacts_a_header_value_in_its_context(self) -> None:
        for text, expected in (
            ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "Authorization: " + cs.REDACTED),
            ("AUTHORIZATION=basic abcdefghijklmnop", "AUTHORIZATION=" + cs.REDACTED),
            ("curl -H 'authorization: bearer abcdefghijklmnop'", "curl -H 'authorization: " + cs.REDACTED + "'"),
            ("curl -H 'Bearer abcdefghijklmnop' url", "curl -H '" + cs.REDACTED + "' url"),
            ('{\"Authorization\": \"Bearer abcdefghijklmnop\"}', '{\"Authorization\": \"' + cs.REDACTED + '\"}'),
            ("token was Bearer abc123def456ghi789", "token was " + cs.REDACTED),
            ("sent Basic dXNlcjpwYXNzd29yZA==", "sent " + cs.REDACTED),
            ("token bearer abc123def456ghi789", "token " + cs.REDACTED),
            ("sent basic dXNlcjpwYXNzd29yZA==", "sent " + cs.REDACTED),
            ("authorization: map[bearer abcdefghijklmnop]", "authorization: map[" + cs.REDACTED + "]"),
            ("Header: map[Authorization:[Bearer abcdefghijklmnop]]", "Header: map[Authorization:[" + cs.REDACTED + "]]"),
            ('{"Authorization": ["Bearer abcdefghijklmnop"]}', '{"Authorization": ["' + cs.REDACTED + '"]}'),
        ):
            with self.subTest(text=text):
                self.assertEqual((expected, 1), cs.redact_tool_output_counted(text))

    def test_a_private_key_runs_armour_to_armour_across_json_strings(self) -> None:
        lines = ["-----BEGIN PRIVATE KEY-----", "MIIEsyntheticFixtureBodyOne", "MIIEsyntheticFixtureBodyTwo",
                 "-----END PRIVATE KEY-----"]
        for label, text in (
            ("split across array elements", json.dumps({"key": lines, "after": "kept"})),
            ("one string with escapes", json.dumps({"key": "\n".join(lines) + "\n", "after": "kept"})),
            ("unclosed, value ends", json.dumps({"key": "\n".join(lines[:2]), "after": "kept"})),
        ):
            with self.subTest(label=label):
                out, count = cs.redact_tool_output_counted(text)
                self.assertEqual(1, count, out)
                for body in ("MIIEsyntheticFixtureBodyOne", "MIIEsyntheticFixtureBodyTwo"):
                    self.assertNotIn(body, out)
                self.assertEqual("kept", json.loads(out)["after"])
        # Cut by the output cap inside the string: no END line and no closing quote.
        cut = '{"key": "' + "\\n".join(lines[:3])[:70]
        out, count = cs.redact_tool_output_counted(cut)
        self.assertEqual(('{"key": "' + cs.REDACTED, 1), (out, count))

    def test_many_unclosed_begin_lines_are_redacted_in_linear_time(self) -> None:
        # A BEGIN with no END near it is looked up, not scanned for: 4 MiB of
        # unclosed armour lines near the output cap takes one pass.
        # Each on a line of its own, and each in a JSON string of its own (a
        # block that stops at its closing quote, so every BEGIN is a lookup).
        for line in ("-----BEGIN PRIVATE KEY-----\n", '"-----BEGIN PRIVATE KEY-----", '):
            text = line * (4 * 1024 * 1024 // len(line))
            with self.subTest(line=line):
                started = time.monotonic()
                out, count = cs.PRIVATE_KEY_BLOCK.subn(cs.REDACTED, text)
                # About half a second on a laptop; the bound leaves room for a
                # loaded runner. The scan-per-BEGIN form ran past a minute.
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertGreaterEqual(count, 1)
                self.assertIsNone(cs.PRIVATE_KEY_BEGIN.search(out), "a whole armour line was left")
                # Memory: the output and its pieces, not a list per END line or run.
                tracemalloc.start()
                try:
                    cs.PRIVATE_KEY_BLOCK.subn(cs.REDACTED, text)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 4 * len(text))

    def test_a_begin_line_with_no_end_near_it_takes_the_span_and_no_more(self) -> None:
        # Fail closed: the 16 KiB after the BEGIN line go, an END 100 KB away
        # does not pull the output between it in, and the rest is kept.
        begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
        unrelated = "".join(f"pod/web-{n:05d} Running\n" for n in range(5000))
        text = "log:\n" + begin + "\nMIIEsyntheticFixtureBodyOne\n" + unrelated + end + "\ndone\n"
        out, count = cs.redact_tool_output_counted(text)
        self.assertEqual(1, count)
        self.assertNotIn("MIIEsyntheticFixtureBodyOne", out)
        kept_from = len("log:\n") + cs.PRIVATE_KEY_MAX_SPAN_CHARS
        self.assertEqual("log:\n" + cs.REDACTED + text[kept_from:], out)

    def test_a_stray_begin_line_does_not_shorten_the_real_key_after_it(self) -> None:
        # A stray header, ~12 KiB of filler, then a whole key whose body runs
        # past 16 KiB from the stray header: the stray block is extended by the
        # real key's, so nothing of the body survives.
        body_lines = [f"MIIEsyntheticStrayKeyBody{n:04d}" for n in range(300)]
        text = ("x -----BEGIN PRIVATE KEY-----\n" + "filler line\n" * 1000
                + "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(body_lines)
                + "\n-----END RSA PRIVATE KEY-----\nafter\n")
        self.assertGreater(text.index("-----END RSA"), cs.PRIVATE_KEY_MAX_SPAN_CHARS)
        out, count = cs.redact_tool_output_counted(text)
        self.assertEqual(("x " + cs.REDACTED + "\nafter\n", 1), (out, count))
        body = "".join(body_lines)
        survivors = [body[i:i + 8] for i in range(len(body) - 7) if body[i:i + 8] in out]
        self.assertEqual([], survivors)

    def test_a_line_that_is_not_key_material_inside_a_key_is_redacted(self) -> None:
        begin, end = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
        for text in (
            f"{begin}\nMIIEsyntheticFixtureBodyOne\nnot base64 (see {{runbook}})!\nMIIEtwo\n{end}\nafter",
            json.dumps({"key": [begin, "MIIEsyntheticFixtureBodyOne", "note: {x}!", end], "after": "kept"}),
        ):
            with self.subTest(text=text[:40]):
                out, count = cs.redact_tool_output_counted(text)
                self.assertEqual(1, count, out)
                for body in ("MIIEsyntheticFixtureBodyOne", "runbook", "note: {x}!"):
                    self.assertNotIn(body, out)
                self.assertTrue(out.endswith("after") or json.loads(out)["after"] == "kept", out)

    def test_a_key_the_cap_cut_goes_from_its_begin_line_to_the_cut(self) -> None:
        # However far the cut is from the BEGIN line, past the span included.
        text = "data: -----BEGIN PRIVATE KEY-----\n" + "MIIEsyntheticFixtureBody\n" * 1000
        self.assertEqual(("data: " + cs.REDACTED, 1), cs.redact_cut_private_key(text))
        closed = "a -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\nb"
        self.assertEqual((closed, 0), cs.redact_cut_private_key(closed))
        self.assertEqual(("pods listed\n", 0), cs.redact_cut_private_key("pods listed\n"))

    def test_the_log_form_is_unchanged_by_the_tool_output_form(self) -> None:
        self.assertEqual(len(cs.CREDENTIAL_SHAPES), len(cs.TOOL_OUTPUT_SHAPES))
        self.assertEqual("x\\nya29." + "a" * 30, cs.redact("x\\nya29." + "a" * 30))


if __name__ == "__main__":
    unittest.main()
