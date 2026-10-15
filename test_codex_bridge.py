"""Synthetic and loopback checks for the direct bridge; no live credentials."""
import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_bridge import Bridge, BridgeError, exchange, load_config, main


CONFIG = {
    "webhook_url": "https://grok.example.invalid/routine",
    "webhook_auth": "Bearer synthetic-webhook-secret",
    "inbox_url": "https://grok.example.invalid/replies",
    "inbox_auth": "Bearer synthetic-inbox-secret",
}


class ScriptedTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, method, url, auth, body=None):
        self.calls.append((method, url, auth, copy.deepcopy(body)))
        if not self.outcomes:
            raise AssertionError("Unexpected network request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def reply_for(request, **changes):
    reply = {
        "v": 1, "in_reply_to": request["id"], "context": request["context"],
        "from": "grokbot", "to": "codex", "kind": "reply", "text": "GROK_BRIDGE_OK",
    }
    reply.update(changes)
    return reply


@contextmanager
def loopback_server(responder):
    """Bind only IPv4 loopback; no external network is used by these tests."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = {
                "method": self.command, "path": self.path,
                "headers": dict(self.headers), "body": self.rfile.read(length),
            }
            requests.append(request)
            status, headers, body = responder(request)
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.now = 1000

    def bridge(self, transport):
        return Bridge(CONFIG.copy(), self.state, transport=transport, clock=lambda: self.now)

    def record(self, request_id):
        path = self.state / (request_id + ".json")
        return json.loads(path.read_text())

    def test_send_persists_private_request_before_acceptance(self):
        calls = []

        def transport(method, url, auth, body=None):
            request = json.loads(body["text"])
            record = self.record(request["id"])
            self.assertEqual(record["status"], "send_outcome_uncertain")
            calls.append((method, url, auth, body, request))
            return 200, None

        sent = self.bridge(transport).send("A harmless test", "active-chat-42")
        method, url, auth, body, request = calls[0]
        self.assertEqual((method, url, auth), ("POST", CONFIG["webhook_url"], CONFIG["webhook_auth"]))
        self.assertEqual(body["from"], "codex")
        self.assertEqual(request, {
            "v": 1, "id": sent["id"], "context": "active-chat-42", "from": "codex",
            "to": "grokbot", "kind": "request", "text": "A harmless test",
        })
        self.assertRegex(sent["id"], r"^[a-f0-9]{32}$")
        self.assertEqual(sent["status"], "awaiting_reply")
        self.assertFalse(sent["round_trip_verified"])
        self.assertEqual(self.record(sent["id"])["expires_at"], 8200)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / (sent["id"] + ".json")).stat().st_mode), 0o600)

    def test_request_file_and_directory_are_synced_before_post(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def synced(fd):
            events.append("sync_directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "sync_file")
            real_fsync(fd)

        def replaced(source, destination):
            real_replace(source, destination)
            events.append("rename")

        def transport(method, url, auth, body=None):
            self.assertEqual(events, ["sync_file", "rename", "sync_directory"])
            self.assertEqual(len(list(self.state.glob("*.json"))), 1)
            events.append("POST")
            return 200, None

        with patch("codex_bridge.os.fsync", side_effect=synced), patch("codex_bridge.os.replace", side_effect=replaced):
            self.bridge(transport).send("Ping", "chat")
        self.assertEqual(events, ["sync_file", "rename", "sync_directory", "POST",
                                  "sync_file", "rename", "sync_directory"])

    def test_directory_sync_failure_prevents_post(self):
        real_fsync = os.fsync
        transport = ScriptedTransport()

        def synced(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("Synthetic directory sync failure")
            real_fsync(fd)

        with patch("codex_bridge.os.fsync", side_effect=synced):
            with self.assertRaisesRegex(OSError, "directory sync failure"):
                self.bridge(transport).send("Ping", "chat")
        self.assertEqual(transport.calls, [])

    def test_404_is_pending_and_only_reads_exact_reply_url(self):
        transport = ScriptedTransport((200, None), (404, None))
        bridge = self.bridge(transport)
        sent = bridge.send("Ping", "chat")
        self.assertEqual(bridge.read(sent["id"]), {
            "id": sent["id"], "status": "awaiting_reply", "round_trip_verified": False,
        })
        self.assertEqual(transport.calls[1], (
            "GET", CONFIG["inbox_url"] + "/" + sent["id"], CONFIG["inbox_auth"], None,
        ))
        self.assertEqual([c[0] for c in transport.calls], ["POST", "GET"])

    def test_matching_reply_is_saved_and_duplicate_read_uses_cache(self):
        transport = ScriptedTransport((200, None))
        bridge = self.bridge(transport)
        sent = bridge.send("Ping", "chat-42")
        reply = reply_for(sent)
        transport.outcomes.append((200, reply))
        first = bridge.read(sent["id"])
        self.assertEqual(first["reply"], reply)
        self.assertTrue(first["round_trip_verified"])
        self.assertEqual(self.record(sent["id"])["reply"], reply)
        self.assertEqual(bridge.read(sent["id"]), first)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual([c[0] for c in transport.calls], ["POST", "GET"])

    def test_wrong_identity_context_or_origin_never_counts_as_reply(self):
        changes = (
            {"in_reply_to": "f" * 32}, {"context": "another-chat"},
            {"from": "bob"}, {"to": "someone-else"}, {"kind": "request"},
            {"v": 2}, {"text": " "}, {"text": "x" * 2000},
        )
        for change in changes:
            with self.subTest(change=next(iter(change))):
                transport = ScriptedTransport((200, None))
                bridge = self.bridge(transport)
                sent = bridge.send("Ping", "chat")
                transport.outcomes.append((200, reply_for(sent, **change)))
                with self.assertRaisesRegex(BridgeError, "Reply rejected"):
                    bridge.read(sent["id"])
                self.assertEqual(self.record(sent["id"])["status"], "awaiting_reply")
                self.assertNotIn("reply", self.record(sent["id"]))
                self.assertEqual([c[0] for c in transport.calls], ["POST", "GET"])

    def test_uncertain_send_retains_id_and_can_read_without_resending(self):
        transport = ScriptedTransport(BridgeError("Transport outcome uncertain"))
        bridge = self.bridge(transport)
        with self.assertRaisesRegex(BridgeError, "Transport outcome uncertain") as caught:
            bridge.send("Ping", "chat")
        files = list(self.state.glob("*.json"))
        self.assertEqual(len(files), 1)
        request = json.loads(files[0].read_text())
        self.assertIn(request["id"], str(caught.exception))
        self.assertEqual(request["status"], "send_outcome_uncertain")
        self.assertEqual(len(transport.calls), 1)
        transport.outcomes.append((200, reply_for(request)))
        self.assertTrue(bridge.read(request["id"])["round_trip_verified"])
        self.assertEqual([c[0] for c in transport.calls], ["POST", "GET"])

    def test_oversize_and_empty_inputs_are_rejected_before_network(self):
        transport = ScriptedTransport()
        bridge = self.bridge(transport)
        for text, context in (("x" * 2000, "chat"), ("x", "c" * 2000), (" ", "chat"), ("Ping", "")):
            with self.subTest(length=len(text), context_length=len(context)):
                with self.assertRaises(BridgeError):
                    bridge.send(text, context)
        self.assertEqual(transport.calls, [])
        self.assertEqual(list(self.state.iterdir()), [])

    def test_expired_request_never_reads_network(self):
        transport = ScriptedTransport((200, None))
        bridge = self.bridge(transport)
        sent = bridge.send("Ping", "chat")
        self.now += 7200
        with self.assertRaisesRegex(BridgeError, "expired"):
            bridge.read(sent["id"])
        self.assertEqual([c[0] for c in transport.calls], ["POST"])

    def test_http_502_after_delivery_allows_exact_reply_without_resend(self):
        transport = ScriptedTransport((502, None))
        bridge = self.bridge(transport)
        sent = bridge.send("Ping", "chat")
        self.assertEqual(sent["http_status"], 502)
        self.assertEqual(sent["status"], "send_outcome_uncertain")
        self.assertFalse(sent["round_trip_verified"])
        self.assertEqual(self.record(sent["id"])["status"], "send_outcome_uncertain")
        transport.outcomes.append((200, reply_for(sent)))
        result = bridge.read(sent["id"])
        self.assertTrue(result["round_trip_verified"])
        self.assertEqual(result["reply"]["in_reply_to"], sent["id"])
        self.assertEqual(bridge.read(sent["id"]), result)
        self.assertEqual([c[0] for c in transport.calls], ["POST", "GET"])

    def test_cli_malformed_status_line_is_redacted_and_retains_request(self):
        config_path = Path(self.tmp.name) / "config.json"
        config_path.write_text(json.dumps(CONFIG))
        config_path.chmod(0o600)
        message_path = Path(self.tmp.name) / "message.txt"
        message_path.write_text("Harmless test")
        output, errors = io.StringIO(), io.StringIO()
        private_marker = "SYNTHETIC_PRIVATE_STATUSLINE_MARKER"
        argv = ["codex_bridge.py", "--config", str(config_path), "--state", str(self.state),
                "send", "--context", "chat", "--text-file", str(message_path)]
        with patch("sys.argv", argv), patch("codex_bridge.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = http.client.BadStatusLine(private_marker)
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(main(), 1)
        result = json.loads(output.getvalue())
        self.assertIn("Transport outcome uncertain", result["error"])
        self.assertNotIn(private_marker, output.getvalue() + errors.getvalue())
        self.assertNotIn("Traceback", output.getvalue() + errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        records = list(self.state.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(record["status"], "send_outcome_uncertain")
        self.assertIn(record["id"], result["error"])
        self.assertEqual(opener.return_value.open.call_count, 1)

    def test_unknown_or_traversal_id_is_rejected_before_network(self):
        transport = ScriptedTransport()
        bridge = self.bridge(transport)
        for request_id in ("../outside", "f" * 32, "a" * 31, None):
            with self.subTest(request_id=request_id):
                with self.assertRaises(BridgeError):
                    bridge.read(request_id)
        self.assertEqual(transport.calls, [])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps(CONFIG))
        self.path.chmod(0o600)

    def test_private_config_loads(self):
        self.assertEqual(load_config(self.path), CONFIG)

    def test_public_permissions_and_symlink_are_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(BridgeError, "private"):
            load_config(self.path)
        self.path.chmod(0o600)
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        with self.assertRaises(BridgeError):
            load_config(link)

    def test_urls_and_auth_cannot_smuggle_credentials_or_newlines(self):
        for key, value in (
            ("webhook_url", "http://example.invalid/"),
            ("inbox_url", "https://user:password@example.invalid/"),
            ("inbox_url", "https://example.invalid/?secret=x"),
            ("inbox_url", "https://example.invalid/#fragment"),
            ("webhook_auth", "Bearer token\r\nX-Leak: value"),
            ("inbox_auth", ""),
        ):
            with self.subTest(key=key, value=value):
                self.path.write_text(json.dumps({**CONFIG, key: value}))
                with self.assertRaises(BridgeError):
                    load_config(self.path)

    def test_nonprivate_state_directory_is_rejected(self):
        state = Path(self.tmp.name) / "state"
        state.mkdir(mode=0o755)
        state.chmod(0o755)
        with self.assertRaisesRegex(BridgeError, "State directory must be private"):
            Bridge(CONFIG, state)


class LoopbackTransportTests(unittest.TestCase):
    def test_actual_exchange_sends_authorization_and_exact_body(self):
        def respond(request):
            if request["method"] == "POST":
                return 200, {}, {"accepted": True}
            return 404, {}, {"pending": True}

        with loopback_server(respond) as (url, requests):
            body = {"from": "codex", "text": "synthetic ping"}
            self.assertEqual(exchange("POST", url + "/routine", CONFIG["webhook_auth"], body), (200, None))
            self.assertEqual(exchange("GET", url + "/replies/abc", CONFIG["inbox_auth"]), (404, None))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["headers"]["Authorization"], CONFIG["webhook_auth"])
        self.assertEqual(requests[0]["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(requests[0]["body"]), body)
        self.assertEqual(requests[1]["headers"]["Authorization"], CONFIG["inbox_auth"])
        self.assertEqual(requests[1]["body"], b"")

    def test_actual_exchange_does_not_follow_redirect_or_forward_auth(self):
        with loopback_server(lambda request: (200, {}, {"text": "must not reach"})) as (destination, hits):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(code=code):
                    with loopback_server(lambda request: (code, {"Location": destination + "/stolen"}, b"")) as (url, requests):
                        self.assertEqual(exchange("POST", url, CONFIG["webhook_auth"], {"text": "test"}), (code, None))
                        self.assertEqual(exchange("GET", url, CONFIG["inbox_auth"]), (code, None))
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(hits, [])

    def test_actual_exchange_rejects_invalid_or_oversized_reply(self):
        for payload, expected in ((b"not-json", "invalid JSON"), (b"x" * 16385, "bounded message size")):
            with self.subTest(expected=expected):
                with loopback_server(lambda request: (200, {}, payload)) as (url, requests):
                    with self.assertRaisesRegex(BridgeError, expected):
                        exchange("GET", url, CONFIG["inbox_auth"])
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
