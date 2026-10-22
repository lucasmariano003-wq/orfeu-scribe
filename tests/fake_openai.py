#!/usr/bin/python3
"""A fake OpenAI-compatible server for tests. Listens only on 127.0.0.1.

- ``POST /v1/audio/transcriptions``: parses the multipart form, records the
  fields (never the audio) and returns ``{"text": ...}``.
- ``POST /v1/chat/completions``: records the request and returns a revision:
  the configured reply, or the ``dictation`` field capitalized with a period.
- ``GET /v1/models``: two models.
- ``GET /v1/redirect``: a 302 to another path, to prove redirects are refused.

Failures can be scripted per endpoint with ``fail``: a list of HTTP statuses
returned (and consumed) before normal answers.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET_ECHO = "fake-body-must-never-reach-the-user"


def parse_multipart(body, content_type):
    boundary = re.search(r"boundary=([^;]+)", content_type)[1].encode()
    fields = []
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if not name:
            continue
        value = value[:-2] if value.endswith(b"\r\n") else value
        if b"filename=" in head:
            fields.append((name[1].decode(), {"filename": re.search(rb'filename="([^"]*)"', head)[1].decode(),
                                               "bytes": len(value), "riff": value.startswith(b"RIFF")}))
        else:
            fields.append((name[1].decode(), value.decode()))
    return fields


class State:
    def __init__(self):
        self.transcriptions = []
        self.chats = []
        self.authorizations = []
        self.text = "let us meet on wednesday afternoon new paragraph thank you"
        self.reply = None
        self.fail = {"transcriptions": [], "chat": []}
        self.reject_schema = False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State

    def log_message(self, *args):
        pass

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, endpoint):
        if self.state.fail[endpoint]:
            status = self.state.fail[endpoint].pop(0)
            # The body echoes something a careless client might show; Orfeu Scribe must not.
            self._json({"error": {"message": SECRET_ECHO}}, status)
            return True
        return False

    def do_GET(self):
        self.state.authorizations.append(self.headers.get("Authorization"))
        if self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": "fake-transcribe"}, {"id": "fake-chat"}]})
        elif self.path == "/v1/redirect/models":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/v1/models")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        self.state.authorizations.append(self.headers.get("Authorization"))
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/v1/audio/transcriptions":
            fields = parse_multipart(body, self.headers.get("Content-Type", ""))
            self.state.transcriptions.append(fields)
            if self._fail("transcriptions"):
                return
            self._json({"text": self.state.text})
        elif self.path == "/v1/chat/completions":
            request = json.loads(body)
            self.state.chats.append(request)
            if self.state.reject_schema and "response_format" in request:
                self._json({"error": {"message": SECRET_ECHO}}, 400)
                return
            if self._fail("chat"):
                return
            reply = self.state.reply
            if reply is None:
                data = json.loads(request["messages"][-1]["content"])
                text = data.get("dictation") or data.get("instruction") or data.get("question") or ""
                if "command" in data:
                    reply = json.dumps({"action": "question", "certain": False, "text": "An answer."})
                else:
                    reply = text[:1].upper() + text[1:]
                    reply = reply if reply.rstrip().endswith((".", "?", "!", "⟧")) else reply + "."
            self._json({"choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                     "finish_reason": "stop"}]})
        else:
            self._json({"error": {"message": "not found"}}, 404)


class FakeServer(ThreadingHTTPServer):
    def close(self):
        self.shutdown()
        self.server_close()


def serve(port=0):
    state = State()
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = FakeServer(("127.0.0.1", port), handler)
    server.state = state
    server.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    server = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(server.url, flush=True)
    threading.Event().wait()
