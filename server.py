import copy
import fnmatch
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
try:
    import google.auth
    from google.auth.transport.requests import Request
except ImportError:
    google = None
    Request = None

sys_path_dir = os.path.dirname(__file__)
if sys_path_dir not in sys.path:
    import sys
    sys.path.insert(0, sys_path_dir)
from code_tools import (
    build_search_roots,
    generate_smart_outline,
    validate_code_syntax,
    resolve_prompt_file_mentions,
    extract_file_mentions,
    find_file_in_roots,
    safe_write_code_file,
    locate_smart_patch_span,
)
from code_tools_hardening import (
    hold,
    read_text_meta,
    safe_write_text,
    mask_secrets as hardened_mask_secrets,
    is_sensitive_path as hardened_is_sensitive_path,
)

DESKTOP_DIR = os.path.abspath(os.path.expandvars(r"%USERPROFILE%\Desktop"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
OSS_SETTINGS_FILE = os.path.join(BASE_DIR, "oss_settings.json")

def load_dotenv_file(env_path=ENV_FILE):
    """โหลดตัวแปรจากไฟล์ .env เข้าสู่ os.environ อัตโนมัติโดยไม่ต้องพึ่งพา python-dotenv"""
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                key = k.strip()
                val = v.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass

load_dotenv_file()

PORT = int(os.environ.get("OSS_PORT", 5050))
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project-id")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
DEFAULT_WORKSPACE = r"."
current_workspace = DEFAULT_WORKSPACE
LOG_FILE = os.path.join(BASE_DIR, "conversations.jsonl")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions_store.json")
POINTER_FILE = os.path.join(BASE_DIR, "last_read_pointer.txt")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
HANDOFF_MD_FILE = os.path.join(BASE_DIR, "handoff_latest.md")
HANDOFF_JSON_FILE = os.path.join(BASE_DIR, "handoff_latest.json")

IGNORED_DIRS = {
    "node_modules", ".git", ".cache", "cache", "dist", "build",
    ".idea", ".vscode", "crashes", "sidecar_data", "__pycache__",
    "brain", "browser_recordings"
}
ALLOWED_EXTS = {
    ".mjs", ".js", ".json", ".html", ".css", ".ts", ".tsx", ".jsx",
    ".py", ".md", ".yml", ".yaml", ".txt", ".sh", ".sql"
}
ALLOWED_NAMES = {"Dockerfile", "docker-compose.yml", "RULE.md", "GEMINI.md"}

SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "*secret*", "*credentials*", "id_rsa*", "id_ed25519*",
    "*service-account*", "*service_account*", "*gateway-config*",
    "*gateway-state*", "token.json"
)
_bridge_semaphore = threading.Semaphore(1)
CODE_SAFE_EXTS = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".md"}

SECRET_MASK_RE = re.compile(
    r'((?:api[_-]?key|secret|password|passwd|private[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*[\'"]?)([^\s\'"`,;]{6,})',
    re.IGNORECASE,
)

MAX_TOOL_ITERATIONS = 8
MAX_RETRIES = 4

USD_TO_THB = 34.0
MODEL_PRICING_USD_PER_1M = {
    "gemini-3.8-flash": {"in": 0.15, "out": 0.60},
    "gemini-4-hydrogen": {"in": 0.30, "out": 1.20},
    "gemini-4-argon": {"in": 1.25, "out": 5.00},
    "gemini-4-carbon": {"in": 2.50, "out": 10.00},
}

MODEL_QUOTA_CACHE = {
    "gemini-3.8-flash": {"status": "ok", "label": "พร้อมใช้", "color": "emerald"},
    "gemini-4-hydrogen": {"status": "ok", "label": "พร้อมใช้", "color": "emerald"},
    "gemini-4-argon": {"status": "ok", "label": "พร้อมใช้", "color": "emerald"},
    "gemini-4-carbon": {"status": "ok", "label": "พร้อมใช้", "color": "emerald"},
}

state_lock = threading.Lock()
_file_locks_guard = threading.Lock()
_file_locks = {}
live_status = {}
cancelled_sessions = set()


def get_file_lock(file_path):
    """คืนค่า threading.Lock เฉพาะไฟล์ (Per-File Mutex) ป้องกัน Race Condition / Lost Update"""
    norm_path = os.path.normcase(os.path.abspath(str(file_path)))
    with _file_locks_guard:
        lock = _file_locks.get(norm_path)
        if lock is None:
            lock = threading.Lock()
            _file_locks[norm_path] = lock
        return lock


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def is_sensitive_file(filepath):
    """ตรวจสอบว่าไฟล์เป็นไฟล์ความลับหรือไม่ (ตรวจคำต้องห้ามก่อนเสมอ 100%)"""
    path_str = str(filepath or "").replace("\\", "/").lower()
    name = os.path.basename(path_str)
    if not name:
        return False
    # ด่านที่ 1: ตรวจสอบคำต้องห้ามและแพทเทิร์นความลับเป็นอันดับแรกสุดเสมอ
    if name == ".env" or name.startswith(".env."):
        return True
    for pat in SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(name, pat.lower()) or fnmatch.fnmatch(path_str, f"*/{pat.lower()}"):
            return True
    for pat in ("*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*service_account*", "*service-account*"):
        if fnmatch.fnmatch(name, pat):
            return True
    # ด่านที่ 2: ตรวจโฟลเดอร์อันตราย
    parts = set(path_str.split("/"))
    if parts.intersection({".ssh", ".aws", "secrets", ".gnupg"}):
        return True
    # ด่านที่ 3: หากไม่ติดคำต้องห้าม จึงอนุญาตให้เช็กนามสกุลไฟล์ปลอดภัย
    ext = os.path.splitext(name)[1].lower()
    if ext in CODE_SAFE_EXTS:
        return False
    return False


def _normalize_real_path(path_val: str) -> str:
    """แปลง Path เป็น Canonical Real Path (คลาย Symlink/Junction) เพื่อป้องกัน Path & Symlink Traversal"""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path_val))))


def _is_within_directory(target_real: str, root_dir: str) -> bool:
    """ตรวจสอบว่า target_real อยู่ภายใต้ root_dir อย่างปลอดภัยด้วย os.path.commonpath"""
    if not root_dir:
        return False
    try:
        root_real = _normalize_real_path(root_dir)
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:
        return False


def extract_prompt_allowed_scope(user_prompt: str, selected_files: list, workspace: str) -> set:
    """รวบรวมไฟล์ทั้งหมดที่ผู้ใช้อนุญาตให้แก้ไขจากไฟล์แนบและคำสั่งใน Prompt (Zero Trust Write Scope)"""
    allowed_paths = set()
    ws = workspace or os.getcwd()

    # 1. รวบรวมจาก selected_files ที่ผู้ใช้เลือกแนบบน UI
    for f in (selected_files or []):
        try:
            full_p = resolve_path(f, ws)
            if os.path.isfile(full_p) and not is_sensitive_file(full_p):
                allowed_paths.add(_normalize_real_path(full_p))
        except Exception:
            pass

    # 2. สกัดจาก Prompt (รองรับ @file, @full:file, @outline:file และการพิมพ์ชื่อไฟล์ตรงๆ นอก Code Block)
    if user_prompt:
        for mp in extract_file_mentions(
            user_prompt,
            search_roots=get_search_roots(ws),
            active_ws=ws,
            is_sensitive_fn=is_sensitive_file,
        ):
            if os.path.isfile(mp):
                allowed_paths.add(_normalize_real_path(str(mp)))

        prompt_no_fences = re.sub(r"```[\s\S]*?```", "", user_prompt)
        direct_mentions = re.findall(
            r"@?(?:(?:full|outline):)?((?:[a-zA-Z]:[\\/])?[\w\-\./\\]+\.(?:py|m?js|cjs|ts|tsx|jsx|html|css|json|md|sql|ya?ml|sh|txt))",
            prompt_no_fences,
            re.IGNORECASE,
        )
        for m in direct_mentions:
            clean_name = m.lstrip("@").strip()
            try:
                resolved = resolve_path(clean_name, ws)
                if os.path.isfile(resolved) and not is_sensitive_file(resolved):
                    allowed_paths.add(_normalize_real_path(resolved))
            except Exception:
                pass

    return allowed_paths


def is_path_in_scope(
    target_path: str,
    allowed_scope: set,
    workspace: str,
    mode: str = "write",
    search_roots: list = None,
) -> bool:
    """
    ตรวจสอบขอบเขตการเข้าถึงไฟล์แบบแยกสิทธิ์ Read / Write (Fail-Closed by Default):
    - mode='read'  : อนุญาตให้อ่านไฟล์ใน active_ws, search_roots และไฟล์ใน allowed_scope ได้เสมอ
    - mode='write' : หากมี allowed_scope ให้เขียนได้เฉพาะไฟล์ที่ระบุ; หากไม่มี ให้เขียนได้เฉพาะใน active_ws
    """
    if not target_path or not str(target_path).strip():
        return False

    ws = workspace if (workspace and os.path.isdir(workspace)) else os.getcwd()
    target_real = _normalize_real_path(target_path)
    norm_allowed = {_normalize_real_path(p) for p in (allowed_scope or set()) if p}

    if str(mode or "write").lower() == "read":
        if target_real in norm_allowed:
            return True
        if _is_within_directory(target_real, ws):
            return True
        effective_roots = search_roots if search_roots is not None else get_search_roots(ws)
        for root in (effective_roots or []):
            if root and os.path.isdir(root) and _is_within_directory(target_real, root):
                return True
        return False

    # โหมด Write (ค่าเริ่มต้นแบบเข้มงวด): ล็อกเฉพาะไฟล์ใน allowed_scope หรือไม่ให้ออกนอก active_ws
    if norm_allowed:
        return target_real in norm_allowed
    return _is_within_directory(target_real, ws)


def mask_secrets(text):
    if not text:
        return text
    return SECRET_MASK_RE.sub(r"\1***REDACTED***", text)


def set_session_cancelled(session_id, flag=True):
    if not session_id:
        return
    with state_lock:
        if flag:
            cancelled_sessions.add(session_id)
        else:
            cancelled_sessions.discard(session_id)


def is_session_cancelled(session_id):
    if not session_id:
        return False
    with state_lock:
        return session_id in cancelled_sessions


def update_live_status(session_id, step_text, iteration=0, reset=False):
    if not session_id:
        return
    with state_lock:
        if reset:
            live_status[session_id] = {
                "step": step_text,
                "iteration": iteration,
                "steps": [step_text] if step_text else [],
                "updatedAt": time.time(),
            }
            return

        entry = live_status.get(session_id, {"step": "", "iteration": 0, "steps": [], "updatedAt": 0.0})
        entry["step"] = step_text
        entry["iteration"] = iteration
        entry["updatedAt"] = time.time()
        steps = list(entry.get("steps", []))
        if step_text and (not steps or steps[-1] != step_text):
            steps.append(step_text)
        if len(steps) > 15:
            steps = steps[-15:]
        entry["steps"] = steps
        live_status[session_id] = entry


def get_live_status(session_id):
    with state_lock:
        entry = live_status.get(session_id)
        if not entry:
            return {"step": "", "iteration": 0, "steps": [], "updatedAt": 0.0}
        return {
            "step": entry.get("step", ""),
            "iteration": entry.get("iteration", 0),
            "steps": list(entry.get("steps", [])),
            "updatedAt": entry.get("updatedAt", 0.0),
        }


def get_search_roots(workspace=None):
    """คืนค่ารายการ Search Roots ที่รวม active_ws, current_workspace, sys_path_dir และ Desktop เสมอ"""
    with state_lock:
        ws_default = current_workspace
    active_ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default
    return build_search_roots(active_ws=active_ws, extra_roots=[ws_default, sys_path_dir, DESKTOP_DIR])


def resolve_path(path, workspace=None):
    with state_lock:
        ws_default = current_workspace
    active_ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default
    if not path or str(path).strip() in ("", "."):
        return os.path.abspath(active_ws)
    p = os.path.expandvars(str(path).strip().strip('"').strip("'"))
    if os.path.isabs(p) and os.path.exists(p):
        return os.path.abspath(p)

    candidate = os.path.abspath(os.path.join(active_ws, p))
    if os.path.exists(candidate):
        return candidate

    found = find_file_in_roots(p, search_roots=get_search_roots(active_ws), active_ws=active_ws)
    if found:
        return str(found)

    return candidate if not os.path.isabs(p) else os.path.abspath(p)


def is_text_code_file(filename):
    if is_sensitive_file(filename):
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTS or filename in ALLOWED_NAMES


def load_sessions_store():
    with state_lock:
        if not os.path.isfile(SESSIONS_FILE):
            return []
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []


def save_sessions_store(sessions_list):
    if not isinstance(sessions_list, list):
        return False
    tmp_path = SESSIONS_FILE + ".tmp"
    with state_lock:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(sessions_list, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, SESSIONS_FILE)
            return True
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False


def log_conversation(model, level, user_prompt, ai_answer, attached_files, session_id=None, session_title=None, workspace=None, usage=None):
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "sessionId": session_id or "default",
        "sessionTitle": session_title or "การสนทนาทั่วไป",
        "workspace": workspace or current_workspace,
        "model": model,
        "thinkingLevel": level,
        "user_prompt": user_prompt,
        "ai_answer": ai_answer,
        "files": attached_files,
        "usage": usage or {},
    }
    with state_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _clamp_read_pointer_unlocked(total_lines):
    """ปรับปรุงค่าใน last_read_pointer.txt ไม่ให้เกินจำนวนบรรทัดใหม่ของ conversations.jsonl"""
    if not os.path.isfile(POINTER_FILE):
        return
    try:
        with open(POINTER_FILE, "r", encoding="utf-8") as pf:
            cur = int(pf.read().strip() or "0")
        if cur > total_lines:
            with open(POINTER_FILE, "w", encoding="utf-8") as pf:
                pf.write(str(total_lines))
    except Exception:
        pass


def sync_delete_conversations_log(session_id=None, remaining_user_prompts=None, sessions_list=None, clear_all=False, deleted_session_id=None):
    """ลบรายการใน conversations.jsonl ให้ตรงกับประวัติที่เหลืออยู่บนหน้าแดชบอร์ด"""
    from collections import Counter

    if sessions_list is not None:
        save_sessions_store(sessions_list)

    with state_lock:
        if not os.path.isfile(LOG_FILE):
            return {"ok": True, "removed": 0, "remaining": 0}

        if clear_all:
            tmp_log = LOG_FILE + ".tmp"
            with open(tmp_log, "w", encoding="utf-8") as f:
                f.write("")
            os.replace(tmp_log, LOG_FILE)
            _clamp_read_pointer_unlocked(0)
            return {"ok": True, "removed": -1, "remaining": 0}

        with open(LOG_FILE, "r", encoding="utf-8") as f:
            raw_lines = [ln for ln in f.readlines() if ln.strip()]

        active_ids = None
        if isinstance(sessions_list, list):
            active_ids = {str(s.get("id")) for s in sessions_list if isinstance(s, dict) and s.get("id")}

        rem_counter = Counter(str(p) for p in (remaining_user_prompts or [])) if (session_id and remaining_user_prompts is not None) else None
        kept_lines = []
        removed = 0

        for ln in raw_lines:
            try:
                entry = json.loads(ln)
            except Exception:
                kept_lines.append(ln if ln.endswith("\n") else ln + "\n")
                continue

            entry_sid = entry.get("sessionId")
            if deleted_session_id and entry_sid == deleted_session_id:
                removed += 1
                continue

            if active_ids is not None and entry_sid and entry_sid.startswith("chat_") and entry_sid not in active_ids:
                removed += 1
                continue

            if rem_counter is not None and entry_sid == session_id:
                up = str(entry.get("user_prompt", ""))
                if rem_counter.get(up, 0) > 0:
                    rem_counter[up] -= 1
                    kept_lines.append(ln if ln.endswith("\n") else ln + "\n")
                else:
                    removed += 1
                continue

            kept_lines.append(ln if ln.endswith("\n") else ln + "\n")

        tmp_log = LOG_FILE + ".tmp"
        with open(tmp_log, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)
        os.replace(tmp_log, LOG_FILE)
        _clamp_read_pointer_unlocked(len(kept_lines))
        return {"ok": True, "removed": removed, "remaining": len(kept_lines)}


