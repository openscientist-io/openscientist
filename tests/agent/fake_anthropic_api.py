"""A fake Messages API for driving the real bundled Claude CLI in tests.

Each streamed request is answered from a script: a tool call the model
"decides" to make, or a closing text. The server records every request body
so a test can see what the CLI sent.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: The reply to one request, from the request body: a Read of a file path, or a closing text.
Reply = Callable[[dict[str, object]], "ReadFile | Say"]


class ReadFile:
    def __init__(self, path: str) -> None:
        self.path = path


class Say:
    def __init__(self, text: str) -> None:
        self.text = text


def _message_start() -> dict[str, object]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": "claude-fake",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _events(reply: ReadFile | Say) -> list[dict[str, object]]:
    if isinstance(reply, ReadFile):
        block: dict[str, object] = {
            "type": "tool_use",
            "id": "toolu_fake",
            "name": "Read",
            "input": {},
        }
        delta: dict[str, object] = {
            "type": "input_json_delta",
            "partial_json": json.dumps({"file_path": reply.path}),
        }
        stop = "tool_use"
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": reply.text}
        stop = "end_turn"
    return [
        _message_start(),
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


def answered_tool(body: dict[str, object]) -> bool:
    """Whether the request already carries a tool result for the model."""
    messages = body.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return True
    return False


class FakeAnthropicApi:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.requests: list[dict[str, object]] = []

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                if self.path.split("?")[0].endswith("/count_tokens"):
                    self._json({"input_tokens": 1})
                    return
                if not body.get("stream"):
                    self._json(
                        {
                            "id": "msg_fake",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-fake",
                            "content": [{"type": "text", "text": "ok"}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    )
                    return
                api.requests.append(body)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for event in _events(api.reply(body)):
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
                    self.wfile.flush()

            def _json(self, payload: dict[str, object]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


@contextmanager
def serving(reply: Reply) -> Iterator[tuple[str, FakeAnthropicApi]]:
    """The fake API on a free loopback port, as its base URL and the api."""
    api = FakeAnthropicApi(reply)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", api
    finally:
        server.shutdown()
        server.server_close()
