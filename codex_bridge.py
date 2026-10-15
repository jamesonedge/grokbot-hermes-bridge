"""Direct Grok webhook sender and exact-ID reply reader. Standard library only.

No agent relay, listener, automatic retry, secret discovery or chat wake-up.
The reply endpoint is a proposed contract, not an existing Grok API.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class BridgeError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_config(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise BridgeError("An explicitly configured private JSON file is required")
    if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BridgeError("Credential file must be private: chmod 600")
    try:
        config = json.loads(path.read_text())
        for name in ("webhook_url", "inbox_url"):
            value = config[name]
            url = urllib.parse.urlsplit(value)
            if (url.scheme != "https" or not url.hostname or url.username or
                    url.password or url.fragment or url.query):
                raise ValueError()
        for name in ("webhook_auth", "inbox_auth"):
            if not isinstance(config[name], str) or not config[name].strip() or any(
                    c in config[name] for c in "\r\n"):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise BridgeError("Config requires two HTTPS URLs and their full Authorization values") from None
    return config


def exchange(method, url, auth, body=None):
    """One request only. No redirects, credential output or automatic retry."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            status = response.status
            raw = response.read(16385)
    except urllib.error.HTTPError as error:
        # Do not expose error bodies/URLs, which may contain private information.
        return error.code, None
    except (OSError, ValueError, http.client.HTTPException):
        raise BridgeError("Transport outcome uncertain; inspect this request before sending again") from None
    if len(raw) > 16384:
        raise BridgeError("Response exceeds the bounded message size")
    if method == "POST":
        return status, None
    try:
        return status, json.loads(raw)
    except (ValueError, UnicodeError):
        raise BridgeError("Reply endpoint returned invalid JSON") from None


class Bridge:
    def __init__(self, config, state, transport=exchange, clock=time.time):
        self.config, self.state, self.transport, self.clock = config, Path(state), transport, clock
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (self.state.is_symlink() or self.state.stat().st_uid != os.getuid() or
                stat.S_IMODE(self.state.stat().st_mode) & 0o077):
            raise BridgeError("State directory must be private: chmod 700")

    def _path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise BridgeError("Invalid request ID")
        return self.state / (request_id + ".json")

    def _save(self, record):
        fd, tmp = tempfile.mkstemp(dir=self.state)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(record, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, self._path(record["id"]))
            directory_fd = os.open(self.state, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def send(self, text, context):
        if not isinstance(text, str) or not text.strip() or not isinstance(context, str) or not context.strip():
            raise BridgeError("Message and destination context are required")
        request = {"v": 1, "id": uuid.uuid4().hex, "context": context, "from": "codex",
                   "to": "grokbot", "kind": "request", "text": text}
        wire = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(wire) >= 2000:
            raise BridgeError("Complete message must be under 2000 characters; send a reference for larger material")
        record = {**request, "created_at": self.clock(), "expires_at": self.clock() + 7200,
                  "status": "send_outcome_uncertain"}
        self._save(record)  # Durable ID exists before the network can accept anything.
        try:
            status, _ = self.transport("POST", self.config["webhook_url"],
                                       self.config["webhook_auth"], {"from": "codex", "text": wire})
        except BridgeError as error:
            raise BridgeError(f"Request {request['id']}: {error}") from None
        # A proxy/server error can occur after delivery. No non-200 status in
        # this webhook contract guarantees non-delivery, so keep the ID readable.
        record.update(http_status=status, status="awaiting_reply" if status == 200 else "send_outcome_uncertain")
        self._save(record)
        return {"id": request["id"], "context": context, "http_status": status,
                "status": record["status"], "round_trip_verified": False}

    def read(self, request_id):
        path = self._path(request_id)
        if path.is_symlink() or not path.is_file():
            raise BridgeError("No locally registered request with that ID")
        record = json.loads(path.read_text())
        if record["status"] == "reply_received":
            return {"status": "reply_received", "reply": record["reply"],
                    "round_trip_verified": True, "untrusted_agent_feedback": True}
        if self.clock() >= record["expires_at"]:
            raise BridgeError("Request expired; no new network request made")
        status, reply = self.transport("GET", self.config["inbox_url"].rstrip("/") + "/" + request_id,
                                       self.config["inbox_auth"])
        if status == 404:
            return {"id": request_id, "status": "awaiting_reply", "round_trip_verified": False}
        if status != 200:
            raise BridgeError(f"Reply read failed: HTTP {status}")
        expected = {"v": 1, "in_reply_to": request_id, "context": record["context"],
                    "from": "grokbot", "to": "codex", "kind": "reply"}
        if (not isinstance(reply, dict) or any(reply.get(k) != v for k, v in expected.items()) or
                not isinstance(reply.get("text"), str) or not reply["text"].strip() or
                len(json.dumps(reply, ensure_ascii=False)) >= 2000):
            raise BridgeError("Reply rejected: ID, context, origin or message format does not match")
        self._save({**record, "status": "reply_received", "reply": reply})
        return {"status": "reply_received", "reply": reply,
                "round_trip_verified": True, "untrusted_agent_feedback": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Explicit mode-600 JSON credential file")
    parser.add_argument("--state", required=True, help="Private local request/reply directory")
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--context", required=True)
    send.add_argument("--text-file", required=True)
    read = sub.add_parser("read")
    read.add_argument("request_id")
    args = parser.parse_args()
    try:
        bridge = Bridge(load_config(args.config), args.state)
        result = (bridge.send(Path(args.text_file).read_text(), args.context)
                  if args.command == "send" else bridge.read(args.request_id))
        print(json.dumps(result))
        return 1 if result.get("status") == "send_outcome_uncertain" else 0
    except (BridgeError, OSError, ValueError):
        # Runtime exceptions can contain URLs or source text; emit only known safe errors.
        import sys
        error = sys.exc_info()[1]
        print(json.dumps({"error": str(error) if isinstance(error, BridgeError)
                          else "Cannot read private configuration, input or state"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