def list_workspace(workspace=None):
    with state_lock:
        ws_default = current_workspace
    ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default
    items = []
    if os.path.isdir(ws):
        for root, dirs, files in os.walk(ws):
            dirs[:] = sorted([d for d in dirs if d not in IGNORED_DIRS])
            for f in sorted(files):
                if is_text_code_file(f):
                    rel = os.path.relpath(os.path.join(root, f), ws)
                    items.append(rel)
    return items


def read_file_lines(path, start_line=1, end_line=400, workspace=None):
    """อ่านไฟล์เฉพาะช่วงบรรทัดที่ต้องการเพื่อประหยัด Token พร้อมกรองไฟล์ความลับ"""
    full_path = resolve_path(path, workspace)
    if is_sensitive_file(full_path):
        return "🔒 Security Block: ไม่อนุญาตให้อ่านหรือแก้ไขไฟล์ความลับ (.env / private key / credentials) เพื่อป้องกันข้อมูลรั่วไหลขึ้น Cloud"
    if not os.path.isfile(full_path):
        return f"Error: File '{path}' not found (resolved: {full_path})."
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        if total_lines == 0:
            return f"File: {full_path} (Empty file, 0 lines)"

        s_line = max(1, int(start_line or 1))
        req_end = int(end_line) if end_line else (s_line + 399)
        e_line = min(total_lines, req_end, s_line + 500)
        if e_line < s_line:
            e_line = min(total_lines, s_line + 399)

        selected = lines[s_line - 1:e_line]
        numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=s_line)]
        output = f"File: {full_path} (Lines {s_line}-{e_line} of {total_lines})\n" + "".join(numbered)
        output = mask_secrets(output)
        if len(output) > 45000:
            output = output[:45000] + "\n...[truncated to 45,000 chars]..."
        return output
    except Exception as e:
        return f"Error reading file '{path}': {e}"


def search_in_codebase(query, file_pattern="", path=".", max_matches=30, workspace=None):
    """ค้นหาข้อความ/Regex ในโปรเจกต์หรือโฟลเดอร์ที่ระบุ เลียนแบบ grep ใน IDE"""
    if not query or not str(query).strip():
        return "Error: 'query' parameter is required."

    query_str = str(query).strip()
    query_lower = query_str.lower()
    search_root = resolve_path(path or ".", workspace)
    if not os.path.exists(search_root):
        return f"Error: Path '{path}' not found (resolved: {search_root})."
    if os.path.isfile(search_root) and is_sensitive_file(search_root):
        return "🔒 Security Block: ไม่อนุญาตให้ค้นหาภายในไฟล์ความลับ (.env / private key / credentials)"

    try:
        regex_obj = re.compile(query_str, re.IGNORECASE)
    except re.error:
        regex_obj = None

    limit = min(max(1, int(max_matches or 30)), 50)
    pattern_lower = str(file_pattern or "").strip().lower()
    matches = []

    def scan_file(full_fp, display_fp):
        if len(matches) >= limit or is_sensitive_file(full_fp):
            return
        try:
            if os.path.getsize(full_fp) > 2 * 1024 * 1024:
                return
            with open(full_fp, "r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, 1):
                    if query_lower in line.lower() or (regex_obj and regex_obj.search(line)):
                        matches.append(f"{display_fp}:{line_no}: {line.strip()[:160]}")
                        if len(matches) >= limit:
                            return
        except Exception:
            return

    if os.path.isfile(search_root):
        scan_file(search_root, search_root)
    else:
        for root, dirs, files in os.walk(search_root):
            dirs[:] = sorted([d for d in dirs if d not in IGNORED_DIRS])
            for fname in sorted(files):
                if not is_text_code_file(fname):
                    continue
                full_fp = os.path.join(root, fname)
                rel_fp = os.path.relpath(full_fp, search_root)
                if pattern_lower:
                    if not (fnmatch.fnmatch(fname.lower(), pattern_lower) or pattern_lower in rel_fp.lower()):
                        continue
                scan_file(full_fp, rel_fp)
                if len(matches) >= limit:
                    break
            if len(matches) >= limit:
                break

    if not matches:
        return f"No matches found for '{query_str}' in {search_root}."
    return mask_secrets(f"Search root: {search_root} ({len(matches)} match(es)):\n" + "\n".join(matches))


def list_directory_contents(path=".", workspace=None):
    """แสดงรายชื่อโฟลเดอร์และไฟล์ในไดเรกทอรีที่ระบุ"""
    target_dir = resolve_path(path or ".", workspace)
    if not os.path.isdir(target_dir):
        return f"Error: Directory '{path}' not found (resolved: {target_dir})."

    try:
        entries = os.listdir(target_dir)
    except Exception as e:
        return f"Error listing directory '{path}': {e}"

    dirs_list = []
    files_list = []
    for name in sorted(entries, key=lambda s: s.lower()):
        full_p = os.path.join(target_dir, name)
        if os.path.isdir(full_p):
            suffix = " (ignored/cache)" if name in IGNORED_DIRS else ""
            dirs_list.append(f"[DIR]  {name}/{suffix}")
        elif os.path.isfile(full_p):
            if is_sensitive_file(name):
                files_list.append(f"[FILE] {name} (🔒 blocked/sensitive)")
                continue
            try:
                size = os.path.getsize(full_p)
                files_list.append(f"[FILE] {name} ({size} bytes)")
            except OSError:
                files_list.append(f"[FILE] {name}")

    combined = (dirs_list + files_list)[:150]
    total_count = len(dirs_list) + len(files_list)
    header = f"Directory: {target_dir} ({total_count} entries"
    if total_count > 150:
        header += ", showing first 150"
    header += ")\n"
    return header + ("\n".join(combined) if combined else "(Empty directory)")


def find_files_by_name(pattern, path=".", max_results=50, workspace=None):
    """ค้นหาไฟล์ตามชื่อหรือ pattern ภายในโฟลเดอร์ที่ระบุ"""
    if not pattern or not str(pattern).strip():
        return "Error: 'pattern' parameter is required."

    pat_lower = str(pattern).strip().lower()
    target_dir = resolve_path(path or ".", workspace)
    if not os.path.isdir(target_dir):
        return f"Error: Directory '{path}' not found (resolved: {target_dir})."

    limit = min(max(1, int(max_results or 50)), 100)
    results = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = sorted([d for d in dirs if d not in IGNORED_DIRS])
        for fname in sorted(files):
            if is_sensitive_file(fname):
                continue
            full_fp = os.path.join(root, fname)
            rel_fp = os.path.relpath(full_fp, target_dir)
            if fnmatch.fnmatch(fname.lower(), pat_lower) or pat_lower in rel_fp.lower():
                results.append(rel_fp)
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    if not results:
        return f"No files matching '{pattern}' found in {target_dir}."
    return f"Found {len(results)} file(s) matching '{pattern}' in {target_dir}:\n" + "\n".join(results)


OUTLINE_PATTERNS = [
    re.compile(r'^\s*(?:async\s+)?def\s+\w+'),
    re.compile(r'^\s*(?:export\s+(?:default\s+)?)?class\s+\w+'),
    re.compile(r'^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+\w+'),
    re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?:\([^)]*\)|[a-zA-Z0-9_]+)\s*=>'),
    re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?function\b'),
    re.compile(r'^\s*export\s+(?:async\s+|default\s+)?(?:function|class|const|interface|type)\s+\w+'),
    re.compile(r'^\s*window\.[a-zA-Z0-9_]+\s*='),
    re.compile(r'^\s*<(?:script|section|header|main|aside|footer)\b[^>]*>', re.IGNORECASE),
]


def get_code_outline(path, workspace=None):
    """สกัดโครงสร้าง class, def, function, import ด้วย AST / Smart Regex เพื่อประหยัด Token สูงสุด"""
    full_path = resolve_path(path, workspace)
    if is_sensitive_file(full_path):
        return "🔒 Security Block: ไม่อนุญาตให้อ่านไฟล์ความลับ (.env / private key / credentials)"
    if not os.path.isfile(full_path):
        return f"Error: File '{path}' not found (resolved: {full_path})."
    try:
        out = generate_smart_outline(full_path)
        return mask_secrets(out)
    except Exception as e:
        return f"Error building smart outline for '{path}': {e}"


