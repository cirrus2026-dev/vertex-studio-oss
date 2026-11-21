#!/usr/bin/env python3
"""
bridge_mcp_server.py — Zero-Dependency Local MCP Server (stdio) for Persistent Chrome CDP Bridge (:8765)

Exposes 3 MCP tools to Antigravity:
  1. ask_argon  — Dispatch prompt/files to Gemini Web (Argon) with optional silent long-poll wait
  2. ask_carbon — Dispatch prompt/files to Google Cloud Studio (Carbon) with optional silent long-poll wait
  3. check_job  — Query job status and retrieve full answer via GET /jobs/{job_id}?wait=...&includeAnswer=1
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SERVER_NAME = "vertex-cdp-bridge"
SERVER_VERSION = "4.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

def _log(msg: str) -> None:
    """เขียน Log เฉพาะ stderr เท่านั้น ห้ามแตะ stdout ซึ่งเป็นท่อ MCP JSON-RPC"""
    sys.stderr.write(f"[bridge_mcp_server] {msg}\n")
    sys.stderr.flush()

def _load_bridge_token() -> str:
    env_tok = os.environ.get("BRIDGE_TOKEN", "").strip()
    if env_tok:
        return env_tok

    candidates = [
        os.path.join(SCRIPT_DIR, f".bridge_token_{BRIDGE_PORT}.json"),
        os.path.join(SCRIPT_DIR, ".bridge_token"),
        os.path.join(os.path.expanduser("~"), ".gemini", "vertex-dashboard", f".bridge_token_{BRIDGE_PORT}.json"),
        os.path.join(os.path.expanduser("~"), ".gemini", "vertex-dashboard", ".bridge_token"),
        os.path.join(os.getcwd(), f".bridge_token_{BRIDGE_PORT}.json"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                file_port = data.get("port")
                if file_port is not None and int(file_port) != BRIDGE_PORT:
                    continue
                tok = str(data.get("token", "")).strip()
                if tok:
                    return tok
        except Exception:
            continue
    return ""

def _bridge_request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 150.0,
) -> Tuple[int, Dict[str, Any]]:
    """ส่ง HTTP Request ไปยัง 127.0.0.1:8765 พร้อม Auto-Retry 1 ครั้งหาก Token เปลี่ยน (401/403)"""
    url = f"{BRIDGE_BASE_URL}{path}"
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None

    for attempt in range(2):
        token = _load_bridge_token()
        headers = {
            "Host": f"{BRIDGE_HOST}:{BRIDGE_PORT}",
            "Origin": BRIDGE_BASE_URL,
        }
        if body_bytes is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if token:
            headers["X-Bridge-Token"] = token

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw) if raw.strip() else {"ok": True}
        except urllib.error.HTTPError as e:
            err_raw = e.read().decode("utf-8", errors="replace")
            if e.code in (401, 403) and attempt == 0:
                time.sleep(0.2)
                continue
            try:
                err_data = json.loads(err_raw)
                if not isinstance(err_data, dict):
                    err_data = {"ok": False, "error": err_raw}
            except Exception:
                err_data = {"ok": False, "error": err_raw or f"HTTP {e.code}"}
            return e.code, err_data
        except urllib.error.URLError as e:
            return 0, {
                "ok": False,
                "error": f"ไม่สามารถเชื่อมต่อ Bridge ({BRIDGE_BASE_URL}) ได้: {e.reason} (ตรวจสอบว่า gemini_bridge_server.py กำลังรันอยู่)",
            }
        except Exception as e:
            return 0, {"ok": False, "error": f"Bridge request error: {e}"}

    return 401, {"ok": False, "error": "X-Bridge-Token ไม่ถูกต้อง"}

def _wait_for_job_result(
    job_id: str,
    total_wait_sec: float,
    include_answer: bool = True,
) -> Tuple[int, Dict[str, Any]]:
    """Long-Poll GET /jobs/{job_id} เป็นช่วงๆ (ครั้งละไม่เกิน 25 วินาที) จนจบหรือครบเวลา"""
    deadline = time.monotonic() + max(0.0, total_wait_sec)
    inc_flag = "1" if include_answer else "0"
    last_status = 0
    last_data: Dict[str, Any] = {"ok": False, "jobId": job_id, "state": "unknown"}

    while True:
        req_t0 = time.monotonic()
        remaining = deadline - req_t0
        slice_wait = max(0.0, min(25.0, remaining))
        qs = urllib.parse.urlencode({"wait": f"{slice_wait:.1f}", "includeAnswer": inc_flag})
        code, data = _bridge_request(
            "GET",
            f"/jobs/{urllib.parse.quote(job_id, safe='')}?{qs}",
            timeout=slice_wait + 15.0,
        )
        last_status, last_data = code, data
        if code != 200 or not data.get("ok"):
            return code, data
        state = data.get("state")
        if state != "generating":
            return code, data
        now = time.monotonic()
        if now >= deadline:
            return last_status, last_data
        # ป้องกัน Busy-Spin 100% CPU กรณีเซิร์ฟเวอร์ตอบกลับทันทีโดยไม่ได้บล็อกรอ Event
        if (now - req_t0) < 0.5:
            time.sleep(min(1.0, max(0.05, deadline - now)))

def _normalize_files(raw_files: Any) -> List[str]:
    if not raw_files:
        return []
    if isinstance(raw_files, str):
        return [os.path.abspath(raw_files.strip())] if raw_files.strip() else []
    if isinstance(raw_files, list):
        out = []
        for item in raw_files:
            if isinstance(item, str) and item.strip():
                out.append(os.path.abspath(item.strip()))
        return out
    return []

def _format_job_text(data: Dict[str, Any]) -> Tuple[str, bool]:
    if not data.get("ok"):
        return json.dumps(data, ensure_ascii=False, indent=2), True

    state = data.get("state")
    job_id = data.get("jobId", "")
    if state == "done":
        answer = data.get("answer")
        header = (
            f"[JOB DONE] jobId={job_id} | target={data.get('target', '-')} "
            f"| chars={data.get('chars', len(answer or ''))} | elapsedMs={data.get('elapsedMs', 0)}"
        )
        if answer is not None:
            return f"{header}\n\n{answer}", False
        return f"{header}\noutputFile={data.get('latestFile', '')}", False

    if state in ("error", "timeout", "quota_limit"):
        return (
            f"[JOB {state.upper()}] jobId={job_id} | error={data.get('error', 'Unknown error')}",
            True,
        )

    return (
        f"[JOB {str(state).upper()}] jobId={job_id} | timestamp={data.get('timestamp', '')}\n"
        f"Still generating. Call check_job(jobId=\"{job_id}\", waitSeconds=60) to continue waiting.",
        False,
    )

def _safe_int(val: Any, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

TOOLS = [
    {
        "name": "ask_argon",
        "description": "Send a prompt and optional files to Gemini 4 Argon Web via the local Persistent Bridge (port 8765) and wait for the completed answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt text for Argon"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional list of file paths"},
                "thinkingLevel": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"], "default": "HIGH"},
                "waitSeconds": {"type": "integer", "default": 180, "description": "Max seconds to wait for answer (0 for fire-and-forget)"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "ask_carbon",
        "description": "Send a prompt and optional files to Gemini 4 Carbon (Google Cloud Studio) via the local Persistent Bridge (port 8765).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt text for Carbon"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional list of file paths"},
                "waitSeconds": {"type": "integer", "default": 180, "description": "Max seconds to wait for answer (0 for fire-and-forget)"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "check_job",
        "description": "Check the status or wait for completion of a dispatched Argon/Carbon job by jobId.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string", "description": "Job ID returned by ask_argon or ask_carbon"},
                "waitSeconds": {"type": "integer", "default": 60}
            },
            "required": ["jobId"]
        }
    },
    {
        "name": "bridge_status",
        "description": "Inspect Persistent Bridge health, connected Chrome tab URL, or trigger a single manual CDP connection attempt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reconnect": {"type": "boolean", "default": False, "description": "Set true to trigger a single /connect-cdp request if disconnected"}
            }
        }
    },
    {
        "name": "extract_latest",
        "description": "Immediately extract and save the latest visible response from the Argon (Gemini Web) or Carbon (Cloud Studio) tab without sending a new prompt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["argon", "carbon"], "default": "argon"}
            }
        }
    }
]

def _handle_tool_call(name: str, args: Dict[str, Any]) -> Tuple[str, bool]:
    if name == "ask_argon":
        wait_s = max(0, _safe_int(args.get("waitSeconds"), 180))
        payload = {
            "prompt": args.get("prompt", ""),
            "files": _normalize_files(args.get("files")),
            "thinkingLevel": args.get("thinkingLevel", "HIGH"),
            "fireAndForget": True,
            "maxWait": max(300, wait_s + 60),
        }
        code, data = _bridge_request("POST", "/ask", payload, timeout=135.0)
        if code != 200 or not data.get("ok"):
            return json.dumps(data, ensure_ascii=False, indent=2), True
        job_id = data.get("jobId")
        if not job_id and "answer" in data:
            return f"[JOB DONE] mode=userscript | elapsedMs={data.get('elapsedMs', 0)}\n\n{data.get('answer', '')}", False
        if wait_s <= 0 or not job_id:
            return f"[DISPATCHED] jobId={job_id}", False
        _, jdata = _wait_for_job_result(job_id, total_wait_sec=float(wait_s), include_answer=True)
        return _format_job_text(jdata)

    if name == "ask_carbon":
        wait_s = max(0, _safe_int(args.get("waitSeconds"), 180))
        payload = {
            "prompt": args.get("prompt", ""),
            "files": _normalize_files(args.get("files")),
            "maxWait": max(600, wait_s + 60),
        }
        code, data = _bridge_request("POST", "/ask_carbon", payload, timeout=135.0)
        if code != 200 or not data.get("ok"):
            return json.dumps(data, ensure_ascii=False, indent=2), True
        job_id = data.get("jobId")
        if wait_s <= 0 or not job_id:
            return f"[DISPATCHED] jobId={job_id}", False
        _, jdata = _wait_for_job_result(job_id, total_wait_sec=float(wait_s), include_answer=True)
        return _format_job_text(jdata)

    if name == "check_job":
        job_id = str(args.get("jobId", "")).strip()
        if not job_id:
            return "Missing jobId", True
        wait_s = max(0, _safe_int(args.get("waitSeconds"), 60))
        _, jdata = _wait_for_job_result(job_id, total_wait_sec=float(wait_s), include_answer=True)
        return _format_job_text(jdata)

    if name == "bridge_status":
        if args.get("reconnect"):
            _, cdata = _bridge_request("POST", "/connect-cdp", {}, timeout=15.0)
            time.sleep(0.5)
            code, sdata = _bridge_request("GET", "/status", timeout=10.0)
            return json.dumps({"connectRequest": cdata, "status": sdata}, ensure_ascii=False, indent=2), (code != 200)
        code, sdata = _bridge_request("GET", "/status", timeout=10.0)
        return json.dumps(sdata, ensure_ascii=False, indent=2), (code != 200)

    if name == "extract_latest":
        target = str(args.get("target", "argon")).strip().lower()
        endpoint = "/extract_carbon" if target == "carbon" else "/extract_markdown"
        code, data = _bridge_request("POST", endpoint, {}, timeout=30.0)
        if code != 200 or not data.get("ok"):
            return json.dumps(data, ensure_ascii=False, indent=2), True
        body = data.get("markdown") if target != "carbon" else data.get("text")
        return f"[EXTRACTED] target={target} | jobId={data.get('jobId', '-')}\n\n{body or ''}", False

    return f"Unknown tool: {name}", True

def _send_rpc(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                continue
        except Exception:
            continue
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        if method == "initialize":
            _send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "vertex-bridge-mcp", "version": "1.0.0"}
                }
            })
        elif method == "ping":
            _send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif isinstance(method, str) and method.startswith("notifications/"):
            continue
        elif method == "tools/list":
            _send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            tname = str(params.get("name", ""))
            targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                text, is_err = _handle_tool_call(tname, targs)
            except Exception as e:
                text, is_err = f"Internal MCP tool error ({tname}): {e}", True
            _send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err
                }
            })
        elif req_id is not None:
            _send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            })


if __name__ == "__main__":
    main()

