# 📋 บันทึกประวัติการอัปเดตระบบ (CHANGELOG)
**โปรเจกต์:** Vertex AI Cloud Studio & Gemini Web Bridge (Dual-Mode Edition)  
**ใบอนุญาต:** MIT License  

---

## 🚀 เวอร์ชัน 4.0.0 (22 พฤศจิกายน 2026) - Native MCP Server, Atomic Job Tickets & Headless Task Service
- **Native MCP Server (`bridge_mcp_server.py`):**
  - เปิดตัว Local Model Context Protocol (MCP) Server ผ่าน `stdio` สำหรับ Antigravity, Cursor และ Claude Desktop
  - รองรับ 5 เครื่องมือมาตรฐาน: `ask_argon`, `ask_carbon`, `check_job`, `bridge_status` และ `extract_latest`
  - ระบบบายพาส Windows System Proxy อัตโนมัติ (`ProxyHandler({})`) เพื่อการเชื่อมต่อ `127.0.0.1:8765` ที่เสถียร 100%
- **Atomic Write & Job Ticket System (`latest.status.json`):**
  - เพิ่มฟังก์ชัน `_atomic_write_file()` ด้วยการสร้างไฟล์ชั่วคราวแบบ UUID + `os.fsync()` + Windows `PermissionError` 5-attempt retry ป้องกันไฟล์ขาดหรือล็อกชนกัน
  - เพิ่มระบบ Job Ticket (`generating` / `done` / `error` / `timeout` / `quota_limit`) เพื่อให้ Agent เช็กสถานะได้แม่นยำ ป้องกันการอ่านคำตอบเก่าซ้ำ
  - เพิ่ม Long-Polling API Endpoint: `GET /jobs/{job_id}?wait=30&includeAnswer=1`
- **Fast Fire-and-Forget & Code Format Guardrails:**
  - ปรับจูนความเร็วการส่งงานหน้าเว็บ (`ask_carbon` / `ask_argon`) เป็นโหมด Fast Fire-and-Forget ภายใน 2 วินาที โดยตัด loop หน่วงเวลาที่ไม่จำเป็นออกทั้งหมด
  - เพิ่ม **Dynamic Markdown Code Fence** (4–5 backticks) และ Code Format Guardrail ในสคริปต์ส่งงาน ป้องกันโค้ดและบล็อก Search & Replace (`<<<<<<< SEARCH / ======= / >>>>>>>`) แตกหักเมื่อมี nested backticks
- **Dedicated AI Chrome Launcher (`start_ai_bridge.bat`) & Background Task Support:**
  - เพิ่มสคริปต์เปิดตัว Chrome โปรไฟล์แยก (`AI-Chrome-Profile`) บนพอร์ต 9222 พร้อมเปิด Bridge Server พอร์ต 8765 เงียบๆ เบื้องหลัง หลีกเลี่ยงปัญหาป๊อปอัป Allow Connections ซ้ำซ้อน
  - อัปเกรดคู่มือสกิล `ask-argon` และ `ask-carbon` ให้รองรับการสตาร์ตผ่าน Windows Task Scheduler (`GeminiBridgeService`) อย่างอิสระ ไม่ค้างคิวในเซสชันของ Agent

## 🚀 เวอร์ชัน 3.9.0 (20 พฤศจิกายน 2026) - Smart Code Bundler CLI & Intent-Based Skills Suite
- **`bundle_project.py` (CLI Standalone Tool):**
  - เครื่องมือสกัดโค้ดทั้งโปรเจกต์เป็น Plain Text (.txt) ไฟล์เดียวก่อนส่งให้ AI บนเว็บหรือผ่าน API
  - **Audit Mode (`--mode audit`):** สกัดโค้ดตัวเต็ม 100% พร้อมบีบอัดช่องว่างซ้ำซ้อน เหมาะสำหรับงานหาบั๊กและตรวจช่องโหว่ความปลอดภัยระดับลึก
  - **Feature Mode (`--mode feature`):** สกัดเฉพาะโครงร่าง AST Outline (Class, Method, Function, Docstring) ประหยัดโทเค็นได้ 70-80% ช่วยเปิดพื้นที่ Context ให้โมเดลคิดสร้างสรรค์ฟีเจอร์ใหม่
  - **Diff Mode (`--mode diff`):** ดึง Git Diff ส่วนต่างล่าสุด ประหยัดโทเค็นได้ถึง 95% สำหรับการคุยงานต่อเนื่อง
- **Intent-Based Natural Language Triggers:**
  - สกิล `ask-carbon` และ `ask-argon` สามารถตรวจจับภาษาพูดทั่วไปของผู้ใช้ (เช่น *"ส่งให้คาบอนหาบั๊ก"* หรือ *"ส่งให้อากอนคิดฟีเจอร์"*) แล้วเลือกโหมดสกัดโค้ดที่เหมาะสมให้โดยอัตโนมัติ