def run_git_status_diff(path=".", target="", workspace=None):
    """ตรวจสอบสถานะ Git และความเปลี่ยนแปลงล่าสุด (git status + git diff) แบบ Read-Only"""
    resolved = resolve_path(path or ".", workspace)
    if os.path.isfile(resolved):
        if not target:
            target = os.path.basename(resolved)
        target_dir = os.path.dirname(resolved)
    else:
        target_dir = resolved

    if not os.path.isdir(target_dir):
        return f"Error: Directory '{path}' not found (resolved: {target_dir})."

    if target and is_sensitive_file(target):
        return "🔒 Security Block: ไม่อนุญาตให้ดู diff ของไฟล์ความลับ"

    try:
        st = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, cwd=target_dir
        )
        if st.returncode != 0:
            err_msg = (st.stderr or st.stdout or "Not a git repository").strip()
            return f"Git unavailable in {target_dir}: {err_msg}"

        diff_stat = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, cwd=target_dir
        )

        sections = [
            f"Git Repository: {target_dir}",
            "=== git status --short ===",
            st.stdout.strip() or "(Clean working tree)",
            "=== git diff --stat ===",
            diff_stat.stdout.strip() or "(No unstaged diff)",
        ]

        if target:
            diff_full = subprocess.run(
                ["git", "diff", "--", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, cwd=target_dir
            )
            diff_text = diff_full.stdout.strip()
            if len(diff_text) > 15000:
                diff_text = diff_text[:15000] + "\n...[diff truncated to 15,000 chars]..."
            sections.append(f"=== git diff -- {target} ===")
            sections.append(diff_text or "(No diff for target)")

        return mask_secrets("\n\n".join(sections))
    except FileNotFoundError:
        return "Error: 'git' command is not installed or not in PATH."
    except subprocess.TimeoutExpired:
        return "Error: Git command timed out after 10 seconds."
    except Exception as e:
        return f"Error running git status/diff: {e}"


def create_edit_proposal(path, start_line, end_line, new_code, description, workspace=None):
    """สร้างข้อเสนอการแก้ไขไฟล์ (Diff Preview) โดยยังไม่เขียนทับไฟล์จริงจนกว่าผู้ใช้จะกดยืนยัน"""
    full_path = resolve_path(path, workspace)
    if is_sensitive_file(full_path):
        return (
            "🔒 Security Block: ไม่อนุญาตให้อ่านหรือแก้ไขไฟล์ความลับ (.env / private key / credentials)",
            f"propose_edit({os.path.basename(full_path)}:blocked)",
            None,
        )
    if not os.path.isfile(full_path):
        return f"Error: File '{path}' not found (resolved: {full_path}).", f"propose_edit({path}:not_found)", None

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total_lines = len(lines)
        if total_lines == 0:
            s_line, e_line = 1, 1
            old_code = ""
        else:
            s_line = max(1, min(int(start_line or 1), total_lines))
            e_line = max(s_line, min(int(end_line or s_line), total_lines))
            old_code = "".join(lines[s_line - 1:e_line])

        with state_lock:
            ws_default = current_workspace
        ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default
        try:
            rel_path = os.path.relpath(full_path, ws)
            if rel_path.startswith(".."):
                rel_path = full_path
        except ValueError:
            rel_path = full_path

        proposal = {
            "id": f"edit_{uuid.uuid4().hex[:8]}",
            "path": full_path,
            "relPath": rel_path,
            "startLine": s_line,
            "endLine": e_line,
            "oldCode": old_code,
            "newCode": new_code or "",
            "description": description or "แก้ไขโค้ดตามข้อเสนอ",
            "applied": False,
            "rejected": False,
        }
        badge = f"propose_edit({os.path.basename(full_path)}:{s_line}-{e_line})"
        msg = (
            f"สร้าง Diff Preview สำหรับไฟล์ '{rel_path}' บรรทัด {s_line}-{e_line} เรียบร้อยแล้ว "
            f"(ยังไม่ได้แก้ไขไฟล์จริง จนกว่าผู้ใช้จะกดปุ่มยืนยันบนหน้าจอ)"
        )
        return msg, badge, proposal
    except Exception as e:
        return f"Error creating edit proposal for '{path}': {e}", f"propose_edit({path}:error)", None


def apply_edit_proposal(path, start_line, end_line, old_code, new_code, workspace=None, allow_shrink=False):
    """นำโค้ดใหม่ไปเขียนทับไฟล์จริงอย่างปลอดภัยผ่าน safe_write_code_file (มี Per-File Lock, Snippet Guard และสร้าง .bak จุดเดียว)"""
    full_path = resolve_path(path, workspace)
    if is_sensitive_file(full_path):
        return {"ok": False, "error": "🔒 Security Block: ไม่อนุญาตให้แก้ไขไฟล์ความลับ (.env / key / credentials)"}
    if not os.path.isfile(full_path):
        return {"ok": False, "error": f"ไม่พบไฟล์: {full_path}"}

    try:
        with hold(get_file_lock(full_path), 30, full_path):
            raw_text_orig, _ = read_text_meta(full_path)
            raw_text = raw_text_orig.replace("\r\n", "\n")
            lines = raw_text.splitlines(keepends=True)

            total_lines = len(lines)
            normalized_new = (new_code if new_code is not None else "").replace("\r\n", "\n")
            norm_old = (old_code or "").replace("\r\n", "\n")
            if norm_old.endswith("\n") and normalized_new and not normalized_new.endswith("\n"):
                normalized_new += "\n"

            if total_lines == 0:
                updated_text = normalized_new
            elif not norm_old:
                return {"ok": False, "stage": "missing_old_code", "error": "ต้องระบุ old_code เมื่อแก้ไขไฟล์ที่มีเนื้อหาอยู่แล้ว"}
            else:
                s_line = max(1, min(int(start_line or 1), total_lines))
                e_line = max(s_line, min(int(end_line or s_line), total_lines))
                slice_text = "".join(lines[s_line - 1:e_line])

                if norm_old and (slice_text == norm_old or slice_text.rstrip("\n") == norm_old.rstrip("\n")):
                    if slice_text.endswith("\n") and normalized_new and not normalized_new.endswith("\n"):
                        normalized_new += "\n"
                    updated_lines = lines[:s_line - 1] + [normalized_new] + lines[e_line:]
                    updated_text = "".join(updated_lines)
                elif norm_old:
                    # เรียกใช้ระบบ Smart Patching 3 ระดับ (Exact / Dedent / AST Fuzzy) ป้องกัน Line-drift
                    patch_span = locate_smart_patch_span(
                        file_path=full_path,
                        search_block=norm_old,
                        near_line=s_line,
                    )
                    if patch_span.get("ok") and not patch_span.get("requires_review"):
                        p_s = int(patch_span["startLine"])
                        p_e = int(patch_span["endLine"])
                        if lines[p_e - 1].endswith("\n") and normalized_new and not normalized_new.endswith("\n"):
                            normalized_new += "\n"
                        updated_lines = lines[:p_s - 1] + [normalized_new] + lines[p_e:]
                        updated_text = "".join(updated_lines)
                    else:
                        err_detail = patch_span.get("error") or ("Fuzzy match ต้องรีวิวก่อน" if patch_span.get("requires_review") else "ไม่พบโค้ดที่ตรงกัน")
                        return {
                            "ok": False,
                            "stage": "stale_diff",
                            "requiresReview": bool(patch_span.get("requires_review")),
                            "candidateSpan": {"startLine": patch_span.get("startLine"), "endLine": patch_span.get("endLine")},
                            "error": f"เนื้อหาต้นฉบับ (old_code) ในไฟล์มีการเปลี่ยนแปลงหรือระบุไม่ตรงจุด ({err_detail}) กรุณาสร้าง Diff Preview ใหม่ก่อนบันทึก",
                        }
                else:
                    if lines[e_line - 1].endswith("\n") and normalized_new and not normalized_new.endswith("\n"):
                        normalized_new += "\n"
                    updated_lines = lines[:s_line - 1] + [normalized_new] + lines[e_line:]
                    updated_text = "".join(updated_lines)

            # ให้ safe_write_code_file จัดการตรวจ Syntax, Snippet Guard, สำรอง .bak และเขียนแบบ Atomic เพียงจุดเดียว
            write_res = safe_write_code_file(
                file_path=full_path,
                new_content=updated_text,
                allow_shrink=allow_shrink,
                backup_dir=BACKUP_DIR,
                validate_syntax=True,
            )
            if not write_res.get("ok"):
                return {
                    "ok": False,
                    "stage": write_res.get("stage", "write_error"),
                    "error": write_res.get("error", "ระงับการบันทึกไฟล์"),
                }

            return {
                "ok": True,
                "backupPath": write_res.get("backup_path"),
                "path": full_path,
                "linesWritten": write_res.get("lines_written"),
                "validation": write_res.get("validation"),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def undo_edit_proposal(path, backup_path, workspace=None):
    """ย้อนกลับไฟล์เดิมจากไฟล์สำรอง .bak ในโฟลเดอร์ backups/ (ป้องกัน Race Condition ด้วย Per-File Lock)"""
    full_path = resolve_path(path, workspace)
    if is_sensitive_file(full_path):
        return {"ok": False, "error": "🔒 Security Block: ไม่อนุญาตให้แก้ไขไฟล์ความลับ"}
    if not backup_path:
        return {"ok": False, "error": "ไม่พบตำแหน่งไฟล์สำรอง (backupPath)"}

    abs_backup = os.path.abspath(str(backup_path))
    abs_backup_dir = os.path.abspath(BACKUP_DIR)
    try:
        common = os.path.commonpath([abs_backup, abs_backup_dir])
    except ValueError:
        common = ""

    if common != abs_backup_dir or not os.path.isfile(abs_backup):
        return {"ok": False, "error": f"ไม่พบไฟล์สำรองใน backups/: {backup_path}"}

    with get_file_lock(full_path):
        try:
            tmp_restore = f"{full_path}.{os.getpid()}.{uuid.uuid4().hex[:4]}.restore.tmp"
            shutil.copy2(abs_backup, tmp_restore)
            os.replace(tmp_restore, full_path)
            return {"ok": True, "path": full_path, "restoredFrom": abs_backup}
        except Exception as e:
            if "tmp_restore" in locals() and os.path.exists(tmp_restore):
                try:
                    os.remove(tmp_restore)
                except OSError:
                    pass
            return {"ok": False, "error": str(e)}


AGENT_TOOLS = [
    {
        "functionDeclarations": [
            {
                "name": "read_file",
                "description": "อ่านเนื้อหาไฟล์พร้อมเลขบรรทัด ระบุ start_line และ end_line เพื่ออ่านเฉพาะจุดได้ รองรับทั้ง relative path และ absolute path",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "Path ของไฟล์ที่ต้องการอ่าน"},
                        "start_line": {"type": "INTEGER", "description": "บรรทัดเริ่มต้น (ค่าเริ่มต้น 1)"},
                        "end_line": {"type": "INTEGER", "description": "บรรทัดสิ้นสุด (ค่าเริ่มต้น 400, สูงสุดไม่เกิน +500 บรรทัด)"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "get_outline",
                "description": "ดึงเฉพาะรายชื่อ class, def, function, export พร้อมเลขบรรทัดของไฟล์ใหญ่ เพื่อประหยัด Token ก่อนเลือกอ่านเฉพาะบรรทัด",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "Path ของไฟล์ที่ต้องการดูโครงสร้าง"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "git_status_diff",
                "description": "ดูสถานะไฟล์ที่ถูกแก้ไขล่าสุดใน Git (git status + git diff) ใน Workspace แบบ Read-Only",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "โฟลเดอร์โปรเจกต์ (ค่าเริ่มต้น '.')"},
                        "target": {"type": "STRING", "description": "ระบุชื่อไฟล์เจาะจงเพื่อดู diff ฉบับเต็ม (ไม่ใส่ก็ได้)"}
                    }
                }
            },
            {
                "name": "search_code",
                "description": "ค้นหาข้อความ ชื่อฟังก์ชัน ตัวแปร หรือ keyword ในโค้ดทั้งโปรเจกต์หรือโฟลเดอร์ที่ระบุ (เหมือน grep)",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING", "description": "คำหรือชื่อฟังก์ชันที่ต้องการค้นหา"},
                        "path": {"type": "STRING", "description": "โฟลเดอร์หรือไฟล์ที่ต้องการค้นหา (ค่าเริ่มต้น '.')"},
                        "file_pattern": {"type": "STRING", "description": "กรองนามสกุลหรือชื่อไฟล์ เช่น *.mjs หรือ *.py"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "list_directory",
                "description": "แสดงรายชื่อโฟลเดอร์และไฟล์ย่อยภายในโฟลเดอร์ที่ระบุ",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "Path ของโฟลเดอร์ (ใช้ '.' สำหรับ workspace ปัจจุบัน หรือระบุ absolute path เช่น C:\\...)"}
                    },
                    "required": ["path"]
                }
            },
            {
                "name": "find_by_name",
                "description": "ค้นหาไฟล์ตามชื่อหรือ glob pattern เช่น *auth*, *.mjs, server.py",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "pattern": {"type": "STRING", "description": "ชื่อไฟล์หรือ glob pattern เช่น *.mjs"},
                        "path": {"type": "STRING", "description": "โฟลเดอร์เริ่มต้นสำหรับค้นหา (ค่าเริ่มต้น '.')"}
                    },
                    "required": ["pattern"]
                }
            },
            {
                "name": "propose_edit",
                "description": "เสนอการแก้ไขโค้ดแบบ Diff Preview (ปลอดภัย 100% ยังไม่แก้ไฟล์จริงจนกว่าผู้ใช้จะกดปุ่มยืนยัน หรือ Auto-Backup ในโหมด Auto-Edit) ต้องเรียก read_file อ่านบรรทัดเป้าหมายให้แน่ใจก่อนเสมอ",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "Path ของไฟล์ที่ต้องการเสนอแก้ไข"},
                        "start_line": {"type": "INTEGER", "description": "บรรทัดเริ่มต้นที่จะถูกแทนที่ (1-indexed)"},
                        "end_line": {"type": "INTEGER", "description": "บรรทัดสิ้นสุดที่จะถูกแทนที่ (1-indexed, inclusive)"},
                        "new_code": {"type": "STRING", "description": "โค้ดใหม่ที่จะนำไปแทนที่ช่วงบรรทัดดังกล่าว"},
                        "description": {"type": "STRING", "description": "คำอธิบายสั้นๆ ว่าแก้ไขอะไรเพื่ออะไร"}
                    },
                    "required": ["path", "start_line", "end_line", "new_code", "description"]
                }
            },
            {
                "name": "ask_gemini_web",
                "description": "ส่งข้อความคำถามหรือคำทักทายไปยังหน้าเว็บ Gemini Web (Web Control บน Google Chrome ผ่าน Bridge พอร์ต 8765) โดยระบุโมเดลบนเว็บ (3.5-flash-lite, 4-hydrogen, 3.1-pro, 4-argon, 4-carbon) และระดับการคิดได้",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "prompt": {"type": "STRING", "description": "ข้อความที่ต้องการส่งไปยัง Gemini Web"},
                        "web_model": {"type": "STRING", "description": "รุ่นโมเดลบนเว็บ: 3.5-flash-lite, 4-hydrogen, 3.1-pro, 4-argon หรือ 4-carbon (ค่าเริ่มต้น 3.5-flash-lite)"},
                        "thinking_level": {"type": "STRING", "description": "ระดับการคิด: LOW, MEDIUM หรือ HIGH"}
                    },
                    "required": ["prompt"]
                }
            }
        ]
    }
]


BRIDGE_ASK_URL = "http://localhost:8765/ask"


