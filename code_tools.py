# .\code_tools.py
# Local AI Coding Dashboard — Smart Outline, @File Mention Resolver, Pre-flight Syntax Check & Safe Writer

import ast
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SYS_PATH_DIR = os.path.abspath(os.path.dirname(__file__))
DESKTOP_DIR = os.path.abspath(os.path.expandvars(r"%USERPROFILE%\Desktop"))
MINIFIED_CHAR_LIMIT = 15000

DEFAULT_SENSITIVE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "*secret*", "*credentials*", "id_rsa*", "id_ed25519*",
    "*service-account*", "*service_account*", "*gateway-config*",
    "*gateway-state*", "token.json"
)

def is_sensitive_path_default(filepath: str) -> bool:
    """ตรวจสอบว่าไฟล์เป็นไฟล์ความลับ (Secret / Credential / Key) หรือไม่เพื่อป้องกันข้อมูลรั่วไหล (ตรวจคำต้องห้ามก่อนเสมอ 100%)"""
    path_str = str(filepath or "").replace("\\", "/").lower()
    name = os.path.basename(path_str)
    if not name:
        return False
    # 1. ตรวจสอบชื่อไฟล์ความลับและแพทเทิร์นต้องห้ามทั้งหมดก่อนเสมอ
    if name == ".env" or name.startswith(".env."):
        return True
    for pat in DEFAULT_SENSITIVE_PATTERNS:
        if fnmatch.fnmatch(name, pat.lower()) or fnmatch.fnmatch(path_str, f"*/{pat.lower()}"):
            return True
    for pat in ("*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*"):
        if fnmatch.fnmatch(name, pat):
            return True
    # 2. ตรวจโฟลเดอร์ความลับ
    if set(path_str.split("/")).intersection({".ssh", ".aws", "secrets", ".gnupg"}):
        return True
    # 3. ไม่พบคำต้องห้าม จึงพิจารณานามสกุลโค้ดทั่วไป
    ext = os.path.splitext(name)[1].lower()
    if ext in {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".md"}:
        return False
    return False

CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)

def _strip_code_fences(text: str) -> str:
    """ตัดบล็อกโค้ด ```...``` ออกชั่วคราวก่อนสกัด @file เพื่อไม่ให้จับติด decorator หรือ @media"""
    return CODE_FENCE_RE.sub("", text or "")

DEFAULT_SEARCH_ROOTS = [
    SYS_PATH_DIR,
    r".",
    r".",
    DESKTOP_DIR,
]

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".backups", "backups"}

def build_search_roots(
    active_ws: Optional[str] = None,
    extra_roots: Optional[List[str]] = None,
) -> List[str]:
    """สร้างรายการ Search Roots ที่ครอบคลุม active_ws, sys_path_dir, Desktop และ DEFAULT_SEARCH_ROOTS เสมอ"""
    candidates: List[str] = []
    if active_ws:
        candidates.append(str(active_ws))
    if extra_roots:
        candidates.extend(str(r) for r in extra_roots if r)
    candidates.extend([SYS_PATH_DIR, DESKTOP_DIR])
    candidates.extend(DEFAULT_SEARCH_ROOTS)

    ordered_roots: List[str] = []
    seen = set()
    for item in candidates:
        clean = str(item or "").strip().strip('"').strip("'")
        if not clean:
            continue
        expanded = os.path.abspath(os.path.expandvars(clean))
        norm = os.path.normcase(expanded)
        if norm not in seen:
            seen.add(norm)
            ordered_roots.append(expanded)
    return ordered_roots

# ==============================================================================
# 1. Smart Outline & Dependency Snippet (ประหยัด Context Token > 90%)
# ==============================================================================

def _first_doc_line(node: ast.AST) -> str:
    """ดึงเฉพาะบรรทัดแรกของ Docstring จาก AST Node"""
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    first_line = doc.strip().splitlines()[0].strip()
    return f" — *{first_line}*" if first_line else ""

def _format_py_args(args: ast.arguments) -> str:
    """แปลงรายการพารามิเตอร์ของฟังก์ชัน Python เป็นข้อความสั้นกระชับ"""
    names = [a.arg for a in args.posonlyargs + args.args]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return ", ".join(names)

