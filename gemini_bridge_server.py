import asyncio
import json
import os
import sys
import time
import uuid
from aiohttp import web

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, os.path.dirname(__file__))
from bridge_security import html_response, make_security_middleware, new_token
from chrome_cdp_fixes import CDPError, HardenedChromeClient, atomic_save_history
from code_tools import build_search_roots

PORT = int(os.environ.get("BRIDGE_PORT", 8765))
TOKEN_FILE = os.path.join(os.path.dirname(__file__), f".bridge_token_{PORT}.json")
TOKEN_FALLBACK_FILE = os.path.join(os.path.dirname(__file__), ".bridge_token")
TOKEN = os.environ.get("BRIDGE_TOKEN") or new_token()

# จัดเก็บ WebSocket Connection ของ Chrome Tab (Userscript fallback)
active_ws_clients = set()
pending_requests = {}
ws_request_owner = {}
gem_client_info = {}
server_bg_tasks = set()

# การเชื่อมต่อตรงผ่าน Chrome DevTools Protocol แบบ Hardened Single Connection
cdp_client = HardenedChromeClient()
cdp_client.tab_preferences = ["/notebook/", "/u/1/app", "your-gemini-chat-id"]
extra_roots = [p.strip() for p in os.environ.get("BRIDGE_EXTRA_UPLOAD_ROOTS", "").split(os.pathsep) if p.strip()]
cdp_client.upload_roots = build_search_roots(extra_roots=extra_roots)
cdp_connect_lock = asyncio.Lock()
cdp_status = {
    "connected": False,
    "connecting": False,
    "error": "",
    "tabUrl": "",
}

def _track_server_task(task: asyncio.Task) -> asyncio.Task:
    server_bg_tasks.add(task)
    def _on_done(t: asyncio.Task):
        server_bg_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                print(f"[Bridge Server] Background task error: {exc!r}")
    task.add_done_callback(_on_done)
    return task

def _persist_bridge_token(port: int, token: str):
    """บันทึก Token ล่าสุดและ PID ลงไฟล์ .bridge_token_{port}.json แบบ Atomic พร้อม Retry บน Windows"""
    payload = json.dumps({"port": port, "token": token, "pid": os.getpid()})
    for path in (TOKEN_FILE, TOKEN_FALLBACK_FILE):
        tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.04 * (attempt + 1))
        except Exception as e:
            print(f"[Bridge Server] Warning: Failed to persist token to {path}: {e}")
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

def _cleanup_bridge_token():
    """ลบไฟล์ Token เฉพาะกรณีที่ PID ในไฟล์ตรงกับโปรเซสนี้เท่านั้น"""
    my_pid = os.getpid()
    for path in (TOKEN_FILE, TOKEN_FALLBACK_FILE):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("pid") == my_pid:
                os.remove(path)
        except Exception:
            pass

async def ensure_cdp_connected():
    """เปิดการเชื่อมต่อ Chrome CDP เพียง 1 ครั้งเท่านั้น (รอสูงสุด 60s ไม่วนยิงซ้ำอัตโนมัติ)"""
    if cdp_client.connected:
        cdp_status["connected"] = True
        return True

    if cdp_connect_lock.locked():
        return False

    async with cdp_connect_lock:
        if cdp_client.connected:
            cdp_status["connected"] = True
            return True

        cdp_status["connecting"] = True
        cdp_status["error"] = ""
        print("[Bridge Server] Requesting SINGLE Chrome CDP connection (waiting up to 60s for Allow)...")
        try:
            await cdp_client.connect()
            cdp_status["connected"] = True
            try:
                _, _, tab_url = await cdp_client.find_and_attach_gemini()
                cdp_status["tabUrl"] = tab_url
                print(f"[Bridge Server] Persistent CDP Attached to Gemini Tab: {tab_url}")
            except Exception as attach_err:
                cdp_status["tabUrl"] = f"(เชื่อมต่อ Chrome แล้ว แต่ยังไม่พบแท็บ Gemini: {attach_err})"
                print(f"[Bridge Server] CDP connected to Chrome browser; waiting for Gemini tab: {attach_err}")
            return True
        except Exception as e:
            cdp_status["connected"] = False
            cdp_status["error"] = str(e)
            print(f"[Bridge Server] CDP single connection attempt failed/timed out (no auto-retry): {e}")
            return False
        finally:
            cdp_status["connecting"] = False

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    active_ws_clients.add(ws)
    print(f"[Bridge Server] Chrome Gemini Userscript Tab Connected! (Active: {len(active_ws_clients)})")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        continue
                    msg_type = data.get("type")

                    if msg_type == "CLIENT_HELLO":
                        gem_client_info["gem"] = data.get("gem", "Gemini Web")
                        gem_client_info["url"] = data.get("url", "")
                        gem_client_info["isOnGeminiWeb"] = data.get("isOnGeminiWeb", False)
                        print(f"[Bridge Server] Client Handshake: Gem={gem_client_info.get('gem')}")

                    elif msg_type == "ANSWER":
                        req_id = data.get("id")
                        if req_id and req_id in pending_requests:
                            future = pending_requests[req_id]
                            if not future.done():
                                future.set_result(data)

                    elif msg_type == "ANSWER_ERROR":
                        req_id = data.get("id")
                        if req_id and req_id in pending_requests:
                            future = pending_requests[req_id]
                            if not future.done():
                                future.set_exception(RuntimeError(data.get("error", "Unknown error from Gemini Web")))

                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                print(f"[Bridge Server] WS Connection error: {ws.exception()}")
    finally:
        active_ws_clients.discard(ws)
        for req_id, owner in list(ws_request_owner.items()):
            if owner is ws:
                ws_request_owner.pop(req_id, None)
                fut = pending_requests.get(req_id)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError("Userscript WebSocket disconnected"))
        if not active_ws_clients:
            for req_id, future in list(pending_requests.items()):
                if not future.done():
                    future.set_exception(RuntimeError("Userscript WebSocket disconnected"))
        print(f"[Bridge Server] Chrome Gemini Userscript Tab Disconnected. (Active: {len(active_ws_clients)})")

    return ws

