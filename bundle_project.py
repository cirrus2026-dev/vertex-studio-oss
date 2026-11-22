#!/usr/bin/env python3
"""
Vertex Studio OSS - Smart Code Bundler (CLI Tool)
=================================================
สคริปต์สำหรับสกัดโค้ดทั้งโปรเจกต์เข้าไฟล์ Plain Text (.txt) ก้อนเดียว
ช่วยลด Token Overhead ลง 40-70% พร้อม Header ชัดเจน และมี 3 โหมดอัจฉริยะ:
  1. --audit   : โหมดหาบั๊ก/ตรวจความปลอดภัย (ส่งโค้ดเต็ม 100% บีบเฉพาะช่องว่างซ้ำซ้อน)
  2. --feature : โหมดคิดฟีเจอร์/วางระบบ (สกัดเฉพาะโครงร่าง AST Outline ประหยัด 70-80%)
  3. --diff    : โหมดส่งเฉพาะสิ่งที่แก้ไขล่าสุด (Git Diff)

วิธีใช้งาน:
  python bundle_project.py --dir . --out my_bundle.txt --mode audit
  python bundle_project.py -f file1.py file2.js --mode feature
"""

import os
import sys
import argparse
import ast
import re
import subprocess

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", "argon_history", "carbon_history"
}

IGNORE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pyc", ".pyo", ".map", ".lock", "package-lock.json"
}

SENSITIVE_PATTERNS = [
    r"^\.env.*", r"^\.bridge_token.*", r".*\.key$", r".*\.pem$",
    r"^sessions_store\.json$", r"^id_rsa.*", r"^.*_bundle\.txt$"
]

def is_sensitive(filename):
    fname = os.path.basename(filename).lower()
    for pat in SENSITIVE_PATTERNS:
        if re.match(pat, fname):
            return True
    return False

def clean_whitespace(content):
    """ตัดช่องว่างท้ายบรรทัดและบีบบรรทัดว่างที่ติดกันเกิน 1 บรรทัด"""
    lines = [line.rstrip() for line in content.splitlines()]
    result = []
    prev_empty = False
    for line in lines:
        if not line:
            if not prev_empty:
                result.append("")
                prev_empty = True
        else:
            result.append(line)
            prev_empty = False
    return "\n".join(result)

def extract_outline(content, ext):
    """สกัดเฉพาะโครงร่าง (AST / Class / Function definitions)"""
    if ext == ".py":
        try:
            tree = ast.parse(content)
            lines = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    lines.append(f"\nclass {node.name}:")
                    doc = ast.get_docstring(node)
                    if doc:
                        lines.append(f'    """{doc}"""')
                    for sub in node.body:
                        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                            args = [a.arg for a in sub.args.args]
                            lines.append(f"    {prefix} {sub.name}({', '.join(args)}): ...")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    args = [a.arg for a in node.args.args]
                    lines.append(f"\n{prefix} {node.name}({', '.join(args)}): ...")
                    doc = ast.get_docstring(node)
                    if doc:
                        lines.append(f'    """{doc}"""')
            if lines:
                return "\n".join(lines)
        except Exception:
            pass
    # Fallback สำหรับ JS/TS/ภาษาอื่นๆ
    patterns = re.findall(r'^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var|def)\s+[\w\d_]+[^\n{:]*', content, re.MULTILINE)
    if patterns:
        return "\n".join(patterns)
    return clean_whitespace(content[:1500]) + "\n# ... [Content truncated for outline mode]"

def get_git_diff(target_dir):
    """ดึง git diff ล่าสุดถ้ามี git"""
    try:
        res = subprocess.run(["git", "diff", "HEAD~1"], cwd=target_dir, capture_output=True, text=True, check=True)
        if res.stdout.strip():
            return res.stdout
        res2 = subprocess.run(["git", "diff"], cwd=target_dir, capture_output=True, text=True, check=True)
        return res2.stdout
    except Exception as e:
        return f"# [Git Diff Unavailable: {e}]"

