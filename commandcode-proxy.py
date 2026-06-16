#!/usr/bin/env python3
"""
CommandCode Proxy — OpenAI-compatible endpoint that translates to CommandCode API.

Listens on 127.0.0.1:20129 (configurable via PORT env var).
Presents /v1/models and /v1/chat/completions in OpenAI format,
translates requests to CommandCode's /alpha/generate native format,
and converts SSE responses back to OpenAI SSE.

Requires: COMMANDCODE_API_KEY env var (starts with 'user_...')
"""

from __future__ import annotations

import json
import os
import sqlite3
import ssl
import sys
import time
import uuid
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────

PORT = int(os.getenv("PROXY_PORT", "20129"))
HOST = os.getenv("PROXY_HOST", "127.0.0.1")
COMMANDCODE_URL = "https://api.commandcode.ai/alpha/generate"

# 9router DB path (for reading the API key)
NINEROUTER_DB = Path.home() / ".9router" / "db" / "data.sqlite"


def _get_api_key() -> str:
    """Read CommandCode API key from 9router's database."""
    env_key = os.getenv("COMMANDCODE_API_KEY", "")
    if env_key:
        return env_key
    if NINEROUTER_DB.exists():
        try:
            db = sqlite3.connect(str(NINEROUTER_DB))
            cursor = db.execute(
                "SELECT data FROM providerConnections WHERE provider = 'commandcode'"
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row[0]).get("apiKey", "")
        except Exception:
            pass
    return ""


API_KEY = _get_api_key()

# ── Supported models ───────────────────────────────────────────────────────

MODELS = [
    {"id": "cmc/deepseek/deepseek-v4-pro",      "owned_by": "cmc"},
    {"id": "cmc/deepseek/deepseek-v4-flash",    "owned_by": "cmc"},
    {"id": "cmc/zai-org/GLM-5",                 "owned_by": "cmc"},
    {"id": "cmc/zai-org/GLM-5.1",               "owned_by": "cmc"},
    {"id": "cmc/moonshotai/Kimi-K2.5",          "owned_by": "cmc"},
    {"id": "cmc/moonshotai/Kimi-K2.6",          "owned_by": "cmc"},
    {"id": "cmc/MiniMaxAI/MiniMax-M2.5",        "owned_by": "cmc"},
    {"id": "cmc/MiniMaxAI/MiniMax-M2.7",        "owned_by": "cmc"},
    {"id": "cmc/Qwen/Qwen3.6-Max-Preview",      "owned_by": "cmc"},
    {"id": "cmc/Qwen/Qwen3.6-Plus",             "owned_by": "cmc"},
    {"id": "cmc/stepfun/Step-3.5-Flash",        "owned_by": "cmc"},
]


# ── Request translation ─────────────────────────────────────────────────────

def strip_cmc_prefix(model: str) -> str:
    """Strip cmc/ prefix for CommandCode API."""
    return model.removeprefix("cmc/").removeprefix("commandcode/")


def is_commandcode_native(body: dict) -> bool:
    """Check if the request is already in CommandCode-native format."""
    return "params" in body and "threadId" in body


def extract_from_native(body: dict) -> tuple:
    """Extract model, messages, stream from CommandCode-native format."""
    params = body.get("params", {})
    model = params.get("model", "deepseek/deepseek-v4-flash")
    messages = params.get("messages", [])
    stream = params.get("stream", True)
    return model, messages, stream


def openai_to_commandcode(body: dict) -> dict:
    """Translate OpenAI or CommandCode-native request to CommandCode native format."""
    if is_commandcode_native(body):
        model, messages, _stream = extract_from_native(body)
        tools = body.get("params", {}).get("tools")
        tool_choice = body.get("params", {}).get("tool_choice")
    else:
        model = strip_cmc_prefix(body.get("model", "deepseek/deepseek-v4-flash"))
        messages = body.get("messages", [])
        tools = body.get("tools")
        tool_choice = body.get("tool_choice")

    # Build params dict
    params: dict = {
        "model": model,
        "messages": messages,
        "stream": True,  # FORCED — CommandCode requires this
    }
    
    # Forward tool definitions — translate OpenAI format to CommandCode format
    if tools:
        cc_tools = []
        for tool in tools:
            cc_tool = {}
            # OpenAI: {type: "function", function: {name, description, parameters}}
            # CommandCode: {name, description, input_schema, type?}
            fn = tool.get("function", {})
            if fn:
                cc_tool["name"] = fn.get("name", tool.get("name", ""))
                cc_tool["description"] = fn.get("description", tool.get("description", ""))
                params_schema = fn.get("parameters", tool.get("input_schema", tool.get("parameters", {})))
                if params_schema:
                    cc_tool["input_schema"] = params_schema
            else:
                # Already in CommandCode format or flat format
                cc_tool = dict(tool)
            cc_tools.append(cc_tool)
        params["tools"] = cc_tools
    if tool_choice:
        params["tool_choice"] = tool_choice

    # Force stream=true — CommandCode requires it
    return {
        "threadId": str(uuid.uuid4()),
        "memory": "",
        "config": {
            "workingDir": os.getenv("HOME", "/tmp"),
            "date": time.strftime("%Y-%m-%d"),
            "environment": "linux",
            "structure": [],
            "isGitRepo": False,
            "currentBranch": "",
            "mainBranch": "",
            "gitStatus": "",
            "recentCommits": [],
        },
        "params": params,
    }


# ── SSE response translation ────────────────────────────────────────────────