def _post_to_bridge(prompt, web_model="3.5-flash-lite", thinking_level="HIGH", files=None, session_id=None):
    """ส่งคำสั่งถาม/สั่งงานไปยัง Gemini Web Bridge (พอร์ต 8765) แบบเรียงคิวทีละ 1 งานป้องกัน Chrome Tab ชนกัน"""
    payload = {
        "prompt": str(prompt or "").strip(),
        "webModel": str(web_model or "3.5-flash-lite").strip(),
        "thinkingLevel": str(thinking_level or "HIGH").strip(),
        "files": files or [],
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_ASK_URL,
        data=body_bytes,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with _bridge_semaphore:
        if is_session_cancelled(session_id):
            raise RuntimeError("CANCELLED_BY_USER")
        try:
            with urllib.request.urlopen(req, timeout=330) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_raw = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_raw)
                err_msg = err_json.get("error") or err_raw
            except Exception:
                err_msg = err_raw
            raise RuntimeError(f"Bridge HTTP {e.code}: {err_msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"ไม่สามารถเชื่อมต่อ Gemini Bridge (พอร์ต 8765) ได้: {e.reason}") from e


def get_tools_for_mode(agent_mode):
    tools = copy.deepcopy(AGENT_TOOLS)
    if agent_mode == "readonly":
        tools[0]["functionDeclarations"] = [
            fn for fn in tools[0]["functionDeclarations"]
            if fn.get("name") != "propose_edit"
        ]
    return tools


READ_SCOPED_TOOLS = ("read_file", "get_outline")
WRITE_SCOPED_TOOLS = ("propose_edit",)


def execute_agent_tool(fn_name, args, workspace, session_id=None, iteration=0, allowed_scope=None):
    args = args or {}

    # 🔒 ตรวจสอบสิทธิ์ Scope Lockdown แยกตามระดับสิทธิ์ Read vs Write
    if fn_name in READ_SCOPED_TOOLS:
        raw_p = args.get("path", "")
        resolved_p = resolve_path(raw_p, workspace)
        if allowed_scope is not None and not is_path_in_scope(
            resolved_p, allowed_scope, workspace, mode="read"
        ):
            err_msg = (
                f"🔒 Scope Lockdown Block: ไม่อนุญาตให้อ่านไฟล์นอกขอบเขตโปรเจกต์ '{raw_p}'! "
                f"ระบบอนุญาตให้อ่านเฉพาะไฟล์ภายใน Workspace, Search Roots หรือไฟล์ที่ผู้ใช้แนบเท่านั้น"
            )
            badge = f"{fn_name}(blocked:out_of_scope)"
            return err_msg, badge, None

    elif fn_name in WRITE_SCOPED_TOOLS:
        raw_p = args.get("path", "")
        resolved_p = resolve_path(raw_p, workspace)
        if allowed_scope is not None and not is_path_in_scope(
            resolved_p, allowed_scope, workspace, mode="write"
        ):
            allowed_names = sorted({os.path.basename(p) for p in allowed_scope}) or ["(เฉพาะภายใน Workspace ปัจจุบัน)"]
            err_msg = (
                f"🔒 Scope Lockdown Block: ไม่อนุญาตให้แก้ไขไฟล์ '{raw_p}'! "
                f"ระบบอนุญาตให้แก้ไขเฉพาะไฟล์ที่ผู้ใช้ระบุใน Prompt หรือไฟล์แนบเท่านั้น: {allowed_names}"
            )
            badge = f"{fn_name}(blocked:out_of_scope)"
            return err_msg, badge, None
    if fn_name == "read_file":
        p = args.get("path", "")
        s = int(args.get("start_line") or 1)
        e = int(args.get("end_line") or (s + 399))
        update_live_status(session_id, f"📖 กำลังอ่านไฟล์ {p} (บรรทัด {s}-{e})...", iteration)
        badge = f"read_file({p}:{s}-{e})"
        return read_file_lines(p, s, e, workspace=workspace), badge, None

    if fn_name == "get_outline":
        p = args.get("path", "")
        update_live_status(session_id, f"🧭 กำลังดึงโครงสร้างไฟล์ {p}...", iteration)
        badge = f"get_outline({p})"
        return get_code_outline(p, workspace=workspace), badge, None

    if fn_name == "git_status_diff":
        p = args.get("path", ".")
        target = args.get("target", "")
        update_live_status(session_id, f"🔀 กำลังตรวจสอบ Git status/diff ({target or p})...", iteration)
        badge = f"git_status_diff({target or p})"
        return run_git_status_diff(p, target=target, workspace=workspace), badge, None

    if fn_name == "search_code":
        q = args.get("query", "")
        p = args.get("path", ".")
        fp = args.get("file_pattern", "")
        update_live_status(session_id, f"🔎 กำลังค้นหาโค้ด '{q}' ใน {p}...", iteration)
        badge = f"search_code('{q}')"
        return search_in_codebase(q, file_pattern=fp, path=p, workspace=workspace), badge, None

    if fn_name == "list_directory":
        p = args.get("path", ".")
        update_live_status(session_id, f"📁 กำลังสำรวจโฟลเดอร์ '{p}'...", iteration)
        badge = f"list_directory('{p}')"
        return list_directory_contents(p, workspace=workspace), badge, None

    if fn_name == "find_by_name":
        pat = args.get("pattern", "")
        p = args.get("path", ".")
        update_live_status(session_id, f"🗂️ กำลังหาไฟล์ '{pat}'...", iteration)
        badge = f"find_by_name('{pat}')"
        return find_files_by_name(pat, path=p, workspace=workspace), badge, None

    if fn_name == "propose_edit":
        p = args.get("path", "")
        s = int(args.get("start_line") or 1)
        e = int(args.get("end_line") or s)
        new_code = args.get("new_code", "")
        desc = args.get("description", "")
        update_live_status(session_id, f"🛠️ กำลังจัดทำ Diff Preview สำหรับ {p} (บรรทัด {s}-{e})...", iteration)
        msg, badge, proposal = create_edit_proposal(p, s, e, new_code, desc, workspace=workspace)
        return msg, badge, proposal

    if fn_name == "ask_gemini_web":
        prompt_arg = str(args.get("prompt", "")).strip()
        web_m = str(args.get("web_model") or "3.5-flash-lite").strip()
        web_lvl = str(args.get("thinking_level") or "HIGH").strip()
        update_live_status(session_id, f"🌐 กำลังส่งข้อความไปยัง Gemini Web ({web_m})...", iteration)
        badge = f"ask_gemini_web({web_m}:{web_lvl})"
        if not prompt_arg:
            return "Error: 'prompt' parameter is required.", badge, None
        try:
            res = _post_to_bridge(prompt_arg, web_model=web_m, thinking_level=web_lvl, session_id=session_id)
            if not res.get("ok"):
                return f"Bridge Error: {res.get('error', 'Unknown error')}", badge, None
            ans_text = res.get("answer", "")
            used_label = res.get("appliedModel") or web_m
            return f"[ตอบกลับจาก Gemini Web ({used_label})]:\n{ans_text}", badge, None
        except Exception as exc:
            return f"Error calling Gemini Web Bridge: {exc}", badge, None

    return f"Error: Unknown tool '{fn_name}'.", f"{fn_name}(unknown)", None


def _sleep_cancellable(seconds, session_id=None):
    """รอเวลาแบบตรวจสอบการกดปุ่ม Stop ของผู้ใช้ทุกๆ 0.4 วินาที"""
    deadline = time.monotonic() + float(seconds or 0)
    while time.monotonic() < deadline:
        if is_session_cancelled(session_id):
            raise RuntimeError("CANCELLED_BY_USER")
        time.sleep(min(0.4, max(0.0, deadline - time.monotonic())))


AI_STUDIO_MODEL_MAP = {
    "gemini-3.8-flash": "gemini-2.5-flash",
    "gemini-4-hydrogen": "gemini-2.5-flash",
    "gemini-4-argon": "gemini-2.5-pro",
    "gemini-4-carbon": "gemini-2.5-pro",
}

def resolve_ai_studio_model(model_name):
    """แปลงชื่อโมเดลให้ตรงกับ Endpoint ของ Google AI Studio (generativelanguage.googleapis.com)"""
    m = str(model_name or "").strip()
    if m.startswith("models/"):
        m = m[7:]
    if m in AI_STUDIO_MODEL_MAP:
        return AI_STUDIO_MODEL_MAP[m]
    return m or "gemini-2.5-flash"

def load_oss_connection_settings():
    """อ่านค่าการตั้งค่าการเชื่อมต่อปัจจุบันจาก oss_settings.json และ .env"""
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    default_mode = "aistudio" if env_key else "web"
    cfg = {
        "connectionMode": default_mode,
        "gcpProjectId": PROJECT_ID,
        "gcpLocation": LOCATION,
        "hasApiKey": bool(env_key),
        "maskedApiKey": (env_key[:6] + "..." + env_key[-4:]) if len(env_key) >= 12 else ("***" if env_key else ""),
    }
    if os.path.isfile(OSS_SETTINGS_FILE):
        try:
            with open(OSS_SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                if saved.get("connectionMode") in ("web", "aistudio", "vertex"):
                    cfg["connectionMode"] = saved["connectionMode"]
                if saved.get("gcpProjectId"):
                    cfg["gcpProjectId"] = str(saved["gcpProjectId"]).strip()
                if saved.get("gcpLocation"):
                    cfg["gcpLocation"] = str(saved["gcpLocation"]).strip()
        except Exception:
            pass
    return cfg

def save_oss_connection_settings(data):
    """บันทึกการตั้งค่าการเชื่อมต่อลง oss_settings.json และอัปเดต GEMINI_API_KEY ลง .env"""
    global PROJECT_ID, LOCATION
    mode = str(data.get("connectionMode") or "web").strip().lower()
    if mode not in ("web", "aistudio", "vertex"):
        mode = "web"

    api_key = re.sub(r"[\r\n]+", "", str(data.get("apiKey") or "")).strip()
    proj_id = str(data.get("gcpProjectId") or PROJECT_ID).strip() or "your-gcp-project-id"
    region = str(data.get("gcpLocation") or LOCATION).strip() or "us-central1"

    with state_lock:
        PROJECT_ID = proj_id
        LOCATION = region
        os.environ["GOOGLE_CLOUD_PROJECT"] = proj_id
        os.environ["GOOGLE_CLOUD_LOCATION"] = region

        # บันทึกค่าลง oss_settings.json (ไม่เก็บ API Key ใน JSON เพื่อความปลอดภัย)
        settings_obj = {
            "connectionMode": mode,
            "gcpProjectId": proj_id,
            "gcpLocation": region,
            "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        tmp_cfg = OSS_SETTINGS_FILE + ".tmp"
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            json.dump(settings_obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp_cfg, OSS_SETTINGS_FILE)

        # หากมีการส่ง apiKey ใหม่มา (และไม่ใช่ค่าที่ถูก Mask) ให้อัปเดตลง .env และ os.environ
        if api_key and "***" not in api_key and "..." not in api_key:
            os.environ["GEMINI_API_KEY"] = api_key
            existing_lines = []
            if os.path.isfile(ENV_FILE):
                with open(ENV_FILE, "r", encoding="utf-8") as ef:
                    existing_lines = ef.readlines()
            updated_lines = []
            found_key = False
            for ln in existing_lines:
                if ln.strip().startswith("GEMINI_API_KEY="):
                    updated_lines.append(f"GEMINI_API_KEY={api_key}\n")
                    found_key = True
                else:
                    updated_lines.append(ln if ln.endswith("\n") else ln + "\n")
            if not found_key:
                updated_lines.append(f"GEMINI_API_KEY={api_key}\n")
            tmp_env = ENV_FILE + ".tmp"
            with open(tmp_env, "w", encoding="utf-8") as ef:
                ef.writelines(updated_lines)
            os.replace(tmp_env, ENV_FILE)
        elif data.get("clearApiKey"):
            os.environ.pop("GEMINI_API_KEY", None)
            if os.path.isfile(ENV_FILE):
                with open(ENV_FILE, "r", encoding="utf-8") as ef:
                    kept = [ln for ln in ef.readlines() if not ln.strip().startswith("GEMINI_API_KEY=")]
                tmp_env = ENV_FILE + ".tmp"
                with open(tmp_env, "w", encoding="utf-8") as ef:
                    ef.writelines(kept)
                os.replace(tmp_env, ENV_FILE)

    return load_oss_connection_settings()

def resolve_effective_api_key(request_api_key=None):
    """ดึง API Key ตามลำดับความสำคัญ: Request Header/Payload -> os.environ -> .env"""
    req_k = str(request_api_key or "").strip()
    if req_k and "***" not in req_k and "..." not in req_k:
        return req_k
    env_k = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_k:
        return env_k
    load_dotenv_file()
    return os.environ.get("GEMINI_API_KEY", "").strip()

def call_ai_studio_api(api_key, model, payload, session_id=None):
    """Adapter ยิงตรงไปยัง Google AI Studio (generativelanguage.googleapis.com/v1beta)"""
    if not api_key:
        raise RuntimeError("ไม่พบ Google AI Studio API Key กรุณากดปุ่ม '⚙️ ตั้งค่าการเชื่อมต่อ' เพื่อใส่คีย์ AIzaSy...")

    studio_model = resolve_ai_studio_model(model)
    encoded_key = urllib.parse.quote(api_key, safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{studio_model}:generateContent?key={encoded_key}"

    # ปรับ Payload ให้เข้ากับสเปกของ AI Studio v1beta (แปลง thinkingLevel -> thinkingBudget)
    studio_payload = copy.deepcopy(payload)
    gen_cfg = studio_payload.get("generationConfig", {})
    t_cfg = gen_cfg.get("thinkingConfig")
    if isinstance(t_cfg, dict) and "thinkingLevel" in t_cfg:
        lvl = str(t_cfg.pop("thinkingLevel")).upper()
        budget_map = {"LOW": 1024, "MEDIUM": 4096, "HIGH": 16384}
        t_cfg["thinkingBudget"] = budget_map.get(lvl, 4096)

    stripped_thinking_fallback = False

    for attempt in range(MAX_RETRIES):
        if is_session_cancelled(session_id):
            raise RuntimeError("CANCELLED_BY_USER")
        body_bytes = json.dumps(studio_payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                with state_lock:
                    MODEL_QUOTA_CACHE[model] = {"status": "ok", "label": "พร้อมใช้ (AI Studio)", "color": "emerald", "ts": time.time()}
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 400 and "thinkingConfig" in err_body and not stripped_thinking_fallback:
                stripped_thinking_fallback = True
                studio_payload.get("generationConfig", {}).pop("thinkingConfig", None)
                continue
            if e.code in (400, 403) and ("API_KEY_INVALID" in err_body or "API key not valid" in err_body):
                raise RuntimeError("Google AI Studio API Key ไม่ถูกต้องหรือหมดอายุ กรุณาตรวจสอบคีย์ใน '⚙️ ตั้งค่าการเชื่อมต่อ'") from e
            if e.code == 429:
                with state_lock:
                    MODEL_QUOTA_CACHE[model] = {"status": "exhausted", "label": "ติดเพดาน 429", "color": "rose", "ts": time.time()}
            if e.code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait_sec = 5.0 * (attempt + 1)
                update_live_status(
                    session_id,
                    f"⏳ AI Studio ติดลิมิต ({e.code}) กำลังพักรอ {wait_sec:.0f} วินาทีแล้วยิงซ้ำอัตโนมัติ (ครั้งที่ {attempt + 1}/{MAX_RETRIES - 1})..."
                )
                _sleep_cancellable(wait_sec, session_id)
                continue
            raise RuntimeError(f"AI Studio HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, ConnectionError) as e:
            if attempt < MAX_RETRIES - 1:
                wait_sec = 2.5 * (attempt + 1)
                update_live_status(
                    session_id,
                    f"⚠️ การเชื่อมต่อ AI Studio สะดุด ({e}) กำลังลองใหม่อัตโนมัติ (ครั้งที่ {attempt + 1}/{MAX_RETRIES - 1})..."
                )
                _sleep_cancellable(wait_sec, session_id)
                continue
            raise RuntimeError(f"AI Studio Network/Timeout Error after retries: {e}") from e

    raise RuntimeError("AI Studio request failed after retries.")

def call_vertex_api(creds, model, payload, session_id=None, project_id=None, location=None):
    target_project = project_id or PROJECT_ID
    target_location = location or LOCATION
    url = f"https://aiplatform.googleapis.com/v1/projects/{target_project}/locations/{target_location}/publishers/google/models/{model}:generateContent"
    body_bytes = json.dumps(payload).encode("utf-8")

    for attempt in range(MAX_RETRIES):
        if is_session_cancelled(session_id):
            raise RuntimeError("CANCELLED_BY_USER")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                with state_lock:
                    MODEL_QUOTA_CACHE[model] = {"status": "ok", "label": "พร้อมใช้", "color": "emerald", "ts": time.time()}
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                with state_lock:
                    MODEL_QUOTA_CACHE[model] = {"status": "exhausted", "label": "ติดเพดาน 429", "color": "rose", "ts": time.time()}
            if e.code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait_sec = 5.0 * (attempt + 1)
                update_live_status(
                    session_id,
                    f"⏳ ติดลิมิต Cloud ({e.code}) กำลังพักรอ {wait_sec:.0f} วินาทีแล้วยิงซ้ำอัตโนมัติ (ครั้งที่ {attempt + 1}/{MAX_RETRIES - 1})..."
                )
                _sleep_cancellable(wait_sec, session_id)
                continue
            if e.code == 401 and attempt == 0:
                creds.refresh(Request())
                continue
            raise RuntimeError(f"Vertex AI HTTP {e.code}: {err_body}") from e
        except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.IncompleteRead, ConnectionError) as e:
            if attempt < MAX_RETRIES - 1:
                wait_sec = 2.5 * (attempt + 1)
                update_live_status(
                    session_id,
                    f"⚠️ การเชื่อมต่อสะดุด ({e}) กำลังลองเชื่อมต่อใหม่อัตโนมัติ (ครั้งที่ {attempt + 1}/{MAX_RETRIES - 1})..."
                )
                _sleep_cancellable(wait_sec, session_id)
                continue
            raise RuntimeError(f"Vertex AI Network/Timeout Error after retries: {e}") from e

    raise RuntimeError("Vertex AI request failed after retries.")


def compute_cost_thb(model, prompt_tokens, output_tokens, thought_tokens):
    rates = MODEL_PRICING_USD_PER_1M.get(model, {"in": 1.25, "out": 5.00})
    cost_usd = (
        (prompt_tokens / 1_000_000.0) * rates["in"]
        + ((output_tokens + thought_tokens) / 1_000_000.0) * rates["out"]
    )
    return round(cost_usd * USD_TO_THB, 4)


def load_project_rules(workspace):
    """โหลดไฟล์กฎประจำโปรเจกต์ (GEMINI.md / RULE.md / .gemini/GEMINI.md) อัตโนมัติ"""
    if not workspace or not os.path.isdir(workspace):
        return "", []
    candidates = ("GEMINI.md", "RULE.md", os.path.join(".gemini", "GEMINI.md"))
    blocks = []
    badges = []
    for rel in candidates:
        fp = os.path.join(workspace, rel)
        if os.path.isfile(fp) and not is_sensitive_file(fp):
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(4000)
                if len(content) == 4000:
                    content += "\n...[truncated]..."
                content = mask_secrets(content).strip()
                if content:
                    blocks.append(f"--- {rel} ---\n{content}")
                    badges.append(f"project_rules({rel})")
            except Exception:
                pass
    if not blocks:
        return "", []
    return "\n\n[กฎประจำโปรเจกต์ (Project Rules อ่านอัตโนมัติ):]\n" + "\n\n".join(blocks), badges


def call_vertex(
    model,
    thinking_level,
    user_prompt,
    selected_files,
    history=None,
    session_id=None,
    session_title=None,
    workspace=None,
    images=None,
    agent_mode="diff",
    api_key=None,
    connection_mode=None,
    gcp_project_id=None,
    gcp_location=None,
):
    set_session_cancelled(session_id, False)
    if agent_mode not in ("readonly", "diff", "auto"):
        agent_mode = "diff"

    saved_cfg = load_oss_connection_settings()
    effective_mode = str(connection_mode or saved_cfg.get("connectionMode") or "").strip().lower()
    effective_api_key = resolve_effective_api_key(api_key)

    use_ai_studio = False
    creds = None
    if effective_mode == "aistudio" or (effective_api_key and effective_mode != "vertex"):
        use_ai_studio = True
        update_live_status(session_id, "🔑 กำลังเชื่อมต่อ Google AI Studio (API Key)...", 0, reset=True)
    else:
        update_live_status(session_id, "🔐 กำลังตรวจสอบสิทธิ์และเริ่มเชื่อมต่อ Vertex AI...", 0, reset=True)
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(Request())
        except Exception as auth_err:
            if effective_api_key:
                use_ai_studio = True
                update_live_status(session_id, "🔄 สลับไปใช้ Google AI Studio API Key อัตโนมัติ (ไม่พบสิทธิ์ Cloud)...", 0)
            else:
                raise RuntimeError(
                    f"ไม่สามารถยืนยันตัวตน Google Cloud ได้ และไม่พบ API Key ในการตั้งค่า ({auth_err})\n"
                    f"คำแนะนำ: กรุณากดปุ่ม '⚙️ ตั้งค่าการเชื่อมต่อ' ด้านบนขวาเพื่อเลือกใช้ 'Gemini Web ฟรี' หรือใส่ 'AI Studio API Key'"
                ) from auth_err

    with state_lock:
        ws_default = current_workspace
    active_ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default
    scanned_actions = []
    proposed_edits = []

    rules_text, rules_badges = load_project_rules(active_ws)
    for rb in rules_badges:
        if rb not in scanned_actions:
            scanned_actions.append(rb)

    if agent_mode == "readonly":
        mode_desc = """[โหมดการทำงานปัจจุบัน: 🔒 READ-ONLY MODE (อ่านวิเคราะห์โค้ดอย่างเดียว 100%)]
- ผู้ใช้เปิดโหมด Read-Only ไว้ จึงมีเฉพาะเครื่องมืออ่าน 6 ตัว (`read_file`, `get_outline`, `git_status_diff`, `search_code`, `list_directory`, `find_by_name`)
- ห้ามพยายามแก้ไฟล์ ให้วิเคราะห์ ชี้ตำแหน่งไฟล์/บรรทัด และอธิบายแนวทางแก้ไขเป็น Markdown เท่านั้น"""
    elif agent_mode == "auto":
        mode_desc = """[โหมดการทำงานปัจจุบัน: ⚡ AUTO-EDIT MODE (สำรองไฟล์และแก้ไขลงดิสก์อัตโนมัติ)]
- เมื่อคุณเรียก `propose_edit` ระบบในเครื่องผู้ใช้จะ Backup ไฟล์เดิมอัตโนมัติและเขียนทับโค้ดใหม่ลงไฟล์จริงให้ทันที
- ต้องอ่านโค้ดจริงด้วย `read_file` ก่อนเรียก `propose_edit` ทุกครั้ง และระบุเฉพาะช่วงบรรทัดที่ต้องการแก้ไขอย่างแม่นยำ"""
    else:
        mode_desc = """[โหมดการทำงานปัจจุบัน: 🛠️ ARCHITECT + DIFF MODE (เสนอ Diff Preview รอผู้ใช้กดยืนยัน)]
- เมื่อผู้ใช้ขอให้ช่วยแก้ไขโค้ดหรือแก้บั๊ก ให้เรียก `read_file` ดูเลขบรรทัดเป้าหมายให้แม่นยำก่อน แล้วใช้ `propose_edit` เพื่อส่ง Diff Preview ให้ผู้ใช้กดยืนยัน (ห้ามบอกว่าแก้ไฟล์ลงดิสก์แล้ว ให้แจ้งว่าได้สร้าง Diff Preview ให้กดปุ่มยืนยันแล้ว)"""

    system_inst = f"""คุณคือ Autonomous Senior Software Architect และ Coding Agent ประจำระบบ
Workspace ปัจจุบัน: {active_ws}

{mode_desc}

เครื่องมือสืบค้นโค้ดที่มีให้ใช้งาน:
1. `list_directory(path)`: ดูโครงสร้างโฟลเดอร์และรายชื่อไฟล์ (รองรับทั้ง relative path และ absolute path เช่น C:\\... หรือ D:\\...)
2. `find_by_name(pattern, path)`: ค้นหาไฟล์ตามชื่อหรือ pattern เช่น *.mjs
3. `get_outline(path)`: ดึงเฉพาะโครงสร้าง class, def, function, export พร้อมเลขบรรทัดสำหรับไฟล์ใหญ่ (ประหยัด Token)
4. `git_status_diff(path, target)`: ตรวจสอบสถานะการแก้ไขไฟล์ล่าสุดใน Git (`git status` + `git diff`)
5. `search_code(query, path, file_pattern)`: ค้นหาชื่อฟังก์ชัน, ตัวแปร, หรือโค้ดทั้งโปรเจกต์ (เหมือน grep)
6. `read_file(path, start_line, end_line)`: เปิดอ่านโค้ดจริงแบบระบุเลขบรรทัด (มีระบบป้องกันไฟล์ความลับ .env อัตโนมัติ)

กฎการทำงานสำคัญ:
- อย่าเดาหรือมโนเนื้อหาโค้ดที่ยังไม่ได้เปิดอ่าน ให้เรียกใช้ Tools เพื่อสืบค้นและอ่านโค้ดจริงก่อนตอบเสมอ
- สำหรับไฟล์ขนาดใหญ่ แนะนำให้ใช้ `get_outline` ก่อน แล้วค่อยใช้ `read_file` เจาะจงช่วงบรรทัด
- ตอบเป็นภาษาไทยอย่างละเอียด เป็นโครงสร้าง (Markdown + Code blocks) ชี้ตำแหน่งไฟล์และเลขบรรทัดที่ตรวจพบจริง{rules_text}"""

    attached_blocks = []
    for sf in (selected_files or []):
        file_text = read_file_lines(sf, 1, 400, workspace=active_ws)
        attached_blocks.append(f"\n=== ATTACHED FILE: {sf} ===\n{file_text}\n=== END FILE ===\n")
        badge = f"attached({sf})"
        if badge not in scanned_actions:
            scanned_actions.append(badge)

    search_roots = get_search_roots(active_ws)
    current_prompt_text = resolve_prompt_file_mentions(
        user_prompt, search_roots=search_roots, active_ws=active_ws, is_sensitive_fn=is_sensitive_file
    )
    if attached_blocks:
        current_prompt_text += "\n\n[ไฟล์ที่ผู้ใช้แนบมาด้วยตนเอง:]\n" + "".join(attached_blocks)

    latest_parts = []
    for img in (images or []):
        mime_type = str(img.get("mimeType") or "image/png")
        b64_data = str(img.get("data") or "")
        if "," in b64_data and b64_data.startswith("data:"):
            b64_data = b64_data.split(",", 1)[1]
        if b64_data:
            latest_parts.append({"inlineData": {"mimeType": mime_type, "data": b64_data}})
            img_badge = f"attached_image({mime_type})"
            if img_badge not in scanned_actions:
                scanned_actions.append(img_badge)
    latest_parts.append({"text": current_prompt_text})

    contents = []
    if history and isinstance(history, list):
        for msg in history:
            role = "user" if msg.get("role") == "user" else "model"
            text = str(msg.get("text", ""))
            if not text.strip():
                continue
            is_err_or_stop = (
                role == "model"
                and (
                    text.lstrip().startswith("❌ Error:")
                    or text.lstrip().startswith("❌ ไม่สามารถเชื่อมต่อ")
                    or text.lstrip().startswith("⏹️ หยุดการทำงาน")
                )
            )
            if is_err_or_stop:
                # ตัดคำสั่ง user ที่ส่งก่อน Error/Stop นี้ออกด้วย เพื่อไม่ให้ประวัติซ้ำซ้อน
                if contents and contents[-1].get("role") == "user":
                    contents.pop()
                continue
            contents.append({"role": role, "parts": [{"text": text}]})

    contents.append({"role": "user", "parts": latest_parts})

    gen_config = {
        "temperature": 0.2,
        "maxOutputTokens": 8192
    }
    lvl_upper = str(thinking_level or "").strip().upper()
    if lvl_upper in ("LOW", "MEDIUM", "HIGH"):
        if "3.8" in str(model) or "flash" in str(model).lower():
            budget_map = {"LOW": 1024, "MEDIUM": 4096, "HIGH": 16384}
            gen_config["thinkingConfig"] = {"thinkingBudget": budget_map[lvl_upper]}
        else:
            gen_config["thinkingConfig"] = {"thinkingLevel": lvl_upper}

    tools_for_request = get_tools_for_mode(agent_mode)
    read_cache = set()

    prompt_tokens = 0
    output_tokens = 0
    thought_tokens = 0
    total_tokens = 0
    iterations_run = 0
    ans = ""
    cancelled_mid_run = False
    is_gemini_4 = "gemini-4" in str(model).lower()
    did_403_fallback = False

    def accumulate_usage(resp_data):
        nonlocal prompt_tokens, output_tokens, thought_tokens, total_tokens
        um = resp_data.get("usageMetadata", {}) or {}
        prompt_tokens += int(um.get("promptTokenCount") or 0)
        output_tokens += int(um.get("candidatesTokenCount") or 0)
        thought_tokens += int(um.get("thoughtsTokenCount") or 0)
        total_tokens += int(um.get("totalTokenCount") or 0)

    # สกัดรายชื่อไฟล์ที่ได้รับอนุญาตประจำเทิร์นนี้ (Explicit Scope Lockdown)
    turn_allowed_scope = extract_prompt_allowed_scope(user_prompt, selected_files, active_ws)

    for iteration_idx in range(MAX_TOOL_ITERATIONS):
        if is_session_cancelled(session_id):
            cancelled_mid_run = True
            ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
            break

        iterations_run = iteration_idx + 1
        if iteration_idx > 0 and is_gemini_4:
            update_live_status(session_id, f"⏱️ พักจังหวะ 1.5s ป้องกันลิมิต 429 บน {model} (รอบที่ {iterations_run})...", iterations_run)
            try:
                _sleep_cancellable(1.5, session_id)
            except RuntimeError as rt_err:
                if "CANCELLED_BY_USER" in str(rt_err):
                    cancelled_mid_run = True
                    ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
                    break
                raise

        update_live_status(session_id, f"🧠 กำลังวิเคราะห์คำสั่งบน {model} (รอบที่ {iterations_run})...", iterations_run)

        payload = {
            "systemInstruction": {"parts": [{"text": system_inst}]},
            "contents": contents,
            "tools": tools_for_request,
            "generationConfig": gen_config
        }
        try:
            if use_ai_studio:
                data = call_ai_studio_api(effective_api_key, model, payload, session_id=session_id)
            else:
                data = call_vertex_api(creds, model, payload, session_id=session_id, project_id=gcp_project_id, location=gcp_location)
        except RuntimeError as rt_err:
            err_str = str(rt_err)
            if "CANCELLED_BY_USER" in err_str:
                cancelled_mid_run = True
                ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
                break
            if (
                not use_ai_studio
                and "Vertex AI HTTP 403" in err_str
                and is_gemini_4
                and not did_403_fallback
                and gen_config.get("thinkingConfig", {}).get("thinkingLevel") == "HIGH"
            ):
                did_403_fallback = True
                gen_config["thinkingConfig"] = {"thinkingLevel": "MEDIUM"}
                scanned_actions.append("thinking_fallback(HIGH->MEDIUM)")
                update_live_status(
                    session_id,
                    f"⚠️ {model} ระดับ HIGH ติดสิทธิ์ 403 กำลังปรับลดเป็น MEDIUM อัตโนมัติ...",
                    iterations_run,
                )
                payload["generationConfig"] = gen_config
                if use_ai_studio:
                    data = call_ai_studio_api(effective_api_key, model, payload, session_id=session_id)
                else:
                    data = call_vertex_api(creds, model, payload, session_id=session_id, project_id=gcp_project_id, location=gcp_location)
            else:
                raise
        accumulate_usage(data)

        if is_session_cancelled(session_id):
            cancelled_mid_run = True
            ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
            break

        candidates = data.get("candidates", [])
        if not candidates:
            ans = "ไม่สามารถสร้างคำตอบได้ (ไม่มี candidate จากโมเดล)"
            break

        cand_content = candidates[0].get("content", {})
        parts = cand_content.get("parts", [])
        function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if function_calls:
            contents.append(cand_content)
            fn_response_parts = []
            for fc in function_calls:
                if is_session_cancelled(session_id):
                    cancelled_mid_run = True
                    ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
                    break

                fn_name = fc.get("name", "")
                fn_args = fc.get("args", {}) or {}

                if fn_name == "read_file":
                    p = fn_args.get("path", "")
                    s_l = int(fn_args.get("start_line") or 1)
                    e_l = int(fn_args.get("end_line") or (s_l + 399))
                    cache_key = (resolve_path(p, active_ws), s_l, e_l)
                    if cache_key in read_cache:
                        result_str = (
                            f"[ระบบแคช: คุณได้อ่านไฟล์ '{p}' บรรทัด {s_l}-{e_l} ไปแล้วในเทิร์นนี้ "
                            f"กรุณาอ้างอิงเนื้อหาจากผลลัพธ์ด้านบนเพื่อประหยัด Token]"
                        )
                        badge = f"read_file_cached({p}:{s_l}-{e_l})"
                        proposal = None
                    else:
                        read_cache.add(cache_key)
                        result_str, badge, proposal = execute_agent_tool(
                            fn_name, fn_args, active_ws, session_id=session_id, iteration=iterations_run, allowed_scope=turn_allowed_scope
                        )
                elif fn_name == "propose_edit":
                    if agent_mode == "readonly":
                        result_str = "🔒 โหมด Read-Only ทำงานอยู่ ไม่อนุญาตให้เสนอหรือแก้ไขไฟล์"
                        badge = "propose_edit(blocked_readonly)"
                        proposal = None
                    else:
                        target_resolved = resolve_path(fn_args.get("path", ""), active_ws)
                        read_cache = {k for k in read_cache if k[0] != target_resolved}
                        result_str, badge, proposal = execute_agent_tool(
                            fn_name, fn_args, active_ws, session_id=session_id, iteration=iterations_run, allowed_scope=turn_allowed_scope
                        )
                        if agent_mode == "auto" and proposal:
                            apply_res = apply_edit_proposal(
                                proposal["path"],
                                proposal["startLine"],
                                proposal["endLine"],
                                proposal["oldCode"],
                                proposal["newCode"],
                                workspace=active_ws,
                            )
                            if apply_res.get("ok"):
                                proposal["applied"] = True
                                proposal["backupPath"] = apply_res.get("backupPath", "")
                                result_str += f" (⚡ Auto-Edit: สำรองไฟล์ที่ {apply_res.get('backupPath', '')} และแก้ไขลงดิสก์อัตโนมัติเรียบร้อยแล้ว)"
                            else:
                                result_str += f" (⚠️ Auto-Edit ล้มเหลว: {apply_res.get('error', 'Unknown error')})"
                else:
                    result_str, badge, proposal = execute_agent_tool(
                        fn_name, fn_args, active_ws, session_id=session_id, iteration=iterations_run, allowed_scope=turn_allowed_scope
                    )

                if badge not in scanned_actions:
                    scanned_actions.append(badge)
                if proposal:
                    proposed_edits.append(proposal)
                fn_response_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"result": result_str}
                    }
                })
            if cancelled_mid_run:
                break
            contents.append({"role": "user", "parts": fn_response_parts})
        else:
            text_parts = [p["text"] for p in parts if "text" in p and not p.get("thought")]
            ans = "\n".join(text_parts).strip()
            break
    else:
        if not is_session_cancelled(session_id):
            update_live_status(session_id, "📝 กำลังสรุปคำตอบจากข้อมูลทั้งหมดที่สืบค้นได้...", MAX_TOOL_ITERATIONS)
            contents.append({
                "role": "user",
                "parts": [{"text": "ครบขีดจำกัดการเรียกเครื่องมือแล้ว กรุณาสรุปผลการวิเคราะห์จากข้อมูลและไฟล์ที่อ่านมาทั้งหมดเป็นภาษาไทยทันที"}]
            })
            fallback_payload = {
                "systemInstruction": {"parts": [{"text": system_inst}]},
                "contents": contents,
                "generationConfig": gen_config
            }
            if use_ai_studio:
                data = call_ai_studio_api(effective_api_key, model, fallback_payload, session_id=session_id)
            else:
                data = call_vertex_api(
                    creds, model, fallback_payload,
                    session_id=session_id, project_id=gcp_project_id, location=gcp_location
                )
            accumulate_usage(data)
            iterations_run += 1
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                ans = "\n".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()
            else:
                ans = "ไม่สามารถสร้างคำตอบสรุปได้ (โมเดลไม่ส่ง candidate กลับมา หรือติด Safety Filter)"

    cost_thb = compute_cost_thb(model, prompt_tokens, output_tokens, thought_tokens)
    usage_summary = {
        "promptTokens": prompt_tokens,
        "outputTokens": output_tokens,
        "thoughtTokens": thought_tokens,
        "totalTokens": total_tokens or (prompt_tokens + output_tokens + thought_tokens),
        "iterations": iterations_run,
        "costThb": cost_thb,
    }

    if cancelled_mid_run or is_session_cancelled(session_id):
        update_live_status(session_id, "⏹️ หยุดการทำงานตามคำสั่งผู้ใช้แล้ว", iterations_run)
        if not ans:
            ans = "⏹️ หยุดการทำงานตามคำสั่งของผู้ใช้แล้ว"
    else:
        update_live_status(session_id, "✅ เสร็จสิ้นการวิเคราะห์", iterations_run)

    try:
        log_conversation(
            model, thinking_level, user_prompt, ans, scanned_actions,
            session_id=session_id, session_title=session_title, workspace=active_ws, usage=usage_summary
        )
    except Exception:
        pass

    return ans, scanned_actions, usage_summary, proposed_edits


def call_gemini_web_bridge(prompt, web_model="4-hydrogen", thinking_level="HIGH", session_id=None, session_title=None, workspace=None, selected_files=None):
    """ส่งข้อความไปยังหน้าเว็บ Gemini Web (Web Control) ผ่าน Local Bridge พอร์ต 8765"""
    set_session_cancelled(session_id, False)
    with state_lock:
        ws_default = current_workspace
    active_ws = workspace if (workspace and os.path.isdir(workspace)) else ws_default

    scanned_actions = []
    search_roots = get_search_roots(active_ws)

    upload_files = []

    # 1. รวบรวมไฟล์จริงจากแท็ก @ชื่อไฟล์ ในข้อความ (กรองไฟล์ความลับ และข้ามไฟล์ที่สั่ง @outline: หรือใหญ่เกินเกณฑ์)
    mentioned_paths = extract_file_mentions(
        str(prompt or ""),
        search_roots=search_roots,
        active_ws=active_ws,
        is_sensitive_fn=is_sensitive_file,
        for_binary_upload=True,
    )
    for mp in mentioned_paths:
        mp_str = str(mp)
        if mp_str not in upload_files:
            upload_files.append(mp_str)

    # 2. รวบรวมไฟล์จริงจาก selected_files (ถ้ามี) โดยกรองไฟล์ความลับออก
    if selected_files:
        for sf in selected_files:
            resolved = find_file_in_roots(sf, search_roots=search_roots, active_ws=active_ws)
            if resolved and not is_sensitive_file(str(resolved)):
                r_str = str(resolved)
                if r_str not in upload_files:
                    upload_files.append(r_str)

    for uf in upload_files:
        badge = f"upload({os.path.basename(uf)})"
        if badge not in scanned_actions:
            scanned_actions.append(badge)

    # ถ้าไม่มีไฟล์ถูกอัปโหลดเข้าท่อ ให้ fallback แทรกเนื้อหาไฟล์ลงในข้อความ prompt (พร้อมกรองความลับ)
    if upload_files:
        full_prompt = str(prompt or "").strip()
    else:
        full_prompt = resolve_prompt_file_mentions(
            str(prompt or "").strip(),
            search_roots=search_roots,
            active_ws=active_ws,
            is_sensitive_fn=is_sensitive_file,
        )
        if selected_files:
            attached_blocks = []
            for sf in selected_files:
                file_text = read_file_lines(sf, 1, 400, workspace=active_ws)
                attached_blocks.append(f"\n=== ATTACHED FILE: {sf} ===\n{file_text}\n=== END FILE ===\n")
                badge = f"attached({sf})"
                if badge not in scanned_actions:
                    scanned_actions.append(badge)
            if attached_blocks:
                full_prompt += "\n\n[ไฟล์ที่แนบมา:]\n" + "".join(attached_blocks)

    if upload_files:
        update_live_status(session_id, f"📎 กำลังส่งข้อความและแนบ {len(upload_files)} ไฟล์เข้าสู่ Gemini Web (Web Control)...", 1, reset=True)
    else:
        update_live_status(session_id, f"🌐 กำลังส่งข้อความไปยัง Gemini Web ({web_model})...", 1, reset=True)

    res = _post_to_bridge(full_prompt, web_model=web_model, thinking_level=thinking_level, files=upload_files, session_id=session_id)
    if is_session_cancelled(session_id):
        raise RuntimeError("CANCELLED_BY_USER")
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "Gemini Web Bridge ส่งคำตอบไม่สำเร็จ")

    ans = res.get("answer", "")
    applied_model = res.get("appliedModel") or web_model
    applied_thinking = res.get("appliedThinking") or thinking_level
    scanned_actions.append(f"gemini_web({applied_model})")
    if applied_thinking:
        scanned_actions.append(f"thinking({applied_thinking})")
    if res.get("selectionNote"):
        scanned_actions.append(f"note({res['selectionNote']})")

    usage_summary = {
        "promptTokens": 0,
        "outputTokens": 0,
        "thoughtTokens": 0,
        "totalTokens": 0,
        "iterations": 1,
        "costThb": 0.0,
        "elapsedMs": res.get("elapsedMs", 0),
    }
    update_live_status(session_id, "✅ ได้รับคำตอบจาก Gemini Web เรียบร้อยแล้ว", 1)

    try:
        log_conversation(
            f"web:{web_model}", thinking_level, prompt, ans, scanned_actions,
            session_id=session_id, session_title=session_title, workspace=active_ws, usage=usage_summary
        )
    except Exception:
        pass

    return ans, scanned_actions, usage_summary, []


