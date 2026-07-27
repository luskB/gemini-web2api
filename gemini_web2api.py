#!/usr/bin/env python3
"""
gemini-web2api - Gemini Web to OpenAI API proxy.

Converts Google Gemini's web interface into an OpenAI-compatible API server.
Zero authentication required. Works on any platform (Windows/macOS/Linux).

Usage:
    pip install httpx
    python gemini_web2api.py [--port 8081] [--config config.json]

Client configuration (Cherry Studio, ChatBox, etc.):
    Base URL: http://localhost:8081/v1
    API Key: (anything or empty)

How it works:
    Sends requests directly to Gemini's public StreamGenerate endpoint.
    The backend does not verify authentication for basic text generation.
    Model selection via MODE_CATEGORY field [79] in the request payload.
    This is NOT a user-tier spoofing attack - the endpoint simply doesn't
    require auth for anonymous access.
"""
import json
import urllib.request
import urllib.parse
import time
import ssl
import sys
import uuid
import re
import os
import hashlib
import argparse
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

__version__ = "1.1.0"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
}

CONFIG = dict(DEFAULT_CONFIG)

# ─── Models ──────────────────────────────────────────────────────────────────
# Mapping from JS source: MODE_CATEGORY enum (028-6eb337387583.js)
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE

MODELS = {
    "gemini-3.6-flash": {
        "mode": 1, "think": 4,
        "desc": "Latest all-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4,
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.5-flash-thinking": {
        "mode": 2, "think": 0,
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4,
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-auto": {
        "mode": 4, "think": 4,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        "mode": 5, "think": 0,
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4,
        "desc": "Lightweight fast model",
    },
}

# ─── Utilities ───────────────────────────────────────────────────────────────

def log(msg: str):
    if CONFIG["log_requests"]:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def load_cookie() -> tuple:
    """Load cookie from file. Returns (cookie_str, sapisid)."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file:
        return "", None
    if not os.path.exists(cookie_file):
        return "", None
    try:
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return "", None


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


# ─── Gemini Protocol ─────────────────────────────────────────────────────────

def gemini_stream_generate(prompt: str, model_id: int, think_mode: int) -> str:
    """Send prompt to Gemini StreamGenerate with retry."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params).encode()
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])

    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = ssl.create_default_context()
            proxy = CONFIG.get("proxy")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def gemini_stream_generate_iter(prompt: str, model_id: int, think_mode: int):
    """Send prompt and yield incremental text deltas using httpx streaming."""
    inner = [None] * 80
    inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [2]
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    body = urllib.parse.urlencode(params)
    reqid = int(time.time()) % 1000000
    prefix = account_prefix()
    url = (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)

    proxy = CONFIG.get("proxy")

    if not HAS_HTTPX:
        # Fallback: non-streaming with urllib
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        if text:
            yield text
        return

    prev_text = ""
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
        with client.stream("POST", url, content=body, headers=headers) as resp:
            resp.raise_for_status()
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                if "BardErrorInfo" in buf:
                    import re as _re
                    m = _re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                    if m:
                        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{m.group(1)}]")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if '"wrb.fr"' not in line or len(line) < 200:
                        continue
                    try:
                        arr = json.loads(line)
                        inner_str = arr[0][2]
                        if not inner_str or len(inner_str) < 50:
                            continue
                        inner2 = json.loads(inner_str)
                        if isinstance(inner2, list) and len(inner2) > 4 and inner2[4]:
                            for part in inner2[4]:
                                if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                                    for t in part[1]:
                                        if isinstance(t, str) and len(t) > len(prev_text):
                                            delta = t[len(prev_text):]
                                            delta = clean_gemini_text(delta, strip=False)
                                            if delta:
                                                yield delta
                                            prev_text = t
                    except (json.JSONDecodeError, IndexError, TypeError):
                        pass


def clean_gemini_text(text: str, strip: bool = True) -> str:
    """Remove internal code execution artifacts."""
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    return text.strip() if strip else text