def collect_files(target_dir, max_files=100, out_path=None):
    collected = []
    abs_out = os.path.abspath(out_path) if out_path else None
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IGNORE_EXTENSIONS or f in IGNORE_EXTENSIONS:
                continue
            if is_sensitive(f):
                continue
            fpath = os.path.join(root, f)
            if abs_out and os.path.abspath(fpath) == abs_out:
                continue
            try:
                if os.path.getsize(fpath) > 1024 * 500: # ข้ามไฟล์ใหญ่เกิน 500 KB
                    continue
            except OSError:
                continue
            collected.append(fpath)
            if len(collected) >= max_files:
                break
        if len(collected) >= max_files:
            break
    return collected

def bundle(files, out_path, mode="audit", base_dir=None):
    total_chars = 0
    file_count = 0

    with open(out_path, "w", encoding="utf-8") as out:
        out.write(f"# ====================================================================\n")
        out.write(f"# VERTEX STUDIO OSS - CODE BUNDLE\n")
        out.write(f"# MODE: {mode.upper()} (audit = 100% full code, feature = outline, diff = changes)\n")
        out.write(f"# ====================================================================\n\n")

        if mode == "diff":
            diff_text = get_git_diff(base_dir or os.getcwd())
            out.write(diff_text)
            total_chars = len(diff_text)
            file_count = 1
        else:
            for fpath in files:
                rel_path = os.path.relpath(fpath, base_dir) if base_dir else os.path.basename(fpath)
                ext = os.path.splitext(fpath)[1].lower()
                out.write(f"# ==================== FILE: {rel_path} ====================\n")
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as inf:
                        raw = inf.read()
                    if mode == "feature" and ext in [".py", ".js", ".ts"]:
                        content = extract_outline(raw, ext)
                    else:
                        content = clean_whitespace(raw)
                    out.write(content)
                    out.write("\n\n")
                    total_chars += len(content)
                    file_count += 1
                except Exception as e:
                    out.write(f"# [Error reading {rel_path}: {e}]\n\n")

    print(f"✅ มัดรวมสำเร็จ: {out_path}")
    print(f"📊 สรุป: {file_count} ไฟล์ | {total_chars:,} ตัวอักษร | โหมด: {mode.upper()}")
    return out_path

def main():
    parser = argparse.ArgumentParser(description="Vertex Studio OSS - Smart Code Bundler")
    parser.add_argument("--dir", "-d", default=None, help="โฟลเดอร์โปรเจกต์ที่ต้องการสแกนมัดรวม")
    parser.add_argument("--files", "-f", nargs="*", default=[], help="ระบุไฟล์เฉพาะที่ต้องการรวม")
    parser.add_argument("--out", "-o", default="project_bundle.txt", help="ชื่อไฟล์ปลายทาง (.txt)")
    parser.add_argument("--mode", "-m", choices=["audit", "feature", "diff"], default="audit",
                        help="โหมดการสกัดโค้ด: audit (ตรวจบั๊ก ละเอียดเต็ม 100%), feature (คิดฟีเจอร์ โครงร่าง AST), diff (สิ่งที่เพิ่งแก้)")

    args = parser.parse_args()

    target_files = []
    base_dir = None
    out_file = os.path.abspath(args.out)

    if args.files:
        target_files = [os.path.abspath(f) for f in args.files if os.path.isfile(f) and os.path.abspath(f) != out_file and not is_sensitive(f)]
        try:
            base_dir = os.path.commonpath(target_files) if target_files else os.getcwd()
        except ValueError:
            base_dir = os.getcwd()
    elif args.dir:
        base_dir = os.path.abspath(args.dir)
        target_files = collect_files(base_dir, out_path=out_file)
    else:
        base_dir = os.getcwd()
        target_files = collect_files(base_dir, out_path=out_file)

    if not target_files and args.mode != "diff":
        print("❌ ไม่พบไฟล์โค้ดที่ต้องการมัดรวม กรุณาระบุ --dir หรือ --files", file=sys.stderr)
        sys.exit(1)

    bundle(target_files, os.path.abspath(args.out), mode=args.mode, base_dir=base_dir)

if __name__ == "__main__":
    main()
