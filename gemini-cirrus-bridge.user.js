// ==UserScript==
// @name         Antigravity - Gemini Web Control Bridge
// @namespace    https://antigravity.google.com/
// @version      1.0
// @description  เชื่อมต่อ Gemini Web กับ Antigravity ผ่าน Local WebSocket Bridge
// @author       Antigravity
// @match        https://gemini.google.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const WS_URL = 'ws://localhost:8765';
  const TARGET_GEM_NAME = 'Web Control';
  let ws = null;
  let reconnectTimer = null;
  let isGenerating = false;
  let activeRequestId = null;

  console.log('[Antigravity Bridge] Userscript Loaded for:', TARGET_GEM_NAME);

  // สร้าง Floating Status Badge ที่มุมขวาบนของหน้าเว็บ
  const badge = document.createElement('div');
  badge.id = 'antigravity-bridge-badge';
  badge.style.cssText = `
    position: fixed;
    top: 14px;
    right: 80px;
    z-index: 999999;
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 12px;
    font-family: system-ui, sans-serif;
    font-weight: 600;
    color: #fff;
    background: #e11d48;
    box-shadow: 0 4px 14px rgba(0,0,0,0.3);
    display: flex;
    align-items: center;
    gap: 8px;
    transition: all 0.3s ease;
    cursor: pointer;
    user-select: none;
  `;
  badge.innerHTML = `<span>🔴</span><span>Antigravity: ตัดการเชื่อมต่อ</span>`;
  document.body.appendChild(badge);

  function updateBadge(status, text, bg) {
    badge.style.background = bg;
    badge.innerHTML = `<span>${status}</span><span>${text}</span>`;
  }

  // ฟังก์ชันตรวจสอบว่าปัจจุบันเปิดอยู่ในหน้า Gemini Web หรือไม่
  function checkIsOnCirrusGem() {
    const pageText = document.body.innerText || '';
    const title = document.title || '';
    const hasCirrusText = title.includes('Gemini') || pageText.includes('Gemini') || pageText.includes('Web Control');
    return hasCirrusText;
  }

  // ฟังก์ชันเชื่อมต่อ WebSocket Bridge
  function connectWebSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    updateBadge('🟡', 'Antigravity: กำลังเชื่อมต่อ...', '#d97706');

    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      updateBadge('🔴', 'Antigravity: ไม่พบ Server (พอร์ต 8765)', '#e11d48');
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      console.log('[Antigravity Bridge] Connected to Python WebSocket Bridge');
      updateBadge('🟢', `Antigravity ↔ Web Control (พร้อมใช้งาน)`, '#059669');
      if (reconnectTimer) clearTimeout(reconnectTimer);

      ws.send(JSON.stringify({
        type: 'CLIENT_HELLO',
        client: 'Tampermonkey-Gemini-Web',
        gem: TARGET_GEM_NAME,
        url: window.location.href,
        isOnCirrus: checkIsOnCirrusGem()
      }));
    };

    ws.onmessage = async (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'ASK') {
          handleIncomingAsk(data);
        } else if (data.type === 'PING') {
          ws.send(JSON.stringify({ type: 'PONG', timestamp: Date.now() }));
        }
      } catch (err) {
        console.error('[Antigravity Bridge] Error parsing message:', err);
      }
    };

    ws.onclose = () => {
      console.warn('[Antigravity Bridge] Disconnected');
      updateBadge('🔴', 'Antigravity: หลุดการเชื่อมต่อ', '#e11d48');
      scheduleReconnect();
    };

    ws.onerror = (err) => {
      console.error('[Antigravity Bridge] WS Error:', err);
      updateBadge('🔴', 'Antigravity: Error พอร์ต 8765', '#e11d48');
      ws.close();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectWebSocket, 3000);
  }

  // ค้นหากล่องพิมพ์ข้อความใน Gemini
  function findInputBox() {
    return document.querySelector('rich-textarea p, rich-textarea div[contenteditable="true"], div[contenteditable="true"][role="textbox"], textarea');
  }

  // ค้นหาปุ่มส่ง
  function findSendButton() {
    return document.querySelector('button[aria-label*="ส่ง"], button[aria-label*="Send"], .send-button, button.send-button-container, button[data-test-id="send-button"]');
  }

  // ค้นหาปุ่ม Stop Generation
  function findStopButton() {
    return document.querySelector('button[aria-label*="หยุด"], button[aria-label*="Stop"], button.stop-button, .streaming-indicator');
  }

  // ดึงข้อความตอบกลับล่าสุดของ Gemini
  function getLatestResponseText() {
    const bubbles = document.querySelectorAll('message-content, .model-response-text, .response-container-content, [data-test-id="model-response"]');
    if (bubbles.length === 0) return '';
    const lastBubble = bubbles[bubbles.length - 1];
    return (lastBubble.innerText || lastBubble.textContent || '').trim();
  }

  // จัดการเมื่อได้รับคำถามจาก Antigravity
  async function handleIncomingAsk(data) {
    const requestId = data.id || ('req_' + Date.now());
    const promptText = data.prompt || '';
    activeRequestId = requestId;

    console.log('[Antigravity Bridge] Received Prompt:', promptText);
    updateBadge('⚡', 'Web Control: กำลังพิมพ์คำถาม...', '#2563eb');

    const inputEl = findInputBox();
    if (!inputEl) {
      ws.send(JSON.stringify({
        type: 'ANSWER_ERROR',
        id: requestId,
        error: 'ไม่พบช่องพิมพ์ข้อความในหน้า Gemini Web (กรุณาเปิดแท็บแชททิ้งไว้)'
      }));
      updateBadge('🟢', `Antigravity ↔ Web Control (พร้อมใช้งาน)`, '#059669');
      return;
    }

    // ใส่ข้อความลงกล่องพิมพ์
    inputEl.focus();
    inputEl.textContent = promptText;
    inputEl.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true }));
    inputEl.dispatchEvent(new Event('change', { bubbles: true }));

    await new Promise(r => setTimeout(r, 400));

    const sendBtn = findSendButton();
    if (sendBtn && !sendBtn.disabled) {
      sendBtn.click();
    } else {
      // Fallback: กดปุ่ม Enter
      inputEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, mechanicalKeyCode: 13, which: 13, bubbles: true }));
    }

    updateBadge('⏳', 'Web Control: กำลังคิดและตอบกลับ...', '#7c3aed');
    isGenerating = true;

    // เริ่มสังเกตการณ์คำตอบ (MutationObserver)
    waitForGeminiResponse(requestId);
  }

  function waitForGeminiResponse(requestId) {
    const startTime = Date.now();
    let initialResponseCount = document.querySelectorAll('message-content, .model-response-text, .response-container-content').length;
    let hasStartedStreaming = false;

    const checkInterval = setInterval(() => {
      const stopBtn = findStopButton();
      const currentBubbles = document.querySelectorAll('message-content, .model-response-text, .response-container-content');

      if (stopBtn) {
        hasStartedStreaming = true;
      }

      // ตรวจสอบว่ามีกล่องคำตอบใหม่โผล่ขึ้นมาแล้ว
      if (currentBubbles.length > initialResponseCount) {
        hasStartedStreaming = true;
      }

      // ถ้าเริ่มสตรีมแล้ว และปุ่ม Stop หายไป (หรือผ่านไป 2 วินาทีหลังจากเนื้อหาหยุดขยับ)
      const elapsed = Date.now() - startTime;
      if (hasStartedStreaming && !stopBtn && elapsed > 2500) {
        clearInterval(checkInterval);
        isGenerating = false;

        const finalText = getLatestResponseText();
        console.log('[Antigravity Bridge] Finished generating answer:', finalText.slice(0, 100));

        ws.send(JSON.stringify({
          type: 'ANSWER',
          id: requestId,
          gem: TARGET_GEM_NAME,
          answer: finalText,
          elapsedMs: elapsed
        }));

        updateBadge('🟢', `Antigravity ↔ Web Control (ตอบเสร็จแล้ว)`, '#059669');
        setTimeout(() => {
          updateBadge('🟢', `Antigravity ↔ Web Control (พร้อมใช้งาน)`, '#059669');
        }, 2000);
      }

      // Timeout ป้องกันค้าง (เกิน 3 นาที)
      if (elapsed > 180000) {
        clearInterval(checkInterval);
        isGenerating = false;
        ws.send(JSON.stringify({
          type: 'ANSWER_ERROR',
          id: requestId,
          error: 'Gemini Web Response Timeout (เกิน 3 นาที)'
        }));
        updateBadge('🟢', `Antigravity ↔ Web Control (พร้อมใช้งาน)`, '#059669');
      }
    }, 400);
  }

  // เริ่มเชื่อมต่อ
  setTimeout(connectWebSocket, 1500);

})();
