"""A tiny OpenAI-compatible stub used to smoke test the agent loop end to end.

Not part of the test suite; run it manually:

    python tests/fake_llm_server.py --port 8099
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])
        last = messages[-1] if messages else {}

        if last.get("role") == "tool" and last.get("name") == "get_legal_moves":
            payload = json.loads(last.get("content", "{}"))
            move = (payload.get("moves") or ["e4"])[0]
            message = {
                "role": "assistant",
                # Pretend to be a reasoning model so the chain of thought path
                # is exercised too.
                "reasoning_content": f"Thinking about {move}: it looks safe.",
                "content": f"I will play {move}.",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "make_move", "arguments": json.dumps({"move": move})},
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "reasoning_content": "First I need the legal moves.",
                "content": "Let me check the legal moves first.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_legal_moves", "arguments": "{}"},
                    }
                ],
            }

        response = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "fake-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }
        data = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: D102 - silence the default logger
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    print(f"fake LLM server on http://127.0.0.1:{args.port}/v1")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