async def _read_json_dict(request):
    """อ่านและตรวจสอบ JSON body อย่างปลอดภัย แยก invalid JSON / payload ใหญ่เกิน / ชนิดข้อมูลผิด ออกจาก 500"""
    try:
        raw = await request.text()
    except web.HTTPRequestEntityTooLarge:
        return None, web.json_response({"ok": False, "error": "Payload ใหญ่เกินขีดจำกัด"}, status=413)
    except Exception as e:
        return None, web.json_response({"ok": False, "error": f"อ่าน Request body ไม่สำเร็จ: {e}"}, status=400)
    if not raw.strip():
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, web.json_response({"ok": False, "error": f"รูปแบบ JSON ไม่ถูกต้อง: {e.msg}"}, status=400)
    if not isinstance(data, dict):
        return None, web.json_response({"ok": False, "error": "JSON payload ต้องเป็น object"}, status=400)
    return data, None

async def handle_connect_cdp(request):
    """ให้ผู้ใช้หรือ Agent กดขอเชื่อมต่อ Chrome CDP ด้วยตนเอง (ครั้งเดียว ไม่วนซ้ำ)"""
    if cdp_client.connected:
        return web.json_response({"ok": True, "alreadyConnected": True, "cdpStatus": cdp_status})
    if cdp_status["connecting"]:
        return web.json_response({
            "ok": True,
            "connecting": True,
            "message": "กำลังรอการเชื่อมต่อ Chrome CDP อยู่ครับ",
            "cdpStatus": cdp_status,
        })

    _track_server_task(asyncio.create_task(ensure_cdp_connected()))
    return web.json_response({
        "ok": True,
        "started": True,
        "message": "ส่งคำขอเชื่อมต่อ Chrome ไป 1 ครั้งแล้ว",
        "cdpStatus": cdp_status,
    })

async def handle_get_job(request):
    """ตรวจสอบสถานะงานตาม job_id พร้อมรองรับ Long-Poll (?wait=วินาที) และแนบคำตอบ (?includeAnswer=1)"""
    job_id = str(request.match_info.get("job_id") or "").strip()
    if not job_id:
        return web.json_response({"ok": False, "error": "Missing job_id"}, status=400)

    raw_wait = request.query.get("wait", "0").strip()
    try:
        wait_sec = float(raw_wait) if raw_wait else 0.0
    except ValueError:
        return web.json_response({"ok": False, "error": "wait ต้องเป็นตัวเลข (วินาที)"}, status=400)
    wait_sec = max(0.0, min(wait_sec, 300.0))

    raw_inc = request.query.get("includeAnswer", "").strip().lower()
    include_answer = raw_inc in ("1", "true", "yes")

    try:
        job = await cdp_client.get_job(job_id, wait_sec=wait_sec, include_answer=include_answer)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"อ่านสถานะงานไม่สำเร็จ: {e}"}, status=500)

    if job is None:
        return web.json_response({"ok": False, "error": f"ไม่พบงาน jobId: {job_id}"}, status=404)

    return web.json_response({"ok": True, **job})