## 🚀 เวอร์ชัน 3.8.0 (20 พฤศจิกายน 2026) - Real-time Auto-Save & Bundled Skills Suite
- **ระบบ Auto-Save บันทึกคำตอบอัจฉริยะ (Real-time Disk Auto-Save):**
  - บันทึกคำตอบจากทั้ง **Argon (Gemini Web)** และ **Carbon (Google Cloud Studio Multimodal)** ลงดิสก์อัตโนมัติทันทีที่โมเดลตอบเสร็จ
  - ป้องกันปัญหาคำตอบสูญหายเมื่อหน้าเว็บรีเฟรช หลุดการเชื่อมต่อ หรือเบราว์เซอร์ปิดตัว
  - จัดเก็บแยกโฟลเดอร์เป็นระเบียบ: `argon_history/` และ `carbon_history/` พร้อมไฟล์ `latest.md` สำหรับเปิดอ่านได้ทันที
- **Unified Persistent Bridge (Port 8765):**
  - รวมท่อเชื่อมต่อ Chrome CDP ระหว่าง Gemini Web (`/ask`) และ Cloud Studio (`/ask_carbon`) ให้ใช้ Session เดียวกัน
  - ขจัดปัญหา Chrome เด้งถามคำขออนุญาตเชื่อมต่อ (Allow Connection) ซ้ำๆ ในทุกการส่งงาน
  - เพิ่ม Fallback Extraction Endpoints (`/extract_markdown` และ `/extract_carbon`) สำหรับดึงคำตอบจากหน้าเว็บได้ตลอดเวลา
- **แพ็กเกจสกิลพร้อมใช้งาน (Bundled Skills Suite):**
  - แนบโฟลเดอร์ `skills/ask-argon` และ `skills/ask-carbon` ให้พร้อมใช้งานกับ AI Coding Agents (Antigravity / Cursor / Claude Code) ทันที
  - กฎ Auto-Save Recovery ในตัวสกิล: เปิดอ่านคำตอบจาก `latest.md` โดยตรงทันที ไม่ต้องรันสคริปต์ค้นหาซ้ำซ้อน
  - สคริปต์ `dispatch.py` และ `dispatch_carbon.py` ปราศจาก Path ส่วนตัว 100% พร้อมระบบค้นหา Token อัตโนมัติ

---

## 🚀 เวอร์ชัน 3.7.0 (20 พฤศจิกายน 2026) - Dual-Mode Release & Deep Architecture Hardening
- **สถาปัตยกรรม Dual-Mode (Headless Pipeline + Visual Cockpit):**
  - รองรับการสั่งงานตรงผ่าน AI Coding Agent (Cursor / Antigravity) ผ่าน Chrome CDP โดยไม่ต้องเปิดหน้าเว็บ
  - ระบบ Smart Code Bundling (.txt) มัดรวมโค้ดทั้งโปรเจกต์ ลดการใช้ Token ได้ 40-70%
- **แก้ปัญหา Zero-Trust Scope Paradox (สถาปัตยกรรมโดย Carbon):**
  - แยกสิทธิ์ Read Scope (อ่านไฟล์ทั้ง Workspace เพื่อวิเคราะห์ Dependency) ออกจาก Write Scope (จำกัดการเขียนเฉพาะไฟล์ที่ระบุ)
  - ป้องกัน Symlink / Junction Traversal ด้วย os.path.realpath() และ os.path.commonpath()
- **ป้องกัน Local Static File Leak บนเซิร์ฟเวอร์ Dashboard:**
  - สลับเป็น BaseHTTPRequestHandler พร้อมระบบ Static Whitelist ปิดการดาวน์โหลดไฟล์ความลับ (.env, *.py, backups/)
  - บล็อก Path Traversal (..) และ URL ผิดปกติด้วย HTTP 403 Forbidden
- **ปิดจุดลัดวงจร Smart Patching & CRLF Tolerance:**
  - ตัดการค้นหาคำดิบ 
orm_old in raw_text ป้องกัน AI แก้โค้ดผิดบรรทัดในไฟล์ขนาดใหญ่
  - ปรับปรุงการเก็บ Newline (CRLF/LF) และ BOM เดิมของไฟล์อย่างสมบูรณ์แบบ
- **Token Middleware & Concurrency ใน Web Bridge:**
  - ยกระดับ Bridge Server ด้วย X-Bridge-Token, Nonce CSP, และ Host/Origin Guard
  - เพิ่มระบบ Timeout สำหรับคิว Semaphore ป้องกัน Thread ค้างเมื่อกดยกเลิกงาน

---
## 🚀 เวอร์ชัน 3.6.0 (19 พฤศจิกายน 2026) - Security Hardening & Ergonomic Handoff
- **แก้ไขช่องโหว่ Sensitive File Filter Bypass:**
  - ย้ายการตรวจสอบ `SENSITIVE_PATTERNS` และโฟลเดอร์ความลับ (`.ssh`, `.aws`, `secrets`, `.gnupg`) ขึ้นมาตรวจก่อนนามสกุลโค้ดเป็นอันดับแรกสุด 100% ทั้งใน `server.py` และ `code_tools.py`
  - ป้องกันการหลุดรั่วของไฟล์ความลับอย่าง `secret_key.py`, `service_account.js`, `credentials.ts` ไม่ให้ส่งขึ้น AI