def commandcode_event_to_openai_chunk(
    event: dict, model: str, chunk_id: str, created: int
) -> dict | None:
    """Convert a single CommandCode SSE event to an OpenAI chunk."""
    etype = event.get("type", "")

    if etype == "text-delta":
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": event.get("text", "")}, "finish_reason": None}],
        }

    if etype == "reasoning-delta":
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"reasoning_content": event.get("text", "")}, "finish_reason": None}],
        }

    if etype == "finish":
        usage = event.get("totalUsage", {})
        finish_reason = event.get("finishReason", "stop")
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": usage.get("promptTokens", 0),
                "completion_tokens": usage.get("completionTokens", 0),
                "total_tokens": usage.get("totalTokens", 0),
            },
        }

    if etype == "error":
        error_msg = event.get("message", event.get("error", "Unknown CommandCode error"))
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": f"[CommandCode error: {error_msg}]"}, "finish_reason": "error"}],
        }

    return None  # start, start-step, text-start, text-end, finish-step, done, etc.


def translate_commandcode_sse_to_openai_sse(
    cc_response_body: bytes, model: str
) -> bytes:
    """Translate CommandCode SSE stream to OpenAI SSE stream."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    output_lines: list[str] = []

    text = cc_response_body.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        chunk = commandcode_event_to_openai_chunk(event, model, chunk_id, created)
        if chunk:
            output_lines.append(f"data: {json.dumps(chunk)}")

    output_lines.append("data: [DONE]")
    return "\n".join(output_lines).encode("utf-8")


def translate_commandcode_sse_to_openai_json(
    cc_response_body: bytes, model: str
) -> dict:
    """Translate CommandCode SSE stream to a single OpenAI JSON response
    (for non-streaming clients — collects all delta text)."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = "stop"
    usage = {}

    text = cc_response_body.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "text-delta":
            content_parts.append(event.get("text", ""))
        elif etype == "reasoning-delta":
            reasoning_parts.append(event.get("text", ""))
        elif etype == "finish":
            finish_reason = event.get("finishReason", "stop")
            usage = event.get("totalUsage", {})
        elif etype == "error":
            content_parts.append(f"[CommandCode error: {event.get('message', '')}]")
            finish_reason = "error"

    response: dict = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("promptTokens", 0),
            "completion_tokens": usage.get("completionTokens", 0),
            "total_tokens": usage.get("totalTokens", 0),
        },
    }

    if reasoning_parts:
        response["choices"][0]["message"]["reasoning_content"] = "".join(reasoning_parts)

    return response


# ── HTTP request to CommandCode API ─────────────────────────────────────────

def call_commandcode_api(body: dict) -> tuple[int, bytes]:
    """Send request to CommandCode API, return (status_code, body_bytes)."""
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "x-command-code-version": "0.25.7",
        "x-cli-environment": "cli",
        "User-Agent": "CommandCode/0.25.7",
        "Accept": "text/event-stream",
    }

    req = urllib.request.Request(COMMANDCODE_URL, data=data, headers=headers, method="POST")
    try:
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, timeout=120, context=ctx)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        error_body = json.dumps({"error": str(e)}).encode()
        return 502, error_body


# ── HTTP Server ─────────────────────────────────────────────────────────────

class ProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the CommandCode proxy."""

    def log_message(self, format, *args):
        """Log to stderr with timestamp."""
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}", file=sys.stderr)

    def _send_json(self, status: int, data: dict | list):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {
                "object": "list",
                "data": [{"id": m["id"], "object": "model", "owned_by": m["owned_by"]} for m in MODELS],
            })
        elif self.path == "/health":
            self._send_json(200, {"status": "ok", "provider": "commandcode-proxy"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        # Accept any POST path — single-purpose proxy
        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        try:
            openai_body = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        model = openai_body.get("model", "")
        want_stream = openai_body.get("stream", False)
        is_native = is_commandcode_native(openai_body)

        # If CommandCode-native format, extract from params
        if is_native:
            model, _msgs, want_stream = extract_from_native(openai_body)
            # Put cmc/ prefix back for response
            if not model.startswith("cmc/"):
                model = "cmc/" + model

        if not API_KEY:
            self._send_json(500, {"error": "COMMANDCODE_API_KEY not set"})
            return

        # Translate and forward
        cc_body = openai_to_commandcode(openai_body)
        status, cc_response = call_commandcode_api(cc_body)

        if status != 200:
            self._send_json(502, {
                "error": f"CommandCode API returned {status}",
                "detail": cc_response.decode("utf-8", errors="replace")[:500],
            })
            return

        if is_native:
            # 9router sent native format — return raw CommandCode SSE,
            # let 9router's handler do the OpenAI translation
            self._send_sse(cc_response)
        elif want_stream:
            # Direct OpenAI client wants SSE
            openai_sse = translate_commandcode_sse_to_openai_sse(cc_response, model)
            self._send_sse(openai_sse)
        else:
            # Return JSON (collect from SSE)
            openai_json = translate_commandcode_sse_to_openai_json(cc_response, model)
            self._send_json(200, openai_json)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: COMMANDCODE_API_KEY environment variable is required.", file=sys.stderr)
        print("Set it in ~/.hermes/.env or export it.", file=sys.stderr)
        sys.exit(1)

    server = HTTPServer((HOST, PORT), ProxyHandler)
    print(f"CommandCode proxy listening on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"Endpoints: /v1/models  /v1/chat/completions  /health", file=sys.stderr)
    print(f"Forwarding to: {COMMANDCODE_URL}", file=sys.stderr)
    print(f"API key: {API_KEY[:8]}...", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