def extract_response_text(raw: str) -> str:
    """Parse StreamGenerate response to extract final text."""
    import re as _re
    bard_err = _re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    texts = []
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner_str = arr[0][2]
            if not inner_str or len(inner_str) < 50:
                continue
            inner = json.loads(inner_str)
            if isinstance(inner, list) and len(inner) > 4 and inner[4]:
                for part in inner[4]:
                    if isinstance(part, list) and len(part) > 1 and part[1]:
                        if isinstance(part[1], list):
                            for t in part[1]:
                                if isinstance(t, str) and len(t) > 0:
                                    texts.append(t)
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    text = ""
    for t in reversed(texts):
        if t.strip():
            text = t
            break
    return clean_gemini_text(text)


# ─── OpenAI Format Helpers ───────────────────────────────────────────────────

def messages_to_prompt(messages: list, tools: list = None) -> str:
    """Convert OpenAI messages to prompt string."""
    parts = []
    if tools:
        tool_defs = []
        for tool in tools:
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            tool_defs.append({
                "name": fn.get("name", tool.get("name", "")),
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            parts.append(
                "[System instruction]: You have access to tools. "
                "To call a tool, respond with:\n"
                '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n'
                "Only use tool_call blocks when needed.\n\n"
                f"Available tools:\n{json.dumps(tool_defs, indent=2)}"
            )
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content
                if c.get("type") in ("text", "input_text")
            )
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tc_strs.append(
                        f'```tool_call\n{{"name": "{fn.get("name")}", '
                        f'"arguments": {fn.get("arguments", "{}")}}}\n```'
                    )
                parts.append(f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs))
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool result for {msg.get('name', '')}]: {content}")
        else:
            parts.append(content if content else "")
    return "\n\n".join(p for p in parts if p)


def parse_tool_calls(text: str) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    pattern = r'```tool_call\s*\n(.*?)\n```'
    for match in re.findall(pattern, text, re.DOTALL):
        try:
            data = json.loads(match.strip())
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": data["name"],
                    "arguments": json.dumps(data.get("arguments", {}), ensure_ascii=False),
                },
            })
        except (json.JSONDecodeError, KeyError):
            pass
    clean = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return clean, tool_calls


def parse_google_function_calls(text: str) -> tuple:
    """Extract Google native ``function_call`` blocks from model output."""
    function_calls = []
    pattern = r'```function_call\s*\n(.*?)\n```'
    clean = text
    for match in re.findall(pattern, clean, re.DOTALL):
        try:
            data = json.loads(match.strip())
            if "name" in data:
                function_calls.append({
                    "name": data["name"],
                    "args": data.get("args", data.get("arguments", {})),
                })
        except (json.JSONDecodeError, KeyError):
            pass
    clean = re.sub(pattern, '', clean, flags=re.DOTALL).strip()
    return clean, function_calls


_TOOL_CALL_MARKER = "```tool_call\n"
_TOOL_CALL_END = "\n```"


def _tool_marker_suffix_length(text: str) -> int:
    max_len = min(len(text), len(_TOOL_CALL_MARKER) - 1)
    for length in range(max_len, 0, -1):
        if text.endswith(_TOOL_CALL_MARKER[:length]):
            return length
    return 0


