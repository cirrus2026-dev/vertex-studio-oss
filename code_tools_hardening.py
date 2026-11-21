"""
code_tools_hardening.py — drop-in fixes for code_tools.py (audit 2026-11-19)

Every section names the audit finding it addresses (IDs match the written report).
Pure stdlib, Python >= 3.9. Nothing here imports code_tools, so it can be adopted piecemeal.
"""
from __future__ import annotations

import codecs
import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import textwrap
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

MAX_READ_BYTES = 2_000_000


# ══════════════════════════════════════════════════════════════════════════════
# S-1  Path jail (fixes: find_file_in_roots L303-325 traversal / UNC, safe_write L558)
# ══════════════════════════════════════════════════════════════════════════════
class PathNotAllowed(Exception):
    pass


def resolve_in_roots(raw: str, roots: Iterable[str], *, must_be_file: bool = True) -> Path:
    """Resolve a user/AI-supplied path. The *resolved* result (symlinks followed) must lie inside a root."""
    s = (raw or "").strip().strip("'\"")
    if not s or "\x00" in s:
        raise PathNotAllowed("empty or invalid path")
    if s.startswith(("\\\\", "//")):                       # UNC / \\?\ / \\.\ -> SMB auth leak on Windows
        raise PathNotAllowed("UNC or device paths are not allowed")
    root_paths = [Path(r).resolve() for r in roots if r]
    if not root_paths:
        raise PathNotAllowed("no allowed roots configured")
    p = Path(s)                                           # NOTE: no os.path.expandvars on prompt-derived text
    candidates = [p] if p.is_absolute() else [r / p for r in root_paths]
    for c in candidates:
        try:
            rp = c.resolve()
        except (OSError, RuntimeError):
            continue
        if any(rp == r or r in rp.parents for r in root_paths):
            if not must_be_file or rp.is_file():
                return rp
    raise PathNotAllowed(f"outside allowed roots or not found: {s}")


# ══════════════════════════════════════════════════════════════════════════════
# S-3  Sensitive-file policy + secret masking (replaces is_sensitive_path_default L30-46)
# ══════════════════════════════════════════════════════════════════════════════
_HARD = (
    ".env", ".env.*", ".env-*", ".env_*", "*.env", ".envrc", ".bridge_token*",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.p8", "*.ppk", "*.jks", "*.keystore", "*.kdbx",
    "*.gpg", "*.asc", "*.pgp", "*.ovpn",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".pgpass", ".htpasswd", ".git-credentials", ".dockercfg", "kubeconfig",
    "*.tfstate", "*.tfstate.*", "*.tfvars", "wp-config.php",
    "adc.json", "access_tokens.db", "credentials.db",                       # gcloud / ADC
    "*serviceaccount*", "*service-account*", "*service_account*", "*adminsdk*",
    "*gateway-config*", "*gateway-state*",
    "cookies", "cookies.sqlite", "login data",
)
_SOFT = ("*secret*", "*credential*", "*password*", "*passwd*", "*apikey*", "*api_key*", "*api-key*", "*token*")
_CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".md"}
_SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".azure", ".docker", ".git", "gcloud", "legacy_credentials"}
_GCP_KEY_NAME = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]-[0-9a-f]{12}\.json$")   # <project>-<12 hex>.json
_SNIFF = re.compile(
    rb'-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----|"type"\s*:\s*"service_account"|"private_key"\s*:|"refresh_token"\s*:|"client_secret"\s*:'
)
_SNIFF_EXT = {"", ".json", ".txt", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".toml", ".properties", ".xml", ".csv", ".bak"}


def sensitive_reason(path: str, sniff: bool = True) -> Optional[str]:
    """Return why a path is sensitive (for logs), or None."""
    p = Path(str(path or ""))
    name = p.name.lower()
    if not name:
        return None
    if {part.lower() for part in p.parts[:-1]} & _SENSITIVE_DIRS:
        return "inside-sensitive-directory"
    if any(fnmatch.fnmatchcase(name, g) for g in _HARD):
        return "hard-pattern"
    if _GCP_KEY_NAME.match(name):
        return "gcp-service-account-filename"
    ext = p.suffix.lower()
    if ext not in _CODE_EXT and not name.startswith("tokenizer") and any(fnmatch.fnmatchcase(name, g) for g in _SOFT):
        return "soft-pattern"
    if sniff and ext in _SNIFF_EXT:
        try:
            with open(p, "rb") as f:
                if _SNIFF.search(f.read(65536)):
                    return "content-sniff"
        except OSError:
            pass
    return None


