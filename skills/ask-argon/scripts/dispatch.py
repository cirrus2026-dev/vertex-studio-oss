import json
import urllib.request
import os
import sys
import argparse
import tempfile
import re
import ast

def get_bridge_token():
    env_tok = os.environ.get("BRIDGE_TOKEN")
    if env_tok:
        return env_tok
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".bridge_token_8765.json"),
        os.path.join(os.path.expanduser("~"), ".gemini", "vertex-dashboard", ".bridge_token_8765.json"),
        os.path.join(os.getcwd(), ".bridge_token_8765.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tok = data.get("token", "")
                    if tok:
                        return tok
            except Exception:
                pass
    return ""

def clean_whitespace(content):
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

def extract_python_outline(code):
    try:
        tree = ast.parse(code)
        outline_lines = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                outline_lines.append(f"\nclass {node.name}:")
                doc = ast.get_docstring(node)
                if doc:
                    outline_lines.append(f'    """{doc}"""')
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                        args = [a.arg for a in sub.args.args]
                        outline_lines.append(f"    {prefix} {sub.name}({', '.join(args)}): ...")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                args = [a.arg for a in node.args.args]
                outline_lines.append(f"\n{prefix} {node.name}({', '.join(args)}): ...")
                doc = ast.get_docstring(node)
                if doc:
                    outline_lines.append(f'    """{doc}"""')
        if outline_lines:
            return "\n".join(outline_lines)
    except Exception:
        pass
    patterns = re.findall(r'^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var|def)\s+[\w\d_]+[^\n{:]*', code, re.MULTILINE)
    if patterns:
        return "\n".join(patterns)
    return clean_whitespace(code[:1500]) + "\n# ... [Content truncated for outline mode]"

def detect_intent(prompt):
    p_lower = prompt.lower()
    if any(k in p_lower for k in ["บั๊ก", "bug", "audit", "security", "ช่องโหว่", "รั่ว", "crash", "error", "แก้บั๊ก"]):
        return "audit"
    if any(k in p_lower for k in ["ฟีเจอร์", "feature", "ออกแบบ", "design", "สถาปัตยกรรม", "architecture", "เพิ่มระบบ", "วางโครงสร้าง", "ไอเดีย", "idea"]):
        return "feature"
    if any(k in p_lower for k in ["diff", "เพิ่งแก้", "จุดเดิม"]):
        return "diff"
    return "audit"

def bundle_files_to_txt(files, prompt="", mode="auto", bundle_name="argon_project_bundle.txt"):
    if mode == "auto":
        mode = detect_intent(prompt)
        print(f"🎯 Auto-Detected Task Intent: [{mode.upper()} MODE]")

    temp_dir = os.path.join(tempfile.gettempdir(), "argon_bundles")

    os.makedirs(temp_dir, exist_ok=True)
    bundle_path = os.path.join(temp_dir, bundle_name)

    total_chars = 0
    with open(bundle_path, "w", encoding="utf-8") as out:
        out.write(f"# === BUNDLE MODE: {mode.upper()} | FILES: {len(files)} ===\n\n")
        for fpath in files:
            fname = os.path.basename(fpath)
            out.write(f"# ==================== FILE: {fname} ====================\n")
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as inf:
                    raw_content = inf.read()

                if mode == "feature" and fpath.endswith((".py", ".js", ".ts")):
                    content = extract_python_outline(raw_content)
                else:
                    content = clean_whitespace(raw_content)

                out.write(content)
                total_chars += len(content)
            except Exception as e:
                out.write(f"# [Error reading file {fname}: {e}]\n")
            out.write("\n\n")

    print(f"📦 Auto-Bundled {len(files)} files into: {bundle_path} ({total_chars} chars, mode: {mode})")
    return bundle_path

def dispatch(prompt, files=None, thinking_level="HIGH", out_file=None, session_id="argon_skill_task", bundle_mode=True, mode="auto"):
    resolved_files = []
    if files:
        for f in files:
            f_abs = os.path.abspath(f)
            if os.path.isfile(f_abs):
                resolved_files.append(f_abs)
            else:
                print(f"⚠️ ไม่พบไฟล์แนบ: {f}", file=sys.stderr)

    final_files = []
    if resolved_files:
        if len(resolved_files) == 1 and not bundle_mode:
            final_files = resolved_files
        else:
            final_files = [bundle_files_to_txt(resolved_files, prompt=prompt, mode=mode)]

    CODE_FORMAT_GUARDRAIL = (
        "\n\n[กฎการจัดรูปแบบโค้ด — ห้ามฝ่าฝืน]:\n"
        "1. ทุกโค้ดบล็อกและทุก Search & Replace Block (<<<<<<< SEARCH ... ======= ... >>>>>>>) "
        "ต้องครอบด้วย Fenced Code Block (เช่น ````javascript ... ````) เสมอ ห้ามวาง <<<<<<< SEARCH, ======= "
        "หรือโค้ดเปลือยๆ นอก Code Block เด็ดขาด\n"
        "2. ห้ามปิด-เปิด Code Block (```) แทรกกลาง Template Literal (เช่น innerHTML = `...`) เด็ดขาด "
        "ให้ส่งโค้ดทั้งก้อนต่อเนื่องใน Code Block เดียว"
    )
    final_prompt = prompt
    if final_files or any(
        k in prompt.lower()
        for k in ["โค้ด", "code", "search", "replace", "ไฟล์", ".js", ".mjs", ".py", ".html"]
    ):
        final_prompt = prompt + CODE_FORMAT_GUARDRAIL

    # ยิงผ่าน Persistent Bridge พอร์ต 8765 ที่เปิดท่อค้างไว้อยู่แล้ว (ไม่เด้งถามสิทธิ์ Chrome ซ้ำ 100%)
    token = get_bridge_token()
    bridge_payload = {
        "prompt": final_prompt,
        "files": final_files,
        "fireAndForget": True
    }
    bridge_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765"
    }
    if token:
        bridge_headers["X-Bridge-Token"] = token

    print(f"🚀 กำลังส่งคำสั่งและไฟล์ผ่านท่อ Persistent Bridge พอร์ต 8765 ที่เปิดค้างไว้...")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8765/ask",
            data=json.dumps(bridge_payload, ensure_ascii=False).encode("utf-8"),
            headers=bridge_headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print("⚡ ส่งงานผ่านท่อสะพานสำเร็จเรียบร้อยแล้ว! (ไม่เด้งถามสิทธิ์ซ้ำ)")
                return True
            else:
                print(f"⚠️ Bridge รายงานข้อผิดพลาด: {data.get('error')}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"❌ ไม่สามารถเชื่อมต่อท่อสะพาน 8765: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Dispatch tasks to Gemini 4 Argon Web via Persistent Bridge 8765")
    parser.add_argument("--prompt", "-p", default="", help="ข้อความคำสั่งที่ต้องการสั่ง Argon")
    parser.add_argument("--quota", "-q", action="store_true", help="แสดงข้อมูลโควต้าล่าสุดของอากอน")
    parser.add_argument("--files", "-f", nargs="*", default=[], help="รายชื่อไฟล์โค้ดที่ต้องการแนบ")
    parser.add_argument("--bundle", "-b", action="store_true", default=True, help="รวมไฟล์เป็น .txt ก้อนเดียว")
    parser.add_argument("--thinking", "-t", default="HIGH", choices=["HIGH", "MEDIUM", "LOW"], help="ระดับการคิด")
    parser.add_argument("--out", "-o", default=None, help="ไฟล์ปลายทางสำหรับบันทึกคำตอบ")
    parser.add_argument("--session", "-s", default="argon_skill_task", help="Session ID")
    parser.add_argument("--mode", "-m", choices=["auto", "audit", "feature", "diff"], default="auto", help="โหมดการสกัดโค้ด")

    args = parser.parse_args()

    if args.quota:
        quota_candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "vertex-dashboard", "argon_history", "quota.json"),
            os.path.join(os.path.expanduser("~"), ".gemini", "vertex-dashboard", "argon_history", "quota.json"),
        ]
        for qp in quota_candidates:
            if os.path.isfile(qp):
                try:
                    with open(qp, "r", encoding="utf-8") as f:
                        qdata = json.load(f)
                        print(qdata.get("header", json.dumps(qdata, ensure_ascii=False)))
                        return
                except Exception:
                    pass
        print("ยังไม่มีข้อมูลโควต้าในแคช")
        return

    if not args.prompt:
        parser.error("ต้องระบุ --prompt / -p เมื่อไม่ได้ใช้คำสั่ง --quota")

    success = dispatch(
        prompt=args.prompt,
        files=args.files,
        thinking_level=args.thinking,
        out_file=args.out,
        session_id=args.session,
        bundle_mode=args.bundle,
        mode=args.mode
    )
    if not success:
        sys.exit(1)

if __name__ == '__main__':
    main()