def iter_stream_events(deltas, allow_tool_calls: bool = True):
    """Turn Gemini text deltas into content and structured tool-call events."""
    if not allow_tool_calls:
        for delta in deltas:
            if delta:
                yield "content", delta
        return

    pending = ""
    tool_body = None
    for delta in deltas:
        if not delta:
            continue
        pending += delta
        while pending:
            if tool_body is not None:
                end_at = pending.find(_TOOL_CALL_END)
                if end_at < 0:
                    tool_body += pending
                    pending = ""
                    break
                tool_body += pending[:end_at]
                block = _TOOL_CALL_MARKER + tool_body + _TOOL_CALL_END
                pending = pending[end_at + len(_TOOL_CALL_END):]
                tool_body = None
                _, calls = parse_tool_calls(block)
                if calls:
                    yield "tool_calls", [
                        {"name": call["function"]["name"], "arguments": call["function"]["arguments"]}
                        for call in calls
                    ]
                else:
                    yield "content", block
                continue

            marker_at = pending.find(_TOOL_CALL_MARKER)
            if marker_at >= 0:
                if marker_at:
                    yield "content", pending[:marker_at]
                pending = pending[marker_at + len(_TOOL_CALL_MARKER):]
                tool_body = ""
                continue

            suffix_length = _tool_marker_suffix_length(pending)
            if len(pending) > suffix_length:
                text = pending[:-suffix_length] if suffix_length else pending
                pending = pending[-suffix_length:] if suffix_length else ""
                yield "content", text
            break

    if tool_body is not None:
        yield "content", _TOOL_CALL_MARKER + tool_body
    elif pending:
        yield "content", pending


_FUNCTION_CALL_MARKER = "```function_call\n"
_FUNCTION_CALL_END = "\n```"


def _function_marker_suffix_length(text: str) -> int:
    max_len = min(len(text), len(_FUNCTION_CALL_MARKER) - 1)
    for length in range(max_len, 0, -1):
        if text.endswith(_FUNCTION_CALL_MARKER[:length]):
            return length
    return 0


def iter_google_function_call_events(deltas, allow_function_calls: bool = True):
    """Turn Gemini text deltas into Google-native content and function-call events."""
    if not allow_function_calls:
        for delta in deltas:
            if delta:
                yield "content", delta
        return

    pending = ""
    function_body = None
    for delta in deltas:
        if not delta:
            continue
        pending += delta
        while pending:
            if function_body is not None:
                end_at = pending.find(_FUNCTION_CALL_END)
                if end_at < 0:
                    function_body += pending
                    pending = ""
                    break
                function_body += pending[:end_at]
                block = _FUNCTION_CALL_MARKER + function_body + _FUNCTION_CALL_END
                pending = pending[end_at + len(_FUNCTION_CALL_END):]
                function_body = None
                _, function_calls = parse_google_function_calls(block)
                if function_calls:
                    yield "function_calls", function_calls
                else:
                    yield "content", block
                continue

            marker_at = pending.find(_FUNCTION_CALL_MARKER)
            if marker_at >= 0:
                if marker_at:
                    yield "content", pending[:marker_at]
                pending = pending[marker_at + len(_FUNCTION_CALL_MARKER):]
                function_body = ""
                continue

            suffix_length = _function_marker_suffix_length(pending)
            if len(pending) > suffix_length:
                text = pending[:-suffix_length] if suffix_length else pending
                pending = pending[-suffix_length:] if suffix_length else ""
                yield "content", text
            break

    if function_body is not None:
        yield "content", _FUNCTION_CALL_MARKER + function_body
    elif pending:
        yield "content", pending


