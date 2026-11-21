import json
import urllib.request
import os
import sys
import argparse
import tempfile
import re
import ast

BRIDGE_URL = "http://127.0.0.1:8765/ask_carbon"

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
    # บีบเว้นบรรทัดซ้อนเกิน 1 บรรทัด และตัด trailing spaces
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
    # Fallback เป็น regex สำหรับภาษาทั่วไป
    patterns = re.findall(r'^(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var)\s+[\w\d_]+[^\n{:]*', code, re.MULTILINE)
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
    return "audit" # ค่าตั้งต้นสำหรับงานโค้ดทั่วไป

def bundle_files_to_txt(files, prompt="", mode="auto", bundle_name="carbon_project_bundle.txt"):
    if mode == "auto":
        mode = detect_intent(prompt)
        print(f"🎯 Auto-Detected Task Intent: [{mode.upper()} MODE]")

    temp_dir = os.path.join(tempfile.gettempdir(), "carbon_bundles")

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

def dispatch_carbon(prompt, files=None, out_file=None, bundle_mode=True, mode="auto"):
    resolved_files = []
    if files:
        for f in files:
            f_abs = os.path.abspath(f)
            if os.path.isfile(f_abs):
                resolved_files.append(f_abs)
            else:
                print(f"⚠️ ไม่พบไฟล์: {f}", file=sys.stderr)

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

    payload = {
        "prompt": final_prompt,
        "files": final_files,
    }

    token = get_bridge_token()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
    }
    if token:
        headers["X-Bridge-Token"] = token

    req = urllib.request.Request(
        BRIDGE_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    print(f"🚀 กำลังส่งคำสั่งตรงไปยัง Gemini 4 Carbon ผ่าน Persistent Bridge (Port 8765)...")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print("⚡ คำสั่งถูกส่งเรียบร้อยแล้ว! Gemini 4 Carbon กำลังคิดและประมวลผลบนแท็บ Studio")
                return True
            else:
                print(f"❌ Bridge ส่งข้อผิดพลาดกลับมา: {data.get('error')}", file=sys.stderr)
                return False
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8', errors='replace')
        print(f"❌ Bridge HTTP Error {e.code}: {err_msg}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"❌ ไม่สามารถเชื่อมต่อ Bridge บนพอร์ต 8765: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Ask Gemini 4 Carbon via Persistent Bridge Pipeline with Smart Bundling")
    parser.add_argument("--prompt", "-p", required=True, help="ข้อความคำสั่งที่ต้องการส่งให้ Carbon")
    parser.add_argument("--files", "-f", nargs="*", default=[], help="รายชื่อไฟล์โค้ดที่ต้องการแนบ")
    parser.add_argument("--out", "-o", default=None, help="ไฟล์ปลายทางสำหรับบันทึกผลลัพธ์")
    parser.add_argument("--mode", "-m", choices=["auto", "audit", "feature", "diff"], default="auto", help="โหมดการสกัดโค้ด (auto, audit, feature, diff)")
    parser.add_argument("--no-bundle", action="store_true", help="ไม่รวมไฟล์เป็น .txt ก้อนเดียว")

    args = parser.parse_args()
    success = dispatch_carbon(
        prompt=args.prompt,
        files=args.files,
        out_file=args.out,
        bundle_mode=not args.no_bundle,
        mode=args.mode
    )
    if not success:
        sys.exit(1)

if __name__ == '__main__':
    main()