def is_sensitive_path(path: str, sniff: bool = True) -> bool:
    return sensitive_reason(path, sniff) is not None


# Linear-time patterns only: every quantifier is bounded or anchored to a literal keyword (no ReDoS).
SECRET_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[A-Za-z0-9+/=\r\n \t:,.]{8,12000}-----END [A-Z ]{0,30}PRIVATE KEY-----")),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("gcp_private_key_field", re.compile(r'"private_key"\s*:\s*"[^"]{20,}"')),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("sk_style_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s/:@]{1,64}:[^\s/@]{1,128}@")),
]
_KV_SECRET = re.compile(
    r"(?i)((?:secret|token|passw(?:or)?d|api[_-]?key|private[_-]?key|access[_-]?key)[A-Za-z0-9_\-]{0,40}\s*[:=]\s*)(['\"]?)([^\s'\"#,;]{8,200})\2"
)


def mask_secrets(text: str, max_chars: int = 5_000_000) -> str:
    if not text:
        return text
    head, tail = text[:max_chars], text[max_chars:]
    for label, rx in SECRET_PATTERNS:
        head = rx.sub(f"«REDACTED:{label}»", head)
    head = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}«REDACTED:value»{m.group(2)}", head)
    return head + tail


# ══════════════════════════════════════════════════════════════════════════════
# B-1  Encoding / BOM / EOL preserving I/O  (fixes: write_text newline="\n" L617, utf-8-sig+replace L270/577/654)
# ══════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class TextMeta:
    encoding: str = "utf-8"
    bom: bool = False
    eol: str = "\n"


_BOMS = {"utf-8": codecs.BOM_UTF8, "utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE}


def decode_bytes(b: bytes) -> Tuple[str, TextMeta]:
    bom, enc = False, "utf-8"
    if b.startswith(codecs.BOM_UTF8):
        bom, b = True, b[3:]
    elif b.startswith(codecs.BOM_UTF16_LE):
        bom, enc, b = True, "utf-16-le", b[2:]
    elif b.startswith(codecs.BOM_UTF16_BE):
        bom, enc, b = True, "utf-16-be", b[2:]
    try:
        text = b.decode(enc)
    except UnicodeDecodeError:
        for enc in ("cp874", "cp1252"):                    # Thai legacy first, then Western
            try:
                text = b.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            enc, text = "latin-1", b.decode("latin-1")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return text.replace("\r\n", "\n"), TextMeta(enc, bom, "\r\n" if crlf > lf else "\n")


def read_text_meta(path, max_bytes: int = MAX_READ_BYTES) -> Tuple[str, TextMeta]:
    p = Path(path)
    if p.stat().st_size > max_bytes:
        raise ValueError(f"file larger than {max_bytes:,} bytes: {p.name}")
    return decode_bytes(p.read_bytes())


def encode_text(text: str, meta: TextMeta) -> Tuple[bytes, TextMeta]:
    t = text.replace("\r\n", "\n")
    if meta.eol == "\r\n":
        t = t.replace("\n", "\r\n")
    try:
        data = t.encode(meta.encoding)
    except UnicodeEncodeError:                             # e.g. new emoji in a cp874 file -> upgrade to UTF-8 (reported)
        meta = TextMeta("utf-8", False, meta.eol)
        data = t.encode("utf-8")
    return (_BOMS.get(meta.encoding, b"") if meta.bom else b"") + data, meta


def atomic_write_bytes(target: Path, data: bytes, mode_from: Optional[Path] = None) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())                           # data is durable BEFORE the rename
        if mode_from is not None and mode_from.exists():
            shutil.copymode(mode_from, tmp)                # keep exec bit / read-only attribute
        os.replace(tmp, target)
        if hasattr(os, "O_DIRECTORY"):                     # POSIX: make the rename itself durable
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