async def handle_eval(request):
    """รัน JS ตรวจสอบ DOM บนแท็บ Gemini ผ่านท่อ Persistent CDP เดิม"""
    if not cdp_client.connected:
        return web.json_response({"ok": False, "error": "Chrome CDP not connected"}, status=503)
    post_data, err_resp = await _read_json_dict(request)
    if err_resp is not None:
        return err_resp
    expr = post_data.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        return web.json_response({"ok": False, "error": "Missing expression"}, status=400)
    try:
        async with cdp_client.ask_lock:
            _, sid, tab_url = await cdp_client.find_and_attach_gemini()
            val = await cdp_client.eval_js(sid, expr.strip())
        return web.json_response({"ok": True, "tabUrl": tab_url, "value": val})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_extract_carbon(request):
    """ดึงคำตอบล่าสุดจากแท็บ Google Cloud Studio (Carbon) และเซฟลง latest.md ทันที"""
    if not cdp_client.connected:
        return web.json_response(
            {
                "ok": False,
                "connecting": bool(cdp_status.get("connecting")),
                "error": "กำลังรอการกด Allow บน Chrome CDP อยู่" if cdp_status.get("connecting") else "Chrome CDP not connected",
            },
            status=503,
        )
    try:
        async with cdp_client.carbon_lock:
            res = await cdp_client.extract_carbon_markdown()
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_extract_markdown(request):
    """ดึงคำตอบล่าสุดจากแท็บ Gemini และแปลงเป็น Markdown โครงสร้างสมบูรณ์ทันที พร้อมเซฟลง latest.md"""
    if not cdp_client.connected:
        return web.json_response(
            {
                "ok": False,
                "connecting": bool(cdp_status.get("connecting")),
                "error": "กำลังรอการกด Allow บน Chrome CDP อยู่" if cdp_status.get("connecting") else "Chrome CDP not connected",
            },
            status=503,
        )
    try:
        async with cdp_client.ask_lock:
            _, sid, tab_url = await cdp_client.find_and_attach_gemini()
            st = {}
            try:
                st = await cdp_client.read_state(sid)
            except Exception:
                pass
            md = await cdp_client.extract_last_message_markdown(sid)
            is_gen = bool(st.get("isGenerating"))
            job_id = None
            if md and md.strip() and not is_gen:
                hist_dir = os.path.join(os.path.dirname(__file__), "argon_history")
                cand_id = f"argon_extract_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
                meta = f"<!-- Auto-Saved by Extract Markdown at {time.strftime('%Y-%m-%d %H:%M:%S')} | Job: {cand_id} -->\n\n"
                try:
                    status_data = await asyncio.to_thread(
                        atomic_save_history, hist_dir, "argon", md.strip(), meta, cand_id
                    )
                    cdp_client._mark_job_done_mem(cand_id, "argon", status_data, md.strip())
                    job_id = cand_id
                except Exception as save_err:
                    print(f"[Bridge Server] Warning: atomic_save_history failed: {save_err}")
        return web.json_response({"ok": True, "jobId": job_id, "generating": is_gen, "tabUrl": tab_url, "markdown": md})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_upload(request):
    """API สำหรับสั่ง CDP ให้อัปโหลดไฟล์เข้าแท็บ Gemini Web ทันที"""
    if not cdp_client.connected:
        return web.json_response({"ok": False, "error": "Chrome CDP not connected"}, status=503)
    post_data, err_resp = await _read_json_dict(request)
    if err_resp is not None:
        return err_resp
    raw_files = post_data.get("files")
    if raw_files is None and post_data.get("filePath") is not None:
        raw_files = [post_data.get("filePath")]
    if isinstance(raw_files, str):
        raw_files = [raw_files]
    if not isinstance(raw_files, list):
        return web.json_response({"ok": False, "error": "files ต้องเป็น list หรือ string"}, status=400)
    files = [str(f) for f in raw_files if isinstance(f, str) and f.strip()]
    if not files:
        return web.json_response({"ok": False, "error": "Missing files"}, status=400)
    try:
        async with cdp_client.ask_lock:
            _, sid, tab_url = await cdp_client.find_and_attach_gemini()
            res = await cdp_client.upload_files_to_gemini(sid, files)
        return web.json_response({**res, "tabUrl": tab_url})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

def _parse_swarm_and_draft_params(post_data):
    """ดึงพารามิเตอร์ target_id, tab_url และ draft_only (หรือ auto_send) จาก request body"""
    target_id = post_data.get("target_id") or post_data.get("targetId")
    if isinstance(target_id, str):
        target_id = target_id.strip() or None
    else:
        target_id = None

    tab_url = post_data.get("tab_url") or post_data.get("tabUrl")
    if isinstance(tab_url, str):
        tab_url = tab_url.strip() or None
    else:
        tab_url = None

    if "draft_only" in post_data:
        draft_only = bool(post_data.get("draft_only"))
    elif "auto_send" in post_data:
        draft_only = not bool(post_data.get("auto_send"))
    else:
        draft_only = False

    return target_id, tab_url, draft_only


