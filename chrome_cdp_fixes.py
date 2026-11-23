"""
chrome_cdp_fixes.py — HardenedChromeClient (v4.0 Headless Job Bridge Edition)

Usage in gemini_bridge_server.py:
    from chrome_cdp_fixes import HardenedChromeClient, CDPError, atomic_save_history
    cdp_client = HardenedChromeClient()
    cdp_client.tab_preferences = ["/u/1/app", "your-gemini-chat-id"]
    cdp_client.upload_roots = [r"C:\\Projects", workspace]

Requires code_tools_hardening.py (same folder) for the path jail / sensitive-file policy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import websockets

from chrome_cdp import ChromeClient, get_cdp_ws_url
from code_tools_hardening import PathNotAllowed, resolve_in_roots, sensitive_reason

log = logging.getLogger("chrome_cdp")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARGON_HIST_DIR = os.path.join(BASE_DIR, "argon_history")
CARBON_HIST_DIR = os.path.join(BASE_DIR, "carbon_history")
MAX_HISTORY_FILES = 200
MAX_INMEM_JOBS = 200

class CDPError(RuntimeError):
    """Protocol error, JS exception or timeout — always carries a readable reason."""

SELECTORS: Dict[str, str] = {
    "bubble": 'message-content, .model-response-text, .response-container-content, [data-test-id="model-response"]',
    "user_turn": 'user-query, .user-query-container, [data-test-id="user-query"]',
    "stop": 'button[aria-label*="หยุด"], button[aria-label*="Stop"], button.stop-button, .streaming-indicator',
    "composer": 'rich-textarea div[contenteditable="true"], div[contenteditable="true"][role="textbox"], rich-textarea p',
    "composer_root": "rich-textarea",
    "send": 'button[aria-label*="ส่ง"], button[aria-label*="Send"], .send-button, button.send-button-container, button[data-test-id="send-button"]',
}

LIMIT_RE = re.compile(
    r"(reached|exceeded)\s+(your\s+)?(usage\s+)?limit|something went wrong|try again (later|in a)|"
    r"ถึงขีดจำกัด|เกินขีดจำกัด|เกิดข้อผิดพลาด|ลองอีกครั้งในภายหลัง|ไม่สามารถตอบ.{0,20}ได้ในขณะนี้",
    re.IGNORECASE,
)

_MOUNT_JS = """(() => {
  if (document.querySelector('input[type="file"]')) return { alreadyMounted: true };
  const plus = document.querySelector('button[aria-label*="อัปโหลดและเครื่องมือ"], button[aria-label*="Upload"], .plus-button');
  if (plus) { plus.click(); return { clicked: true }; }
  return { notFound: true };
})()"""

_CLOSE_MENU_JS = """(() => {
  const b = document.querySelector('.cdk-overlay-backdrop'); if (b) b.click();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
})()"""

_CARBON_STATE_JS = """(() => {
  const stopBtn = document.querySelector(
    'button[aria-label*="Stop"], button[aria-label*="หยุด"], button[aria-label*="Cancel"], ' +
    'mat-spinner, mat-progress-bar, [aria-busy="true"], .streaming-indicator'
  );
  const statusArea = document.querySelector('mat-sidenav-content, main') || document.body;
  const responses = Array.from(document.querySelectorAll(
    '.model-response, [class*="response-container"], [data-message-author="model"], ' +
    'ms-chat-bubble, ms-message-list, markdown, .rendered-markdown'
  ));
  const lastEl = responses.length > 0 ? responses[responses.length - 1] : null;
  const t = lastEl ? (lastEl.innerText || lastEl.textContent || '').trim() : '';
  let h = 5381;
  for (let i = 0; i < t.length; i++) {
    h = ((h << 5) + h + t.charCodeAt(i)) | 0;
  }
  const hasGenText = !!(
    statusArea &&
    (statusArea.textContent || '').match(/Generating response|Thinking:/i) &&
    (stopBtn || t.length === 0)
  );
  return {
    isGen: !!(stopBtn || hasGenText),
    count: responses.length,
    lastLen: t.length,
    lastHash: h
  };
})()"""

_CARBON_EXTRACT_JS = r"""(() => {
  function domToMarkdown(root) {
    if (!root) return '';
    function walk(node, depth = 0, listType = null, listIndex = 1) {
      if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || '';
      if (node.nodeType !== Node.ELEMENT_NODE) return '';
      const tag = node.tagName.toLowerCase();

      if (tag === 'button' || tag === 'mat-icon' || tag === 'svg') return '';
      if (node.matches && node.matches('.code-block-header, [aria-label*="Copy"]')) return '';

      if (tag === 'code-block' || tag === 'ms-code-block' || tag === 'pre') {
        const code = node.querySelector('code');
        const pre = node.querySelector('pre') || (tag === 'pre' ? node : null);
        let lang = '';
        const langEl = node.querySelector('.header-text, .code-block-header, .language-header, [class*="language-"]:not(code):not(pre)');
        if (langEl) {
          lang = langEl.textContent.trim().toLowerCase();
        } else if (code) {
          lang = (code.className.match(/language-([a-z0-9_-]+)/i) || [])[1] || '';
        }
        lang = lang.replace(/[^a-z0-9_-]/gi, '');
        const rawText = (code ? code.textContent : (pre ? pre.textContent : node.textContent)).replace(/\r\n/g, '\n').trim();
        const fence = rawText.includes('````') ? '`````' : (rawText.includes('```') ? '````' : '```');
        return '\n' + fence + lang + '\n' + rawText + '\n' + fence + '\n\n';
      }

      if (tag === 'code') {
        if (node.closest('pre, code-block, ms-code-block')) return node.textContent;
        return '`' + node.textContent.trim() + '`';
      }

      if (tag === 'h1') return '\n# ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\n\n';
      if (tag === 'h2') return '\n## ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\n\n';
      if (tag === 'h3') return '\n### ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\n\n';
      if (tag === 'h4') return '\n#### ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\n\n';

      if (tag === 'p') {
        const pText = Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim();
        return pText ? pText + '\n\n' : '';
      }

      if (tag === 'b' || tag === 'strong') {
        return '**' + Array.from(node.childNodes).map(c => walk(c, depth)).join('') + '**';
      }

      if (tag === 'i' || tag === 'em') {
        return '*' + Array.from(node.childNodes).map(c => walk(c, depth)).join('') + '*';
      }

      if (tag === 'a') {
        const href = node.getAttribute('href') || '';
        const text = Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim();
        return href ? '[' + text + '](' + href + ')' : text;
      }

      if (tag === 'ul') {
        let res = '\n';
        let childIdx = 1;
        for (const child of node.children) {
          if (child.tagName.toLowerCase() === 'li') res += walk(child, depth + 1, 'ul', childIdx++);
        }
        return res + '\n';
      }

      if (tag === 'ol') {
        let res = '\n';
        const start = parseInt(node.getAttribute('start') || '1', 10);
        let childIdx = start;
        for (const child of node.children) {
          if (child.tagName.toLowerCase() === 'li') res += walk(child, depth + 1, 'ol', childIdx++);
        }
        return res + '\n';
      }

      if (tag === 'li') {
        const indent = '  '.repeat(Math.max(0, depth - 1));
        const bullet = listType === 'ol' ? `${listIndex}. ` : '- ';
        const inlineParts = [];
        const sublistParts = [];
        for (const child of node.childNodes) {
          if (child.nodeType === Node.ELEMENT_NODE && ['ul', 'ol', 'pre', 'code-block', 'ms-code-block'].includes(child.tagName.toLowerCase())) {
            sublistParts.push(walk(child, depth));
          } else {
            inlineParts.push(walk(child, depth));
          }
        }
        const itemText = inlineParts.join('').trim().replace(/\s+/g, ' ');
        return `${indent}${bullet}${itemText}\n${sublistParts.join('')}`;
      }

      if (tag === 'blockquote') {
        const text = Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim();
        return '\n> ' + text.split('\n').join('\n> ') + '\n\n';
      }

      if (tag === 'hr') return '\n---\n\n';
      if (tag === 'br') return '\n';
      if (tag === 'table') {
        const rows = Array.from(node.querySelectorAll('tr'));
        if (rows.length === 0) return '';
        const mdRows = [];
        rows.forEach((tr) => {
          const cells = Array.from(tr.querySelectorAll('th, td'));
          const cellTexts = cells.map(cell => walk(cell).replace(/\s+/g, ' ').replace(/\|/g, '\\|').trim());
          if (cellTexts.length === 0) return;
          mdRows.push('| ' + cellTexts.join(' | ') + ' |');
          if (mdRows.length === 1) {
            mdRows.push('| ' + cellTexts.map(() => '---').join(' | ') + ' |');
          }
        });
        return mdRows.length > 0 ? '\n\n' + mdRows.join('\n') + '\n\n' : '';
      }

      return Array.from(node.childNodes).map(c => walk(c, depth, listType, listIndex)).join('');
    }

    return walk(root).replace(/\n{3,}/g, '\n\n').trim();
  }

  const candidates = Array.from(document.querySelectorAll(
    'ms-chat-bubble, ms-message-list, [data-message-author="model"], ' +
    '.model-response, [class*="response-container"], markdown, .rendered-markdown'
  ));
  let text = '';
  if (candidates.length > 0) {
    const lastCandidate = candidates[candidates.length - 1];
    text = domToMarkdown(lastCandidate);
    if (!text) {
      text = (lastCandidate.innerText || lastCandidate.textContent || '').trim();
    }
  }
  if (!text || text.length < 50) {
    const contentArea = document.querySelector('mat-sidenav-content') || document.body;
    text = contentArea ? (contentArea.innerText || '').trim() : '';
  }
  return { text: text, url: window.location.href };
})()"""

def _atomic_write_file(target_path: str, content: str, retries: int = 5) -> None:
    """เขียนไฟล์แบบ Atomic พร้อม Unique Temp + fsync + Retry PermissionError บน Windows"""
    abs_target = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(abs_target), exist_ok=True)
    tmp_path = f"{abs_target}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fo:
            fo.write(content)
            fo.flush()
            os.fsync(fo.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp_path, abs_target)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.04 * (attempt + 1))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

def _prune_history_dir(hist_dir: str, prefix: str, keep: int = MAX_HISTORY_FILES) -> None:
    """ลบไฟล์ประวัติเก่าเกินจำนวน keep โดยไม่แตะ latest.md, latest.status.json, quota.json"""
    try:
        if not os.path.isdir(hist_dir):
            return
        entries = []
        for name in os.listdir(hist_dir):
            if not name.startswith(f"{prefix}_") or not name.endswith(".md"):
                continue
            full = os.path.join(hist_dir, name)
            try:
                entries.append((os.path.getmtime(full), full))
            except OSError:
                continue
        if len(entries) <= keep:
            return
        entries.sort(key=lambda x: x[0], reverse=True)
        for _, old_path in entries[keep:]:
            try:
                os.remove(old_path)
            except OSError:
                pass
    except Exception as e:
        log.warning("History prune skipped (%s): %r", hist_dir, e)

import threading

_HIST_LOCKS: Dict[str, threading.Lock] = {}
_HIST_LOCKS_GUARD = threading.Lock()
_ACTIVE_JOB_IDS: Dict[str, str] = {}

def _get_hist_lock(hist_dir: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(hist_dir))
    with _HIST_LOCKS_GUARD:
        lk = _HIST_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _HIST_LOCKS[key] = lk
        return lk

def set_job_pending(hist_dir: str, job_id: str, prompt_snippet: str = "") -> Dict[str, Any]:
    """ขึ้นป้ายไฟแดง (generating) แบบ Atomic"""
    status_file = os.path.join(hist_dir, "latest.status.json")
    status_data: Dict[str, Any] = {
        "jobId": job_id,
        "state": "generating",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompt": (prompt_snippet or "")[:120].strip(),
    }
    key = os.path.normcase(os.path.abspath(hist_dir))
    with _get_hist_lock(hist_dir):
        _ACTIVE_JOB_IDS[key] = job_id
        try:
            _atomic_write_file(status_file, json.dumps(status_data, ensure_ascii=False, indent=2))
        except Exception as e:
            log.warning("set_job_pending failed: %r", e)
    return status_data

def set_job_failed(
    hist_dir: str,
    job_id: str,
    state: str,
    error_msg: str,
    elapsed_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """ปิดป้ายไฟแดงเป็นสถานะจบงานแบบผิดพลาด (error | timeout | quota_limit) เพื่อไม่ให้ Agent รอค้าง"""
    valid_state = state if state in ("error", "timeout", "quota_limit") else "error"
    status_file = os.path.join(hist_dir, "latest.status.json")
    status_data: Dict[str, Any] = {
        "jobId": job_id,
        "state": valid_state,
        "error": str(error_msg)[:500],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if elapsed_ms is not None:
        status_data["elapsedMs"] = int(elapsed_ms)
    key = os.path.normcase(os.path.abspath(hist_dir))
    with _get_hist_lock(hist_dir):
        active = _ACTIVE_JOB_IDS.get(key)
        if active and active != job_id:
            return status_data
        try:
            _atomic_write_file(status_file, json.dumps(status_data, ensure_ascii=False, indent=2))
        except Exception as e:
            log.warning("set_job_failed write error: %r", e)
    return status_data

def atomic_save_history(
    hist_dir: str,
    prefix: str,
    content: str,
    meta: str,
    job_id: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """บันทึกคำตอบแบบ Atomic Commit Barrier: 1) history/{job_id}.md -> 2) latest.md -> 3) latest.status.json (done)"""
    os.makedirs(hist_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    effective_job_id = job_id or f"{prefix}_{ts}_{uuid.uuid4().hex[:4]}"
    safe_job_name = re.sub(r"[^A-Za-z0-9_.-]", "_", effective_job_id)
    hist_file = os.path.join(hist_dir, f"{safe_job_name}.md")
    latest_file = os.path.join(hist_dir, "latest.md")
    status_file = os.path.join(hist_dir, "latest.status.json")

    full_text = meta + content

    key = os.path.normcase(os.path.abspath(hist_dir))
    with _get_hist_lock(hist_dir):
        # 1. เขียนไฟล์ประวัติเฉพาะงานนี้แบบ Atomic
        _atomic_write_file(hist_file, full_text)

        active = _ACTIVE_JOB_IDS.get(key)
        is_extract = effective_job_id.startswith(("argon_extract_", "carbon_extract_"))
        if active and active != effective_job_id and not is_extract:
            return {
                "jobId": effective_job_id,
                "state": "superseded",
                "historyFile": hist_file,
            }

        # 2. เขียน latest.md แบบ Atomic
        _atomic_write_file(latest_file, full_text)

        # 3. อัปเดตสถานะเป็น DONE เป็นลำดับสุดท้าย (Commit Signal)
        status_data: Dict[str, Any] = {
            "jobId": effective_job_id,
            "state": "done",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chars": len(content),
            "latestFile": latest_file,
            "historyFile": hist_file,
        }
        if elapsed_ms is not None:
            status_data["elapsedMs"] = int(elapsed_ms)

        _atomic_write_file(status_file, json.dumps(status_data, ensure_ascii=False, indent=2))
        _prune_history_dir(hist_dir, prefix)
        return status_data

def _read_disk_job_sync(job_id: str, include_answer: bool = False) -> Optional[Dict[str, Any]]:
    """ค้นหาข้อมูลงานจากดิสก์เป็น Fallback (รองรับกรณี Bridge ถูกรีสตาร์ท)"""
    safe_job_name = re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    for target, hist_dir in (("argon", ARGON_HIST_DIR), ("carbon", CARBON_HIST_DIR)):
        status_file = os.path.join(hist_dir, "latest.status.json")
        if os.path.isfile(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    st = json.load(f)
                if isinstance(st, dict) and st.get("jobId") == job_id:
                    result = dict(st)
                    result.setdefault("target", target)
                    if result.get("state") == "generating":
                        # หากไม่อยู่ใน self._jobs ของโปรเซสปัจจุบัน แปลว่าเป็นงานค้างจากเซิร์ฟเวอร์รอบก่อนที่ถูกปิดไป
                        orphan_msg = "งานค้างจากโปรเซสก่อนหน้าที่หยุดทำงาน (สามารถเรียก extract_latest เพื่อกู้คำตอบจากหน้าเว็บได้)"
                        result = set_job_failed(hist_dir, job_id, "error", orphan_msg)
                        result["target"] = target
                    elif include_answer and result.get("state") == "done":
                        ans_path = result.get("historyFile") or result.get("latestFile")
                        if ans_path and os.path.isfile(ans_path):
                            with open(ans_path, "r", encoding="utf-8", errors="replace") as af:
                                result["answer"] = af.read()
                    return result
            except Exception:
                pass

        hist_file = os.path.join(hist_dir, f"{safe_job_name}.md")
        if os.path.isfile(hist_file):
            try:
                with open(hist_file, "r", encoding="utf-8", errors="replace") as af:
                    text = af.read()
                result = {
                    "jobId": job_id,
                    "target": target,
                    "state": "done",
                    "chars": len(text),
                    "historyFile": hist_file,
                    "latestFile": os.path.join(hist_dir, "latest.md"),
                }
                if include_answer:
                    result["answer"] = text
                return result
            except Exception:
                pass
    return None

class HardenedChromeClient(ChromeClient):
    def __init__(self):
        super().__init__()
        self.tab_preferences: List[str] = []             # substrings, highest priority first
        self.upload_roots: Optional[List[str]] = None    # None = no jail (not recommended)
        self.selectors: Dict[str, str] = dict(SELECTORS)
        self.last_disconnect_reason: Optional[str] = None
        self.carbon_target_id: Optional[str] = None
        self.carbon_session_id: Optional[str] = None

        # แยก Lock ระหว่างแท็บ Gemini กับแท็บ Carbon (คง ask_lock ไว้เพื่อความเข้ากันได้เดิม)
        self.carbon_lock = asyncio.Lock()

        # จัดการ Watcher และ Background Tasks ไม่ให้ซ้อนทับหรือหลุด GC
        self._argon_watchers: Dict[str, asyncio.Task] = {}
        self._carbon_watchers: Dict[str, asyncio.Task] = {}
        self._bg_tasks: Set[asyncio.Task] = set()

        # In-Memory Job Registry สำหรับ GET /jobs/{job_id}?wait=...
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_events: Dict[str, asyncio.Event] = {}
        self._job_answers: Dict[str, str] = {}

    # ── job registry helpers ─────────────────────────────────────────────────
    def _track_bg_task(self, task: asyncio.Task) -> asyncio.Task:
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _prune_inmem_jobs(self) -> None:
        while len(self._jobs) > MAX_INMEM_JOBS:
            oldest_key = next(iter(self._jobs))
            self._jobs.pop(oldest_key, None)
            self._job_events.pop(oldest_key, None)
            self._job_answers.pop(oldest_key, None)

    def _register_job(self, job_id: str, target: str, hist_dir: str, prompt_snippet: str = "") -> None:
        self._prune_inmem_jobs()
        self._jobs[job_id] = {
            "jobId": job_id,
            "target": target,
            "state": "generating",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": (prompt_snippet or "")[:120].strip(),
            "statusFile": os.path.join(hist_dir, "latest.status.json"),
            "latestFile": os.path.join(hist_dir, "latest.md"),
        }
        self._job_events[job_id] = asyncio.Event()

    def _mark_job_done_mem(
        self,
        job_id: str,
        target: str,
        status_data: Dict[str, Any],
        answer_text: str,
    ) -> None:
        entry = dict(self._jobs.get(job_id) or {})
        entry.update(status_data)
        entry.pop("error", None)
        entry["jobId"] = job_id
        entry["target"] = target
        entry["state"] = "done"
        self._jobs[job_id] = entry
        self._job_answers[job_id] = answer_text
        ev = self._job_events.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._job_events[job_id] = ev
        ev.set()

    async def _mark_job_failed(
        self,
        hist_dir: str,
        job_id: str,
        target: str,
        state: str,
        error_msg: str,
        t0: float,
    ) -> None:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        status_data = await asyncio.to_thread(set_job_failed, hist_dir, job_id, state, error_msg, elapsed_ms)
        entry = dict(self._jobs.get(job_id) or {})
        entry.update(status_data)
        entry["jobId"] = job_id
        entry["target"] = target
        self._jobs[job_id] = entry
        ev = self._job_events.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._job_events[job_id] = ev
        ev.set()

    def _mark_job_superseded_mem(self, job_id: str) -> None:
        """อัปเดตเฉพาะในหน่วยความจำเมื่อถูกแทนที่โดยงานใหม่ (ไม่เขียนทับ latest.status.json ของงานใหม่)"""
        entry = self._jobs.get(job_id)
        if entry and entry.get("state") == "generating":
            entry["state"] = "error"
            entry["error"] = "งานถูกยกเลิกเนื่องจากมีคำสั่งใหม่เข้ามายังแท็บเดียวกัน"
            entry["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ev = self._job_events.get(job_id)
        if ev:
            ev.set()

    async def _cancel_task_safely(self, task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_job(
        self,
        job_id: str,
        wait_sec: float = 0.0,
        include_answer: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """ดึงสถานะงานตาม job_id พร้อมรองรับ Long-Poll (wait_sec) และแนบคำตอบ (include_answer)"""
        ev = self._job_events.get(job_id)
        if wait_sec > 0 and ev is not None and not ev.is_set():
            try:
                await asyncio.wait_for(ev.wait(), timeout=wait_sec)
            except asyncio.TimeoutError:
                pass

        entry = self._jobs.get(job_id)
        if entry is not None:
            res = dict(entry)
            if include_answer and res.get("state") == "done":
                ans = self._job_answers.get(job_id)
                if ans is None:
                    ans_path = res.get("historyFile") or res.get("latestFile")
                    if ans_path:
                        def _read_ans(p: str) -> str:
                            with open(p, "r", encoding="utf-8", errors="replace") as af:
                                return af.read()
                        try:
                            ans = await asyncio.to_thread(_read_ans, ans_path)
                        except Exception:
                            ans = ""
                res["answer"] = ans or ""
            return res

        return await asyncio.to_thread(_read_disk_job_sync, job_id, include_answer)

    # ── transport ────────────────────────────────────────────────────────────
    async def connect(self):
        # ปิดการเชื่อมต่อเดิมและยกเลิก Listener เก่าให้เรียบร้อยก่อนสร้างเส้นใหม่
        old_task = getattr(self, "listen_task", None)
        old_ws = getattr(self, "ws", None)
        self.ws = None
        self.connected = False
        if old_ws is not None:
            try:
                await old_ws.close()
            except Exception:
                pass
        await self._cancel_task_safely(old_task)

        url = await asyncio.to_thread(get_cdp_ws_url)
        ws = await websockets.connect(
            url,
            open_timeout=60,
            ping_interval=None,
            ping_timeout=None,
            max_size=64 * 1024 * 1024,
        )
        self.ws = ws
        self.connected = True
        self.last_disconnect_reason = None
        self.gemini_target_id = None
        self.gemini_session_id = None
        self.carbon_target_id = None
        self.carbon_session_id = None
        self.sessions.clear()
        self.listen_task = asyncio.create_task(self._listener(ws))

    async def close(self):
        for t in list(self._argon_watchers.values()) + list(self._carbon_watchers.values()):
            await self._cancel_task_safely(t)
        self._argon_watchers.clear()
        self._carbon_watchers.clear()

        for jid, info in list(self._jobs.items()):
            if info.get("state") == "generating":
                hdir = CARBON_HIST_DIR if info.get("target") == "carbon" else ARGON_HIST_DIR
                await asyncio.to_thread(
                    set_job_failed, hdir, jid, "error", "Bridge Server ปิดการทำงานระหว่างรอคำตอบ"
                )

        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

        old_ws = getattr(self, "ws", None)
        old_task = getattr(self, "listen_task", None)
        self.ws = None
        self.connected = False
        if old_ws is not None:
            try:
                await old_ws.close()
            except Exception:
                pass
        await self._cancel_task_safely(old_task)
        self.sessions.clear()

    def _dispatch(self, data: Dict[str, Any]) -> None:
        method, params = data.get("method"), data.get("params") or {}
        if method == "Target.attachedToTarget":
            tid, sid = (params.get("targetInfo") or {}).get("targetId"), params.get("sessionId")
            if tid and sid:
                self.sessions[tid] = sid
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            if sid == self.gemini_session_id:
                self.gemini_session_id = self.gemini_target_id = None
            if sid == self.carbon_session_id:
                self.carbon_session_id = self.carbon_target_id = None
            for k, v in list(self.sessions.items()):
                if v == sid:
                    self.sessions.pop(k, None)
        elif method == "Target.targetDestroyed":
            tid = params.get("targetId")
            if tid:
                self.sessions.pop(tid, None)
            if tid == self.gemini_target_id:
                self.gemini_session_id = self.gemini_target_id = None
            if tid == self.carbon_target_id:
                self.carbon_session_id = self.carbon_target_id = None
        mid = data.get("id")
        if mid and mid in self.pending:
            fut = self.pending.pop(mid)
            if not fut.done():
                fut.set_result(data)

    async def _listener(self, ws=None):
        active_ws = ws if ws is not None else self.ws
        reason = "connection closed"
        try:
            async for raw in active_ws:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        self._dispatch(data)
                except Exception as e:
                    log.warning("CDP frame ignored: %r", e)
        except asyncio.CancelledError:
            reason = "listener cancelled"
            raise
        except Exception as e:
            reason = repr(e)
            log.error("CDP listener stopped: %s", reason)
        finally:
            # ป้องกัน Listener ตัวเก่าที่เพิ่งปิดมาทำลายสถานะของ Connection ใหม่
            if self.ws is active_ws:
                self.connected = False
                self.last_disconnect_reason = reason
                self.gemini_target_id = self.gemini_session_id = None
                self.carbon_target_id = self.carbon_session_id = None
                self.sessions.clear()
                for fut in list(self.pending.values()):
                    if not fut.done():
                        fut.set_exception(CDPError(f"Chrome CDP connection lost: {reason}"))
                self.pending.clear()

    async def send_cmd(self, method, params=None, session_id=None, timeout=15.0, strict=True):
        try:
            res = await super().send_cmd(method, params, session_id, timeout)
        except asyncio.TimeoutError:
            raise CDPError(f"{method} timed out after {timeout}s") from None
        if strict and "error" in res:
            raise CDPError(f"{method}: {(res['error'] or {}).get('message', res['error'])}")
        return res

    async def eval_js(self, session_id, expression, timeout=15.0):
        res = await self.send_cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session_id,
            timeout=timeout,
        )
        r = res.get("result") or {}
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            desc = (
                (r.get("result") or {}).get("description")
                or (d.get("exception") or {}).get("description")
                or d.get("text")
            )
            raise CDPError(f"JS exception: {desc}")
        return (r.get("result") or {}).get("value")

    # ── tab discovery: reuse the session, detach the one we drop ─────────────
    def _pick_tab(self, tabs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        usable = [
            t
            for t in tabs
            if "gemini.google.com" in t.get("url", "") and "RotateCookies" not in t.get("url", "")
        ]
        for pref in self.tab_preferences:
            for t in usable:
                if pref in t["url"]:
                    return t
        for needle in ("gemini.google.com/app", "gemini.google.com"):
            for t in usable:
                if needle in t["url"]:
                    return t
        return None

    async def _resolve_tab_target(
        self,
        default_matcher,
        target_id: Optional[str] = None,
        tab_url: Optional[str] = None,
        not_found_msg: str = "ไม่พบแท็บที่ต้องการใน Chrome",
    ):
        """ค้นหาและ attach เข้าแท็บตาม target_id หรือ tab_url ที่ระบุ (หรือใช้ default_matcher)"""
        tabs = await self.get_tabs()
        tab = None
        if target_id:
            tab = next((t for t in tabs if t.get("targetId") == target_id), None)
            if not tab:
                raise CDPError(f"ไม่พบแท็บที่มี target_id={target_id}")
        elif tab_url:
            tab = next((t for t in tabs if tab_url in t.get("url", "")), None)
            if not tab:
                raise CDPError(f"ไม่พบแท็บที่มี URL ตรงกับ '{tab_url}'")
        else:
            tab = default_matcher(tabs)
            if not tab:
                raise CDPError(not_found_msg)

        tid = tab["targetId"]
        sid = await self.attach_tab(tid)
        if not sid:
            raise CDPError(f"ไม่สามารถ Attach เข้าแท็บ {tid} ได้")
        return tid, sid, tab.get("url", "")

    async def find_and_attach_gemini(self, target_id: Optional[str] = None, tab_url: Optional[str] = None):
        if not target_id and not tab_url and self.gemini_session_id and self.gemini_target_id:
            try:
                url = await self.eval_js(self.gemini_session_id, "window.location.href", timeout=5.0)
                if url and "gemini.google.com" in url:
                    return self.gemini_target_id, self.gemini_session_id, url
            except Exception:
                pass
            await self._detach(self.gemini_session_id)
            self.gemini_session_id = self.gemini_target_id = None
        tid, sid, url = await self._resolve_tab_target(
            self._pick_tab,
            target_id=target_id,
            tab_url=tab_url,
            not_found_msg="ไม่พบแท็บ gemini.google.com เปิดอยู่ใน Chrome",
        )
        if not target_id and not tab_url:
            self.gemini_target_id, self.gemini_session_id = tid, sid
        return tid, sid, url

    async def _detach(self, sid: Optional[str]) -> None:
        if sid:
            try:
                await self.send_cmd("Target.detachFromTarget", {"sessionId": sid}, timeout=3.0, strict=False)
            except Exception:
                pass

    # ── page-state helpers (JS built from SELECTORS) ─────────────────────────
    def _q(self, key: str) -> str:
        return json.dumps(self.selectors[key])

    def state_js(self) -> str:
        return f"""(() => {{
          const bubbles = document.querySelectorAll({self._q('bubble')});
          const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
          const t = last ? (last.innerText || last.textContent || '').trim() : '';
          let h = 5381; for (let i = 0; i < t.length; i++) h = ((h << 5) + h + t.charCodeAt(i)) | 0;
          const comp = document.querySelector({self._q('composer')});
          return {{ isGenerating: !!document.querySelector({self._q('stop')}), bubbles: bubbles.length,
                   userTurns: document.querySelectorAll({self._q('user_turn')}).length,
                   lastLen: t.length, lastHash: h,
                   composerLen: comp ? (comp.innerText || comp.textContent || '').trim().length : -1 }};
        }})()"""

    def type_js(self, text: str) -> str:
        return f"""(() => {{
          const text = {json.dumps(text, ensure_ascii=False)};
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
          const el = document.querySelector({self._q('composer')});
          if (!el) return {{ success: false, error: 'ไม่พบช่องพิมพ์ข้อความในหน้า Gemini Web' }};
          el.focus();
          const sel = window.getSelection(), range = document.createRange();
          range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
          let inserted = false;
          try {{ inserted = document.execCommand('insertText', false, text); }} catch (e) {{}}
          let cur = el.innerText || el.textContent || '';
          if (!inserted || norm(cur) !== norm(text)) {{
            el.textContent = '';
            el.textContent = text;
            el.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, inputType: 'insertText', data: text }}));
            cur = el.innerText || el.textContent || '';
          }}
          return {{ success: norm(cur).length > 0, len: norm(cur).length, exact: norm(cur) === norm(text) }};
        }})()"""

    def _send_lookup(self) -> str:
        return f"""const el = document.querySelector({self._q('composer')});
          const scope = (el && el.closest({self._q('composer_root')})) || document;
          const btn = scope.querySelector({self._q('send')}) || document.querySelector({self._q('send')});
          const ready = !!btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true';"""

    def send_ready_js(self) -> str:
        return f"(() => {{ {self._send_lookup()} return {{ found: !!btn, ready }}; }})()"

    def send_js(self) -> str:
        return f"""(() => {{ {self._send_lookup()}
          if (ready) {{ btn.click(); return {{ sent: true, method: 'button_click' }}; }}
          if (el) {{ el.focus();
            for (const type of ['keydown', 'keypress', 'keyup'])
              el.dispatchEvent(new KeyboardEvent(type, {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
            return {{ sent: true, method: 'enter_key' }}; }}
          return {{ sent: false }}; }})()"""

    def blocks_js(self) -> str:
        return f"""(() => {{
          const bubbles = document.querySelectorAll({self._q('bubble')});
          if (!bubbles.length) return {{ blocks: [] }};
          const panel = bubbles[bubbles.length - 1];
          const top = Array.from(panel.querySelectorAll('code-block, pre'))
            .filter(el => !(el.parentElement && el.parentElement.closest('code-block, pre')));
          return {{ blocks: top.map(el => {{
            const code = el.querySelector('code');
            const langEl = el.querySelector('.header-text, .code-block-decoration-header, .language-header');
            const cls = ((code && code.className) || '').match(/language-([\\w+-]+)/i) || [];
            return {{ lang: ((langEl && langEl.textContent) || cls[1] || '').trim().toLowerCase(),
                     code: (code || el).textContent.replace(/\\r\\n/g, '\\n') }};
          }}) }};
        }})()"""

    def stop_js(self) -> str:
        return (
            f"(() => {{ const b = document.querySelector({self._q('stop')}); "
            "if (b) { b.click(); return { clicked: true }; } return { clicked: false }; })()"
        )

    async def _stop_generation(self, sid) -> None:
        try:
            await self.eval_js(sid, self.stop_js(), timeout=5.0)
        except Exception as e:
            log.warning("could not press Stop after cancel: %r", e)

    async def read_state(self, sid, timeout: float = 10.0) -> Dict[str, Any]:
        st = await self.eval_js(sid, self.state_js(), timeout=timeout)
        if not isinstance(st, dict):
            raise CDPError("state probe returned no data")
        return st

    async def extract_last_blocks(self, sid) -> List[Dict[str, str]]:
        try:
            return (await self.eval_js(sid, self.blocks_js())).get("blocks", [])
        except Exception as e:
            log.warning("block extraction failed: %r", e)
            return []

    # ── shared upload validation (jail + sensitive filter) ───────────────────
    def _validate_upload_files_sync(self, file_paths: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
        files: List[str] = []
        rejected: List[Dict[str, str]] = []
        for fp in file_paths:
            try:
                p = str(resolve_in_roots(fp, self.upload_roots)) if self.upload_roots else os.path.realpath(str(fp))
            except PathNotAllowed as e:
                rejected.append({"path": fp, "reason": str(e)})
                continue
            if not os.path.isfile(p):
                rejected.append({"path": fp, "reason": "file not found"})
                continue
            why = sensitive_reason(p)
            if why:
                rejected.append({"path": fp, "reason": f"sensitive file ({why})"})
                continue
            files.append(p)
        return files, rejected

    async def upload_files_to_gemini(self, sid, file_paths):
        if not file_paths:
            return {"ok": True, "count": 0, "files": []}
        files, rejected = await asyncio.to_thread(self._validate_upload_files_sync, file_paths)
        if rejected:
            return {
                "ok": False,
                "error": "ไฟล์ถูกปฏิเสธ: " + "; ".join(f"{r['path']} ({r['reason']})" for r in rejected),
                "rejected": rejected,
            }
        oid = None
        try:
            await self.eval_js(sid, _MOUNT_JS)
            await asyncio.sleep(0.4)
            res = await self.send_cmd(
                "Runtime.evaluate",
                {"expression": "document.querySelector('input[type=\"file\"]')", "returnByValue": False},
                session_id=sid,
            )
            oid = ((res.get("result") or {}).get("result") or {}).get("objectId")
            if not oid:
                return {"ok": False, "error": "ไม่พบ input[type='file'] บนหน้า Gemini Web"}
            await self.send_cmd("DOM.setFileInputFiles", {"files": files, "objectId": oid}, session_id=sid)
            await self.eval_js(sid, _CLOSE_MENU_JS)
            await asyncio.sleep(0.8)
            return {"ok": True, "files": files, "count": len(files)}
        except Exception as e:
            log.error("file upload failed: %r", e)
            return {"ok": False, "error": str(e)}
        finally:
            if oid:
                try:
                    await self.send_cmd("Runtime.releaseObject", {"objectId": oid}, session_id=sid, strict=False)
                except Exception:
                    pass

    # ── the ask pipeline (Gemini Web / Argon) ────────────────────────────────
    async def _wait_idle(self, sid, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not (await self.read_state(sid))["isGenerating"]:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_send_ready(self, sid, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = await self.eval_js(sid, self.send_ready_js())
            if r and r.get("ready"):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _confirm_sent(self, sid, before: Dict[str, Any], composer_filled: bool, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = await self.read_state(sid)
            if (
                st["userTurns"] > before["userTurns"]
                or st["isGenerating"]
                or st["bubbles"] > before["bubbles"]
                or (composer_filled and st["composerLen"] == 0)
            ):
                return True
            await asyncio.sleep(0.4)
        return False

    async def ask_gemini_tab(
        self,
        prompt_text,
        web_model=None,
        thinking_level=None,
        files=None,
        *,
        max_wait: float = 300.0,
        send_confirm_timeout: float = 10.0,
        quiet_samples: int = 4,
        fire_and_forget: bool = False,
        target_id: Optional[str] = None,
        tab_url: Optional[str] = None,
        draft_only: bool = False,
    ):
        async with self.ask_lock:
            t0 = time.monotonic()
            deadline = t0 + max_wait
            tid, sid, resolved_url = await self.find_and_attach_gemini(target_id=target_id, tab_url=tab_url)
            try:
                await self.send_cmd("Target.activateTarget", {"targetId": tid}, timeout=5.0)
            except Exception:
                pass

            # 0) ไม่เริ่มส่งทับคำตอบที่กำลังเจนอยู่ และรอให้ Watcher งานเก่าดูดคำตอบเซฟลงดิสก์ให้เสร็จก่อน
            if (await self.read_state(sid))["isGenerating"] and not await self._wait_idle(sid, 60.0):
                return {"ok": False, "error": "Gemini ยังสร้างคำตอบก่อนหน้าไม่เสร็จ (ปุ่ม Stop ค้างเกิน 60 วินาที)"}
            prev_watcher = self._argon_watchers.get(tid)
            if prev_watcher and not prev_watcher.done():
                await asyncio.wait([prev_watcher], timeout=5.0)

            sel_info: Dict[str, Any] = {}
            if web_model or thinking_level:
                sel_info = await self.select_model_and_thinking(
                    sid, web_model=web_model, thinking_level=thinking_level
                )
                await asyncio.sleep(0.25)

            before = await self.read_state(sid)

            upload = None
            if files:
                upload = await self.upload_files_to_gemini(sid, files)
                if not upload.get("ok"):
                    return {"ok": False, "error": f"แนบไฟล์ไม่สำเร็จ: {upload.get('error')}", "upload": upload}

            typed = await self.eval_js(sid, self.type_js(prompt_text))
            if not typed or not typed.get("success"):
                return {"ok": False, "error": (typed or {}).get("error", "ไม่สามารถพิมพ์ข้อความลงแท็บ Gemini ได้")}

            if draft_only:
                return {
                    "ok": True,
                    "draft": True,
                    "targetId": tid,
                    "tabUrl": resolved_url,
                    "targetTab": "Gemini Web",
                    "elapsedMs": int((time.monotonic() - t0) * 1000),
                    "attachedFiles": (upload or {}).get("files", []),
                    "appliedModel": sel_info.get("appliedModel"),
                    "appliedThinking": sel_info.get("appliedThinking"),
                    "selectionNote": sel_info.get("note", ""),
                }

            if not await self._wait_send_ready(sid, 60.0 if files else 5.0) and files:
                return {"ok": False, "error": "ไฟล์ยังอัปโหลดไม่เสร็จ (ปุ่มส่งยังถูกปิดเกิน 60 วินาที)"}
            sent = await self.eval_js(sid, self.send_js())
            if not sent or not sent.get("sent"):
                return {"ok": False, "error": "ไม่พบปุ่มส่งข้อความบนหน้า Gemini Web"}

            # ยืนยันว่าหน้า Gemini เริ่มรับข้อความเข้าระบบแล้ว (ตรวจสอบเพียงครั้งเดียว)
            if not await self._confirm_sent(sid, before, typed.get("len", 0) > 0, send_confirm_timeout):
                return {
                    "ok": False,
                    "error": (
                        f"สั่งส่งแล้ว ({sent.get('method')}) แต่หน้า Gemini ไม่รับข้อความ — "
                        "ไม่มี user turn ใหม่/ช่องพิมพ์ยังไม่ว่าง/ไม่เริ่มสร้างคำตอบ"
                    ),
                }

            # ยกเลิก Watcher เก่าของแท็บนี้ก่อนเริ่มงานใหม่เสมอ (ป้องกัน Watcher ซ้อนกันในแท็บเดียวกัน)
            await self._cancel_task_safely(self._argon_watchers.pop(tid, None))

            job_id = f"argon_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
            self._register_job(job_id, "argon", ARGON_HIST_DIR, prompt_text)
            await asyncio.to_thread(set_job_pending, ARGON_HIST_DIR, job_id, prompt_text)

            # โหมด Fire-and-Forget: ปล่อย Background Watcher แล้วตอบกลับพร้อม jobId ทันที
            if fire_and_forget:
                watcher = self._track_bg_task(
                    asyncio.create_task(
                        self._watch_and_save_argon(
                            sid,
                            before,
                            t0,
                            deadline,
                            max_wait,
                            quiet_samples,
                            job_id=job_id,
                        )
                    )
                )
                self._argon_watchers[tid] = watcher
                return {
                    "ok": True,
                    "dispatched": True,
                    "jobId": job_id,
                    "statusFile": os.path.join(ARGON_HIST_DIR, "latest.status.json"),
                    "outputFile": os.path.join(ARGON_HIST_DIR, "latest.md"),
                    "targetTab": "Gemini Web",
                    "elapsedMs": int((time.monotonic() - t0) * 1000),
                    "sendMethod": sent.get("method"),
                    "attachedFiles": (upload or {}).get("files", []),
                    "appliedModel": sel_info.get("appliedModel"),
                    "appliedThinking": sel_info.get("appliedThinking"),
                    "selectionNote": sel_info.get("note", ""),
                }

            try:
                return await self._await_answer(
                    sid,
                    before,
                    t0,
                    deadline,
                    sent,
                    upload,
                    sel_info,
                    max_wait,
                    quiet_samples,
                    job_id=job_id,
                )
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_generation(sid))
                await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", "คำขอถูกยกเลิกโดยผู้เรียก", t0)
                raise

    async def _watch_and_save_argon(
        self,
        sid,
        before,
        t0,
        deadline,
        max_wait,
        quiet_samples,
        job_id: str,
    ):
        """เฝ้ามอนิเตอร์หน้าจอ Argon แบบ Background พร้อมดัก Timeout/Error/Quota ครบทุกกรณี"""
        try:
            prev_sig, stable, seen_new, errs = None, 0, False, 0
            st: Dict[str, Any] = {}
            while time.monotonic() < deadline:
                try:
                    st = await self.read_state(sid)
                    errs = 0
                except Exception as e:
                    errs += 1
                    if errs >= 5:
                        err_msg = f"อ่านสถานะหน้า Gemini ไม่ได้ต่อเนื่อง 5 ครั้ง: {e}"
                        log.warning("watch argon read_state failed: %s", err_msg)
                        await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", err_msg, t0)
                        return
                    await asyncio.sleep(1.0)
                    continue

                if st["isGenerating"] or st["bubbles"] > before["bubbles"] or st["lastHash"] != before["lastHash"]:
                    seen_new = True
                if seen_new and not st["isGenerating"] and st["lastLen"] > 0:
                    sig = (st["lastLen"], st["lastHash"])
                    stable = stable + 1 if sig == prev_sig else 0
                    prev_sig = sig
                    if stable >= quiet_samples - 1:
                        break
                else:
                    stable, prev_sig = 0, None
                await asyncio.sleep(0.6)
            else:
                timeout_msg = (
                    f"Timeout: ไม่พบสัญญาณจบคำตอบภายใน {int(max_wait)} วินาที "
                    f"(seen_new={seen_new}, isGenerating={st.get('isGenerating')}, bubbles={st.get('bubbles')})"
                )
                log.warning("watch argon timed out (%s): %s", job_id, timeout_msg)
                await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "timeout", timeout_msg, t0)
                return

            md = await self.extract_last_message_markdown(sid)
            answer = md.strip() if (md and md.strip()) else ""
            if not answer:
                answer = (
                    await self.eval_js(
                        sid,
                        f"(() => {{ const b = document.querySelectorAll({self._q('bubble')}); "
                        "return b.length ? (b[b.length-1].innerText || '').trim() : ''; })()",
                    )
                ) or ""

            if not answer:
                await self._mark_job_failed(
                    ARGON_HIST_DIR, job_id, "argon", "error", "ดึงคำตอบจากหน้า Gemini Web ได้ข้อความว่าง", t0
                )
                return

            if len(answer) < 400 and LIMIT_RE.search(answer):
                limit_msg = f"Gemini ตอบด้วยข้อความจำกัดโควต้า/ข้อผิดพลาด: {answer[:200]}"
                log.warning("Argon quota limit (%s): %s", job_id, limit_msg)
                await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "quota_limit", limit_msg, t0)
                return

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            meta = (
                f"<!-- Auto-Saved by Argon Bridge at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"| Job: {job_id} | Elapsed: {elapsed_ms}ms -->\n\n"
            )
            status_data = await asyncio.to_thread(
                atomic_save_history, ARGON_HIST_DIR, "argon", answer, meta, job_id, elapsed_ms
            )
            self._mark_job_done_mem(job_id, "argon", status_data, answer)
            log.info("✅ Argon answer Auto-Saved (%s, %d chars)", job_id, len(answer))
        except asyncio.CancelledError:
            log.info("Argon watcher cancelled for job %s", job_id)
            self._mark_job_superseded_mem(job_id)
            raise
        except Exception as e:
            log.warning("Error watching Argon state (%s): %r", job_id, e)
            await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", str(e), t0)

    async def _await_answer(
        self,
        sid,
        before,
        t0,
        deadline,
        sent,
        upload,
        sel_info,
        max_wait,
        quiet_samples,
        job_id: str,
    ):
        prev_sig, stable, seen_new, errs = None, 0, False, 0
        st: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                st = await self.read_state(sid)
                errs = 0
            except Exception as e:
                errs += 1
                if errs >= 5:
                    err_msg = f"อ่านสถานะหน้า Gemini ไม่ได้ต่อเนื่อง 5 ครั้ง: {e}"
                    await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", err_msg, t0)
                    return {"ok": False, "jobId": job_id, "error": err_msg}
                await asyncio.sleep(1.0)
                continue

            if st["isGenerating"] or st["bubbles"] > before["bubbles"] or st["lastHash"] != before["lastHash"]:
                seen_new = True
            if seen_new and not st["isGenerating"] and st["lastLen"] > 0:
                sig = (st["lastLen"], st["lastHash"])
                stable = stable + 1 if sig == prev_sig else 0
                prev_sig = sig
                if stable >= quiet_samples - 1:
                    break
            else:
                stable, prev_sig = 0, None
            await asyncio.sleep(0.6)
        else:
            timeout_msg = (
                f"Timeout: ไม่พบสัญญาณจบคำตอบภายใน {int(max_wait)} วินาที "
                f"(seen_new={seen_new}, isGenerating={st.get('isGenerating')}, bubbles={st.get('bubbles')})"
            )
            await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "timeout", timeout_msg, t0)
            return {"ok": False, "jobId": job_id, "error": timeout_msg}

        try:
            md = await self.extract_last_message_markdown(sid)
            answer = md.strip() if (md and md.strip()) else ""
            if not answer:
                answer = (
                    await self.eval_js(
                        sid,
                        f"(() => {{ const b = document.querySelectorAll({self._q('bubble')}); "
                        "return b.length ? (b[b.length-1].innerText || '').trim() : ''; })()",
                    )
                ) or ""
        except Exception as e:
            err_msg = f"สกัดคำตอบจากหน้า Gemini ไม่สำเร็จ: {e}"
            await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", err_msg, t0)
            return {"ok": False, "jobId": job_id, "error": err_msg}

        if not answer:
            empty_msg = "ดึงคำตอบจากหน้า Gemini Web ได้ข้อความว่าง"
            await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "error", empty_msg, t0)
            return {"ok": False, "jobId": job_id, "error": empty_msg}

        if len(answer) < 400 and LIMIT_RE.search(answer):
            limit_msg = f"Gemini ตอบด้วยข้อความจำกัดโควต้า/ข้อผิดพลาด: {answer[:200]}"
            await self._mark_job_failed(ARGON_HIST_DIR, job_id, "argon", "quota_limit", limit_msg, t0)
            return {"ok": False, "jobId": job_id, "error": limit_msg, "limit": True}

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            meta = (
                f"<!-- Auto-Saved by Bridge at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"| Job: {job_id} | Elapsed: {elapsed_ms}ms -->\n\n"
            )
            status_data = await asyncio.to_thread(
                atomic_save_history, ARGON_HIST_DIR, "argon", answer, meta, job_id, elapsed_ms
            )
            self._mark_job_done_mem(job_id, "argon", status_data, answer)
        except Exception as e:
            log.warning("Auto-save Argon response failed: %r", e)

        return {
            "ok": True,
            "jobId": job_id,
            "gem": "Gemini Web",
            "answer": answer,
            "blocks": await self.extract_last_blocks(sid),
            "elapsedMs": elapsed_ms,
            "sendMethod": sent.get("method"),
            "attachedFiles": (upload or {}).get("files", []),
            "appliedModel": sel_info.get("appliedModel"),
            "appliedThinking": sel_info.get("appliedThinking"),
            "selectionNote": sel_info.get("note", ""),
        }

    # ── Google Cloud Studio (Gemini 4 Carbon) Persistent Pipe ───────────────
    async def find_and_attach_carbon(self, target_id: Optional[str] = None, tab_url: Optional[str] = None):
        """ค้นหาและเกาะแท็บ Google Cloud Studio (Carbon) แบบค้าง Session"""
        if not target_id and not tab_url and self.carbon_session_id and self.carbon_target_id:
            try:
                url = await self.eval_js(self.carbon_session_id, "window.location.href", timeout=5.0)
                if url and "agent-platform/studio" in url:
                    return self.carbon_target_id, self.carbon_session_id, url
            except Exception:
                pass
            await self._detach(self.carbon_session_id)
            self.carbon_session_id = self.carbon_target_id = None

        tid, sid, url = await self._resolve_tab_target(
            lambda tabs: next(
                (t for t in tabs if t.get("type") == "page" and "agent-platform/studio" in t.get("url", "")),
                None,
            ),
            target_id=target_id,
            tab_url=tab_url,
            not_found_msg="ไม่พบแท็บ Google Cloud Studio (agent-platform/studio) ใน Chrome",
        )
        if not target_id and not tab_url:
            self.carbon_target_id, self.carbon_session_id = tid, sid
        return tid, sid, url

    async def _read_carbon_state(self, sid: str, timeout: float = 10.0) -> Dict[str, Any]:
        st = await self.eval_js(sid, _CARBON_STATE_JS, timeout=timeout)
        if not isinstance(st, dict):
            raise CDPError("carbon state probe returned no data")
        return st

    async def ask_carbon_tab(
        self,
        prompt_text: str,
        files: Optional[List[str]] = None,
        timeout: float = 600.0,
        target_id: Optional[str] = None,
        tab_url: Optional[str] = None,
        draft_only: bool = False,
    ) -> Dict[str, Any]:
        """ส่งคำถามและไฟล์แนบไปยังแท็บ Google Cloud Studio ผ่าน Persistent Session (ใช้ carbon_lock แยกอิสระ)"""
        async with self.carbon_lock:
            tid, sid, url = await self.find_and_attach_carbon(target_id=target_id, tab_url=tab_url)
            t0 = time.monotonic()

            # 1. ตรวจสอบไฟล์ตาม Path Jail และ Sensitive Filter
            uploaded_files: List[str] = []
            if files:
                uploaded_files, rejected = await asyncio.to_thread(self._validate_upload_files_sync, files)
                if rejected:
                    return {
                        "ok": False,
                        "error": (
                            "ไฟล์ถูกปฏิเสธโดยมาตรการความปลอดภัย: "
                            + "; ".join(f"{r['path']} ({r['reason']})" for r in rejected)
                        ),
                        "rejected": rejected,
                    }

            # 0) เก็บ Snapshot ก่อนส่ง (โหมด Fast Fire-and-Forget)
            try:
                before = await self._read_carbon_state(sid)
            except Exception:
                before = {"isGen": False, "count": 0, "lastLen": 0, "lastHash": 5381}

            if uploaded_files:
                doc = await self.send_cmd("DOM.getDocument", session_id=sid, timeout=10.0)
                root_id = doc.get("result", {}).get("root", {}).get("nodeId") or doc.get("root", {}).get("nodeId")
                file_node = await self.send_cmd(
                    "DOM.querySelector",
                    {"nodeId": root_id, "selector": 'input[type="file"]'},
                    session_id=sid,
                    timeout=10.0,
                )
                node_id = file_node.get("result", {}).get("nodeId") or file_node.get("nodeId")
                if not node_id:
                    return {"ok": False, "error": "ไม่พบ input[type='file'] บนหน้า Google Cloud Studio"}
                await self.send_cmd(
                    "DOM.setFileInputFiles",
                    {"files": uploaded_files, "nodeId": node_id},
                    session_id=sid,
                    timeout=15.0,
                )
                await asyncio.sleep(2.0)

            # 2. ป้อนข้อความ Prompt ลง Textbox (Serialize ครั้งเดียวด้วย ensure_ascii=False)
            js_type = f"""(() => {{
                const promptText = {json.dumps(prompt_text, ensure_ascii=False)};
                const box = document.querySelector('div[role="textbox"]') || document.querySelector('textarea');
                if (!box) return {{ ok: false, error: 'NO_BOX' }};
                box.focus();
                if (box.tagName === 'TEXTAREA') {{
                    box.value = promptText;
                    box.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    box.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }} else {{
                    box.innerText = promptText;
                    box.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: promptText }}));
                }}
                return {{ ok: true }};
            }})()"""
            type_res = await self.eval_js(sid, js_type, timeout=10.0)
            if not type_res or not type_res.get("ok"):
                return {"ok": False, "error": f"ไม่สามารถป้อนข้อความลงหน้า Studio ได้: {type_res}"}

            if draft_only:
                return {
                    "ok": True,
                    "draft": True,
                    "targetId": tid,
                    "tabUrl": url,
                    "targetTab": "Google Cloud Studio (Carbon)",
                    "elapsedMs": int((time.monotonic() - t0) * 1000),
                    "attachedFiles": uploaded_files,
                }

            await asyncio.sleep(0.5)

            # 3. กดส่งคำสั่ง (Run / Send / Enter)
            js_send = """(() => {
                const box = document.querySelector('div[role="textbox"]') || document.querySelector('textarea');
                const scope = (box && (box.closest('form, ms-prompt-input-wrapper, .prompt-input-container, [class*="composer"], [class*="input-wrapper"]'))) || document;
                const btns = Array.from(scope.querySelectorAll('button, [role="button"]')).reverse();
                const isVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const runBtn = btns.find(b => {
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true' || !isVisible(b)) return false;
                    if (b.closest('code-block, ms-code-block, pre')) return false;
                    const label = (b.getAttribute('aria-label') || b.innerText || '').trim().toLowerCase();
                    return /^send$|^run$|\\bsend\\b|\\brun\\b/.test(label) && !label.includes('rerun');
                });
                if (runBtn) {
                    runBtn.click();
                    return 'CLICKED_RUN';
                }
                if (box) {
                    box.focus();
                    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                    return 'KEY_ENTER';
                }
                return 'NOT_FOUND';
            })()"""
            send_res = await self.eval_js(sid, js_send, timeout=10.0)
            if send_res == "NOT_FOUND":
                return {"ok": False, "error": "ไม่พบปุ่ม Run/Send หรือช่องพิมพ์บนหน้า Google Cloud Studio"}

            # 4. ยกเลิก Carbon Watcher เก่าของแท็บนี้ก่อน แล้วขึ้นป้ายไฟแดงและเริ่ม Watcher ใหม่
            await self._cancel_task_safely(self._carbon_watchers.pop(tid, None))

            job_id = f"carbon_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
            self._register_job(job_id, "carbon", CARBON_HIST_DIR, prompt_text)
            await asyncio.to_thread(set_job_pending, CARBON_HIST_DIR, job_id, prompt_text)

            watcher = self._track_bg_task(
                asyncio.create_task(
                    self._watch_and_save_carbon(sid, before, t0, timeout, job_id=job_id)
                )
            )
            self._carbon_watchers[tid] = watcher

            return {
                "ok": True,
                "dispatched": True,
                "jobId": job_id,
                "statusFile": os.path.join(CARBON_HIST_DIR, "latest.status.json"),
                "outputFile": os.path.join(CARBON_HIST_DIR, "latest.md"),
                "targetTab": url,
                "sendMethod": send_res,
                "elapsedMs": int((time.monotonic() - t0) * 1000),
                "uploadedFiles": uploaded_files,
            }

    async def _watch_and_save_carbon(
        self,
        sid: str,
        before: Dict[str, Any],
        t0: float,
        max_wait: float,
        job_id: str,
    ):
        """เฝ้ามอนิเตอร์หน้าจอ Carbon แบบ Hash-Only Polling พร้อมดัก Timeout/Error ครบถ้วน"""
        deadline = t0 + max_wait
        try:
            await asyncio.sleep(2.5)
            prev_sig = None
            stable_count = 0
            seen_new = False
            errs = 0
            st: Dict[str, Any] = {}

            while time.monotonic() < deadline:
                try:
                    st = await self._read_carbon_state(sid, timeout=10.0)
                    errs = 0
                except Exception as e:
                    errs += 1
                    if errs >= 5:
                        err_msg = f"อ่านสถานะหน้า Cloud Studio ไม่ได้ต่อเนื่อง 5 ครั้ง: {e}"
                        log.warning("watch carbon read_state failed (%s): %s", job_id, err_msg)
                        await self._mark_job_failed(CARBON_HIST_DIR, job_id, "carbon", "error", err_msg, t0)
                        return
                    await asyncio.sleep(2.0)
                    continue

                is_gen = bool(st.get("isGen", False))
                cur_count = int(st.get("count", 0))
                cur_len = int(st.get("lastLen", 0))
                cur_hash = st.get("lastHash")

                if (
                    is_gen
                    or cur_count > int(before.get("count", 0))
                    or cur_hash != before.get("lastHash")
                ):
                    seen_new = True

                if seen_new and not is_gen and cur_len > 0:
                    sig = (cur_len, cur_hash)
                    stable_count = stable_count + 1 if sig == prev_sig else 0
                    prev_sig = sig
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    prev_sig = None

                await asyncio.sleep(2.0)
            else:
                timeout_msg = (
                    f"Timeout: ไม่พบสัญญาณจบคำตอบจาก Carbon ภายใน {int(max_wait)} วินาที "
                    f"(seen_new={seen_new}, isGen={st.get('isGen')}, len={st.get('lastLen')})"
                )
                log.warning("watch carbon timed out (%s): %s", job_id, timeout_msg)
                await self._mark_job_failed(CARBON_HIST_DIR, job_id, "carbon", "timeout", timeout_msg, t0)
                if not st.get("isGen"):
                    return
                log.info("Carbon (%s) is still generating after timeout; continuing background late-save watch...", job_id)
                late_deadline = time.monotonic() + 600.0
                late_stable = 0
                late_prev_sig = None
                while time.monotonic() < late_deadline:
                    if not self.connected:
                        return
                    await asyncio.sleep(15.0)
                    if not self.connected:
                        return
                    try:
                        st = await self._read_carbon_state(sid, timeout=10.0)
                    except Exception:
                        late_stable = 0
                        late_prev_sig = None
                        continue
                    cur_len = int(st.get("lastLen", 0))
                    cur_hash = st.get("lastHash")
                    if not st.get("isGen") and cur_len > 0:
                        sig = (cur_len, cur_hash)
                        late_stable = late_stable + 1 if sig == late_prev_sig else 1
                        late_prev_sig = sig
                        if late_stable >= 2:
                            break
                    else:
                        late_stable = 0
                        late_prev_sig = None
                else:
                    return

            extracted = await self.eval_js(sid, _CARBON_EXTRACT_JS, timeout=15.0)
            cur_text = ((extracted or {}).get("text") or "").strip()
            if not cur_text:
                await self._mark_job_failed(
                    CARBON_HIST_DIR, job_id, "carbon", "error", "ดึงคำตอบจากหน้า Cloud Studio ได้ข้อความว่าง", t0
                )
                return

            if len(cur_text) < 400 and LIMIT_RE.search(cur_text):
                limit_msg = f"Carbon ตอบด้วยข้อความจำกัดโควต้า/ข้อผิดพลาด: {cur_text[:200]}"
                await self._mark_job_failed(CARBON_HIST_DIR, job_id, "carbon", "quota_limit", limit_msg, t0)
                return

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            meta = (
                f"<!-- Auto-Saved by Carbon Bridge at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"| Job: {job_id} | Elapsed: {int(elapsed_ms / 1000)}s -->\n\n"
            )
            status_data = await asyncio.to_thread(
                atomic_save_history, CARBON_HIST_DIR, "carbon", cur_text, meta, job_id, elapsed_ms
            )
            self._mark_job_done_mem(job_id, "carbon", status_data, cur_text)
            log.info("✅ Carbon answer Auto-Saved (%s, %d chars)", job_id, len(cur_text))
        except asyncio.CancelledError:
            log.info("Carbon watcher cancelled for job %s", job_id)
            self._mark_job_superseded_mem(job_id)
            raise
        except Exception as e:
            log.warning("Error watching Carbon state (%s): %r", job_id, e)
            await self._mark_job_failed(CARBON_HIST_DIR, job_id, "carbon", "error", str(e), t0)

    async def extract_carbon_markdown(self) -> Dict[str, Any]:
        """ดึงคำตอบล่าสุดจากแท็บ Google Cloud Studio (Carbon) และเซฟลง latest.md ทันที"""
        tid, sid, url = await self.find_and_attach_carbon()
        st = {}
        try:
            st = await self._read_carbon_state(sid, timeout=10.0)
        except Exception:
            pass
        res = await self.eval_js(sid, _CARBON_EXTRACT_JS, timeout=15.0)
        text = (res or {}).get("text", "").strip() if res else ""
        is_gen = bool(st.get("isGen"))
        job_id = None
        if text and not is_gen:
            cand_id = f"carbon_extract_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
            meta = f"<!-- Auto-Saved by Carbon Extract at {time.strftime('%Y-%m-%d %H:%M:%S')} | Job: {cand_id} -->\n\n"
            try:
                status_data = await asyncio.to_thread(
                    atomic_save_history, CARBON_HIST_DIR, "carbon", text, meta, cand_id
                )
                self._mark_job_done_mem(cand_id, "carbon", status_data, text)
                job_id = cand_id
            except Exception as save_err:
                log.warning("extract_carbon_markdown save failed: %r", save_err)
        return {"ok": True, "jobId": job_id, "generating": is_gen, "text": text, "url": (res or {}).get("url", url)}