def _atomic_write_text(target_path, content):
    """เขียนข้อความลงไฟล์ชั่วคราว (.tmp) แล้วสลับชื่อแบบ Atomic ด้วย os.replace"""
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.{uuid.uuid4().hex[:4]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, target_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def save_handoff_data(payload):
    """บันทึกคำตอบ/โค้ดลงไฟล์กลาง handoff_latest.json และ handoff_latest.md แบบ Atomic"""
    ts = str(payload.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    handoff_obj = {
        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "ts": ts,
        "sessionId": str(payload.get("sessionId") or ""),
        "sessionTitle": str(payload.get("sessionTitle") or ""),
        "workspace": str(payload.get("workspace") or current_workspace),
        "modelInfo": str(payload.get("modelInfo") or ""),
        "userPrompt": str(payload.get("userPrompt") or ""),
        "aiAnswer": str(payload.get("aiAnswer") or ""),
        "scannedFiles": payload.get("scannedFiles") or [],
        "proposedEdits": payload.get("proposedEdits") or [],
    }
    md_lines = [
        f"# Export Handoff — Vertex AI Studio OSS (Port {PORT})",
        f"- **เวลาข้อความ:** {handoff_obj['ts']}",
        f"- **บันทึกส่งต่อเมื่อ:** {handoff_obj['savedAt']}",
        f"- **Session:** {handoff_obj['sessionTitle']} (`{handoff_obj['sessionId']}`)",
        f"- **Workspace:** `{handoff_obj['workspace']}`",
        f"- **Model:** {handoff_obj['modelInfo']}",
        "",
        "## คำสั่งของผู้ใช้ (User Prompt)",
        handoff_obj["userPrompt"] or "*(ไม่มี)*",
        "",
        "## คำตอบและโค้ดที่ต้องการให้ Antigravity นำไปทำต่อ",
        handoff_obj["aiAnswer"] or "*(ไม่มี)*",
    ]
    if handoff_obj["scannedFiles"]:
        md_lines.extend(["", "## ไฟล์/เครื่องมือที่ถูกสืบค้น", "\n".join(f"- `{f}`" for f in handoff_obj["scannedFiles"])])
    if handoff_obj["proposedEdits"]:
        md_lines.extend(["", "## Proposed Edits", json.dumps(handoff_obj["proposedEdits"], ensure_ascii=False, indent=2)])

    json_text = json.dumps(handoff_obj, ensure_ascii=False, indent=2)
    md_text = "\n".join(md_lines) + "\n"

    with state_lock:
        _atomic_write_text(HANDOFF_MD_FILE, md_text)
        _atomic_write_text(HANDOFF_JSON_FILE, json_text)

    return {"ok": True, "path": HANDOFF_MD_FILE, "ts": ts}


def query_messages(ts_query="", session_id="", limit=0):
    """ค้นหาข้อความตาม timestamp (ts) หรือดึงข้อความล่าสุดจาก sessions_store.json"""
    sessions = load_sessions_store()
    if not isinstance(sessions, list):
        return []

    q = str(ts_query or "").strip()
    sid = str(session_id or "").strip()
    results = []

    if q:
        for s in sessions:
            s_id = s.get("id", "")
            if sid and s_id != sid:
                continue
            msgs = s.get("messages", [])
            included_indices = set()
            for idx, m in enumerate(msgs):
                if q in str(m.get("ts", "")):
                    included_indices.add(idx)
                    if m.get("role") != "user" and idx > 0 and msgs[idx - 1].get("role") == "user":
                        included_indices.add(idx - 1)
                    elif m.get("role") == "user" and idx + 1 < len(msgs) and msgs[idx + 1].get("role") != "user":
                        included_indices.add(idx + 1)
            for idx in sorted(included_indices):
                m = msgs[idx]
                results.append({
                    "sessionId": s_id,
                    "sessionTitle": s.get("title", ""),
                    "workspace": s.get("workspace", ""),
                    "role": m.get("role", ""),
                    "modelInfo": m.get("modelInfo", ""),
                    "ts": m.get("ts", ""),
                    "text": m.get("text", ""),
                    "scannedFiles": m.get("scannedFiles", []),
                })
        return results

    if limit > 0:
        target = None
        if sid:
            target = next((s for s in sessions if s.get("id") == sid), None)
        else:
            target = next((s for s in sessions if s.get("active")), sessions[0] if sessions else None)
        if not target:
            return []
        for m in target.get("messages", [])[-limit:]:
            results.append({
                "sessionId": target.get("id", ""),
                "sessionTitle": target.get("title", ""),
                "workspace": target.get("workspace", ""),
                "role": m.get("role", ""),
                "modelInfo": m.get("modelInfo", ""),
                "ts": m.get("ts", ""),
                "text": m.get("text", ""),
                "scannedFiles": m.get("scannedFiles", []),
            })
        return results

    for s in sessions:
        if sid and s.get("id") != sid:
            continue
        for m in s.get("messages", []):
            results.append({
                "sessionId": s.get("id", ""),
                "sessionTitle": s.get("title", ""),
                "workspace": s.get("workspace", ""),
                "role": m.get("role", ""),
                "modelInfo": m.get("modelInfo", ""),
                "ts": m.get("ts", ""),
                "text": m.get("text", ""),
                "scannedFiles": m.get("scannedFiles", []),
            })
    return results


def check_bridge_status():
    """เช็คสถานะ Gemini Web Persistent Bridge พอร์ต 8765"""
    try:
        req = urllib.request.Request("http://localhost:8765/status", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "online": True,
            "cdpConnected": bool(data.get("connected")),
            "mode": data.get("mode", ""),
            "tabUrl": (data.get("cdpStatus") or {}).get("tabUrl", ""),
        }
    except Exception as e:
        return {
            "ok": True,
            "online": False,
            "cdpConnected": False,
            "error": str(e),
        }


import ctypes
from ctypes import wintypes

_folder_dialog_lock = threading.Lock()

def get_windows_drives():
    """ดึงรายการ Drive ที่มีอยู่จริงบน Windows แบบทันที (ไม่ค้างกับ Drive ว่าง)"""
    drives = []
    if os.name == "nt":
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(65 + i)
                    root_path = f"{letter}:\\"
                    if os.path.isdir(root_path):
                        drives.append({"name": f"Drive ({letter}:)", "path": root_path})
        except Exception:
            pass
    if not drives:
        drives.append({"name": "Root (/)", "path": os.path.abspath(os.sep)})
    return drives

def browse_local_directories(target_path=""):
    """อ่านรายการโฟลเดอร์ย่อยแบบ Fast Non-blocking สำหรับ Web Folder Picker Modal (รองรับภาษาไทย 100%)"""
    with state_lock:
        ws_default = current_workspace

    raw = str(target_path or "").strip()
    if not raw:
        raw = ws_default if os.path.isdir(ws_default) else os.getcwd()

    abs_path = os.path.abspath(os.path.expandvars(raw))
    if not os.path.isdir(abs_path):
        abs_path = os.path.abspath(os.getcwd())

    parent_path = os.path.dirname(abs_path)
    if parent_path == abs_path:
        parent_path = ""

    folders = []
    try:
        with os.scandir(abs_path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    # ข้ามโฟลเดอร์ระบบของ Windows ที่ไม่สามารถเข้าถึงได้
                    if name.upper() in ("$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION", "RECOVERY"):
                        continue
                    folders.append({
                        "name": name,
                        "path": os.path.abspath(entry.path),
                        "isHidden": name.startswith("."),
                    })
                except OSError:
                    continue
    except PermissionError:
        return {
            "ok": False,
            "error": f"ไม่มีสิทธิ์เข้าถึงโฟลเดอร์: {abs_path}",
            "current": abs_path,
            "parent": parent_path,
            "drives": get_windows_drives(),
            "folders": [],
        }

    # เรียงโฟลเดอร์ปกติขึ้นก่อน โฟลเดอร์ซ่อน (.git ฯลฯ) อยู่ท้ายสุด
    folders.sort(key=lambda x: (x["isHidden"], x["name"].lower()))

    home_dir = os.path.expanduser("~")
    shortcuts = []
    for label, p in [
        ("Desktop", os.path.join(home_dir, "Desktop")),
        ("Documents", os.path.join(home_dir, "Documents")),
        ("Home", home_dir),
    ]:
        if os.path.isdir(p):
            shortcuts.append({"name": label, "path": os.path.abspath(p)})

    return {
        "ok": True,
        "current": abs_path,
        "parent": parent_path,
        "drives": get_windows_drives(),
        "shortcuts": shortcuts,
        "folders": folders[:300],
    }

def _pick_folder_win32_native(initial_dir=""):
    """เปิด Windows 10/11 Modern IFileOpenDialog (FOS_PICKFOLDERS) ด้วย ctypes COM + WS_EX_TOPMOST Owner"""
    if os.name != "nt":
        return {"ok": False, "cancelled": False, "error": "Not Windows"}

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_str(cls, s):
            u = uuid.UUID(s)
            return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    # ป้องกันภาพเบลอบนจอ High-DPI
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]

    COINIT_APARTMENTTHREADED = 0x2
    COINIT_DISABLE_OLE1DDE = 0x4
    CLSCTX_INPROC_SERVER = 0x1
    FOS_PICKFOLDERS = 0x00000020
    FOS_FORCEFILESYSTEM = 0x00000040
    FOS_PATHMUSTEXIST = 0x00000800
    SIGDN_FILESYSPATH = 0x80058000
    HRESULT_CANCELLED = -2147023673  # 0x800704C7

    CLSID_FileOpenDialog = GUID.from_str("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
    IID_IFileOpenDialog = GUID.from_str("{D57C7288-D4AD-4768-BE02-9D969532D960}")
    IID_IShellItem = GUID.from_str("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")

    hr_init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    owner_hwnd = None
    p_dialog = ctypes.c_void_p()
    p_init_item = ctypes.c_void_p()
    p_result_item = ctypes.c_void_p()

    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IFileOpenDialog), ctypes.byref(p_dialog),
        )
        if hr < 0 or not p_dialog.value:
            return {"ok": False, "cancelled": False, "error": f"CoCreateInstance failed: {hr:#x}"}

        vt = ctypes.cast(p_dialog, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn_release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vt[2])
        fn_show = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)(vt[3])
        fn_set_options = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)(vt[9])
        fn_get_options = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))(vt[10])
        fn_set_folder = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(vt[12])
        fn_set_title = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR)(vt[17])
        fn_get_result = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(vt[20])

        opts = wintypes.DWORD(0)
        fn_get_options(p_dialog, ctypes.byref(opts))
        fn_set_options(p_dialog, opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)
        fn_set_title(p_dialog, "เลือกโฟลเดอร์งาน (Select Workspace Folder)")

        if initial_dir and os.path.isdir(initial_dir):
            hr_item = shell32.SHCreateItemFromParsingName(
                os.path.abspath(initial_dir), None,
                ctypes.byref(IID_IShellItem), ctypes.byref(p_init_item),
            )
            if hr_item >= 0 and p_init_item.value:
                fn_set_folder(p_dialog, p_init_item)

        # สร้าง Owner Window ขนาด 0x0 แบบ WS_EX_TOPMOST เพื่อให้ Dialog ลอยทับ Chrome เสมอ 100%
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080
        WS_POPUP = 0x80000000
        owner_hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "", WS_POPUP,
            -32000, -32000, 0, 0, None, None, None, None,
        )
        if owner_hwnd:
            user32.ShowWindow(owner_hwnd, 4)  # SW_SHOWNOACTIVATE
            user32.SetWindowPos(owner_hwnd, wintypes.HWND(-1), -32000, -32000, 0, 0, 0x0001 | 0x0002)
            user32.SetForegroundWindow(owner_hwnd)

        hr_show = fn_show(p_dialog, owner_hwnd or 0)
        if hr_show == HRESULT_CANCELLED:
            return {"ok": False, "cancelled": True, "folder": ""}
        if hr_show < 0:
            return {"ok": False, "cancelled": False, "error": f"Show failed: {hr_show:#x}"}

        hr_res = fn_get_result(p_dialog, ctypes.byref(p_result_item))
        if hr_res < 0 or not p_result_item.value:
            return {"ok": False, "cancelled": True, "folder": ""}

        item_vt = ctypes.cast(p_result_item, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn_get_name = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(item_vt[5])
        p_path = ctypes.c_void_p()
        hr_name = fn_get_name(p_result_item, SIGDN_FILESYSPATH, ctypes.byref(p_path))
        if hr_name >= 0 and p_path.value:
            folder_path = ctypes.wstring_at(p_path.value)
            ole32.CoTaskMemFree(p_path)
            return {"ok": True, "cancelled": False, "folder": os.path.abspath(folder_path)}
        return {"ok": False, "cancelled": False, "error": "Cannot read selected path"}
    finally:
        for ptr in (p_result_item, p_init_item, p_dialog):
            if ptr and ptr.value:
                try:
                    vt_rel = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                    ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vt_rel[2])(ptr)
                except Exception:
                    pass
        if owner_hwnd:
            user32.DestroyWindow(owner_hwnd)
        if hr_init >= 0:
            ole32.CoUninitialize()

