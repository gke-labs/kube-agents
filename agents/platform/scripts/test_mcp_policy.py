"""Unit tests for mcp_policy: what the credential proxy relays to remote MCP servers."""

import json
import unittest

import mcp_policy

# The server's own write tools, from the live `tools/list` of 2026-10-13: the
# eight of its 23 that are not annotated `readOnlyHint`. Each must be refused.
GKE_WRITE_TOOLS = (
    "apply_k8s_manifest",
    "cancel_operation",
    "create_cluster",
    "create_node_pool",
    "delete_k8s_resource",
    "patch_k8s_resource",
    "update_cluster",
    "update_node_pool",
)


def call(name, request_id=1, **extra):
    message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
               "params": {"name": name, "arguments": {}}}
    message.update(extra)
    return message


class EvaluateTest(unittest.TestCase):
    def test_every_read_tool_is_allowed(self):
        for name in mcp_policy.GKE_READ_TOOLS:
            with self.subTest(name=name):
                self.assertTrue(mcp_policy.evaluate("gke", call(name)).allowed)

    def test_every_write_tool_is_refused(self):
        for name in GKE_WRITE_TOOLS:
            with self.subTest(name=name):
                decision = mcp_policy.evaluate("gke", call(name))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.rule_id, mcp_policy.RULE_READ_ONLY)

    def test_the_read_set_and_the_write_set_are_the_whole_server(self):
        self.assertEqual(len(mcp_policy.GKE_READ_TOOLS), 15)
        self.assertFalse(mcp_policy.GKE_READ_TOOLS & set(GKE_WRITE_TOOLS))

    def test_an_unknown_tool_is_refused_the_same_way(self):
        decision = mcp_policy.evaluate("gke", call("get_cluster_and_also_delete_it"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.rule_id, mcp_policy.RULE_READ_ONLY)

    def test_a_tool_allowed_on_one_server_is_refused_on_another(self):
        decision = mcp_policy.evaluate("developer_knowledge", call("get_cluster"))
        self.assertEqual(decision.rule_id, mcp_policy.RULE_READ_ONLY)
        self.assertTrue(mcp_policy.evaluate("developer_knowledge", call("search_documents")).allowed)

    def test_a_missing_or_non_string_name_is_refused(self):
        for params in (None, {}, {"name": None}, {"name": ["get_cluster"]}, "get_cluster"):
            with self.subTest(params=params):
                message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
                self.assertEqual(mcp_policy.evaluate("gke", message).rule_id, mcp_policy.RULE_READ_ONLY)

    def test_an_unknown_server_is_refused_first(self):
        decision = mcp_policy.evaluate("gke/../secretmanager", call("get_cluster"))
        self.assertEqual(decision.rule_id, mcp_policy.RULE_SERVER)

    def test_methods_outside_the_set_are_refused(self):
        for method in ("resources/read", "prompts/get", "completion/complete", "logging/setLevel", "TOOLS/CALL"):
            with self.subTest(method=method):
                message = {"jsonrpc": "2.0", "id": 1, "method": method}
                self.assertEqual(mcp_policy.evaluate("gke", message).rule_id, mcp_policy.RULE_METHOD)

    def test_the_handshake_and_listing_are_allowed(self):
        for method in ("initialize", "notifications/initialized", "ping", "tools/list"):
            with self.subTest(method=method):
                self.assertTrue(mcp_policy.evaluate("gke", {"jsonrpc": "2.0", "method": method}).allowed)

    def test_no_refusal_repeats_caller_text(self):
        marker = "MARKER injected"
        for decision in (
            mcp_policy.evaluate(marker, call("get_cluster")),
            mcp_policy.evaluate("gke", None),
            mcp_policy.evaluate("gke", {"method": marker}),
            mcp_policy.evaluate("gke", call(marker)),
        ):
            with self.subTest(rule=decision.rule_id):
                self.assertFalse(decision.allowed)
                self.assertNotIn("MARKER", decision.message)
                self.assertNotIn("MARKER", decision.rule_id)
                self.assertRegex(decision.rule_id, r"^[a-z0-9]+(\.[a-z0-9-]+)+$")


class ParseTest(unittest.TestCase):
    def test_one_object_parses(self):
        self.assertEqual(mcp_policy.parse(b'{"method":"ping"}'), {"method": "ping"})

    def test_a_repeated_key_is_refused_at_any_depth(self):
        # Python keeps the last of two equal keys and other parsers keep the
        # first, so either order could smuggle a write past the check.
        for body in (
            b'{"method":"tools/call","params":{"name":"get_cluster","name":"patch_k8s_resource"}}',
            b'{"method":"tools/call","method":"ping"}',
        ):
            with self.subTest(body=body):
                self.assertIsNone(mcp_policy.parse(body))

    def test_a_batch_and_other_shapes_are_refused(self):
        for body in (b'[{"method":"ping"}]', b'"ping"', b"null", b"not json", b"\xff\xfe", b"[" * 100000):
            with self.subTest(body=body[:20]):
                self.assertIsNone(mcp_policy.parse(body))

    def test_an_escaped_name_is_judged_by_what_it_decodes_to(self):
        message = mcp_policy.parse(b'{"method":"tools/call","id":1,"params":{"name":"patch\\u005fk8s_resource"}}')
        self.assertEqual(mcp_policy.evaluate("gke", message).rule_id, mcp_policy.RULE_READ_ONLY)


class RefusalTest(unittest.TestCase):
    def test_a_refused_call_is_a_tool_result_the_model_reads(self):
        message = call("patch_k8s_resource", request_id=7)
        reply = mcp_policy.refusal(message, mcp_policy.evaluate("gke", message))
        self.assertEqual(reply["id"], 7)
        self.assertTrue(reply["result"]["isError"])
        text = reply["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("SECURITY_POLICY_BLOCKED: "))
        self.assertTrue(text.endswith("\npolicy rule: gcp.mcp.read-only"))

    def test_a_refused_method_is_a_jsonrpc_error(self):
        message = {"jsonrpc": "2.0", "id": "a", "method": "resources/read"}
        reply = mcp_policy.refusal(message, mcp_policy.evaluate("gke", message))
        self.assertEqual(reply["error"]["code"], mcp_policy.JSONRPC_METHOD_NOT_FOUND)
        self.assertIn("policy rule: gcp.mcp.method", reply["error"]["message"])

    def test_an_unreadable_request_answers_with_a_null_id(self):
        reply = mcp_policy.refusal(None, mcp_policy.evaluate("gke", None))
        self.assertIsNone(reply["id"])
        self.assertEqual(reply["error"]["code"], mcp_policy.JSONRPC_INVALID_REQUEST)

    def test_a_refused_notification_gets_no_jsonrpc_reply(self):
        message = {"jsonrpc": "2.0", "method": "notifications/cancelled"}
        self.assertIsNone(mcp_policy.refusal(message, mcp_policy.evaluate("gke", message)))


class FilterToolsListTest(unittest.TestCase):
    def reply(self, names, **result):
        body = {"jsonrpc": "2.0", "id": 2,
                "result": {"tools": [{"name": name, "description": "d"} for name in names], **result}}
        return json.dumps(body).encode()

    def test_only_the_table_survives(self):
        names = sorted(mcp_policy.GKE_READ_TOOLS) + list(GKE_WRITE_TOOLS) + ["brand_new_tool"]
        filtered = json.loads(mcp_policy.filter_tools_list("gke", self.reply(names, nextCursor="c")))
        self.assertEqual({tool["name"] for tool in filtered["result"]["tools"]}, mcp_policy.GKE_READ_TOOLS)
        self.assertNotIn("nextCursor", filtered["result"])
        self.assertEqual(filtered["id"], 2)

    def test_an_error_reply_passes_unchanged(self):
        body = b'{"jsonrpc":"2.0","id":2,"error":{"code":-32603,"message":"x"}}'
        self.assertEqual(mcp_policy.filter_tools_list("gke", body), body)

    def test_an_unreadable_reply_is_none(self):
        for body in (b"", b"<html>", b'{"result":{}}', b'{"result":{"tools":"all"}}',
                     b'{"result":{"tools":[],"tools":[{"name":"patch_k8s_resource"}]}}'):
            with self.subTest(body=body):
                self.assertIsNone(mcp_policy.filter_tools_list("gke", body))


if __name__ == "__main__":
    unittest.main()