PLACEHOLDER_RE = re.compile(
    r"(?:(?://|#|/\*|<!--|--|\*)[ \t]*)?(?:\.\.\.|…)[ \t]*[(\[]?[ \t]*"
    r"(?:existing|rest of|remaining|other|unchanged|previous|same as|โค้ด(?:ส่วน)?(?:ที่เหลือ|เดิม))"
    r"|(?://|#|/\*|<!--)[ \t]*[(\[]?[ \t]*(?:rest of|remaining|previous)[ \t]+(?:of[ \t]+)?(?:the[ \t]+)?"
    r"(?:code|file|function|class|content|implementation|methods?)\b.{0,30}?(?:unchanged|remains?|same|here|omitted)",
    re.IGNORECASE,
)


def safe_write_text(
    file_path: str,
    new_content: str,
    *,
    roots: Optional[Iterable[str]] = None,
    validator: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
    allow_shrink: bool = False,
    shrink_ratio: float = 0.45,
    backup_dir: Optional[str] = None,
    keep_backups: int = 20,
    allow_sensitive: bool = False,
) -> Dict[str, object]:
    """Never raises. Same result keys / stage names as the original safe_write_code_file (ok, stage, error, backup_path, file_path, lines_written, validation)."""
    try:
        target = resolve_in_roots(file_path, roots, must_be_file=False) if roots else Path(file_path).resolve()
    except PathNotAllowed as e:
        return {"ok": False, "stage": "path_policy", "error": str(e), "backup_path": None}
    if not allow_sensitive and sensitive_reason(str(target), sniff=False):
        return {"ok": False, "stage": "sensitive_file", "error": f"refusing to write sensitive file: {target.name}", "backup_path": None}
    if validator:
        ok, msg = validator(str(target), new_content)
        if not ok:
            return {"ok": False, "stage": "syntax_validation", "error": msg, "backup_path": None}

    backup_path: Optional[str] = None
    try:
        meta = TextMeta()
        if target.is_file():
            old_text, meta = read_text_meta(target)
            old_lines, new_lines = old_text.count("\n") + 1, new_content.count("\n") + 1
            shrunk = (old_lines >= 50 and new_lines < old_lines * shrink_ratio) or (
                len(old_text) >= 2500 and len(new_content) < len(old_text) * shrink_ratio
            )
            if not allow_shrink and (shrunk or PLACEHOLDER_RE.search(new_content)):
                return {"ok": False, "stage": "snippet_guard", "error": "looks like a partial snippet / placeholder, not a full file", "backup_path": None}
            bdir = Path(backup_dir).resolve() if backup_dir else target.parent / ".backups"
            bdir.mkdir(parents=True, exist_ok=True)
            bfile = bdir / f"{target.name}.{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.bak"
            shutil.copy2(target, bfile)
            backup_path = str(bfile)
            olds = sorted(bdir.glob(f"{target.name}.*.bak"), key=lambda x: x.stat().st_mtime, reverse=True)
            for stale in olds[keep_backups:]:
                stale.unlink(missing_ok=True)
        data, meta2 = encode_text(new_content, meta)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, data, target if target.exists() else None)
    except Exception as e:                                 # never leak an exception through the API
        return {"ok": False, "stage": "atomic_write", "error": f"{type(e).__name__}: {e}", "backup_path": backup_path}
    return {"ok": True, "file_path": str(target), "lines_written": new_content.count("\n") + 1, "backup_path": backup_path, "validation": "OK",
            "encoding": meta2.encoding, "bom": meta2.bom, "eol": "CRLF" if meta2.eol == "\r\n" else "LF",
            "encoding_changed": meta2.encoding != meta.encoding}


# ══════════════════════════════════════════════════════════════════════════════
# B-3  Patch locator v2 (fixes locate_smart_patch_span L637-728)
#   • one line-splitting rule ("\n" only) for every tier  • line-aligned exact match
#   • symbol_hint & near_line honoured by ALL tiers       • ambiguity detected in tiers 1-3
#   • fuzzy tier: pre-filtered + cached SequenceMatcher   • returns file hash for TOCTOU check
# ══════════════════════════════════════════════════════════════════════════════
def _norm_lines(ls: List[str]) -> str:
    return textwrap.dedent("\n".join(l.rstrip().expandtabs(4) for l in ls)).strip("\n")