def _outline_python(content: str, file_name: str) -> str:
    """สกัดโครงสร้างไฟล์ .py ด้วยโมดูล ast"""
    try:
        tree = ast.parse(content, filename=file_name)
    except SyntaxError as e:
        return f"### 📄 `{file_name}`\n- ⚠️ **SyntaxError บรรทัดที่ {e.lineno}:** {e.msg}"

    imports: List[str] = []
    classes: List[str] = []
    functions: List[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imp = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                if imp not in imports:
                    imports.append(imp)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imp = f"from {'.' * node.level}{mod} import {names}"
            if imp not in imports:
                imports.append(imp)

    seen_funcs = set()
    seen_classes = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            seen_classes.add(node.name)
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            base_str = f"({', '.join(bases)})" if bases else ""
            cls_header = f"- `class {node.name}{base_str}` (L{node.lineno}){_first_doc_line(node)}"
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen_funcs.add(item.name)
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    arg_str = _format_py_args(item.args)
                    methods.append(
                        f"  - `{prefix} {item.name}({arg_str})` (L{item.lineno}){_first_doc_line(item)}"
                    )
            classes.append("\n".join([cls_header] + methods))

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            seen_funcs.add(node.name)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            arg_str = _format_py_args(node.args)
            functions.append(
                f"- `{prefix} {node.name}({arg_str})` (L{node.lineno}){_first_doc_line(node)}"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name not in seen_classes:
            seen_classes.add(node.name)
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            base_str = f"({', '.join(bases)})" if bases else ""
            classes.append(f"- `class {node.name}{base_str}` (L{node.lineno}, nested){_first_doc_line(node)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in seen_funcs:
            seen_funcs.add(node.name)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            arg_str = _format_py_args(node.args)
            functions.append(f"- `{prefix} {node.name}({arg_str})` (L{node.lineno}, nested){_first_doc_line(node)}")

    total_lines = len(content.splitlines())
    md = [f"### 📄 `{file_name}` ({total_lines} lines, {len(content):,} chars)"]

    if imports:
        md.append("**Imports:**\n" + "\n".join(f"- `{imp}`" for imp in imports[:25]))
    if classes:
        md.append("**Classes & Methods:**\n" + "\n".join(classes))
    if functions:
        md.append("**Functions:**\n" + "\n".join(functions))
    if not (imports or classes or functions):
        md.append("- *(ไม่พบการประกาศ Class หรือ Function หลัก)*")

    return "\n\n".join(md)

def _outline_js_or_html(content: str, file_name: str) -> str:
    """สกัดโครงสร้างไฟล์ .js / .mjs / .html พร้อม API Routes ด้วย Regex (รองรับไฟล์ Minified)"""
    imports: List[str] = []
    routes: List[str] = []
    classes: List[str] = []
    functions: List[str] = []

    for m in re.finditer(
        r"(?:^\s*|\b)(?:import\s+[\s\S]{1,240}?from\s+['\"].*?['\"]|const\s+[A-Za-z0-9_${},\s]+\s*=\s*require\(['\"].*?['\"]\))",
        content,
        re.MULTILINE,
    ):
        stmt = " ".join(m.group(0).strip().split())
        if len(stmt) <= 180 and stmt not in imports:
            imports.append(stmt)

    for m in re.finditer(
        r"(?:pathname\s*===\s*|pathname\.startsWith\(\s*|app\.(?:get|post|put|delete|use)\(\s*)(['\"`]/[A-Za-z0-9_\-/:.*]+['\"`])",
        content,
    ):
        route_str = m.group(1).strip("'\"`")
        if route_str not in routes:
            routes.append(route_str)

    for m in re.finditer(
        r"(?:^\s*|\b)(?:export\s+(?:default\s+)?)?class\s+([A-Za-z0-9_$]+)(?:\s+extends\s+([A-Za-z0-9_$.]+))?",
        content,
        re.MULTILINE,
    ):
        cls_name, base_name = m.group(1), m.group(2)
        ext_str = f" extends {base_name}" if base_name else ""
        entry = f"- `class {cls_name}{ext_str}`"
        if entry not in classes:
            classes.append(entry)

    fn_pattern = re.compile(
        r"(?:^\s*|\b)(export\s+(?:default\s+)?)?(?:(async)\s+)?function\s+([A-Za-z0-9_$]+)\s*\(([^)]{0,120})\)"
        r"|(?:^\s*|\b)(export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:(async)\s*)?\(([^)]{0,120})\)\s*=>",
        re.MULTILINE,
    )
    for m in fn_pattern.finditer(content):
        if m.group(3):
            is_exp, is_async, name, args = bool(m.group(1)), bool(m.group(2)), m.group(3), m.group(4).strip()
        else:
            is_exp, name, is_async, args = bool(m.group(5)), m.group(6), bool(m.group(7)), m.group(8).strip()

        exp_prefix = "export " if is_exp else ""
        fn_prefix = "async function" if is_async else "function"
        clean_args = " ".join(args.split())
        entry = f"- `{exp_prefix}{fn_prefix} {name}({clean_args})`"
        if entry not in functions:
            functions.append(entry)

    total_lines = len(content.splitlines())
    char_count = len(content)
    minified_tag = " • Minified/Large" if char_count > MINIFIED_CHAR_LIMIT and total_lines <= 50 else ""
    md = [f"### 📄 `{file_name}` ({total_lines} lines, {char_count:,} chars{minified_tag})"]

    if imports:
        md.append("**Imports:**\n" + "\n".join(f"- `{imp}`" for imp in imports[:25]))
    if routes:
        md.append("**Detected Routes / Endpoints:**\n" + "\n".join(f"- `{r}`" for r in routes[:30]))
    if classes:
        md.append("**Classes:**\n" + "\n".join(classes[:40]))
    if functions:
        md.append("**Functions:**\n" + "\n".join(functions[:60]))
    if not (imports or routes or classes or functions):
        md.append("- *(ไม่พบการประกาศ Function หรือ Class)*")

    return "\n\n".join(md)

def generate_smart_outline(file_path: str) -> str:
    """
    อ่านไฟล์โค้ดและสกัดเฉพาะโครงสร้างสำคัญ (Outline & Dependencies)
    คืนค่าเป็น Markdown String สำหรับแนบเข้า Prompt ส่งให้ Cloud AI
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        return f"### 📄 `{path_obj.name}`\n- ❌ ไม่พบไฟล์: `{file_path}`"

    try:
        content = path_obj.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return f"### 📄 `{path_obj.name}`\n- ❌ อ่านไฟล์ไม่สำเร็จ: {e}"

    ext = path_obj.suffix.lower()
    if ext == ".py":
        return _outline_python(content, path_obj.name)
    if ext in {".js", ".mjs", ".cjs", ".ts", ".html"}:
        return _outline_js_or_html(content, path_obj.name)

    lines = content.splitlines()
    return f"### 📄 `{path_obj.name}` ({len(lines)} lines, {len(content):,} chars)\n- *(ชนิดไฟล์ `{ext}` ไม่รองรับการสกัด AST/Regex)*"

# ==============================================================================
# 2. Automatic @File Mention Resolver (รองรับ Search Roots + Minified Detection)
# ==============================================================================

FILE_MENTION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])@(?:(full|outline):)?((?:[a-zA-Z]:[\\/])?[^\s@\r\n\t:,;()\[\]{}'\"`]+\.[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

def find_file_in_roots(
    target_name: str,
    search_roots: Optional[List[str]] = None,
    active_ws: Optional[str] = None,
) -> Optional[Path]:
    """ค้นหาไฟล์จากชื่อไฟล์หรือ Relative Path ใน active_ws, sys_path_dir และ Desktop เสมอ"""
    roots = build_search_roots(active_ws=active_ws, extra_roots=search_roots)
    clean_target = str(target_name or "").strip().strip("'\"")
    if not clean_target:
        return None

    direct_path = Path(os.path.expandvars(clean_target))
    if direct_path.is_file():
        return direct_path.resolve()

    for root_str in roots:
        root = Path(root_str)
        if not root.exists():
            continue

        candidate = root / clean_target
        if candidate.is_file():
            return candidate.resolve()

        target_basename = Path(clean_target).name.lower()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fname in filenames:
                if fname.lower() == target_basename:
                    found = Path(dirpath) / fname
                    if clean_target.replace("\\", "/").lower() in str(found).replace("\\", "/").lower():
                        return found.resolve()

    return None

def extract_file_mentions(
    prompt_text: str,
    search_roots: Optional[List[str]] = None,
    active_ws: Optional[str] = None,
    is_sensitive_fn=None,
    for_binary_upload: bool = False,
    full_file_max_lines: int = 300,
    max_char_length: int = MINIFIED_CHAR_LIMIT,
) -> List[Path]:
    """สกัดและค้นหา Path ของไฟล์จริงจากแท็ก @ชื่อไฟล์ พร้อมกรองไฟล์ความลับและข้ามไฟล์ที่ต้องใช้ Outline"""
    cleaned_prompt = _strip_code_fences(prompt_text)
    if not cleaned_prompt or "@" not in cleaned_prompt:
        return []

    roots = build_search_roots(active_ws=active_ws, extra_roots=search_roots)
    found_paths: List[Path] = []
    seen = set()
    chk_sensitive = is_sensitive_fn if callable(is_sensitive_fn) else is_sensitive_path_default

    for m in FILE_MENTION_PATTERN.finditer(cleaned_prompt):
        mode = (m.group(1) or "auto").lower()
        raw_ref = m.group(2)
        resolved = find_file_in_roots(raw_ref, search_roots=roots, active_ws=active_ws)
        if not resolved:
            continue
        if chk_sensitive and chk_sensitive(str(resolved)):
            continue

        # หากใช้สำหรับอัปโหลดไฟล์ขึ้น Web Bridge: ข้ามไฟล์ที่สั่ง @outline: หรือใหญ่เกินเกณฑ์ เพื่อให้ไปใช้ Smart Outline แทน
        if for_binary_upload:
            try:
                content = resolved.read_text(encoding="utf-8-sig", errors="replace")
                if mode == "outline" or (
                    mode == "auto" and (len(content.splitlines()) > full_file_max_lines or len(content) > max_char_length)
                ):
                    continue
            except Exception:
                continue

        if str(resolved) not in seen:
            seen.add(str(resolved))
            found_paths.append(resolved)

    return found_paths

def resolve_prompt_file_mentions(
    prompt_text: str,
    search_roots: Optional[List[str]] = None,
    full_file_max_lines: int = 300,
    max_char_length: int = MINIFIED_CHAR_LIMIT,
    active_ws: Optional[str] = None,
    is_sensitive_fn=None,
) -> str:
    """
    ตรวจจับแท็ก @ชื่อไฟล์ ใน Prompt แล้วแนบโค้ดจริงหรือ Smart Outline ต่อท้ายอัตโนมัติ:
    - @ชื่อไฟล์.py          -> หากไม่เกิน 300 บรรทัด และ len(content) <= 15000 แนบโค้ดเต็มไฟล์, หากเกิน แนบ Smart Outline
    - @full:ชื่อไฟล์.py     -> บังคับแนบโค้ดเต็มไฟล์เสมอ
    - @outline:ชื่อไฟล์.py  -> บังคับแนบเฉพาะ Smart Outline (ประหยัด Token 90%)
    """
    if not prompt_text or "@" not in prompt_text:
        return prompt_text

    cleaned_for_match = _strip_code_fences(prompt_text)
    matches = list(FILE_MENTION_PATTERN.finditer(cleaned_for_match))
    if not matches:
        return prompt_text

    roots = build_search_roots(active_ws=active_ws, extra_roots=search_roots)
    attachments: List[str] = []
    seen_files = set()
    chk_sensitive = is_sensitive_fn if callable(is_sensitive_fn) else is_sensitive_path_default

    for m in matches:
        mode = (m.group(1) or "auto").lower()
        raw_ref = m.group(2)
        resolved = find_file_in_roots(raw_ref, search_roots=roots, active_ws=active_ws)

        if not resolved:
            attachments.append(f"### ⚠️ ไม่พบไฟล์สำหรับ `@{raw_ref}` ในระบบ")
            continue

        if chk_sensitive and chk_sensitive(str(resolved)):
            attachments.append(f"### 🔒 Security Block: ไม่อนุญาตให้อ่านหรือแนบไฟล์ความลับ `{resolved.name}` เพื่อความปลอดภัย")
            continue

        key = (str(resolved), mode)
        if key in seen_files:
            continue
        seen_files.add(key)

        try:
            content = resolved.read_text(encoding="utf-8-sig", errors="replace")
            line_count = len(content.splitlines())
            char_count = len(content)
        except Exception as e:
            attachments.append(f"### ❌ อ่านไฟล์ `{resolved}` ไม่สำเร็จ: {e}")
            continue

        is_minified_or_large = char_count > max_char_length
        use_outline = (
            mode == "outline"
            or (mode == "auto" and (line_count > full_file_max_lines or is_minified_or_large))
        )

        if use_outline:
            outline_md = generate_smart_outline(str(resolved))
            reason_note = (
                f"ความยาว {char_count:,} ตัวอักษร (> {max_char_length:,} chars / Minified)"
                if is_minified_or_large
                else f"ขนาด {line_count} บรรทัด (> {full_file_max_lines} lines)"
            )
            attachments.append(
                f"#### 📌 โครงสร้างย่อ (Smart Outline) ของ `{resolved}`\n"
                f"*(ไฟล์มี{reason_note} ระบบจึงสกัด Outline อัตโนมัติ หากต้องการโค้ดเต็มให้พิมพ์ `@full:{raw_ref}`)*\n\n"
                f"{outline_md}"
            )
        else:
            ext = resolved.suffix.lstrip(".").lower() or "text"
            try:
                from code_tools_hardening import mask_secrets as hardened_mask
                safe_content = hardened_mask(content)
            except Exception:
                safe_content = content
            attachments.append(
                f"#### 📄 เนื้อหาไฟล์ฉบับเต็ม: `{resolved}` ({line_count} lines, {char_count:,} chars)\n"
                f"```{ext}\n{safe_content}\n```"
            )

    if not attachments:
        return prompt_text

    appendix = "\n\n---\n### 📎 ไฟล์อ้างอิงที่แนบอัตโนมัติจากเครื่อง (Local File Context)\n\n" + "\n\n".join(attachments)
    return prompt_text + appendix

# ==============================================================================
# 3. Pre-flight Syntax Validation (รองรับ .py, .json, .js, .mjs, .cjs)
# ==============================================================================

def _find_node_cmd() -> Optional[str]:
    """ค้นหาคำสั่ง node บนเครื่อง"""
    candidates = [
        "node",
        r"node",
        r"C:\Program Files\nodejs\node.exe",
    ]
    for c in candidates:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return None

def validate_code_syntax(file_path: str, new_content: str) -> Tuple[bool, str]:
    """
    ตรวจสอบความถูกต้องของไวยากรณ์ (Syntax) ก่อนบันทึกทับไฟล์จริง
    รองรับ .py (ast.parse), .json (json.loads), และ .js/.mjs/.cjs (node --check)
    คืนค่า: (True, "OK") หรือ (False, "บรรทัดที่ X: รายละเอียด Error")
    """
    if new_content is None or not str(new_content).strip():
        return False, "บรรทัดที่ 1: เนื้อหาโค้ดว่างเปล่า"

    ext = os.path.splitext(str(file_path))[1].lower()

    if ext == ".py":
        try:
            ast.parse(new_content, filename=str(file_path))
            return True, "OK"
        except SyntaxError as e:
            lineno = e.lineno or 1
            err_line = f" -> `{e.text.strip()}`" if e.text and e.text.strip() else ""
            return False, f"บรรทัดที่ {lineno}: Python SyntaxError ({e.msg}){err_line}"
        except ValueError as e:
            return False, f"บรรทัดที่ 1: ValueError ({e})"

    if ext == ".json":
        try:
            json.loads(new_content)
            return True, "OK"
        except json.JSONDecodeError as e:
            return False, f"บรรทัดที่ {e.lineno} (คอลัมน์ {e.colno}): JSONDecodeError ({e.msg})"

    if ext in {".js", ".mjs", ".cjs"}:
        node_bin = _find_node_cmd()
        if not node_bin:
            return True, "OK (Skipped node --check: Node.js not found)"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=ext, encoding="utf-8", delete=False
            ) as tmp_file:
                tmp_path = tmp_file.name
                tmp_file.write(new_content)

            proc = subprocess.run(
                [node_bin, "--check", tmp_path],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or proc.stdout or "").strip()
                line_match = re.search(r":(\d+)\r?\n", stderr)
                lineno = line_match.group(1) if line_match else "?"
                short_err = " | ".join(line.strip() for line in stderr.splitlines()[-3:] if line.strip())
                return False, f"บรรทัดที่ {lineno}: JS SyntaxError ({short_err})"
            return True, "OK"
        except Exception as e:
            return False, f"JS Validation Error: {e}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return True, "OK"

# ==============================================================================
# 4. Safe Atomic Write + Auto-Backup + Snippet Overwrite Guard
# ==============================================================================

def safe_write_code_file(
    file_path: str,
    new_content: str,
    allow_shrink: bool = False,
    shrink_threshold_ratio: float = 0.45,
    backup_dir: Optional[str] = None,
    validate_syntax: bool = True,
    allow_snippet: Optional[bool] = None,
) -> Dict[str, object]:
    """
    บันทึกโค้ดทับไฟล์จริงอย่างปลอดภัย:
    1. ตรวจ Pre-flight Syntax Validation ก่อนเขียน
    2. ป้องกันการนำโค้ดบางส่วน (Snippet) ไปเขียนทับไฟล์เต็มโดยไม่ตั้งใจ (Snippet Guard)
    3. สำรองไฟล์เดิมไว้ในโฟลเดอร์ backup_dir (หรือ .backups/) อัตโนมัติ
    4. เขียนลงไฟล์ชั่วคราวแล้วสลับชื่อแบบ Atomic (os.replace)
    """
    target = Path(file_path).resolve()
    permit_shrink = bool(allow_snippet) if allow_snippet is not None else bool(allow_shrink)

    validation_msg = "OK (Skipped)"
    if validate_syntax:
        is_valid, validation_msg = validate_code_syntax(str(target), new_content)
        if not is_valid:
            return {
                "ok": False,
                "stage": "syntax_validation",
                "error": f"ระงับการบันทึกไฟล์เนื่องจากไวยากรณ์ผิดพลาด — {validation_msg}",
                "backup_path": None,
            }

    new_lines = len((new_content or "").splitlines())
    new_chars = len(new_content or "")
    backup_path_str = None

    if target.is_file():
        old_content = target.read_text(encoding="utf-8-sig", errors="replace")
        old_lines = len(old_content.splitlines())
        old_chars = len(old_content)

        line_shrunk = old_lines >= 50 and new_lines < int(old_lines * shrink_threshold_ratio)
        char_shrunk = old_chars >= 2500 and new_chars < int(old_chars * shrink_threshold_ratio)
        has_snippet_placeholder = bool(
            re.search(
                r"(?://|#|\*)\s*\.\.\.\s*(?:existing|rest of|other|unchanged|โค้ดส่วนที่เหลือ|โค้ดเดิม)",
                new_content or "",
                re.IGNORECASE,
            )
        )

        if not permit_shrink and (line_shrunk or char_shrunk or has_snippet_placeholder):
            reason = (
                "ตรวจพบคอมเมนต์ละโค้ดเดิม (Snippet Placeholder)"
                if has_snippet_placeholder
                else f"โค้ดใหม่มีเพียง {new_lines} บรรทัด / {new_chars:,} ตัวอักษร (จากเดิม {old_lines} บรรทัด / {old_chars:,} ตัวอักษร)"
            )
            return {
                "ok": False,
                "stage": "snippet_guard",
                "error": (
                    f"ระงับการบันทึก (Snippet Guard): {reason} "
                    f"คาดว่าเป็นโค้ดเฉพาะบางส่วน (Snippet) ไม่ใช่ไฟล์เต็ม"
                ),
                "backup_path": None,
            }

        target_backup_dir = Path(backup_dir).resolve() if backup_dir else (target.parent / ".backups")
        target_backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = target_backup_dir / f"{target.name}.{ts}_{uuid.uuid4().hex[:4]}.bak"
        shutil.copy2(target, backup_file)
        backup_path_str = str(backup_file)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target.with_suffix(f"{target.suffix}.{os.getpid()}.{uuid.uuid4().hex[:4]}.tmp")
    try:
        tmp_file.write_text(new_content, encoding="utf-8", newline="\n")
        os.replace(tmp_file, target)
    except Exception as e:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)
        return {"ok": False, "stage": "atomic_write", "error": str(e), "backup_path": backup_path_str}

    return {
        "ok": True,
        "file_path": str(target),
        "lines_written": new_lines,
        "backup_path": backup_path_str,
        "validation": validation_msg,
    }


# ==============================================================================
# 5. Smart Patching Engine (3-Tier Resolution Pipeline ป้องกัน Line-drift)
# ==============================================================================

def locate_smart_patch_span(
    file_path: str,
    search_block: str,
    symbol_hint: str = "",
    near_line: int = 0,
    min_fuzzy_ratio: float = 0.84,
) -> Dict[str, object]:
    """
    ค้นหาช่วงบรรทัด (1-indexed) ที่แม่นยำสำหรับ Patching โดยไม่ต้องพึ่งพาเลขบรรทัดเป๊ะ (แก้ปัญหา Line-drift):
    Tier 1: Exact Match -> Tier 2: Dedented/Whitespace Match -> Tier 3: AST Symbol-Scoped Fuzzy Match
    """
    target = Path(file_path)
    if not target.is_file():
        return {"ok": False, "error": f"ไม่พบไฟล์: {file_path}"}
    if not search_block or not search_block.strip():
        return {"ok": False, "error": "search_block ต้องไม่ว่างเปล่า"}

    raw_text = target.read_text(encoding="utf-8-sig", errors="replace")
    lines = raw_text.splitlines(keepends=True)
    total_lines = len(lines)
    norm_search = search_block.replace("\r\n", "\n")

    # Tier 1: Exact Match (เลือกจุดที่ใกล้ near_line ที่สุดหากมีซ้ำ)
    exact_hits = []
    pos = 0
    raw_lf = raw_text.replace("\r\n", "\n")
    while True:
        idx = raw_lf.find(norm_search, pos)
        if idx == -1:
            break
        s_line = raw_lf[:idx].count("\n") + 1
        e_line = s_line + norm_search.rstrip("\n").count("\n")
        exact_hits.append((s_line, e_line))
        pos = idx + max(1, len(norm_search))

    if exact_hits:
        best = min(exact_hits, key=lambda span: abs(span[0] - near_line)) if near_line > 0 else exact_hits[0]
        if len(exact_hits) > 1 and near_line <= 0:
            return {"ok": False, "error": f"พบ search_block ซ้ำกัน {len(exact_hits)} จุด กรุณาระบุ near_line หรือ symbol_hint"}
        return {"ok": True, "tier": "exact", "confidence": 1.0, "startLine": best[0], "endLine": best[1]}

    # จำกัดขอบเขตการค้นหาด้วย AST Symbol (ถ้ามี symbol_hint และเป็น .py)
    win_start, win_end = 1, total_lines
    if symbol_hint and target.suffix.lower() == ".py":
        try:
            tree = ast.parse(raw_text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol_hint.strip():
                    win_start = getattr(node, "lineno", 1)
                    win_end = getattr(node, "end_lineno", total_lines)
                    break
        except SyntaxError:
            pass

    # Tier 2 & 3: Sliding Window (Dedented Match + Fuzzy SequenceMatcher)
    dedented_target = textwrap.dedent(norm_search).strip()
    search_len = max(1, len(norm_search.rstrip("\n").splitlines()))
    scored_windows = []

    for offset in (-2, -1, 0, 1, 2):
        w_len = max(1, search_len + offset)
        for s_idx in range(max(0, win_start - 1), min(total_lines, win_end) - w_len + 1):
            slice_str = "".join(lines[s_idx : s_idx + w_len])
            dedented_slice = textwrap.dedent(slice_str.replace("\r\n", "\n")).strip()
            if dedented_slice == dedented_target:
                return {
                    "ok": True,
                    "tier": "dedent_exact",
                    "confidence": 0.98,
                    "startLine": s_idx + 1,
                    "endLine": s_idx + w_len,
                }
            ratio = difflib.SequenceMatcher(None, dedented_target, dedented_slice).ratio()
            if ratio >= min_fuzzy_ratio:
                scored_windows.append((ratio, s_idx + 1, s_idx + w_len))

    if scored_windows:
        scored_windows.sort(key=lambda x: (-x[0], abs(x[1] - near_line) if near_line > 0 else 0))
        best_ratio, b_start, b_end = scored_windows[0]
        # ตรวจสอบความไม่กำกวมเทียบกับหน้าต่างที่ไม่ได้ซ้อนทับกัน
        non_overlapping = [w for w in scored_windows[1:] if w[2] < b_start or w[1] > b_end]
        if non_overlapping and (best_ratio - non_overlapping[0][0]) < 0.08:
            return {"ok": False, "error": f"Fuzzy match กำกวม ({best_ratio:.2f} vs {non_overlapping[0][0]:.2f}) กรุณาเพิ่มบริบทใน search_block"}
        return {
            "ok": True,
            "tier": "fuzzy_semantic",
            "confidence": round(best_ratio, 3),
            "startLine": b_start,
            "endLine": b_end,
        }

    return {"ok": False, "error": "ไม่พบตำแหน่งโค้ดที่ตรงกับ search_block (ทั้งแบบ Exact, Dedent และ Fuzzy)"}