async def handle_tabs(request):
    """API แสดงรายการแท็บ Gemini / Cloud Studio ทั้งหมดที่เปิดอยู่ใน Chrome (พร้อม target_id และ url)"""
    if not cdp_client.connected:
        return web.json_response(
            {"ok": False, "error": f"Chrome CDP not connected ({cdp_status.get('error') or 'Not connected'})"},
            status=503,
        )
    try:
        include_all = request.query.get("all") in ("1", "true", "yes")
        raw_tabs = await cdp_client.get_tabs()
        result_tabs = []
        for t in raw_tabs:
            url = t.get("url", "")
            if "gemini.google.com" in url:
                kind = "argon"
            elif "agent-platform/studio" in url:
                kind = "carbon"
            else:
                kind = "other"
            if include_all or kind != "other":
                result_tabs.append(
                    {
                        "target_id": t.get("targetId"),
                        "url": url,
                        "title": t.get("title", ""),
                        "kind": kind,
                    }
                )
        return web.json_response({"ok": True, "count": len(result_tabs), "tabs": result_tabs})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_ask_carbon(request):
    """API สำหรับส่งคำถามและไฟล์ตรงเข้าแท็บ Google Cloud Studio (Carbon) ผ่าน Persistent CDP Session"""
    if not cdp_client.connected:
        if cdp_status["connecting"]:
            return web.json_response(
                {"ok": False, "error": "กำลังรอการเชื่อมต่อ Chrome CDP อยู่ กรุณารอสักครู่"},
                status=503,
            )
        return web.json_response(
            {"ok": False, "error": f"Chrome CDP not connected ({cdp_status.get('error') or 'Not connected'})"},
            status=503,
        )

    post_data, err_resp = await _read_json_dict(request)
    if err_resp is not None:
        return err_resp

    raw_prompt = post_data.get("prompt")
    if not isinstance(raw_prompt, str) or not raw_prompt.strip():
        return web.json_response({"ok": False, "error": "ต้องระบุ prompt"}, status=400)
    prompt = raw_prompt.strip()

    raw_files = post_data.get("files", [])
    if isinstance(raw_files, str):
        files = [raw_files] if raw_files.strip() else []
    elif isinstance(raw_files, list):
        files = [str(f) for f in raw_files if isinstance(f, str) and f.strip()]
    else:
        return web.json_response({"ok": False, "error": "files ต้องเป็น list หรือ string"}, status=400)

    raw_max_wait = post_data.get("maxWait")
    try:
        max_wait = max(30.0, min(float(raw_max_wait), 900.0)) if raw_max_wait is not None else 600.0
    except (TypeError, ValueError):
        max_wait = 600.0

    target_id, tab_url, draft_only = _parse_swarm_and_draft_params(post_data)

    try:
        res = await asyncio.shield(
            _track_server_task(
                asyncio.create_task(
                    cdp_client.ask_carbon_tab(
                        prompt_text=prompt,
                        files=files,
                        timeout=max_wait,
                        target_id=target_id,
                        tab_url=tab_url,
                        draft_only=draft_only,
                    )
                )
            )
        )
        status_code = 200 if res.get("ok") else 400
        return web.json_response(res, status=status_code)
    except CDPError as e:
        return web.json_response({"ok": False, "error": f"CDP Error: {e}"}, status=502)
    except Exception as e:
        print(f"ask_carbon failed: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_ask(request):
    """API สำหรับให้ Antigravity หรือโปรแกรมภายนอกยิงคำถามเข้ามาหา Gemini Web (Argon)"""
    post_data, err_resp = await _read_json_dict(request)
    if err_resp is not None:
        return err_resp

    raw_prompt = post_data.get("prompt")
    if not isinstance(raw_prompt, str) or not raw_prompt.strip():
        return web.json_response({"ok": False, "error": "ต้องระบุ prompt"}, status=400)
    prompt = raw_prompt.strip()

    web_model = post_data.get("webModel") or post_data.get("model")
    thinking_level = post_data.get("thinkingLevel")
    raw_files = post_data.get("files", [])
    if isinstance(raw_files, str):
        files = [raw_files] if raw_files.strip() else []
    elif isinstance(raw_files, list):
        files = [str(f) for f in raw_files if isinstance(f, str) and f.strip()]
    else:
        return web.json_response({"ok": False, "error": "files ต้องเป็น list หรือ string"}, status=400)

    fire_and_forget = bool(post_data.get("fireAndForget", False))
    target_id, tab_url, draft_only = _parse_swarm_and_draft_params(post_data)
    raw_max_wait = post_data.get("maxWait")
    try:
        max_wait = max(30.0, min(float(raw_max_wait), 900.0)) if raw_max_wait is not None else 300.0
    except (TypeError, ValueError):
        max_wait = 300.0

    # 1. ใช้ท่อ Persistent Chrome CDP โดยตรงเป็นอันดับแรก
    if cdp_client.connected:
        try:
            coro = cdp_client.ask_gemini_tab(
                prompt,
                web_model=web_model,
                thinking_level=thinking_level,
                files=files,
                max_wait=max_wait,
                fire_and_forget=fire_and_forget,
                target_id=target_id,
                tab_url=tab_url,
                draft_only=draft_only,
            )
            if fire_and_forget:
                res = await asyncio.shield(_track_server_task(asyncio.create_task(coro)))
            else:
                res = await coro
            if res.get("limit"):
                status_code = 422
            elif res.get("ok"):
                status_code = 200
            else:
                status_code = 504 if "Timeout" in str(res.get("error", "")) else 500
            return web.json_response(res, status=status_code)
        except CDPError as e:
            return web.json_response({"ok": False, "error": f"CDP Error: {e}"}, status=502)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"CDP Error: {e}"}, status=500)

    # 2. ถ้า CDP ไม่ได้เชื่อม ให้ใช้ Userscript WebSocket
    if active_ws_clients:
        req_id = "req_" + uuid.uuid4().hex[:8]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending_requests[req_id] = future

        payload = json.dumps(
            {
                "type": "ASK",
                "id": req_id,
                "prompt": prompt,
                "targetGem": "Gemini Web",
            },
            ensure_ascii=False,
        )

        sent_any = False
        for client in list(active_ws_clients):
            try:
                await client.send_str(payload)
                ws_request_owner[req_id] = client
                sent_any = True
                break
            except Exception:
                continue

        if not sent_any:
            pending_requests.pop(req_id, None)
            ws_request_owner.pop(req_id, None)
            return web.json_response({"ok": False, "error": "ไม่สามารถส่งข้อความไปยัง Userscript WebSocket ได้"}, status=503)

        try:
            result_data = await asyncio.wait_for(future, timeout=180.0)
            return web.json_response({
                "ok": True,
                "gem": "Gemini Web",
                "answer": result_data.get("answer", ""),
                "elapsedMs": result_data.get("elapsedMs", 0),
            })
        except asyncio.TimeoutError:
            return web.json_response(
                {"ok": False, "error": "Timeout รอคำตอบจาก Gemini Web เกิน 180 วินาที"},
                status=504,
            )
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        finally:
            pending_requests.pop(req_id, None)
            ws_request_owner.pop(req_id, None)

    # 3. ยังไม่ได้เชื่อมต่อทั้งสองช่องทาง
    if cdp_status["connecting"]:
        return web.json_response(
            {
                "ok": False,
                "error": "กำลังรอคุณกดปุ่ม 'Allow / ยอมรับ' บนหน้าต่าง Chrome เพื่อให้ท่อ CDP เชื่อมต่อสำเร็จ",
            },
            status=503,
        )

    return web.json_response(
        {
            "ok": False,
            "error": (
                f"ยังไม่ได้เชื่อมต่อกับ Chrome (CDP: {cdp_status.get('error') or 'Not connected'}) "
                f"กรุณากดปุ่ม 'เชื่อมต่อ Chrome CDP' ที่ http://localhost:{PORT}"
            ),
        },
        status=503,
    )