- **ระบบล็อกสิทธิ์อ่าน-เขียนเฉพาะไฟล์ที่สั่ง (Explicit File Scope Lockdown):**
  - เพิ่มฟังก์ชัน `extract_prompt_allowed_scope` และ `is_path_in_scope` ใน `server.py`
  - สกัดไฟล์ที่ระบุในคำสั่ง Prompt (เช่น `@server.py`, `@code_tools.py`) และไฟล์แนบมาเป็น Scope อนุญาตประจำเทิร์น
  - บล็อกเครื่องมือของ AI ไม่ให้แอบอ่านหรือแก้ไขไฟล์อื่นนอกเหนือจากที่ผู้ใช้ระบุในคำสั่ง
- **เพิ่มปุ่มส่งต่องานลงด้านล่าง (Bottom Handoff & Quick Action):**
  - เพิ่มปุ่ม "🤖 ส่งต่อให้ Antigravity" ที่ด้านล่างสุดของกล่องข้อความคำตอบ (ติดกับแถบเหรียญ/Token)
  - เพิ่มปุ่ม "🤖 ส่งต่อให้ Antigravity" ที่แถบ Bottom Controls Bar ข้างปุ่ม "⏪ ย้อนข้อความ" เพื่อให้กดส่งงานคำตอบล่าสุดได้ทันทีโดยไม่ต้องเลื่อนจอย้อนกลับขึ้นไป

## 🚀 เวอร์ชัน 3.5.1 (19 พฤศจิกายน 2026) - Live Model Quota Badge, JS Hotfix & RULE.md Adoption
- **ปุ่มแสดงสถานะโควต้าโมเดลสด (Live Model Quota Badge & Probe):**
  - เพิ่มปุ่มสถานะขนาดย่อมวางประกบข้างปุ่มเลือกโมเดล แสดงผลสถานะทันทีเมื่อสลับโมเดล:
    - 🔴 **`ติดเพดาน 429` (กะพริบสีแดง):** แจ้งเตือนทันทีเมื่อเลือกโมเดลที่โควต้าคลาวด์เต็ม (เช่น Gemini 4 Carbon/Argon) ตั้งแต่ก่อนพิมพ์
    - 🟢 **`พร้อมใช้` (สีเขียว):** สำหรับโมเดลที่โควต้าปกติพร้อมส่งงาน (เช่น Gemini 3.8 Flash)
    - 🟣 **`Web ฟรี` (สีม่วง):** สำหรับโหมด Gemini Web
  - รองรับการกดคลิกที่ตัวปุ่มเพื่อทดสอบยิงเช็คสด (Live Quota Probe) ไปยัง Google Cloud Vertex AI ทันที
  - เพิ่ม Backend Endpoint `/api/model-quota` และ `/api/probe-quota` ใน `server.py`
- **แก้ไขบั๊ก JavaScript SyntaxError ในหน้าแดชบอร์ด (`index.html`):**
  - ตัดการประกาศตัวแปร `let currentWebLevel` และ `currentWebLevelLabel` ที่ซ้ำซ้อนกันออก คืนชีพให้ระบบ Event Listener และปุ่มเลือกโมเดลกลับมาคลิกเปิดเมนูได้ 100%
- **บังคับใช้กฎหลักโปรเจกต์ (`RULE.md`):**
  - บันทึกและดึงกฎ `RULE.md` เข้าสู่ระบบการทำงานหลัก (Bottom-Line First, โหมดช่างหน้างาน Gemini 3.8 Flash, ท่อส่งงาน Argon Web Protocol)
- **จัดทำเอกสารแผนพัฒนาระยะยาว (`LTD.md`):**
  - บันทึกข้อเสนอและสเปกการทำ Sliding Window Context (3-4 ข้อความ) และการย้ายฐานข้อมูลสู่ SQLite
- **จัดระเบียบพื้นที่เก็บข้อมูล:**
  - ซิงค์ไฟล์ทั้งหมดระหว่างไดรฟ์ C และไดรฟ์ E ให้เป็นชุด Golden Baseline เดียวกัน และลบโฟลเดอร์สำรองชั่วคราว `D:\vertex-dashboard` ทิ้งอย่างปลอดภัย

---

## 🚀 เวอร์ชัน 3.5 (18 พฤศจิกายน 2026) - Hybrid Web Bridge, Token Gauge & Concurrency Hardening
ยกระดับระบบสู่สถาปัตยกรรมลูกผสม 2 เอนจิน (Vertex AI Cloud + Gemini Web CDP Bridge) พร้อมระบบตรวจจับโควต้าโทเค็นและการจัดการ Concurrency เต็มรูปแบบ:

### 🛡️ ปิดจ๊อบ 3 จุดเรื้อรังสมบูรณ์แบบ (Final Audit Hardening)
- **อุดช่องโหว่ไฟล์ความลับหลุดผ่าน `@file`:**
  - เพิ่ม `is_sensitive_path_default` ใน `code_tools.py` และส่งตัวกรอง `is_sensitive_file` ใน `server.py` บล็อก `.env`, `*gateway-config*`, `*gateway-state*`, `token.json` 100%
  - เพิ่ม `_strip_code_fences` ตัดบล็อกโค้ด ````...```` ออกก่อนสแกนแท็กไฟล์ ป้องกันการจับผิดพลาดจาก `@decorator` และ `@media`
- **ระบบคิวเบราว์เซอร์ Semaphore & กฎ `@outline:` สำหรับ Web Bridge:**
  - เพิ่ม `_bridge_semaphore = threading.Semaphore(1)` ใน `_post_to_bridge` จัดคิวส่งคำขอเข้า Chrome ทีละ 1 งาน ป้องกันการพิมพ์ชนกันในช่องแชต
  - เพิ่มพารามิเตอร์ `for_binary_upload` ใน `extract_file_mentions` ข้ามการแนบไฟล์ตัวเต็มเข้า Chrome เมื่อผู้ใช้ระบุ `@outline:` เพื่อให้ใช้ Smart Outline แทน
  - ปรับปรุงการกดปุ่ม Stop ในโหมด Web ให้ตัดการทำงานทันที ไม่เขียนทับสถานะว่าสำเร็จ
- **ระบบ Smart Patching 3 ระดับแก้ปัญหา Line-drift (`locate_smart_patch_span`):**
  - ติดตั้งฟังก์ชัน `locate_smart_patch_span` ค้นหาตำแหน่งโค้ดที่จะแก้ไข 3 ระดับชั้น (Tier 1: Exact Match, Tier 2: Dedent Whitespace Match, Tier 3: AST Symbol-Scoped Fuzzy Match)
  - เชื่อมโยงเข้ากับ `apply_edit_proposal` ทำให้การแก้ไขโค้ดตรงจุดเป๊ะ 100% แม้เลขบรรทัดจะเลื่อนจากการแก้ไขจุดอื่นในไฟล์
- **ล็อกการเชื่อมต่อเซิร์ฟเวอร์เฉพาะ Localhost:**
  - ผูกพอร์ตเซิร์ฟเวอร์กับ `"127.0.0.1"` แทน `0.0.0.0` เพื่อความปลอดภัย ป้องกันเครื่องอื่นในเครือข่ายเข้าถึงแดชบอร์ด

### 🌐 ระบบสะพานเชื่อมต่อคู่ขนาน Gemini Web (Chrome CDP Bridge - พอร์ต 8765)
- **เชื่อมต่อแท็บเบราว์เซอร์จริงผ่าน Chrome DevTools Protocol (CDP):**
  - พัฒนาโมดูล `chrome_cdp.py` และ `gemini_bridge_server.py` (พอร์ต 8765) เกาะ (Attach) เข้ากับ Google Chrome พอร์ต 9222 โดยตรง
  - สื่อสารกับแท็บห้องแชทเดิม (`Web Control`) โดยใช้เซสชันจริงของคุณ ไม่ต้องล็อกอินใหม่ และไม่ติดการตรวจจับบอทหรือ CAPTCHA
- **ระบบแนบไฟล์จริงเข้าหน้าเว็บอัตโนมัติ (Automated CDP File Upload):**
  - ใช้คำสั่งระดับต่ำ `DOM.setFileInputFiles` เจาะตรงเข้าแท็ก `<input type="file">` ทำให้แนบไฟล์โค้ดจริง (.py, .js, .txt) เข้าช่องแชตได้โดยตรงเหมือนคลิกเลือกไฟล์ด้วยตนเอง
- **Persistent Single WebSocket Connection:**
  - เปิดและรักษาท่อเชื่อมต่อ WebSocket ค้างไว้เพียงท่อเดียว เพื่อให้ Chrome แสดงหน้าต่างยืนยันกด "ยอมรับ (Allow)" เพียงครั้งเดียว แล้วใช้งานได้ตลอดไป
- **แกะโครงสร้าง Markdown และ Code Block จากเว็บ:**
  - ปรับปรุงตัวสกัดข้อความใน `chrome_cdp.py` ให้เจาะทะลุแท็ก `<code-block>` และ `<pre><code>` ดึงโค้ดจริงออกมาแสดงผลอย่างถูกต้อง 100% (แก้บั๊กแสดงแค่คำว่า `Python`)