def choose_folder_dialog(initial_dir=""):
    """รัน Native Windows IFileOpenDialog ใน Isolated STA Subprocess (ไม่ค้าง, ไม่ชนกัน, แยก Cancel ชัดเจน)"""
    if not _folder_dialog_lock.acquire(blocking=False):
        return {"ok": False, "cancelled": False, "error": "หน้าต่างเลือกโฟลเดอร์เปิดค้างอยู่แล้ว กรุณาตรวจสอบแถบ Taskbar"}
    try:
        target_dir = os.path.abspath(initial_dir) if (initial_dir and os.path.isdir(initial_dir)) else os.getcwd()
        if os.name == "nt":
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--pick-folder", target_dir],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            raw_out = (proc.stdout or "").strip()
            if raw_out:
                try:
                    return json.loads(raw_out.splitlines()[-1])
                except Exception:
                    pass
            return {"ok": False, "cancelled": True, "folder": ""}

        # สำหรับ macOS / Linux ใช้ Tkinter ใน Subprocess แยก
        env = os.environ.copy()
        env["OSS_INIT_DIR"] = target_dir
        tk_script = (
            "import json, os, sys, tkinter as tk; from tkinter import filedialog; "
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "p = filedialog.askdirectory(initialdir=os.environ.get('OSS_INIT_DIR', '.'), title='Select Workspace Folder'); "
            "r.destroy(); print(json.dumps({'ok': bool(p), 'cancelled': not bool(p), 'folder': p or ''}, ensure_ascii=False))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", tk_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=180,
        )
        raw_out = (proc.stdout or "").strip()
        return json.loads(raw_out.splitlines()[-1]) if raw_out else {"ok": False, "cancelled": True, "folder": ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": False, "error": "หมดเวลารอการเลือกโฟลเดอร์"}
    except Exception as e:
        return {"ok": False, "cancelled": False, "error": str(e)}
    finally:
        _folder_dialog_lock.release()


# รายการไฟล์และโฟลเดอร์ Static ที่อนุญาตให้เข้าถึงผ่าน HTTP GET บนหน้า Dashboard เท่านั้น
PUBLIC_ROOT_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
PUBLIC_ASSET_DIRS = ("static", "assets")
PUBLIC_ASSET_MIME_MAP = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
PROTECTED_BACKEND_NAMES = {
    "server.py", "code_tools.py", "code_tools_hardening.py",
    "bridge_security.py", "chrome_cdp_fixes.py",
    "oss_settings.json", "sessions_store.json", "conversations.jsonl",
    "last_read_pointer.txt", "handoff_latest.json", "handoff_latest.md",
}
PROTECTED_BACKEND_DIRS = {"backups", ".backups", ".git", "__pycache__", ".venv", "venv"}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_forbidden_http_path(self, raw_url_path: str) -> bool:
        """ตรวจจับความพยายามเข้าถึงไฟล์ความลับ ไฟล์ระบบหลังบ้าน หรือทำ Path Traversal ผ่าน URL"""
        decoded = urllib.parse.unquote(str(raw_url_path or ""))
        if "\x00" in decoded or "\\" in decoded or ":" in decoded:
            return True
        parts = [seg for seg in decoded.split("/") if seg]
        if any(seg in ("..", ".") or seg.startswith(".") for seg in parts):
            return True
        if any(seg.lower() in PROTECTED_BACKEND_DIRS for seg in parts):
            return True
        leaf = parts[-1].lower() if parts else ""
        if leaf in PROTECTED_BACKEND_NAMES or leaf.endswith((".py", ".jsonl", ".bak", ".log")):
            return True
        if is_sensitive_file(decoded) or hardened_is_sensitive_path(decoded):
            return True
        return False

    def _serve_whitelisted_static(self, url_path: str, head_only: bool = False):
        """เสิร์ฟเฉพาะไฟล์ที่อยู่ใน Static Whitelist ภายใต้ BASE_DIR อย่างเข้มงวด"""
        if self._is_forbidden_http_path(url_path):
            self.send_json({"ok": False, "error": "403 Forbidden: Access denied"}, status=403)
            return

        decoded = urllib.parse.unquote(url_path)
        target_file = None
        content_type = None

        # กรณี 1: ไฟล์หน้าหลักและไอคอนระดับ Root ที่ระบุใน PUBLIC_ROOT_ASSETS
        if decoded in PUBLIC_ROOT_ASSETS:
            rel_name, content_type = PUBLIC_ROOT_ASSETS[decoded]
            candidate = os.path.join(BASE_DIR, rel_name)
            if _is_within_directory(_normalize_real_path(candidate), BASE_DIR):
                target_file = candidate

        # กรณี 2: ไฟล์ Static ภายใต้โฟลเดอร์ /static/... หรือ /assets/... เท่านั้น
        else:
            parts = [seg for seg in decoded.split("/") if seg]
            if len(parts) >= 2 and parts[0] in PUBLIC_ASSET_DIRS:
                ext = os.path.splitext(parts[-1])[1].lower()
                if ext in PUBLIC_ASSET_MIME_MAP:
                    allowed_subroot = os.path.join(BASE_DIR, parts[0])
                    candidate = os.path.join(BASE_DIR, *parts)
                    if (
                        os.path.isdir(allowed_subroot)
                        and _is_within_directory(_normalize_real_path(candidate), allowed_subroot)
                        and not is_sensitive_file(candidate)
                    ):
                        target_file = candidate
                        content_type = PUBLIC_ASSET_MIME_MAP[ext]

        if not target_file or not os.path.isfile(target_file):
            self.send_json({"ok": False, "error": "404 Not Found"}, status=404)
            return

        try:
            with open(target_file, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if decoded in ("/", "/index.html"):
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)
        except OSError:
            self.send_json({"ok": False, "error": "500 Internal Server Error"}, status=500)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_json({"ok": False, "error": "405 Method Not Allowed"}, status=405)
            return
        self._serve_whitelisted_static(parsed.path, head_only=True)

    def do_GET(self):
        global current_workspace
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._serve_whitelisted_static(parsed.path)
        elif parsed.path == "/api/status":
            session_id = qs.get("sessionId", [""])[0]
            self.send_json({"ok": True, "status": get_live_status(session_id)})
        elif parsed.path == "/api/sessions":
            self.send_json({"ok": True, "sessions": load_sessions_store()})
        elif parsed.path == "/api/bridge-status":
            self.send_json(check_bridge_status())
        elif parsed.path == "/api/connection-settings":
            self.send_json({"ok": True, "settings": load_oss_connection_settings()})
        elif parsed.path == "/api/model-quota":
            target_model = qs.get("model", [""])[0]
            with state_lock:
                if target_model:
                    info = MODEL_QUOTA_CACHE.get(target_model, {"status": "ok", "label": "พร้อมใช้", "color": "emerald"})
                    self.send_json({"ok": True, "model": target_model, **info})
                else:
                    self.send_json({"ok": True, "quotas": MODEL_QUOTA_CACHE})
        elif parsed.path == "/api/probe-quota":
            target_model = qs.get("model", ["gemini-3.8-flash"])[0]
            header_key = self.headers.get("X-Gemini-Api-Key", "")
            effective_key = resolve_effective_api_key(header_key)
            cfg = load_oss_connection_settings()
            try:
                probe_payload = {
                    "contents": [{"role": "user", "parts": [{"text": "p"}]}],
                    "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1}
                }
                if cfg.get("connectionMode") == "aistudio" or effective_key:
                    call_ai_studio_api(effective_key, target_model, probe_payload)
                    lbl = "พร้อมใช้ (AI Studio)"
                else:
                    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                    creds.refresh(Request())
                    probe_url = f"https://aiplatform.googleapis.com/v1/projects/{cfg.get('gcpProjectId', PROJECT_ID)}/locations/{cfg.get('gcpLocation', LOCATION)}/publishers/google/models/{target_model}:generateContent"
                    probe_req = urllib.request.Request(
                        probe_url,
                        data=json.dumps(probe_payload).encode("utf-8"),
                        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(probe_req, timeout=10) as resp:
                        json.loads(resp.read().decode("utf-8"))
                    lbl = "พร้อมใช้ (Vertex AI)"
                with state_lock:
                    MODEL_QUOTA_CACHE[target_model] = {"status": "ok", "label": lbl, "color": "emerald", "ts": time.time()}
                self.send_json({"ok": True, "model": target_model, "status": "ok", "label": lbl, "color": "emerald"})
            except urllib.error.HTTPError as pe:
                err_text = pe.read().decode("utf-8", errors="replace")
                is_exh = pe.code == 429 or "Resource exhausted" in err_text
                lbl = "ติดเพดาน 429" if is_exh else "ข้อผิดพลาด"
                col = "rose" if is_exh else "amber"
                with state_lock:
                    MODEL_QUOTA_CACHE[target_model] = {"status": "exhausted" if is_exh else "error", "label": lbl, "color": col, "ts": time.time()}
                self.send_json({"ok": False, "model": target_model, "status": "exhausted" if is_exh else "error", "label": lbl, "color": col, "error": err_text})
            except Exception as pe:
                err_text = str(pe)
                is_exh = "429" in err_text or "Resource exhausted" in err_text
                lbl = "ติดเพดาน 429" if is_exh else "ยังไม่ตั้งค่า/ผิดพลาด"
                col = "rose" if is_exh else "amber"
                with state_lock:
                    MODEL_QUOTA_CACHE[target_model] = {"status": "exhausted" if is_exh else "error", "label": lbl, "color": col, "ts": time.time()}
                self.send_json({"ok": False, "model": target_model, "status": "exhausted" if is_exh else "error", "label": lbl, "color": col, "error": err_text})
        elif parsed.path == "/api/handoff":
            if os.path.isfile(HANDOFF_JSON_FILE):
                try:
                    with open(HANDOFF_JSON_FILE, "r", encoding="utf-8") as fj:
                        self.send_json({"ok": True, "handoff": json.load(fj)})
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)})
            else:
                self.send_json({"ok": False, "error": "ยังไม่มีงานที่ส่งต่อ"})
        elif parsed.path == "/api/messages":
            ts_q = qs.get("ts", [""])[0]
            sid_q = qs.get("sessionId", [""])[0]
            try:
                limit_q = int(qs.get("limit", ["0"])[0] or 0)
            except Exception:
                limit_q = 0
            self.send_json({"ok": True, "messages": query_messages(ts_q, sid_q, limit_q)})
        elif parsed.path == "/api/files":
            with state_lock:
                cur_ws = current_workspace
            req_ws = qs.get("workspace", [None])[0]
            target_ws = req_ws.strip() if (req_ws and req_ws.strip()) else cur_ws
            if os.path.isdir(target_ws):
                abs_ws = os.path.abspath(target_ws)
                self.send_json({
                    "ok": True,
                    "workspace": abs_ws,
                    "files": list_workspace(abs_ws)
                })
            else:
                self.send_json({
                    "ok": False,
                    "error": f"ไม่พบโฟลเดอร์: {target_ws}",
                    "workspace": cur_ws,
                    "files": list_workspace(cur_ws)
                })
        elif parsed.path == "/api/browse-dirs":
            req_path = qs.get("path", [""])[0]
            self.send_json(browse_local_directories(req_path))
        elif parsed.path == "/api/choose-folder":
            with state_lock:
                cur_ws = current_workspace
            res_diag = choose_folder_dialog(cur_ws)
            if isinstance(res_diag, dict):
                chosen = res_diag.get("folder", "") if res_diag.get("ok") else ""
            else:
                chosen = str(res_diag or "")
            if chosen and os.path.isdir(chosen):
                with state_lock:
                    current_workspace = chosen
                self.send_json({"ok": True, "folder": chosen, "files": list_workspace(chosen)})
            else:
                self.send_json({"ok": False, "cancelled": True, "folder": cur_ws})
        elif parsed.path.startswith("/api/"):
            self.send_json({"ok": False, "error": f"Unknown API endpoint: {parsed.path}"}, status=404)
        else:
            self._serve_whitelisted_static(parsed.path)

    def do_POST(self):
        global current_workspace
        parsed = urllib.parse.urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        try:
            post_data = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            if not isinstance(post_data, dict):
                post_data = {}
        except Exception:
            self.send_json({"ok": False, "error": "Invalid JSON payload"}, status=400)
            return

        if parsed.path == "/api/choose-folder":
            req_init = str(post_data.get("current", "")).strip()
            with state_lock:
                cur_ws = current_workspace
            initial_dir = req_init if (req_init and os.path.isdir(req_init)) else cur_ws
            res_diag = choose_folder_dialog(initial_dir)
            if isinstance(res_diag, dict):
                if res_diag.get("ok") and res_diag.get("folder") and os.path.isdir(res_diag.get("folder")):
                    chosen = os.path.abspath(res_diag.get("folder"))
                    with state_lock:
                        current_workspace = chosen
                    self.send_json({
                        "ok": True,
                        "folder": chosen,
                        "files": list_workspace(chosen)
                    })
                elif res_diag.get("error"):
                    self.send_json({
                        "ok": False,
                        "cancelled": False,
                        "error": res_diag.get("error"),
                        "folder": cur_ws
                    })
                else:
                    self.send_json({
                        "ok": False,
                        "cancelled": True,
                        "folder": cur_ws
                    })
            else:
                chosen = str(res_diag or "").strip()
                if chosen and os.path.isdir(chosen):
                    with state_lock:
                        current_workspace = chosen
                    self.send_json({
                        "ok": True,
                        "folder": chosen,
                        "files": list_workspace(chosen)
                    })
                else:
                    self.send_json({
                        "ok": False,
                        "cancelled": True,
                        "folder": cur_ws
                    })
        elif parsed.path == "/api/workspace":
            ws = str(post_data.get("workspace", "")).strip()
            if ws and os.path.isdir(ws):
                abs_ws = os.path.abspath(ws)
                if post_data.get("setDefault"):
                    with state_lock:
                        current_workspace = abs_ws
                self.send_json({
                    "ok": True,
                    "workspace": abs_ws,
                    "files": list_workspace(abs_ws)
                })
            else:
                with state_lock:
                    cur_ws = current_workspace
                self.send_json({
                    "ok": False,
                    "error": f"ไม่พบโฟลเดอร์ที่ระบุ: {ws}",
                    "workspace": cur_ws
                })
        elif parsed.path == "/api/estimate-tokens":
            prompt = str(post_data.get("prompt", ""))
            attached_files = post_data.get("files", [])
            provider = str(post_data.get("provider", "cloud")).lower()
            model = str(post_data.get("model", "gemini-4-carbon"))
            level = str(post_data.get("thinkingLevel", "HIGH"))
            req_ws = post_data.get("workspace")
            with state_lock:
                cur_ws = current_workspace
            active_ws = req_ws if (req_ws and os.path.isdir(req_ws)) else cur_ws

            history_msgs = post_data.get("history", [])
            history_chars = 0
            if isinstance(history_msgs, list):
                for hm in history_msgs[-10:]:
                    if isinstance(hm, dict):
                        history_chars += len(str(hm.get("text", "")))

            text_chars = len(prompt) + history_chars
            roots = get_search_roots(active_ws)
            mentioned = extract_file_mentions(prompt, search_roots=roots, active_ws=active_ws, is_sensitive_fn=is_sensitive_file)

            # รวมไฟล์ที่เลือกผ่าน UI เข้ามาคำนวณด้วย
            for af in attached_files:
                f_path = find_file_in_roots(af, search_roots=roots, active_ws=active_ws)
                if f_path and not is_sensitive_file(str(f_path)) and f_path not in mentioned:
                    mentioned.append(f_path)

            files_chars = 0
            file_names = []
            for p in mentioned:
                try:
                    fsize = os.path.getsize(p)
                    p_name = p.name
                    p_rel = p_name
                    try:
                        p_rel = str(p.relative_to(active_ws))
                    except Exception:
                        pass
                    is_forced_full = bool(re.search(rf"@full:(?:{re.escape(p_name)}|{re.escape(p_rel)}|{re.escape(str(p))})", prompt, re.IGNORECASE))
                    is_forced_outline = bool(re.search(rf"@outline:(?:{re.escape(p_name)}|{re.escape(p_rel)}|{re.escape(str(p))})", prompt, re.IGNORECASE))

                    if p.name in attached_files and f"@{p.name}" not in prompt and not is_forced_full:
                        files_chars += min(fsize, 16000)
                    elif is_forced_outline or (not is_forced_full and fsize > 15000):
                        files_chars += 2500
                    else:
                        files_chars += fsize
                    file_names.append(p.name)
                except Exception:
                    pass

            total_chars = text_chars + files_chars
            est_tokens = max(0, int(total_chars / 3.6)) if total_chars > 0 else 0

            if not prompt.strip() and not file_names:
                self.send_json({
                    "ok": True,
                    "tokens": 0,
                    "total_tokens": 0,
                    "risk": "none",
                    "color": "gray",
                    "tip": "",
                    "files": []
                })
                return

            if provider == "web" or model.startswith("web:"):
                risk = "safe_web"
                status_color = "purple"
                tip = f"🌐 Web Free (~{est_tokens:,} tokens) ไม่มีเพดาน 429"
            elif est_tokens > 25000:
                risk = "danger"
                status_color = "red"
                tip = f"⚠️ ~{est_tokens:,} tokens เสี่ยงติด 429 สูงมาก แนะนำสลับโหมด 🌐 Web หรือใช้ @outline:"
            elif est_tokens >= 10000:
                risk = "warning"
                status_color = "yellow"
                tip = f"⚡ ~{est_tokens:,} tokens แนะนำปรับ Thinking เป็น Low/Medium"
            else:
                risk = "safe"
                status_color = "green"
                tip = f"🟢 ~{est_tokens:,} tokens ปลอดภัย ยิงได้ทุกโมเดล"

            self.send_json({
                "ok": True,
                "tokens": est_tokens,
                "total_tokens": est_tokens,
                "totalChars": total_chars,
                "risk": risk,
                "color": status_color,
                "tip": tip,
                "files": file_names
            })
        elif parsed.path == "/api/sessions":
            sessions_list = post_data.get("sessions", [])
            ok = save_sessions_store(sessions_list)
            if ok and (post_data.get("clearAll") or post_data.get("deletedSessionId") or post_data.get("syncLog")):
                sync_delete_conversations_log(
                    sessions_list=sessions_list,
                    clear_all=bool(post_data.get("clearAll")),
                    deleted_session_id=post_data.get("deletedSessionId"),
                )
            self.send_json({"ok": ok})
        elif parsed.path == "/api/delete-messages":
            session_id = str(post_data.get("sessionId") or "")
            remaining_prompts = post_data.get("remainingUserPrompts", [])
            sessions_list = post_data.get("sessions")
            res = sync_delete_conversations_log(
                session_id=session_id,
                remaining_user_prompts=remaining_prompts,
                sessions_list=sessions_list,
            )
            self.send_json(res)
        elif parsed.path == "/api/handoff":
            self.send_json(save_handoff_data(post_data))
        elif parsed.path == "/api/cancel":
            session_id = str(post_data.get("sessionId") or "")
            set_session_cancelled(session_id, True)
            update_live_status(session_id, "⏹️ กำลังหยุดการทำงานตามคำสั่งผู้ใช้...")
            self.send_json({"ok": True})
        elif parsed.path == "/api/undo-edit":
            with state_lock:
                cur_ws = current_workspace
            res = undo_edit_proposal(
                path=post_data.get("path", ""),
                backup_path=post_data.get("backupPath", ""),
                workspace=post_data.get("workspace", cur_ws),
            )
            self.send_json(res)
        elif parsed.path == "/api/apply-edit":
            if post_data.get("agentMode") == "readonly":
                self.send_json({"ok": False, "error": "🔒 โหมด Read-Only ทำงานอยู่ ไม่อนุญาตให้แก้ไขไฟล์"})
                return
            with state_lock:
                cur_ws = current_workspace
            res = apply_edit_proposal(
                path=post_data.get("path", ""),
                start_line=post_data.get("startLine", 1),
                end_line=post_data.get("endLine", 1),
                old_code=post_data.get("oldCode", ""),
                new_code=post_data.get("newCode", ""),
                workspace=post_data.get("workspace", cur_ws),
            )
            self.send_json(res)
        elif parsed.path == "/api/connection-settings":
            updated = save_oss_connection_settings(post_data)
            self.send_json({"ok": True, "settings": updated})
        elif parsed.path == "/api/test-connection":
            mode = str(post_data.get("connectionMode") or "aistudio").strip().lower()
            if mode == "web":
                self.send_json(check_bridge_status())
            elif mode == "aistudio":
                test_key = resolve_effective_api_key(
                    self.headers.get("X-Gemini-Api-Key") or post_data.get("apiKey")
                )
                try:
                    call_ai_studio_api(
                        test_key,
                        "gemini-2.5-flash",
                        {"contents": [{"role": "user", "parts": [{"text": "ping"}]}], "generationConfig": {"maxOutputTokens": 2}},
                    )
                    self.send_json({"ok": True, "message": "✅ เชื่อมต่อ Google AI Studio API Key สำเร็จพร้อมใช้งาน!"})
                except Exception as te:
                    self.send_json({"ok": False, "error": str(te)})
            else:
                try:
                    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                    creds.refresh(Request())
                    self.send_json({"ok": True, "message": "✅ เชื่อมต่อ Google Cloud Vertex AI Credentials สำเร็จ!"})
                except Exception as te:
                    self.send_json({"ok": False, "error": f"ไม่พบสิทธิ์ Google Cloud ADC: {te}"})
        elif parsed.path == "/api/chat":
            provider = str(post_data.get("provider", "cloud")).strip().lower()
            model = post_data.get("model", "gemini-4-carbon")
            level = post_data.get("thinkingLevel", "MEDIUM")
            web_model = post_data.get("webModel", "3.5-flash-lite")
            web_level = post_data.get("webThinkingLevel", "HIGH")
            prompt = post_data.get("prompt", "")
            files = post_data.get("files", [])
            images = post_data.get("images", [])
            history = post_data.get("history", [])
            session_id = post_data.get("sessionId", "chat_default")
            session_title = post_data.get("sessionTitle", "การสนทนาทั่วไป")
            agent_mode = post_data.get("agentMode", "diff")
            req_ws = post_data.get("workspace")

            header_api_key = self.headers.get("X-Gemini-Api-Key", "")
            req_api_key = header_api_key or post_data.get("apiKey", "")
            req_conn_mode = post_data.get("connectionMode", "")
            req_gcp_project = post_data.get("gcpProjectId", "")
            req_gcp_region = post_data.get("gcpLocation", "")

            if req_ws and os.path.isdir(str(req_ws).strip()):
                cur_ws = os.path.abspath(str(req_ws).strip())
            else:
                with state_lock:
                    cur_ws = current_workspace

            try:
                if provider == "web" or str(model).startswith("web:"):
                    target_web_model = str(model)[4:] if str(model).startswith("web:") else web_model
                    answer, scanned_files, usage, proposed_edits = call_gemini_web_bridge(
                        prompt,
                        web_model=target_web_model,
                        thinking_level=web_level,
                        session_id=session_id,
                        session_title=session_title,
                        workspace=cur_ws,
                        selected_files=files,
                    )
                else:
                    answer, scanned_files, usage, proposed_edits = call_vertex(
                        model, level, prompt, files,
                        history=history,
                        session_id=session_id,
                        session_title=session_title,
                        workspace=cur_ws,
                        images=images,
                        agent_mode=agent_mode,
                        api_key=req_api_key,
                        connection_mode=req_conn_mode,
                        gcp_project_id=req_gcp_project,
                        gcp_location=req_gcp_region,
                    )
                res = {
                    "ok": True,
                    "answer": answer,
                    "scannedFiles": scanned_files,
                    "usage": usage,
                    "proposedEdits": proposed_edits,
                    "workspace": cur_ws,
                    "cancelled": is_session_cancelled(session_id),
                }
            except Exception as e:
                update_live_status(session_id, f"❌ เกิดข้อผิดพลาด: {e}")
                res = {"ok": False, "error": str(e), "scannedFiles": [], "usage": {}, "proposedEdits": []}

            self.send_json(res)
        else:
            self.send_json({"ok": False, "error": f"Unknown API endpoint: {parsed.path}"}, status=404)


if __name__ == "__main__":
    if "--pick-folder" in sys.argv:
        idx = sys.argv.index("--pick-folder")
        target_init = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        res = _pick_folder_win32_native(target_init)
        print(json.dumps(res, ensure_ascii=False))
        sys.exit(0)

    os.chdir(os.path.dirname(__file__))
    with ThreadedHTTPServer(("127.0.0.1", PORT), DashboardHandler) as httpd:
        print(f"Vertex AI Threaded Dashboard (OSS) Running at http://127.0.0.1:{PORT}")
        httpd.serve_forever()