def build_openai_stream_chunks(events, completion_id: str, model_name: str, created: int = None):
    """Build OpenAI chat-completion chunks from content and tool-call events."""
    created = int(time.time()) if created is None else created
    finish_reason = "stop"
    tool_index = 0
    for kind, value in events:
        if kind == "content":
            if not value:
                continue
            yield {
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}],
            }
        elif kind == "tool_calls":
            finish_reason = "tool_calls"
            for call in value:
                yield {
                    "id": completion_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": tool_index,
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }]}, "finish_reason": None}],
                }
                tool_index += 1
    yield {
        "id": completion_id, "object": "chat.completion.chunk", "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }


def build_responses_stream_events(events, response_id: str, model_name: str, prompt_tokens: int):
    """Build Responses API SSE events from text and tool-call stream events."""
    created_at = int(time.time())
    output = []
    active_message = None
    active_message_index = None
    active_part = None
    output_tokens = 0

    def response(status: str, include_output: bool = False):
        value = {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": status,
            "model": model_name,
            "output": output if include_output else [],
        }
        if status == "completed":
            value["usage"] = {
                "input_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens,
            }
        return value

    def close_message():
        nonlocal active_message, active_message_index, active_part
        if active_message is None:
            return
        yield "response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": active_message["id"],
            "output_index": active_message_index,
            "content_index": 0,
            "text": active_part["text"],
        }
        yield "response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": active_message["id"],
            "output_index": active_message_index,
            "content_index": 0,
            "part": active_part,
        }
        active_message["status"] = "completed"
        yield "response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": active_message_index,
            "item": active_message,
        }
        active_message = None
        active_message_index = None
        active_part = None

    yield "response.created", {"type": "response.created", "response": response("in_progress")}
    yield "response.in_progress", {"type": "response.in_progress", "response": response("in_progress")}

    for kind, value in events:
        if kind == "content":
            if not value:
                continue
            if active_message is None:
                active_message_index = len(output)
                active_message = {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
                output.append(active_message)
                yield "response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": active_message_index,
                    "item": active_message,
                }
                active_part = {"type": "output_text", "text": "", "annotations": []}
                active_message["content"].append(active_part)
                yield "response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": active_message["id"],
                    "output_index": active_message_index,
                    "content_index": 0,
                    "part": active_part,
                }
            active_part["text"] += value
            output_tokens += len(value) // 4
            yield "response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": active_message["id"],
                "output_index": active_message_index,
                "content_index": 0,
                "delta": value,
            }
        elif kind == "tool_calls":
            yield from close_message()
            for call in value:
                output_index = len(output)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:12]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": call["name"],
                    "arguments": "",
                }
                output.append(item)
                yield "response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": item,
                }
                arguments = call["arguments"]
                item["arguments"] = arguments
                output_tokens += len(arguments) // 4
                yield "response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "delta": arguments,
                }
                yield "response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "arguments": arguments,
                }
                item["status"] = "completed"
                yield "response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }

    yield from close_message()
    yield "response.completed", {
        "type": "response.completed",
        "response": response("completed", include_output=True),
    }


def build_google_stream_chunks(events, model_name: str, prompt_tokens: int):
    """Build Google native streaming chunks from text and function-call events."""
    output_characters = 0
    for kind, value in events:
        if kind == "content":
            if not value:
                continue
            output_characters += len(value)
            yield {
                "candidates": [{
                    "content": {"parts": [{"text": value}], "role": "model"},
                    "index": 0,
                }],
                "modelVersion": model_name,
            }
        elif kind == "function_calls":
            for function_call in value:
                output_characters += len(json.dumps(function_call, ensure_ascii=False))
                yield {
                    "candidates": [{
                        "content": {
                            "parts": [{"functionCall": {
                                "name": function_call["name"],
                                "args": function_call["args"],
                            }}],
                            "role": "model",
                        },
                        "index": 0,
                    }],
                    "modelVersion": model_name,
                }

    yield {
        "candidates": [{"finishReason": "STOP", "index": 0}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_characters // 4,
            "totalTokenCount": prompt_tokens + output_characters // 4,
        },
        "modelVersion": model_name,
    }


# ─── HTTP Handler ────────────────────────────────────────────────────────────