### 📊 ระบบมาตรวัดโทเค็นและป้องกัน Error 429 (Real-time Token Gauge & Pre-flight 429 Guard)
- **Badge คำนวณขนาดโทเค็นแบบ Real-time:**
  - เพิ่ม Endpoint `POST /api/estimate-tokens` และแถบป้ายสถานะติดกับปุ่มส่งงานในหน้าเว็บ [index.html](#)
  - ประเมินขนาดตัวอักษรและไฟล์แนบ แปลงเป็นจำนวนโทเค็นทันทีที่มีการพิมพ์หรือเลือกไฟล์
- **แสดง 4 ระดับความเสี่ยงตามสี (Color-Coded Status):**
  - 🟢 **สีเขียว (`safe` < 10,000 tokens):** ปลอดภัย ส่งได้ทุกโมเดลบน Cloud
  - ⚡ **สีเหลือง (`warning` 10,000 - 25,000 tokens):** ขนาดปานกลาง แนะนำปรับ Thinking Budget เป็น Low/Medium
  - ⚠️ **สีแดงกะพริบ (`danger` > 25,000 tokens):** เสี่ยงติด Vertex AI HTTP 429 (Resource Exhausted) สูงมาก
  - 🟣 **สีม่วง (`safe_web`):** โหมด 🌐 Gemini Web ฟรี 100% ไม่มีลิมิต TPM 429
- **คลิกเดียวสลับโหมด (One-Click Auto-Switch):**
  - สามารถคลิกที่ตัว Badge สถานะเพื่อสลับไปใช้ห้อง 🌐 Gemini Web ได้ทันที
- **ระบบเบรกเกอร์แจ้งเตือนก่อนส่ง (Pre-flight Confirmation Guard):**
  - หากสั่งยิงคำขอขนาดใหญ่เกิน 25k tokens บน Cloud ระบบจะเด้งเตือนล่วงหน้า พร้อมทางเลือกให้สลับไป Web อัตโนมัติ

### 🧠 ระบบกลั่นกรองโค้ดอัจฉริยะ (Smart AST Outline & File Mentions)
- **โมดูลวิศวกรรมโค้ดอิสระ (`code_tools.py`):**
  - รองรับการสกัดโครงสร้างไฟล์ Python (`ast.parse`) และ JavaScript/TypeScript (Regex Pattern) ดึงชื่อฟังก์ชัน, คลาส, เส้นทาง Endpoint, และ Imports
  - ย่อขนาดไฟล์ขนาดใหญ่กว่า 1,000 บรรทัดให้เหลือเพียงโครงร่างย่อ ช่วยประหยัดโทเค็นได้ถึง 90-97%
- **แท็กควบคุมการอ่านไฟล์แบบละเอียดในช่องแชท:**
  - `@ชื่อไฟล์`: โหมด Auto (หากไฟล์ยาวเกิน 300 บรรทัด จะส่งเฉพาะ Smart Outline)
  - `@full:ชื่อไฟล์`: บังคับส่งโค้ดฉบับเต็มทั้งไฟล์
  - `@outline:ชื่อไฟล์`: บังคับส่งเฉพาะ Smart Outline
- **ปรับปรุง Regex ค้นหาไฟล์ด้วย Negative Lookbehind:**
  - ใช้ `(?<![A-Za-z0-9._%+-])@` ป้องกันการจับผิดพลาดจาก Email (เช่น `admin@gmail.com`) และตัดเครื่องหมายวรรคตอนท้ายชื่อไฟล์ทิ้งอัตโนมัติ

### 🔒 ยกระดับความมั่นคงปลอดภัยและการจัดการ Concurrency (Round 2 Architecture Hardening)
- **ระบบแม่กุญแจล็อกไฟล์เดี่ยว (Per-File Mutex Lock):**
  - เพิ่ม `get_file_lock()` ใน `server.py` ล็อกไฟล์ตาม Path จริง (`os.path.normcase`) ป้องกัน Race Condition และ Lost Update จากการกดยืนยันแก้ไฟล์หรือย้อนโค้ดพร้อมกัน
- **ระบบสำรองไฟล์แบบ Single .bak:**
  - ตัดการก็อปปี้ไฟล์ซ้ำซ้อนใน `apply_edit_proposal` ออก เหลือการสำรองใน `safe_write_code_file` เพียงจุดเดียว โดยจะสร้าง Backup ต่อเมื่อโค้ดใหม่ผ่านการตรวจ Syntax และ Snippet Guard แล้วเท่านั้น
- **การบันทึกแบบ Atomic Write สำหรับ Handoff:**
  - เขียนข้อมูลลงไฟล์ชั่วคราว `.{pid}.{uuid}.tmp` ให้เสร็จก่อน แล้วค่อยสลับชื่อเป็น `handoff_latest.md` และ `.json` ด้วย `os.replace` ป้องกันปัญหาไฟล์เสียหายขณะส่งต่องาน
- **ระบบย้อนข้อความและทำความสะอาดประวัติ (`⏪ ย้อนข้อความ` / Undo Turn):**
  - เพิ่มปุ่มดึงคำสั่งเดิมกลับมาแก้ไข พร้อมลบเทิร์นล่าสุดและข้อผิดพลาด (เช่น 429) ออกจาก `conversations.jsonl` และหน้าจอแดชบอร์ดอัตโนมัติ

---

## 🔥 เวอร์ชัน 3.0 (18 พฤศจิกายน 2026) - Complete Security, Stability & Antigravity IDE Upgrade
ปรับปรุงและเพิ่มฟีเจอร์ครบทั้ง 13 ข้อตามข้อเสนอแนะเชิงสถาปัตยกรรมของ Gemini 4 Hydrogen:

### 🛡️ ด้านความปลอดภัยและความเสถียร (Security & Stability)
- **บล็อกและปิดบังไฟล์ความลับอัตโนมัติ (`.env` Protection & Secret Masking):**
  - ตัด `.env` ออกจากรายชื่อไฟล์ที่อนุญาต และเพิ่มฟังก์ชัน `is_sensitive_file()` ปิดกั้น `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `*credentials*`, `*secret*` ออกจากเครื่องมืออ่าน/ค้นหา/แก้ไขทุกตัว
  - เพิ่ม `mask_secrets()` ปิดบังค่า `API_KEY`, `SECRET`, `PASSWORD`, `PRIVATE_KEY`, `ACCESS_TOKEN` เป็น `***REDACTED***` อัตโนมัติก่อนส่งข้อมูลขึ้น Vertex AI Cloud
- **แก้บั๊ก `IndexError` ใน Loop สรุปคำตอบ (`server.py`):**
  - ตรวจสอบ `candidates` ว่ามีข้อมูลก่อนเข้าถึง `candidates[0]` เมื่อวนครบ `MAX_TOOL_ITERATIONS` ป้องกันเซิร์ฟเวอร์พังหากติด Safety Filter หรือคลาวด์ตอบกลับว่าง
- **ป้องกัน XSS จากการแสดงผล Markdown (`index.html`):**
  - เพิ่มไลบรารี `DOMPurify` (3.0.9) มาครอบ `marked.parse()` ใน `renderMarkdown()` เพื่อกรองแท็ก HTML/Script อันตราย
- **จัดเก็บประวัติแชทบนเซิร์ฟเวอร์ (`sessions_store.json` + `/api/sessions`):**
  - เพิ่มการซิงก์ประวัติแชททั้งหมดไปยังไฟล์บนเครื่อง (`GET/POST /api/sessions`) ปลดล็อกข้อจำกัด `localStorage` 5MB เต็ม และป้องกัน `QuotaExceededError`
- **ปุ่ม `Undo` และ `Reject` ใน Diff Card พร้อมระบุตำแหน่งแก้ไขแม่นยำขึ้น:**
  - เพิ่ม Endpoint `/api/undo-edit` และปุ่ม **`⏪ ย้อนกลับโค้ดเดิม (Undo)`** เพื่อกู้คืนไฟล์จาก `.bak` ใน `backups/` ได้ทันทีบนหน้าจอ
  - เพิ่มปุ่ม **`✖️ ปฏิเสธ (Reject)`** และ **`↩️ เปิดข้อเสนออีกครั้ง`**
  - ปรับ `apply_edit_proposal()` ให้ค้นหาจุดที่ใกล้เคียงกับ `startLine` มากที่สุดเมื่อข้อความ `old_code` ซ้ำกันหลายจุดในไฟล์
- **Retry อัตโนมัติเมื่อเน็ตหลุดหรือ Timeout (`server.py`):**
  - ดักจับ `URLError`, `TimeoutError`, `socket.timeout`, `http.client.IncompleteRead` และ `ConnectionError` ให้หน่วงเวลาและยิงซ้ำอัตโนมัติสูงสุด 4 รอบ

### ⚡ ฟีเจอร์ระดับ Antigravity IDE (Productivity & Control)
- **สวิตช์เลือก 3 โหมด Agent บนหน้าจอ:**
  - `🔒 Read-Only (อ่านอย่างเดียว)`: ถอด `propose_edit` ออกจาก Tools และปฏิเสธ `/api/apply-edit` 100%
  - `🛠️ สถาปนิก + เสนอ Diff`: เสนอโค้ดเป็นการ์ด Diff ให้ผู้ใช้ตรวจและกดยืนยัน (Auto-Backup ก่อนบันทึก)
  - `⚡ Auto-Edit (แก้ไฟล์อัตโนมัติ)`: สำรอง `.bak` และเขียนทับลงไฟล์จริงทันทีเมื่อโมเดลส่งคำสั่งแก้ไข
- **ปุ่มหยุดการทำงานกลางคัน (`⏹️ หยุด (Stop)` + `/api/cancel`):**
  - ใช้ `AbortController` ฝั่ง Frontend ร่วมกับ `cancelled_sessions` ฝั่ง Server เพื่อตัดการทำงานของ Tool Loop ทันที ไม่เสีย Token ต่อ
- **โหลดกฎประจำโปรเจกต์อัตโนมัติ (`GEMINI.md` / `RULE.md`):**
  - อ่านไฟล์ `GEMINI.md`, `RULE.md`, `.gemini/GEMINI.md` ใน Workspace อัตโนมัติ (สูงสุด 4,000 ตัวอักษรและผ่าน Secret Masking) แนบเข้า `systemInstruction`
- **เพิ่ม 2 เครื่องมือสืบค้นประหยัด Token (`get_outline` & `git_status_diff`):**
  - `get_outline(path)`: ดึงเฉพาะโครงสร้าง `class`, `def`, `function`, `export` พร้อมเลขบรรทัด สำหรับไฟล์ขนาดใหญ่
  - `git_status_diff(path, target)`: เรียกดู `git status --short` และ `git diff` ของไฟล์ที่เพิ่งแก้ไขแบบ Read-Only
- **ระบบพิมพ์ `@` ค้นหาและแนบไฟล์อัตโนมัติ (`@mention` Autocomplete):**
  - พิมพ์ `@` ในช่องแชทเพื่อเปิดหน้าต่างค้นหาไฟล์แบบ Fuzzy Search กดลูกศร `↑↓` และ `Enter`/`Tab` เพื่อแนบเข้าบริบททันที
- **แคชกันอ่านไฟล์ซ้ำ & เกจวัด Context + ปุ่ม `🧹 ย่อความจำ (Compact)`:**
  - ป้องกันโมเดลเรียก `read_file` ช่วงบรรทัดเดิมซ้ำภายในเทิร์นเดียวกัน ลดการเสีย Input Tokens ซ้ำซ้อน
  - แสดง `% Context` บนแถบด้านบน และเพิ่มปุ่มย่อบทสนทนาเก่าให้กระชับเมื่อแชทยาวเกินไป
- **แยก Workspace, ไฟล์แนบ และโหมด Agent ตามห้องแชท (`sessionId`):**
  - ไม่ใช้ตัวแปร Global ทับกันข้ามแท็บ ช่วยให้เปิดหลายหน้าต่างทำงานคนละโปรเจกต์พร้อมกันได้อย่างปลอดภัย

---

## 🚀 เวอร์ชัน 2.4 (18 พฤศจิกายน 2026) - UI Contrast & Code Review Upgrade
- **ปรับปรุงสไตล์แถวข้อความผู้ใช้ (High-Contrast User Prompt Card):**
  - เปลี่ยนจากพื้นหลังเทามืดเดิม มาเป็นสีม่วงครามไล่เฉด `bg-gradient-to-r from-[#201d4a] via-[#1b1c3b] to-[#16152b]`
  - เพิ่มแถบสีเน้นขอบซ้ายขนาด 4px (`border-l-4 border-l-indigo-400 border border-indigo-500/40`) เพื่อแยกจุดเริ่มต้นข้อความของผู้ใช้ออกจากคำตอบ AI ทันที
  - เพิ่ม Badge หัวข้อ `👤 คำสั่งของคุณ` พร้อมป้ายกำกับ `User Prompt`
- **ระบบ Code Syntax Highlighting & One-Click Copy:**
  - ติดตั้ง `marked.js` เรนเดอร์ Markdown เต็มรูปแบบ (หัวข้อ, รายการ, ตาราง, ตัวหนา)
  - ติดตั้ง `highlight.js` (ธีม Atom One Dark) ไฮไลต์สีโค้ดทุกภาษาอัตโนมัติ
  - เพิ่มปุ่ม **"📋 คัดลอกโค้ด"** ที่หัวบล็อกโค้ดทุกกล่อง สามารถคลิกคัดลอกลง Clipboard ได้ทันที พร้อมแสดงสถานะ `✅ คัดลอกแล้ว`
- **จัดเก็บประวัติและรองรับ Full-Path Resolution:**
  - รองรับทั้งไดรฟ์ `C:\` และ `D:\` แบบยืดหยุ่น สามารถพิมพ์ Absolute Path ในกล่องแชทเพื่อให้โมเดลไปอ่านได้ทันที

---

## ⚡ เวอร์ชัน 2.3 (18 พฤศจิกายน 2026) - Concurrency & Multimodal Image Support
- **อัปเกรดเซิร์ฟเวอร์รองรับ Multi-Window (`ThreadedHTTPServer`):**
  - แปลง Backend ใน `server.py` เป็น Multi-threaded รองรับการเปิดหน้าจอแชทหลายหน้าต่างพร้อมกันได้โดยไม่บล็อกการทำงาน
  - บันทึกประวัติการสนทนาแยกรหัสห้อง (`sessionId`) ลงไฟล์ `conversations.jsonl`
- **รองรับการแนบรูปภาพและการวางรูปจาก Clipboard (`Ctrl + V`):**
  - รองรับการกดปุ่มลัด `Ctrl + V` เพื่อวางรูปภาพจากคลิปบอร์ดเข้าสู่ช่องแชทได้ทันที
  - มีปุ่ม `🖼️ แนบรูป / วาง Ctrl+V` ให้เลือกไฟล์รูปภาพจากเครื่อง
  - ส่งข้อมูลรูปภาพแบบ Base64 ผ่าน `inlineData` ไปยัง Vertex AI Multimodal API
- **ระบบสถานะการทำงานสด (Live Tool Status Polling):**
  - เพิ่ม Endpoint `/api/status?sessionId=...` และกล่องสถานะบนหน้าเว็บ
  - แสดงชื่อเครื่องมือและชื่อไฟล์ที่โมเดลกำลังสืบค้นแบบเรียลไทม์ (เช่น กำลังอ่านไฟล์, กำลังค้นหาโค้ด)
- **ระบบ Smart Retry with Exponential Backoff:**
  - รองรับการดักจับข้อผิดพลาด HTTP 429 (Quota / Rate Limit) และ 503 จาก Google Cloud แล้วหน่วงเวลาลองใหม่สูงสุด 4 รอบอัตโนมัติ

---

## 🛡️ เวอร์ชัน 2.2 (18 พฤศจิกายน 2026) - Safe Diff Preview & Auto-Backup
- **ระบบตรวจแก้โค้ดแบบปลอดภัย (Safe Propose Edit):**
  - เพิ่ม Tool `propose_edit` ให้โมเดลส่งเฉพาะโค้ดส่วนที่ต้องการแก้ไขพร้อมระบุเลขบรรทัด
  - ฝั่ง Frontend แสดงผลเป็นการ์ด **Diff Preview (เปรียบเทียบโค้ดเดิมสีแดง vs โค้ดใหม่สีเขียว)**
  - โมเดล**ไม่สามารถเขียนทับไฟล์จริงบนเครื่องได้โดยตรง** จนกว่าผู้ใช้จะกดปุ่มยืนยัน
- **ระบบสำรองไฟล์อัตโนมัติก่อนแก้ไข (Auto-Backup):**
  - เพิ่ม Endpoint `/api/apply-edit` ซึ่งจะสร้างไฟล์สำรอง `.bak` พร้อมประทับเวลาลงในโฟลเดอร์ `backups/` ทุกครั้งก่อนเขียนทับไฟล์จริง
- **มิเตอร์คำนวณเหรียญและค่าใช้จ่ายสด (Real-Time THB Cost Calculator):**
  - คำนวณค่าใช้จ่ายจาก Input Tokens, Thinking Tokens, และ Output Tokens จริงของแต่ละโมเดล
  - แสดงผลค่าใช้จ่ายเป็นเงินบาท (฿) บนแถบหัวข้อของแต่ละห้องสนทนาและสรุปท้ายกล่องคำตอบของแต่ละรอบ

---

## 🧠 เวอร์ชัน 2.1 (18 พฤศจิกายน 2026) - Advanced Model & Thinking Selector
- **เมนูเลือกโมเดลตามมาตรฐาน Antigravity IDE:**
  - รองรับ 4 โมเดลหลักของ Google Cloud:
    1. **Gemini 4 Carbon** (รุ่นใหม่ คิดลึก ความเร็วสูง)
    2. **Gemini 4 Hydrogen** (รุ่นเรือธง สำหรับงานสถาปัตยกรรมและการเขียนโค้ดซับซ้อน)
    3. **Gemini 4 Argon** (รุ่นทรงพลังสำหรับงาน Logic)
    4. **Gemini 3.8 Flash** (รุ่นเร็วพิเศษและประหยัดเครดิต)
- **เมนูย่อยเลือกระดับการคิด (Thinking Level Submenu):**
  - เลือกระดับได้ 3 ขั้นสำหรับแต่ละโมเดล:
    * **Low:** เร็วพิเศษ & ประหยัดเครดิต (Thinking budget ~1,024 tokens)
    * **Medium:** สมดุลความเร็ว & คุณภาพ (Thinking budget ~4,096 tokens)
    * **High:** คิดลึก & วิเคราะห์โค้ดละเอียด (Thinking budget ~16,384 tokens)
  - เพิ่ม Invisible Bridge ป้องกันเมนูย่อยหลุดขณะเลื่อนเมาส์ และรองรับการคลิกเปิด/ปิด

---

## 🤖 เวอร์ชัน 2.0 (17-18 พฤศจิกายน 2026) - Autonomous Agent & 5 Core Tools
- **เปลี่ยนผ่านสู่ AI Coding Agent แบบ Function Calling:**
  - ยกเลิกการใช้ RegEx ตรวจจับไฟล์แบบเดิม
  - ให้โมเดลทำงานผ่านลูปอัตโนมัติ (Autonomous Tool Execution Loop สูงสุด 8 รอบต่อคำสั่ง)
- **ชุดเครื่องมือ 5 ชนิดที่ติดตั้งให้โมเดลเรียกใช้ได้เอง:**
  1. `read_file(path, start_line, end_line)`: อ่านไฟล์เฉพาะบรรทัดที่ต้องการ พร้อมเลขบรรทัด
  2. `search_code(query, path, file_pattern)`: ค้นหาข้อความหรือ Regex ทั่วทั้งโปรเจกต์ (Grep)
  3. `list_directory(path)`: สำรวจรายชื่อไฟล์และโฟลเดอร์
  4. `find_by_name(pattern, path)`: ค้นหาไฟล์ตามชื่อหรือนามสกุล
  5. `propose_edit(path, start_line, end_line, new_code, description)`: เสนอโค้ดแก้ไขแบบปลอดภัย

---

## 📦 เวอร์ชัน 1.0 (17 พฤศจิกายน 2026) - Initial Studio Setup
- สร้างระบบ Local Dashboard พื้นฐานบน Python `http.server` พอร์ต 5005
- เชื่อมต่อ Vertex AI ผ่าน Google Cloud SDK (ADC Token)
- สร้างหน้าเว็บ Tailwind CSS แบบ Single Page Application
- รองรับการสลับ Workspace เบื้องต้น
