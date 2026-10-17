"""A tiny stand-in for llama-server, used by the tests.

Speaks just enough of the real API: GET /health (503 while "loading", then
200) and POST /v1/chat/completions with chunked server-sent events. The reply
is ``echo[<message count>]: <last user message>``, streamed word by word.
Send the message ``__error__`` to get an HTTP 400 like a context overflow.
"""

import argparse
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--startup-delay", type=float, default=0.0)
args, _ = parser.parse_known_args()

STARTED = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, text):
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _structured(self, request, last):
        """Non-streaming JSON reply, like a llama-server given a grammar (or not).

        ``__json__:<text>``    returns <text> verbatim (any reply, even broken JSON)
        ``__length__``         simulates a reply that was cut off
        ``__grammar_error__``  rejects a request that carries a grammar, with the
                               exact error some real llama.cpp builds give
        ``__always_400__``     rejects every structured request
        With a grammar the default reply is bare JSON; without one it is JSON
        wrapped in chatty prose and a code fence, like an unconstrained model.
        """
        grammar = request.get("grammar")

        if last == "__always_400__":
            return self._json(400, {"error": {"message": "something else went wrong"}})

        if last == "__grammar_error__" and grammar:
            return self._json(
                400,
                {
                    "error": {
                        "message": "Failed to initialize samplers: Unexpected empty "
                        "grammar stack after accepting piece: model (2028)"
                    }
                },
            )

        if last.startswith("__json__:"):
            content = last[len("__json__:"):]
        else:
            answer = (
                f"grammar={bool(grammar)} stream={request.get('stream')} "
                f"max_tokens={request.get('max_tokens')} "
                f"has_root={'root ::=' in (grammar or '')} "
                f"response_format={'response_format' in request}"
            )
            payload = json.dumps({"why": "fake", "action": "answer", "answer": answer})
            content = payload if grammar else f"Sure! Here is my reply:\n```json\n{payload}\n```\nHope that helps."

        finish = "length" if last == "__length__" else "stop"

        self._json(
            200,
            {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]},
        )

    def do_GET(self):
        if self.path != "/health":
            return self._json(404, {"error": {"message": "not found"}})

        if time.monotonic() - STARTED < args.startup_delay:
            return self._json(503, {"error": {"message": "Loading model"}})

        self._json(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")

        # Like the real server: /health is open, everything else needs the key.
        key = os.environ.get("LLAMA_API_KEY")

        if key and self.headers.get("Authorization") != f"Bearer {key}":
            return self._json(401, {"error": {"message": "Invalid API Key"}})

        messages = request.get("messages", [])
        last = messages[-1]["content"] if messages else ""

        if last == "__error__":
            return self._json(
                400, {"error": {"message": "prompt exceeds the context size"}}
            )

        # Non-streaming requests are ORION's structured (JSON) ones.
        if request.get("stream") is False:
            return self._structured(request, last)

        reply = f"echo[{len(messages)}]: {last}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def event(delta, finish=None):
            return "data: " + json.dumps(
                {"choices": [{"delta": delta, "finish_reason": finish}]}
            ) + "\n\n"

        self._chunk(event({"role": "assistant", "content": None}))

        for piece in re.findall(r"\S+\s*|\s+", reply):
            self._chunk(event({"content": piece}))
            time.sleep(0.005)

        self._chunk(event({}, "stop"))
        self._chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