async def handle_status(request):
    """ส่งสถานะการเชื่อมต่อของ Chrome CDP และ Userscript"""
    is_cdp = bool(cdp_client.connected)
    cdp_status["connected"] = is_cdp
    mode = "CDP Direct (Persistent)" if is_cdp else ("Userscript WS" if active_ws_clients else "Waiting")
    return web.json_response({
        "ok": True,
        "connected": (is_cdp or len(active_ws_clients) > 0),
        "mode": mode,
        "clientsCount": len(active_ws_clients),
        "targetGem": "Gemini Web",
        "gemInfo": gem_client_info,
        "cdpStatus": cdp_status,
        "lastDisconnectReason": cdp_client.last_disconnect_reason,
    })

def _read_userscript_sync(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

INDEX_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <title>Antigravity ↔ Gemini Web Bridge</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #0f1117; color: #f1f5f9; min-height: 100vh; padding: 24px; font-family: system-ui, -apple-system, sans-serif; }
    .container { max-width: 672px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }
    .header { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .logo { width: 40px; height: 40px; border-radius: 12px; background: linear-gradient(135deg, #ec4899, #e11d48); display: flex; align-items: center; justify-content: center; font-size: 20px; font-weight: bold; box-shadow: 0 10px 15px -3px rgba(236,72,153,0.3); }
    .title { font-size: 18px; font-weight: 700; color: #fff; margin: 0; display: flex; align-items: center; gap: 8px; }
    .pill { font-size: 12px; background: rgba(236,72,153,0.2); color: #f9a8d4; border: 1px solid rgba(236,72,153,0.4); padding: 2px 8px; border-radius: 9999px; font-family: ui-monospace, monospace; }
    .subtitle { font-size: 12px; color: #94a3b8; margin: 2px 0 0; }
    .badge { padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 600; border: 1px solid; }
    .badge-ok { background: rgba(16,185,129,0.2); color: #6ee7b7; border-color: rgba(16,185,129,0.4); }
    .badge-warn { background: rgba(245,158,11,0.2); color: #fcd34d; border-color: rgba(245,158,11,0.4); }
    .badge-err { background: rgba(244,63,94,0.2); color: #fda4af; border-color: rgba(244,63,94,0.4); }
    .card { padding: 20px; border-radius: 16px; background: #161822; border: 1px solid #1e293b; display: flex; flex-direction: column; gap: 12px; }
    .card-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
    .h2-cyan { font-size: 14px; font-weight: 700; color: #67e8f9; margin: 0; }
    .h2-white { font-size: 14px; font-weight: 700; color: #fff; margin: 0; }
    .btn-cyan { padding: 6px 12px; border-radius: 12px; background: rgba(8,145,178,0.3); color: #a5f3fc; border: 1px solid rgba(6,182,212,0.4); font-size: 12px; font-weight: 700; cursor: pointer; }
    .btn-cyan:hover { background: rgba(8,145,178,0.5); }
    .btn-pink { padding: 8px 16px; border-radius: 12px; background: linear-gradient(90deg, #ec4899, #e11d48); color: #fff; border: none; font-size: 12px; font-weight: 700; cursor: pointer; align-self: flex-start; }
    .btn-pink:disabled, .btn-cyan:disabled { opacity: 0.6; cursor: not-allowed; }
    .detail-box { padding: 12px; border-radius: 12px; background: rgba(15,23,42,0.8); border: 1px solid #1e293b; font-size: 12px; font-family: ui-monospace, monospace; color: #cbd5e1; display: flex; flex-direction: column; gap: 4px; }
    .hint { font-size: 11px; color: #94a3b8; margin: 0; }
    .textarea { width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 12px; padding: 12px; font-size: 14px; color: #f1f5f9; outline: none; font-family: inherit; }
    .textarea:focus { border-color: #22d3ee; }
    .output { padding: 16px; border-radius: 12px; background: #11131c; border: 1px solid #1e293b; font-size: 12px; font-family: ui-monospace, monospace; display: flex; flex-direction: column; gap: 4px; }
    .hidden { display: none; }
    .text-cyan { color: #67e8f9; }
    .text-ok { color: #34d399; font-weight: 700; }
    .text-warn { color: #fcd34d; }
    .text-err { color: #fb7185; font-weight: 700; }
    .text-ans { color: #e2e8f0; white-space: pre-wrap; font-family: system-ui, sans-serif; font-size: 14px; margin-top: 4px; }
    .truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <div class="logo">🥁</div>
        <div>
          <h1 class="title">
            Antigravity ↔ Gemini Web Bridge
            <span class="pill">v4.0 OSS</span>
          </h1>
          <p class="subtitle">เชื่อมต่อตรงแบบ Persistent Single-Connection (กด Allow เพียงครั้งเดียว)</p>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <button id="langToggleBtn" type="button" class="btn-cyan" style="padding:6px 12px;border-radius:9999px;">🌐 EN / TH</button>
        <div id="statusBadge" class="badge badge-err">
          🔴 กำลังตรวจสอบสถานะ...
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <h2 class="h2-cyan">🔌 สถานะท่อ Chrome Remote Debugging (Persistent CDP)</h2>
        <button id="connectCdpBtn" type="button" class="btn-cyan">
          🔌 เชื่อมต่อ Chrome CDP (กดเมื่อต้องการต่อใหม่)
        </button>
      </div>
      <div id="cdpDetailBox" class="detail-box">
        <div>กำลังอ่านสถานะ...</div>
      </div>
      <p class="hint">💡 ระบบจะส่งคำขอเชื่อมต่อไปที่ Chrome เพียงครั้งเดียว (ไม่ยิงซ้ำให้รำคาญ) หากหน้าต่าง Chrome มีป๊อปอัปถาม ให้กดปุ่ม <b>Allow / ยอมรับ</b> ได้เลยครับ</p>
    </div>

    <div class="card">
      <h2 class="h2-white">🧪 ทดสอบส่งคำถามหา Gemini Web (Argon):</h2>
      <textarea id="testPrompt" rows="3" placeholder="พิมพ์ข้อความทดสอบส่งไปถาม Gemini Web ในหน้า Gemini Web..." class="textarea"></textarea>
      <button id="testBtn" class="btn-pink">🚀 ส่งข้อความไปที่แท็บ Gemini Web</button>
      <div id="testOutput" class="output hidden"></div>
    </div>
  </div>

  <script>
    const TOKEN = "__BRIDGE_TOKEN__";
    let statusInFlight = false;
    let currentLang = localStorage.getItem('bridge_lang') || ((navigator.language || '').toLowerCase().startsWith('th') ? 'th' : 'en');
    const I18N = {
      th: {
        subtitle: 'เชื่อมต่อตรงแบบ Persistent Single-Connection (กด Allow เพียงครั้งเดียว)',
        cdpTitle: '🔌 สถานะท่อ Chrome Remote Debugging (Persistent CDP)',
        connectBtn: '🔌 เชื่อมต่อ Chrome CDP (กดเมื่อต้องการต่อใหม่)',
        hint: '💡 ระบบจะส่งคำขอเชื่อมต่อไปที่ Chrome เพียงครั้งเดียว หากหน้าต่าง Chrome มีป๊อปอัปถาม ให้กด Allow / ยอมรับ ได้เลยครับ',
        testTitle: '🧪 ทดสอบส่งคำถามหา Gemini Web (Argon):',
        placeholder: 'พิมพ์ข้อความทดสอบส่งไปถาม Gemini Web...',
        sendBtn: '🚀 ส่งข้อความไปที่แท็บ Gemini Web',
        toggle: '🇹🇭 ภาษาไทย (TH)'
      },
      en: {
        subtitle: 'Direct Persistent Single-Connection (One-time Allow prompt)',
        cdpTitle: '🔌 Chrome Remote Debugging Status (Persistent CDP)',
        connectBtn: '🔌 Reconnect Chrome CDP',
        hint: '💡 Connects to Chrome once without nagging. Click "Allow" if Chrome shows a debugging permission prompt.',
        testTitle: '🧪 Test Dispatch to Gemini Web (Argon):',
        placeholder: 'Type a test prompt to send to Gemini Web...',
        sendBtn: '🚀 Send Prompt to Gemini Web Tab',
        toggle: '🇬🇧 English (EN)'
      }
    };
    function applyLocale(lang) {
      currentLang = lang;
      localStorage.setItem('bridge_lang', lang);
      const t = I18N[lang] || I18N.en;
      const sub = document.querySelector('.subtitle'); if (sub) sub.textContent = t.subtitle;
      const h2c = document.querySelector('.h2-cyan'); if (h2c) h2c.textContent = t.cdpTitle;
      const cbtn = document.getElementById('connectCdpBtn'); if (cbtn) cbtn.textContent = t.connectBtn;
      const hint = document.querySelector('.hint'); if (hint) hint.textContent = t.hint;
      const h2w = document.querySelector('.h2-white'); if (h2w) h2w.textContent = t.testTitle;
      const tp = document.getElementById('testPrompt'); if (tp) tp.placeholder = t.placeholder;
      const tb = document.getElementById('testBtn'); if (tb && !tb.disabled) tb.textContent = t.sendBtn;
      const lbtn = document.getElementById('langToggleBtn'); if (lbtn) lbtn.textContent = t.toggle;
    }
    document.getElementById('langToggleBtn')?.addEventListener('click', () => applyLocale(currentLang === 'th' ? 'en' : 'th'));
    applyLocale(currentLang);

    function mkLine(prefix, value, valueClass, rowClass) {
      const row = document.createElement('div');
      if (rowClass) row.className = rowClass;
      if (prefix) row.appendChild(document.createTextNode(prefix));
      if (value !== undefined && value !== null) {
        const sp = document.createElement('span');
        if (valueClass) sp.className = valueClass;
        sp.textContent = String(value);
        row.appendChild(sp);
      }
      return row;
    }

    async function checkStatus() {
      if (document.hidden || statusInFlight) return;
      statusInFlight = true;
      try {
        const res = await fetch('/status', { headers: { 'X-Bridge-Token': TOKEN } });
        const d = await res.json();
        const badge = document.getElementById('statusBadge');
        const cdpBox = document.getElementById('cdpDetailBox');
        const cdp = d.cdpStatus || {};

        if (d.connected) {
          badge.className = 'badge badge-ok';
          badge.textContent = '🟢 เชื่อมต่อแล้ว (' + (d.mode || 'Connected') + ')';
        } else if (cdp.connecting) {
          badge.className = 'badge badge-warn';
          badge.textContent = '🟡 รอคุณกด Allow บนหน้าต่าง Chrome...';
        } else {
          badge.className = 'badge badge-err';
          badge.textContent = '🔴 ยังไม่เชื่อมต่อ';
        }

        const rows = [
          mkLine('Mode: ', d.mode || '-', 'text-cyan'),
          mkLine('CDP Connected: ', String(!!cdp.connected) + (cdp.connecting ? ' (กำลังรอ Allow...)' : ''), cdp.connected ? 'text-ok' : 'text-err')
        ];
        if (cdp.tabUrl) rows.push(mkLine('Tab: ', cdp.tabUrl, '', 'truncate'));
        if (cdp.error) rows.push(mkLine('Error: ', cdp.error, 'text-err'));
        cdpBox.replaceChildren(...rows);
      } catch (e) {} finally {
        statusInFlight = false;
      }
    }
    setInterval(checkStatus, 2000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) checkStatus(); });
    checkStatus();

    document.getElementById('connectCdpBtn').addEventListener('click', async () => {
      const btn = document.getElementById('connectCdpBtn');
      btn.disabled = true;
      try {
        const res = await fetch('/connect-cdp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': TOKEN },
          body: '{}'
        });
        const d = await res.json();
        await checkStatus();
        if (d.message) {
          const box = document.getElementById('cdpDetailBox');
          box.appendChild(mkLine('', d.message, 'text-warn'));
        }
      } catch (e) {}
      setTimeout(() => { btn.disabled = false; }, 2000);
    });

    document.getElementById('testBtn').addEventListener('click', async () => {
      const prompt = document.getElementById('testPrompt').value.trim();
      if (!prompt) return;
      const btn = document.getElementById('testBtn');
      const out = document.getElementById('testOutput');
      btn.disabled = true;
      btn.textContent = 'กำลังรอคำตอบจาก Gemini Web... ⏳';
      out.classList.remove('hidden');
      out.textContent = '🚀 ส่งคำถามไปยังหน้าต่าง Chrome แล้ว กำลังรอข้อความตอบกลับจาก Gemini Web...';

      try {
        const res = await fetch('/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Bridge-Token': TOKEN },
          body: JSON.stringify({ prompt })
        });
        const d = await res.json();
        if (d.ok) {
          const head = mkLine('', '✅ คำตอบจาก Gemini Web (ใช้เวลา ' + ((d.elapsedMs || 0) / 1000).toFixed(1) + 's):', 'text-ok');
          const ans = mkLine('', d.answer || '', 'text-ans');
          out.replaceChildren(head, ans);
        } else {
          out.replaceChildren(mkLine('❌ Error: ', d.error || 'Unknown error', 'text-err'));
        }
      } catch (e) {
        out.replaceChildren(mkLine('', '❌ ไม่สามารถเชื่อมต่อกับ Bridge Server ได้', 'text-err'));
      } finally {
        btn.disabled = false;
        btn.textContent = '🚀 ส่งข้อความไปที่แท็บ Gemini Web';
      }
    });
  </script>
</body>
</html>
"""

async def handle_index(request):
    """หน้าจอ Dashboard ควบคุมและมอนิเตอร์สถานะการเชื่อมต่อกับ Gemini Web (Hardened CSP + Token)"""
    return html_response(INDEX_HTML, TOKEN, allow_tailwind_cdn=False)

async def main():
    app = web.Application(
        middlewares=[make_security_middleware(PORT, TOKEN)],
        client_max_size=32 * 1024 * 1024,
    )
    app.router.add_get("/", handle_index)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/tabs", handle_tabs)
    app.router.add_get("/jobs/{job_id}", handle_get_job)
    app.router.add_post("/ask", handle_ask)
    app.router.add_post("/ask_carbon", handle_ask_carbon)
    app.router.add_post("/upload", handle_upload)
    if os.environ.get("BRIDGE_ENABLE_EVAL") == "1":
        app.router.add_post("/eval", handle_eval)
    app.router.add_post("/extract_markdown", handle_extract_markdown)
    app.router.add_get("/extract_markdown", handle_extract_markdown)
    app.router.add_post("/extract_carbon", handle_extract_carbon)
    app.router.add_get("/extract_carbon", handle_extract_carbon)
    app.router.add_post("/connect-cdp", handle_connect_cdp)
    app.router.add_get("/ws", ws_handler)

    runner = web.AppRunner(app, handler_cancellation=True)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", PORT, reuse_address=False)
        await site.start()
        _persist_bridge_token(PORT, TOKEN)

        print("===========================================================")
        print(f"🚀 Gemini Web Persistent Bridge Server (v4.0) on port {PORT}")
        print(f"👉 Web Panel: http://127.0.0.1:{PORT}")
        print(f"👉 Job API:   GET http://127.0.0.1:{PORT}/jobs/{{job_id}}?wait=30&includeAnswer=1")
        print("👉 Security:  Middleware Token Auth + Host/Origin Guard Active")
        print("👉 Triggering SINGLE CDP connection to Chrome (timeout 60s)...")
        print("===========================================================")

        cdp_task = _track_server_task(asyncio.create_task(ensure_cdp_connected()))
        while True:
            await asyncio.sleep(3600)
    finally:
        for t in list(server_bg_tasks):
            if not t.done():
                t.cancel()
        if server_bg_tasks:
            await asyncio.gather(*list(server_bg_tasks), return_exceptions=True)
        try:
            await cdp_client.close()
        except Exception:
            pass
        await runner.cleanup()
        _cleanup_bridge_token()

def _check_for_updates_async():
    """Check GitHub VERSION file in background without blocking server startup."""
    import threading
    import urllib.request

    def _worker():
        try:
            ver_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
            local_ver = open(ver_file, "r", encoding="utf-8").read().strip() if os.path.exists(ver_file) else "0.0.0"
            req = urllib.request.Request(
                "https://raw.githubusercontent.com/cirrus2026-dev/vertex-studio-oss/main/VERSION",
                headers={"User-Agent": "vertex-studio-oss-updater"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                remote_ver = resp.read().decode("utf-8").strip()
            if remote_ver and remote_ver != local_ver:
                print(f"\n🔔 [Update Available] Local: v{local_ver} -> GitHub: v{remote_ver}")
                print("👉 Run update.bat to update automatically (your tokens & history are preserved).\n")
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


if __name__ == "__main__":
    _check_for_updates_async()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping Bridge Server...")