def _symbol_windows(text: str, name: str, total: int) -> List[Tuple[int, int]]:
    import ast
    out: List[Tuple[int, int]] = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return out
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])    # include decorators
            out.append((start - 1, min(getattr(n, "end_lineno", total), total)))
    return out


def _pick(hits: List[Tuple[int, int]], near_line: int, max_drift: int, tier: str, conf: float, base: dict):
    if len(hits) == 1:
        s, e = hits[0]
        return {"ok": True, "tier": tier, "confidence": conf, "startLine": s, "endLine": e, **base}
    if near_line > 0:
        s, e = min(hits, key=lambda h: abs(h[0] - near_line))
        if abs(s - near_line) <= max_drift:
            return {"ok": True, "tier": tier, "confidence": conf, "startLine": s, "endLine": e, **base}
        return {"ok": False, "error": f"{len(hits)} matches, nearest is {abs(s - near_line)} lines from near_line (> {max_drift})"}
    return {"ok": False, "error": f"ambiguous: {len(hits)} matches at lines {[h[0] for h in hits[:6]]}; pass near_line or symbol_hint"}


def locate_span_v2(file_path: str, search_block: str, symbol_hint: str = "", near_line: int = 0,
                   min_fuzzy_ratio: float = 0.90, max_drift: int = 200, max_block_lines: int = 400,
                   fuzzy_budget_s: float = 3.0) -> Dict[str, object]:
    p = Path(file_path)
    if not p.is_file():
        return {"ok": False, "error": f"file not found: {file_path}"}
    if not search_block or not search_block.strip():
        return {"ok": False, "error": "search_block is empty"}
    text, _ = read_text_meta(p)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    want = search_block.replace("\r\n", "\n").strip("\n").split("\n")
    k = len(want)
    if k > max_block_lines:
        return {"ok": False, "error": f"search_block too large ({k} lines > {max_block_lines})"}
    base = {"file_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    windows = [(0, total)]
    if symbol_hint and p.suffix.lower() == ".py":
        sw = _symbol_windows(text, symbol_hint.strip(), total)
        if sw:
            windows = sw
        else:
            base["warning"] = f"symbol_hint '{symbol_hint}' not found; searched whole file"

    def scan(match_fn: Callable[[int], bool]) -> List[Tuple[int, int]]:
        hits, seen = [], set()
        for a, b in windows:
            for i in range(a, b - k + 1):
                if i not in seen and match_fn(i):
                    seen.add(i)
                    hits.append((i + 1, i + k))
        return hits

    first = want[0]
    hits = scan(lambda i: lines[i] == first and lines[i:i + k] == want)                       # tier 1: exact, line-aligned
    if hits:
        return _pick(hits, near_line, max_drift, "exact", 1.0, base)

    tgt = _norm_lines(want)
    f_strip = first.strip()
    hits = scan(lambda i: lines[i].strip() == f_strip and _norm_lines(lines[i:i + k]) == tgt)  # tier 2: indentation/trailing-ws tolerant
    if hits:
        return _pick(hits, near_line, max_drift, "dedent_exact", 0.98, base)

    # tier 3 — fuzzy.  Stage A: rare-line voting finds candidate starts in ~O(N).  Stage B: exact ratio on survivors only.
    deadline = time.monotonic() + fuzzy_budget_s
    sm = difflib.SequenceMatcher(None, "", tgt, autojunk=False)   # b = target is fixed -> b2j built once

    def squash(x: str) -> str:
        return " ".join(x.split())

    index: Dict[str, List[int]] = {}
    for a, b in windows:
        for m in range(a, b):
            index.setdefault(squash(lines[m]), []).append(m)
    votes: Dict[int, float] = {}
    for j, wl in enumerate(squash(w) for w in want):
        pos = index.get(wl) if wl else None
        if not pos or len(pos) > 50:                       # blank / ubiquitous lines carry no information
            continue
        for m in pos:
            votes[m - j] = votes.get(m - j, 0.0) + 1.0 / len(pos)
    starts = [st for st, _ in sorted(votes.items(), key=lambda kv: -kv[1])[:12]]
    if not starts:                                         # every line differs -> degrade to first-line anchor, still time-boxed
        fsm = difflib.SequenceMatcher(None, "", f_strip, autojunk=False)
        for a, b in windows:
            for i in range(a, b):
                fsm.set_seq1(lines[i].strip())
                if fsm.real_quick_ratio() >= 0.6 and fsm.quick_ratio() >= 0.6 and fsm.ratio() >= 0.6:
                    starts.append(i)
                if time.monotonic() > deadline:
                    return {"ok": False, "error": f"fuzzy search exceeded {fuzzy_budget_s}s budget; give a more specific search_block"}
    cand: List[Tuple[float, int, int]] = []
    seen_c = set()
    for st in starts:
        for d in (-2, -1, 0, 1, 2):
            for ln in (k - 1, k, k + 1):
                i = st + d
                if ln < 1 or (i, ln) in seen_c or not any(a <= i and i + ln <= b for a, b in windows):
                    continue
                seen_c.add((i, ln))
                sm.set_seq1(_norm_lines(lines[i:i + ln]))
                if sm.real_quick_ratio() < min_fuzzy_ratio - 0.08 or sm.quick_ratio() < min_fuzzy_ratio - 0.08:
                    continue
                r = sm.ratio()
                if r >= min_fuzzy_ratio - 0.08:
                    cand.append((r, i + 1, i + ln))
        if time.monotonic() > deadline:
            return {"ok": False, "error": f"fuzzy search exceeded {fuzzy_budget_s}s budget; give a more specific search_block"}
    cand.sort(key=lambda c: (-c[0], abs(c[1] - near_line) if near_line > 0 else 0))
    if cand and cand[0][0] >= min_fuzzy_ratio:
        best = cand[0]
        rivals = [c for c in cand[1:] if (c[2] < best[1] or c[1] > best[2]) and best[0] - c[0] < 0.08]
        if rivals and near_line <= 0:
            return {"ok": False, "error": f"fuzzy match ambiguous ({best[0]:.2f} @L{best[1]} vs {rivals[0][0]:.2f} @L{rivals[0][1]})"}
        diff = "\n".join(difflib.unified_diff(lines[best[1] - 1:best[2]], want, "file", "search_block", lineterm="", n=0))
        return {"ok": True, "tier": "fuzzy_semantic", "confidence": round(best[0], 3), "startLine": best[1], "endLine": best[2],
                "requires_review": True, "diff": diff, **base}     # fuzzy hits must NEVER be applied silently
    return {"ok": False, "error": "no match (exact / normalized / fuzzy)"}


# ══════════════════════════════════════════════════════════════════════════════
# B-6  Markdown fence that cannot be closed by the content (fixes L445-448)
# ══════════════════════════════════════════════════════════════════════════════
def fence_for(content: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}{lang}\n{content}\n{f}"


# ══════════════════════════════════════════════════════════════════════════════
# C-1  Per-file lock registry (reference implementation for server.py's get_file_lock)
# ══════════════════════════════════════════════════════════════════════════════
class _Lock:
    """Weak-referenceable re-entrant lock wrapper."""
    __slots__ = ("_l", "__weakref__")

    def __init__(self):
        self._l = threading.RLock()

    def acquire(self, timeout: float = -1) -> bool:
        return self._l.acquire(timeout=timeout)

    def release(self):
        self._l.release()


class FileLocks:
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: "weakref.WeakValueDictionary[str, _Lock]" = weakref.WeakValueDictionary()

    @staticmethod
    def key(path: str) -> str:                              # one key for ./a.py, a/../a.py, A.PY (Windows), symlinks
        return os.path.normcase(os.path.realpath(path))

    def get(self, path: str) -> _Lock:
        k = self.key(path)
        with self._guard:                                   # check-then-create must be atomic
            lk = self._locks.get(k)
            if lk is None:
                lk = _Lock()
                self._locks[k] = lk
            return lk


@contextmanager
def hold(lock, timeout: float, what: str):
    """Acquire with a deadline so a stuck holder surfaces as an error, not a silent hang."""
    if not lock.acquire(timeout=timeout):
        raise TimeoutError(f"lock timeout after {timeout}s: {what}")
    try:
        yield
    finally:
        lock.release()
