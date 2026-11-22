import asyncio
import json
import os
import time
import websockets

DEVTOOLS_PORT_FILE = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\DevToolsActivePort")


def get_cdp_ws_url():
    if not os.path.isfile(DEVTOOLS_PORT_FILE):
        raise RuntimeError(f"ไม่พบไฟล์ DevToolsActivePort ที่ {DEVTOOLS_PORT_FILE}")
    with open(DEVTOOLS_PORT_FILE, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    if len(lines) < 2:
        raise RuntimeError("รูปแบบไฟล์ DevToolsActivePort ไม่สมบูรณ์")
    port = lines[0]
    path = lines[1]
    return f"ws://127.0.0.1:{port}{path}"


class ChromeClient:
    def __init__(self):
        self.ws = None
        self.connected = False
        self.msg_id = 0
        self.pending = {}
        self.sessions = {}
        self.listen_task = None
        self.gemini_target_id = None
        self.gemini_session_id = None
        self.ask_lock = asyncio.Lock()

    async def connect(self):
        url = get_cdp_ws_url()
        self.ws = await websockets.connect(
            url,
            open_timeout=60,
            ping_interval=20,
            ping_timeout=20,
            max_size=20_000_000,
        )
        self.connected = True
        self.gemini_target_id = None
        self.gemini_session_id = None
        self.listen_task = asyncio.create_task(self._listener())

    async def _listener(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                method = data.get("method")
                params = data.get("params", {})

                if method == "Target.attachedToTarget":
                    tid = params.get("targetInfo", {}).get("targetId")
                    sid = params.get("sessionId")
                    if tid and sid:
                        self.sessions[tid] = sid
                elif method == "Target.detachedFromTarget":
                    sid = params.get("sessionId")
                    if sid == self.gemini_session_id:
                        self.gemini_session_id = None
                        self.gemini_target_id = None
                elif method == "Target.targetDestroyed":
                    tid = params.get("targetId")
                    if tid == self.gemini_target_id:
                        self.gemini_session_id = None
                        self.gemini_target_id = None

                mid = data.get("id")
                if mid and mid in self.pending:
                    fut = self.pending.pop(mid)
                    if not fut.done():
                        fut.set_result(data)
        except Exception:
            pass
        finally:
            self.connected = False
            self.gemini_target_id = None
            self.gemini_session_id = None
            for mid, fut in list(self.pending.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError("Chrome CDP connection closed"))
            self.pending.clear()

    async def send_cmd(self, method, params=None, session_id=None, timeout=15.0):
        if not self.connected or not self.ws:
            raise RuntimeError("ยังไม่ได้เชื่อมต่อกับ Chrome CDP")
        self.msg_id += 1
        mid = self.msg_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending[mid] = fut

        msg = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id

        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.pending.pop(mid, None)

    async def get_tabs(self):
        res = await self.send_cmd("Target.getTargets")
        targets = res.get("result", {}).get("targetInfos", [])
        return [t for t in targets if t.get("type") == "page"]

    async def attach_tab(self, target_id):
        res = await self.send_cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = res.get("result", {}).get("sessionId") or self.sessions.get(target_id)
        if not sid:
            await asyncio.sleep(0.2)
            sid = self.sessions.get(target_id)
        return sid

    async def eval_js(self, session_id, expression, timeout=15.0, raise_on_js_error=False):
        res = await self.send_cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id=session_id,
            timeout=timeout,
        )
        if "error" in res:
            raise RuntimeError(res["error"].get("message", "CDP Runtime error"))
        result_obj = res.get("result", {})
        if "exceptionDetails" in result_obj:
            exc = result_obj["exceptionDetails"]
            msg = exc.get("exception", {}).get("description") or exc.get("text", "JS execution error")
            if raise_on_js_error:
                raise RuntimeError(f"JS Exception: {msg}")
            print(f"[ChromeClient] JS Exception (ignored): {msg}")
        return result_obj.get("result", {}).get("value")

    async def find_and_attach_gemini(self):
        if self.gemini_session_id and self.gemini_target_id:
            try:
                cur_url = await self.eval_js(self.gemini_session_id, "window.location.href", timeout=5.0)
                if cur_url and "gemini.google.com" in cur_url:
                    if "/u/1/" in cur_url or "your-gemini-chat-id" in cur_url:
                        return self.gemini_target_id, self.gemini_session_id, cur_url
            except Exception:
                self.gemini_session_id = None
                self.gemini_target_id = None

        tabs = await self.get_tabs()
        gemini_tab = None

        # ลำดับที่ 1: แท็บห้องแชทที่ระบุ
        for t in tabs:
            url = t.get("url", "")
            if "your-gemini-chat-id" in url and "/u/1/" in url:
                gemini_tab = t
                break

        # ลำดับที่ 2: แท็บ Gemini Web (/app) ใดๆ
        if not gemini_tab:
            for t in tabs:
                url = t.get("url", "")
                if "/u/1/app" in url or "/app" in url:
                    gemini_tab = t
                    break

        # ลำดับที่ 3: ห้อง your-gemini-chat-id
        if not gemini_tab:
            for t in tabs:
                url = t.get("url", "")
                if "your-gemini-chat-id" in url:
                    gemini_tab = t
                    break

        # ลำดับที่ 4: แท็บ gemini.google.com/app ทั่วไป
        if not gemini_tab:
            for t in tabs:
                url = t.get("url", "")
                if "gemini.google.com/app" in url:
                    gemini_tab = t
                    break

        if not gemini_tab:
            for t in tabs:
                url = t.get("url", "")
                if "gemini.google.com" in url and "RotateCookies" not in url:
                    gemini_tab = t
                    break

        if not gemini_tab:
            raise RuntimeError("ไม่พบแท็บ gemini.google.com เปิดอยู่ใน Chrome")

        tid = gemini_tab.get("targetId")
        sid = await self.attach_tab(tid)
        if not sid:
            raise RuntimeError("ไม่สามารถ Attach เข้าแท็บ Gemini ได้")

        self.gemini_target_id = tid
        self.gemini_session_id = sid
        return tid, sid, gemini_tab.get("url", "")

    async def select_model_and_thinking(self, sid, web_model=None, thinking_level=None):
        """คลิกเลือกโมเดลและระดับการคิดบนเมนูของ Gemini Web ก่อนส่งคำถาม"""
        model_map = {
            "3.5-flash-lite": ["3.5 Flash-Lite", "Flash-Lite", "Flash", "แฟลช", "คำตอบด่วน"],
            "4-hydrogen": ["4 Hydrogen", "Hydrogen", "ไฮโดรเจน", "ความช่วยเหลือในเรื่องทั่วๆ ไป"],
            "3.1-pro": ["3.1 Pro", "Pro", "โปร", "การค้นคว้าข้อมูลโดยละเอียด"],
            "4-argon": ["4 Argon", "Argon", "อาร์กอน", "โค้ดและการวิเคราะห์"],
            "4-carbon": ["4 Carbon", "Carbon", "คาร์บอน", "งานที่ซับซ้อนที่สุด"],
        }
        thinking_map = {
            "LOW": ["ต่ำ", "Fast and efficient", "low"],
            "MEDIUM": ["ปานกลาง", "ความลึกในระดับที่สมดุล", "medium"],
            "HIGH": ["สูง", "ละเอียดเป็นพิเศษ", "high"],
        }

        norm_model = str(web_model or "").strip().lower()
        norm_level = str(thinking_level or "").strip().upper()
        if (not norm_model or norm_model in ["current", "auto", "keep", "default", "web", "none"]) and (not norm_level or norm_level in ["CURRENT", "AUTO", "KEEP", "DEFAULT", "NONE"]):
            return {"appliedModel": "Web Control (แท็บปัจจุบัน)", "appliedThinking": "ตามแท็บ", "note": "ใช้ห้องและโมเดลปัจจุบันของ Web Control โดยตรง"}

        result_info = {"appliedModel": None, "appliedThinking": None, "note": ""}

        # ฟังก์ชัน JS ช่วยหาปุ่มตัวเลือกโมเดลและสถานะเมนู
        read_trigger_js = """
        (() => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const isVis = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
            };
            let btn = document.querySelector('[data-test-id="bard-mode-menu-button"]');
            if (!btn || !isVis(btn)) {
                const candidates = Array.from(document.querySelectorAll(
                    'bard-mode-switcher button, [data-test-id="bard-mode-switcher"] button, button[data-test-id*="mode"], button[aria-haspopup="menu"], .input-area-container button, form button'
                )).filter(isVis);
                const kw = /flash|hydrogen|pro|argon|carbon|อาร์กอน|ไฮโดรเจน|คาร์บอน|โปร|แฟลช/i;
                btn = candidates.find(b => kw.test(norm(b.innerText || b.textContent)));
            }
            if (!btn || !isVis(btn)) {
                const kw = /flash|hydrogen|pro|argon|carbon|อาร์กอน|ไฮโดรเจน|คาร์บอน|โปร|แฟลช/i;
                btn = Array.from(document.querySelectorAll('button')).filter(isVis).find(b => {
                    const r = b.getBoundingClientRect();
                    return r.top > window.innerHeight * 0.45 && kw.test(norm(b.innerText || b.textContent));
                });
            }
            const menuItems = Array.from(document.querySelectorAll(
                '.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"], .cdk-overlay-pane .mat-mdc-menu-item, [role="menu"] [role="menuitem"], [role="menu"] button'
            )).filter(isVis);
            return {
                found: !!btn,
                label: btn ? norm(btn.innerText || btn.textContent) : '',
                menuOpen: menuItems.length > 0
            };
        })()
        """

        open_menu_js = """
        (() => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const isVis = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
            };
            const openItems = Array.from(document.querySelectorAll(
                '.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"], .cdk-overlay-pane .mat-mdc-menu-item, [role="menu"] [role="menuitem"]'
            )).filter(isVis);
            if (openItems.length > 0) return { opened: true, alreadyOpen: true };

            let btn = document.querySelector('[data-test-id="bard-mode-menu-button"]');
            if (!btn || !isVis(btn)) {
                const candidates = Array.from(document.querySelectorAll(
                    'bard-mode-switcher button, [data-test-id="bard-mode-switcher"] button, button[data-test-id*="mode"], button[aria-haspopup="menu"], .input-area-container button, form button'
                )).filter(isVis);
                const kw = /flash|hydrogen|pro|argon|carbon|อาร์กอน|ไฮโดรเจน|คาร์บอน|โปร|แฟลช/i;
                btn = candidates.find(b => kw.test(norm(b.innerText || b.textContent)));
            }
            if (!btn || !isVis(btn)) {
                const kw = /flash|hydrogen|pro|argon|carbon|อาร์กอน|ไฮโดรเจน|คาร์บอน|โปร|แฟลช/i;
                btn = Array.from(document.querySelectorAll('button')).filter(isVis).find(b => {
                    const r = b.getBoundingClientRect();
                    return r.top > window.innerHeight * 0.45 && kw.test(norm(b.innerText || b.textContent));
                });
            }
            if (!btn) return { opened: false, error: 'ไม่พบปุ่มเลือกโมเดลบนหน้า Gemini Web' };
            btn.click();
            return { opened: true, label: norm(btn.innerText || btn.textContent) };
        })()
        """

        # 1. เปลี่ยนระดับการคิด (Thinking Level) ถ้าระบุมา
        if norm_level in thinking_map:
            open_res = await self.eval_js(sid, open_menu_js)
            await asyncio.sleep(0.35)
            if open_res and open_res.get("opened"):
                hover_thinking_js = """
                (() => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVis = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const items = Array.from(document.querySelectorAll(
                        '.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"], .cdk-overlay-pane .mat-mdc-menu-item, .cdk-overlay-pane button, [role="menu"] button'
                    )).filter(isVis);
                    const sub = items.find(el => /ระดับการคิด|thinking/i.test(norm(el.innerText || el.textContent)));
                    if (!sub) return { found: false };
                    ['pointerenter', 'mouseenter', 'mouseover'].forEach(ev => {
                        sub.dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true }));
                    });
                    sub.click();
                    return { found: true };
                })()
                """
                sub_res = await self.eval_js(sid, hover_thinking_js)
                if sub_res and sub_res.get("found"):
                    await asyncio.sleep(0.35)
                    t_kws_json = json.dumps(thinking_map[norm_level], ensure_ascii=False)
                    click_thinking_js = f"""
                    (() => {{
                        const kws = {t_kws_json};
                        const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                        const isVis = (el) => {{
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }};
                        const items = Array.from(document.querySelectorAll(
                            '.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"], .cdk-overlay-pane .mat-mdc-menu-item, [role="menu"] [role="menuitemradio"], [role="menu"] [role="menuitem"]'
                        )).filter(isVis);
                        const match = items.find(el => {{
                            const lines = (el.innerText || el.textContent || '').split(/\\n/).map(x => x.trim()).filter(Boolean);
                            const full = norm(el.innerText || el.textContent);
                            return kws.some(k => lines.includes(k) || full.includes(k));
                        }});
                        if (!match) return {{ clicked: false }};
                        if (match.disabled || match.getAttribute('aria-disabled') === 'true') {{
                            return {{ clicked: false, disabled: true }};
                        }}
                        match.click();
                        return {{ clicked: true, label: norm(match.innerText || match.textContent) }};
                    }})()
                    """
                    tk_res = await self.eval_js(sid, click_thinking_js)
                    if tk_res and tk_res.get("clicked"):
                        result_info["appliedThinking"] = norm_level
                    await asyncio.sleep(0.35)

        # 2. เปลี่ยนโมเดล (Model) ถ้าระบุมาและยังไม่ตรงกับโมเดลปัจจุบัน
        if norm_model in model_map:
            target_kws = model_map[norm_model]
            trig = await self.eval_js(sid, read_trigger_js)
            cur_label = (trig or {}).get("label", "")
            # เช็กเฉพาะคำเฉพาะตัวของรุ่น (ตัวแรกของ target_kws หรือชื่อรุ่นย่อ)
            short_match_kw = target_kws[0].lower()
            already_selected = short_match_kw in cur_label.lower() or (
                norm_model == "3.5-flash-lite" and "flash" in cur_label.lower()
            ) or (
                norm_model == "4-hydrogen" and "hydrogen" in cur_label.lower()
            ) or (
                norm_model == "3.1-pro" and "pro" in cur_label.lower()
            ) or (
                norm_model == "4-argon" and "argon" in cur_label.lower()
            ) or (
                norm_model == "4-carbon" and "carbon" in cur_label.lower()
            )

            if already_selected:
                result_info["appliedModel"] = cur_label
            else:
                await self.eval_js(sid, open_menu_js)
                await asyncio.sleep(0.35)
                kws_json = json.dumps(target_kws, ensure_ascii=False)
                click_model_js = f"""
                (() => {{
                    const kws = {kws_json};
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                    const isVis = (el) => {{
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }};
                    const items = Array.from(document.querySelectorAll(
                        '.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"], .cdk-overlay-pane .mat-mdc-menu-item, [role="menu"] [role="menuitem"], [role="menu"] button'
                    )).filter(isVis);
                    const match = items.find(el => {{
                        const txt = norm(el.innerText || el.textContent);
                        return kws.some(k => txt.toLowerCase().includes(k.toLowerCase()));
                    }});
                    if (!match) return {{ clicked: false, error: 'ไม่พบรายการโมเดลในเมนูของหน้า Gemini Web' }};
                    const hasLock = !!(match.querySelector('mat-icon') && /lock/i.test(match.querySelector('mat-icon').textContent || ''));
                    if (match.disabled || match.getAttribute('aria-disabled') === 'true' || hasLock) {{
                        return {{ clicked: false, locked: true, error: 'โมเดลนี้ถูกล็อกสิทธิ์การใช้งานในหน้า Gemini Web' }};
                    }}
                    match.click();
                    return {{ clicked: true, label: norm(match.innerText || match.textContent) }};
                }})()
                """
                m_res = await self.eval_js(sid, click_model_js)
                await asyncio.sleep(0.4)
                if m_res and m_res.get("clicked"):
                    trig_after = await self.eval_js(sid, read_trigger_js)
                    result_info["appliedModel"] = (trig_after or {}).get("label") or m_res.get("label")
                else:
                    result_info["note"] = (m_res or {}).get("error", "สลับโมเดลบนหน้าเว็บไม่สำเร็จ")

        # ปิดเมนูที่อาจเปิดค้างอยู่ก่อนเริ่มพิมพ์ข้อความ
        await self.eval_js(
            sid,
            """
            (() => {
                const open = document.querySelector('.cdk-overlay-pane [role="menuitem"], .cdk-overlay-pane [role="menuitemradio"]');
                if (open) {
                    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
                    const backdrop = document.querySelector('.cdk-overlay-backdrop');
                    if (backdrop) backdrop.click();
                }
            })()
            """,
        )
        return result_info

    async def extract_last_message_markdown(self, sid):
        """แปลง DOM ของ message ล่าสุดในแท็บ Gemini ให้เป็น Markdown โครงสร้างสมบูรณ์ (เลขข้อ, บูลเล็ตย่อหน้า, โค้ดบล็อก, ตัวหนา)"""
        js = """
        (() => {
            function domToMarkdown(root) {
                if (!root) return '';
                function walk(node, depth = 0, listType = null, listIndex = 1) {
                    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
                    if (node.nodeType !== Node.ELEMENT_NODE) return '';
                    const tag = node.tagName.toLowerCase();

                    // ข้ามปุ่ม Copy, ไอคอน, SVG และ Tag ขยะของ Angular/Sources
                    if (['button', 'mat-icon', 'svg', 'sources-carousel-inline', 'source-footnote', 'source-inline-chip', 'response-element'].includes(tag)) return '';
                    if (node.classList && node.classList.contains('no-md') && !node.querySelector('pre, code-block')) {
                        const chip = node.querySelector('.chip, button, span, code');
                        return chip ? ' `' + chip.textContent.trim() + '` ' : '';
                    }

                    if (tag === 'code-block' || tag === 'pre') {
                        const code = node.querySelector('code');
                        const pre = node.querySelector('pre') || (tag === 'pre' ? node : null);
                        let lang = '';
                        const langEl = node.querySelector('.header-text, .code-block-decoration-header, .language-header, [class*="language-"]');
                        if (langEl) {
                            lang = langEl.textContent.trim().toLowerCase();
                        } else if (code) {
                            lang = (code.className.match(/language-([a-z0-9_-]+)/i) || [])[1] || '';
                        }
                        lang = lang.replace(/[^a-z0-9_-]/gi, '');
                        const rawText = (code ? code.textContent : (pre ? pre.textContent : node.textContent)).replace(/\\r\\n/g, '\\n').trim();
                        const fence = rawText.includes('````') ? '`````' : (rawText.includes('```') ? '````' : '```');
                        return '\\n' + fence + lang + '\\n' + rawText + '\\n' + fence + '\\n\\n';
                    }

                    if (tag === 'code') {
                        if (node.closest('pre') || node.closest('code-block')) return node.textContent;
                        return '`' + node.textContent.trim() + '`';
                    }

                    if (tag === 'h1') return '\\n# ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\\n\\n';
                    if (tag === 'h2') return '\\n## ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\\n\\n';
                    if (tag === 'h3') return '\\n### ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\\n\\n';
                    if (tag === 'h4') return '\\n#### ' + Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim() + '\\n\\n';

                    if (tag === 'p') {
                        const pText = Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim();
                        return pText ? pText + '\\n\\n' : '';
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
                        let res = '\\n';
                        let childIdx = 1;
                        for (const child of node.children) {
                            if (child.tagName.toLowerCase() === 'li') res += walk(child, depth + 1, 'ul', childIdx++);
                        }
                        return res + '\\n';
                    }

                    if (tag === 'ol') {
                        let res = '\\n';
                        let start = parseInt(node.getAttribute('start') || '1', 10);
                        let childIdx = start;
                        for (const child of node.children) {
                            if (child.tagName.toLowerCase() === 'li') res += walk(child, depth + 1, 'ol', childIdx++);
                        }
                        return res + '\\n';
                    }

                    if (tag === 'li') {
                        const indent = '  '.repeat(Math.max(0, depth - 1));
                        const bullet = listType === 'ol' ? `${listIndex}. ` : '- ';
                        let inlineParts = [];
                        let sublistParts = [];
                        for (const child of node.childNodes) {
                            if (child.nodeType === Node.ELEMENT_NODE && ['ul', 'ol', 'pre', 'code-block'].includes(child.tagName.toLowerCase())) {
                                sublistParts.push(walk(child, depth));
                            } else {
                                inlineParts.push(walk(child, depth));
                            }
                        }
                        const itemText = inlineParts.join('').trim().replace(/\\s+/g, ' ');
                        return `${indent}${bullet}${itemText}\\n${sublistParts.join('')}`;
                    }

                    if (tag === 'blockquote') {
                        const text = Array.from(node.childNodes).map(c => walk(c, depth)).join('').trim();
                        return '\\n> ' + text.split('\\n').join('\\n> ') + '\\n\\n';
                    }

                    if (tag === 'hr') return '\\n---\\n\\n';
                    if (tag === 'table') {
                        const rows = Array.from(node.querySelectorAll('tr'));
                        if (rows.length === 0) return '';
                        const mdRows = [];
                        rows.forEach((tr) => {
                            const cells = Array.from(tr.querySelectorAll('th, td'));
                            const cellTexts = cells.map(cell => walk(cell).replace(/\\s+/g, ' ').replace(/\\|/g, '\\\\|').trim());
                            if (cellTexts.length === 0) return;
                            mdRows.push('| ' + cellTexts.join(' | ') + ' |');
                            if (mdRows.length === 1) {
                                mdRows.push('| ' + cellTexts.map(() => '---').join(' | ') + ' |');
                            }
                        });
                        return mdRows.length > 0 ? '\\n\\n' + mdRows.join('\\n') + '\\n\\n' : '';
                    }

                    return Array.from(node.childNodes).map(c => walk(c, depth, listType, listIndex)).join('');
                }

                return walk(root).replace(/\\n{3,}/g, '\\n\\n').trim();
            }

            const bubbles = document.querySelectorAll('message-content, .model-response-text, .response-container-content, [data-test-id="model-response"]');
            if (bubbles.length === 0) return { markdown: '' };
            const lastEl = bubbles[bubbles.length - 1];
            const panel = lastEl.querySelector('.markdown-main-panel, .md-content') || lastEl;
            return { markdown: domToMarkdown(panel) };
        })()
        """
        try:
            res = await self.eval_js(sid, js)
            return (res or {}).get("markdown", "")
        except Exception as e:
            print(f"[CDP] Error extracting DOM markdown: {e}")
            return ""

    async def upload_files_to_gemini(self, sid, file_paths):
        """แนบไฟล์จากเครื่องเข้าแท็บ Gemini Web อัตโนมัติผ่าน CDP DOM.setFileInputFiles"""
        if not file_paths:
            return {"ok": True, "count": 0}

        valid_files = []
        for fp in file_paths:
            p = os.path.abspath(fp)
            if os.path.isfile(p):
                valid_files.append(p)

        if not valid_files:
            return {"ok": False, "error": "ไม่พบไฟล์ที่ระบุบนเครื่อง"}

        try:
            # 1. ตรวจสอบว่ามี input[type="file"] หรือยัง ถ้ายังไม่มีให้คลิกปุ่มบวกเพื่อ Mount เมนู
            mount_js = """
            (() => {
                const inp = document.querySelector('input[type="file"].hidden-file-input, input[type="file"]');
                if (inp) return { alreadyMounted: true };
                const plusBtn = document.querySelector('button[aria-label*="อัปโหลดและเครื่องมือ"], button[aria-label*="Upload"], .plus-button');
                if (plusBtn) {
                    plusBtn.click();
                    return { clicked: true };
                }
                return { notFound: true };
            })()
            """
            await self.eval_js(sid, mount_js)
            await asyncio.sleep(0.4)

            # 2. ค้นหา Document Node แบบเต็มความลึก (depth: -1)
            doc_res = await self.send_cmd("DOM.getDocument", {"depth": -1}, session_id=sid)
            root_node_id = (doc_res.get("result", {}) or {}).get("root", {}).get("nodeId")
            if not root_node_id:
                return {"ok": False, "error": "ไม่พบ DOM Root Node"}

            query_res = await self.send_cmd(
                "DOM.querySelectorAll",
                {"nodeId": root_node_id, "selector": "input[type='file'].hidden-file-input, input[type='file']"},
                session_id=sid,
            )
            node_ids = (query_res.get("result", {}) or {}).get("nodeIds", [])
            if not node_ids:
                return {"ok": False, "error": "ไม่พบแท็ก input[type='file'] บนหน้า Gemini Web หลังเปิดเมนู"}

            target_node_id = node_ids[0]

            # 3. สั่ง CDP นำไฟล์เข้า input โดยตรง
            await self.send_cmd(
                "DOM.setFileInputFiles",
                {"files": valid_files, "nodeId": target_node_id},
                session_id=sid,
            )

            # 4. กระตุ้น Event change/input และปิดเมนู Backdrop
            dispatch_js = """
            (() => {
                const inputs = document.querySelectorAll('input[type="file"]');
                for (let input of inputs) {
                    if (input.files && input.files.length > 0) {
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
                const backdrop = document.querySelector('.cdk-overlay-backdrop');
                if (backdrop) backdrop.click();
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
            })()
            """
            await self.eval_js(sid, dispatch_js)
            await asyncio.sleep(1.2)
            return {"ok": True, "files": valid_files, "count": len(valid_files)}
        except Exception as e:
            print(f"[CDP] File Upload Error: {e}")
            return {"ok": False, "error": str(e)}

    async def ask_gemini_tab(self, prompt_text, web_model=None, thinking_level=None, files=None):
        async with self.ask_lock:
            t0 = time.monotonic()
            tid, sid, _ = await self.find_and_attach_gemini()

            # Ensure tab isn't frozen in background
            try:
                await self.send_cmd("Target.activateTarget", {"targetId": tid}, timeout=5.0)
            except Exception:
                pass

            sel_info = {}
            if web_model or thinking_level:
                sel_info = await self.select_model_and_thinking(sid, web_model=web_model, thinking_level=thinking_level)
                await asyncio.sleep(0.25)

            # Snapshot initial bubbles
            init_state = await self.eval_js(
                sid,
                """
                (() => {
                    const bubbles = document.querySelectorAll('message-content, .model-response-text, .response-container-content, [data-test-id="model-response"]');
                    const lastText = bubbles.length > 0 ? (bubbles[bubbles.length - 1].innerText || bubbles[bubbles.length - 1].textContent || '').trim() : '';
                    return { initialCount: bubbles.length, initialLastText: lastText };
                })()
                """,
            )
            initial_count = (init_state or {}).get("initialCount", 0)
            initial_last_text = (init_state or {}).get("initialLastText", "")

            # แนบไฟล์เข้าแท็บถ้ามีระบุมา
            attached_files_info = []
            if files:
                up_res = await self.upload_files_to_gemini(sid, files)
                if up_res.get("ok"):
                    attached_files_info = up_res.get("files", [])
                await asyncio.sleep(0.5)

            prompt_json = json.dumps(prompt_text, ensure_ascii=False)
            type_js = f"""
            (() => {{
                const text = {prompt_json};
                const inputEl = document.querySelector('rich-textarea p, rich-textarea div[contenteditable="true"], div[contenteditable="true"][role="textbox"]');
                if (!inputEl) return {{ success: false, error: 'ไม่พบช่องพิมพ์ข้อความในหน้า Gemini Web' }};
                inputEl.focus();
                try {{
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(inputEl);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('selectAll', false, null);
                    document.execCommand('insertText', false, text);
                }} catch (e) {{}}
                const cur = (inputEl.innerText || inputEl.textContent || '').trim();
                if (!cur || cur !== text.trim()) {{
                    inputEl.textContent = text;
                    inputEl.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: text }}));
                    inputEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
                return {{ success: true }};
            }})()
            """
            type_res = await self.eval_js(sid, type_js)
            if not type_res or not type_res.get("success"):
                return {
                    "ok": False,
                    "error": (type_res or {}).get("error", "ไม่สามารถพิมพ์ข้อความลงแท็บ Gemini ได้"),
                }

            await asyncio.sleep(0.4)

            send_js = """
            (() => {
                const sendBtn = document.querySelector('button[aria-label*="ส่ง"], button[aria-label*="Send"], .send-button, button.send-button-container, button[data-test-id="send-button"]');
                if (sendBtn && !sendBtn.disabled && sendBtn.getAttribute('aria-disabled') !== 'true') {
                    sendBtn.click();
                    return { sent: true, method: 'button_click' };
                }
                const inputEl = document.querySelector('rich-textarea div[contenteditable="true"], div[contenteditable="true"][role="textbox"], rich-textarea p');
                if (inputEl) {
                    inputEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                    return { sent: true, method: 'enter_key' };
                }
                return { sent: false };
            })()
            """
            send_res = await self.eval_js(sid, send_js)
            if not send_res or not send_res.get("sent"):
                return {"ok": False, "error": "ไม่พบปุ่มส่งข้อความบนหน้า Gemini Web"}

            await asyncio.sleep(1.2)

            prev_text = ""
            stable_ticks = 0
            seen_new = False

            for _ in range(500):  # สูงสุด 300 วินาที (500 * 0.6s)
                st = await self.eval_js(
                    sid,
                    """
                    (() => {
                        const stopBtn = document.querySelector('button[aria-label*="หยุด"], button[aria-label*="Stop"], button.stop-button, .streaming-indicator');
                        const bubbles = document.querySelectorAll('message-content, .model-response-text, .response-container-content, [data-test-id="model-response"]');
                        const lastText = bubbles.length > 0 ? (bubbles[bubbles.length - 1].innerText || bubbles[bubbles.length - 1].textContent || '').trim() : '';
                        return {
                            isGenerating: !!stopBtn,
                            bubblesCount: bubbles.length,
                            lastText: lastText
                        };
                    })()
                    """,
                )
                if st:
                    is_gen = st.get("isGenerating", False)
                    b_count = st.get("bubblesCount", 0)
                    last_text = st.get("lastText", "")

                    if is_gen or b_count > initial_count or (last_text and last_text != initial_last_text):
                        seen_new = True

                    if seen_new and not is_gen and last_text:
                        if last_text == prev_text:
                            stable_ticks += 1
                        else:
                            stable_ticks = 0
                        prev_text = last_text

                        if stable_ticks >= 2:
                            md_text = await self.extract_last_message_markdown(sid)
                            final_answer = md_text if (md_text and len(md_text.strip()) > 0) else last_text
                            elapsed_ms = int((time.monotonic() - t0) * 1000)
                            return {
                                "ok": True,
                                "gem": "Web Control",
                                "answer": final_answer,
                                "elapsedMs": elapsed_ms,
                                "appliedModel": sel_info.get("appliedModel"),
                                "appliedThinking": sel_info.get("appliedThinking"),
                                "selectionNote": sel_info.get("note", ""),
                            }

                await asyncio.sleep(0.6)

            return {"ok": False, "error": "Timeout รอคำตอบจาก Gemini Web เกิน 300 วินาที"}

    async def close(self):
        self.connected = False
        if self.listen_task:
            self.listen_task.cancel()
        if self.ws:
            await self.ws.close()