class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] in keys:
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for h in ("x-api-key", "x-goog-api-key"):
            if self.headers.get(h, "") in keys:
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and pair[4:] in keys:
                    return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            if self.path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif self.path.startswith("/v1beta/models"):
                self._handle_google_models_list()
            elif self.path == "/":
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"GET error: {e}")

    def do_POST(self):
        try:
            if self.path.startswith("/v1") and not self._authorized():
                self.send_json({"error": {"message": "invalid api key"}}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if self.path == "/v1/chat/completions":
                self.handle_chat(body)
            elif self.path == "/v1/responses":
                self.handle_responses(body)
            elif ":generateContent" in self.path:
                self._handle_google_generate(body, stream=False)
            elif ":streamGenerateContent" in self.path:
                self._handle_google_generate(body, stream=True)
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except:
                pass

    def _resolve_model(self, model_name):
        think_override = None
        if "@think=" in model_name:
            model_name, think_str = model_name.rsplit("@think=", 1)
            think_override = int(think_str)
        cfg = MODELS.get(model_name)
        if not cfg:
            return None, None, None, f"Unknown model: {model_name}"
        return model_name, cfg["mode"], (think_override if think_override is not None else cfg["think"]), None

    def _call_gemini(self, prompt, model_id, think_mode, tools):
        raw = gemini_stream_generate(prompt, model_id, think_mode)
        text = extract_response_text(raw)
        tool_calls = None
        if tools and text:
            text, tool_calls = parse_tool_calls(text)
        return text or "", tool_calls

    def handle_chat(self, body: bytes):
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt = messages_to_prompt(
            req.get("messages", []), tools if tool_choice != "none" else None,
        )
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        if stream:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                events = iter_stream_events(
                    gemini_stream_generate_iter(prompt, model_id, think_mode),
                    allow_tool_calls=bool(tools) and tool_choice != "none",
                )
                for chunk in build_openai_stream_chunks(events, cid, model_name):
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Stream error: {e}")
            return

        # Non-streaming requests need a complete response for tool parsing.
        try:
            text, tool_calls = self._call_gemini(
                prompt, model_id, think_mode, tools if tool_choice != "none" else None,
            )
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        self.send_json({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": len(prompt)//4, "completion_tokens": len(text)//4,
                      "total_tokens": (len(prompt)+len(text))//4},
        })

    def handle_responses(self, body: bytes):
        """OpenAI Responses API for Codex CLI compatibility."""
        req = json.loads(body)
        model_name, model_id, think_mode, err = self._resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        input_items = req.get("input", [])
        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")

        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    if item.get("type") == "function_call_output":
                        messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                         "name": item.get("name", ""), "content": item.get("output", "")})
                    elif item.get("role") == "assistant" or (item.get("type") == "message" and item.get("role") == "assistant"):
                        cp = item.get("content", [])
                        text_acc, tc_list = "", []
                        if isinstance(cp, list):
                            for c in cp:
                                if isinstance(c, dict):
                                    if c.get("type") == "output_text": text_acc += c.get("text", "")
                                    elif c.get("type") == "function_call": tc_list.append(c)
                        elif isinstance(cp, str):
                            text_acc = cp
                        m = {"role": "assistant", "content": text_acc or None}
                        if tc_list:
                            m["tool_calls"] = [{"id": tc.get("call_id", f"call_{i}"), "type": "function",
                                                "function": {"name": tc.get("name",""), "arguments": tc.get("arguments","{}")}}
                                               for i, tc in enumerate(tc_list)]
                        messages.append(m)
                    else:
                        role = item.get("role", "user")
                        content = item.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(c.get("text", "") for c in content if c.get("type") in ("text", "input_text"))
                        messages.append({"role": role, "content": content})

        if tools:
            tools = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t for t in tools]

        prompt = messages_to_prompt(messages, tools if tool_choice != "none" else None)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        if req.get("stream"):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                events = iter_stream_events(
                    gemini_stream_generate_iter(prompt, model_id, think_mode),
                    allow_tool_calls=bool(tools) and tool_choice != "none",
                )
                for event_name, payload in build_responses_stream_events(
                    events, f"resp_{uuid.uuid4().hex[:16]}", model_name, len(prompt) // 4,
                ):
                    self.wfile.write(
                        f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            text, tool_calls = self._call_gemini(
                prompt, model_id, think_mode, tools if tool_choice != "none" else None,
            )
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"], "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": text or "", "annotations": []}]})

        self.send_json({"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
                        "model": model_name, "output": output,
                        "usage": {"input_tokens": len(prompt)//4, "output_tokens": len(text)//4, "total_tokens": (len(prompt)+len(text))//4}})


    # ─── Google Native API (Gemini CLI compatible) ────────────────────────────

    def _parse_google_model_from_path(self):
        """Extract model name from /v1beta/models/{model}:method path."""
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        if m:
            return m.group(1)
        return None

    def _handle_google_models_list(self):
        """GET /v1beta/models — Google AI format model list."""
        models = []
        for name, cfg in MODELS.items():
            models.append({
                "name": f"models/{name}",
                "displayName": name,
                "description": cfg["desc"],
                "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
            })
        self.send_json({"models": models})

    def _google_contents_to_prompt(self, req: dict) -> str:
        """Convert Google API contents format to prompt string."""
        parts = []
        tool_defs = []
        for tool in req.get("tools", []):
            for function in tool.get("functionDeclarations", []):
                tool_defs.append({
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                })
        sys_inst = req.get("systemInstruction")
        if sys_inst:
            sys_parts = sys_inst.get("parts", [])
            sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
            if sys_text:
                parts.append(f"[System instruction]: {sys_text}")

        if tool_defs:
            fc_config = req.get("toolConfig", {}).get("functionCallingConfig", {})
            mode = fc_config.get("mode", "AUTO")
            constraint = ""
            if mode == "NONE":
                constraint = "\nDo not call tools; respond with text only."
            elif mode == "ANY":
                allowed = fc_config.get("allowedFunctionNames", [])
                names = ", ".join(allowed)
                constraint = f"\nYou must call{' one of: ' + names if names else ' at least one tool'}; do not respond with text only."
            parts.append(
                "[System instruction]: You can call tools. Use exactly:\n"
                '```function_call\n{"name": "function_name", "args": {...}}\n```\n'
                "When calling a tool, output only the function_call block.\n\n"
                f"Available tools:\n{json.dumps(tool_defs, ensure_ascii=False, indent=2)}{constraint}"
            )

        for content in req.get("contents", []):
            role = content.get("role", "user")
            text_parts = []
            for p in content.get("parts", []):
                if p.get("text"):
                    text_parts.append(p["text"])
                elif p.get("functionCall"):
                    fc = p["functionCall"]
                    text_parts.append(
                        f'```function_call\n{json.dumps({"name": fc.get("name", ""), "args": fc.get("args", {})}, ensure_ascii=False)}\n```'
                    )
                elif p.get("functionResponse"):
                    fr = p["functionResponse"]
                    text_parts.append(
                        f'[Tool result for {fr.get("name", "")}]: {json.dumps(fr.get("response", {}), ensure_ascii=False)}'
                    )
            text = " ".join(text_parts)
            if role == "model":
                parts.append(f"[Assistant]: {text}")
            else:
                parts.append(text)
        return "\n\n".join(p for p in parts if p)

    def _handle_google_generate(self, body: bytes, stream: bool):
        """Handle Google native generateContent / streamGenerateContent."""
        req = json.loads(body)
        model_name = self._parse_google_model_from_path()
        if not model_name:
            self.send_json({"error": {"message": "model not specified in path"}}, 400)
            return

        model_name, model_id, think_mode, err = self._resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt = self._google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        if stream:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                events = iter_google_function_call_events(
                    gemini_stream_generate_iter(prompt, model_id, think_mode),
                    allow_function_calls=has_tools,
                )
                for chunk in build_google_stream_chunks(events, model_name, len(prompt) // 4):
                    self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            raw = gemini_stream_generate(prompt, model_id, think_mode)
            text = extract_response_text(raw)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        response_parts = []
        if has_tools and text:
            clean_text, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean_text:
                    response_parts.append({"text": clean_text})
                response_parts.extend(
                    {"functionCall": {"name": call["name"], "args": call["args"]}}
                    for call in function_calls
                )
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({"text": text or ""})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text or "") // 4,
            "totalTokenCount": (len(prompt) + len(text or "")) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        self.send_json(response_obj)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(path: str):
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
        log(f"Config loaded: {path}")


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None, help="Path to cookie file")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG")
    if not config_path:
        for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
            if os.path.exists(p):
                config_path = p
                break
    load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
