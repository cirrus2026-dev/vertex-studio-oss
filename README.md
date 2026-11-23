<div align="center">

**🌐 Language:** [🇬🇧 English](./README.md) | [🇹🇭 ภาษาไทย](./README.th.md)

# ⚡ Vertex AI Studio & Gemini Web Bridge (v4.0 OSS)

**Zero-Trust Bridge connecting AI Coding Agents (Antigravity, Cursor, Claude Code) directly to Gemini 4 Web & Google Cloud Studio — Fast, Free, and API-Key-Free.**

[![Release](https://img.shields.io/badge/Release-v4.0.0-blue?style=for-the-badge)](./CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![MCP Ready](https://img.shields.io/badge/MCP-Protocol%20Ready-8A2BE2?style=for-the-badge)](./bridge_mcp_server.py)
[![Chrome CDP](https://img.shields.io/badge/Chrome-CDP%20Bridge-4285F4?style=for-the-badge&logo=googlechrome&logoColor=white)](./gemini_bridge_server.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](./LICENSE)

*Direct Chrome DevTools Protocol (Port 9222 ↔ Bridge Port 8765) • Native MCP Server • Smart Code Bundler (.txt) • Auto-Locale UI (🇹🇭/🇬🇧) • Visual Cockpit Dashboard (Port 5005)*

</div>

---

## 🧭 Architecture Overview (v4.0)

```mermaid
flowchart LR
    Agent["🤖 AI Coding Agent<br/>(Antigravity / Cursor / Claude)"] -->|MCP stdio or Skill CLI| Dispatch["⚡ MCP / Dispatch<br/>(Fast Fire-and-Forget)"]
    Dispatch -->|HTTP :8765 + Token Auth| Bridge["🌉 Persistent Bridge Server<br/>(gemini_bridge_server.py)"]
    Bridge -->|Chrome CDP :9222| Chrome["🌐 Chrome AI Profile<br/>(Gemini Web / Cloud Studio)"]
    Chrome -->|Atomic Auto-Save| Disk[("💾 Job Ticket & History<br/>latest.status.json + latest.md")]
    Disk -->|Read Result / Search & Replace| Agent
    User["👨‍💻 Visual Cockpit (Optional)"] -->|HTTP :5005| Dash["🎛️ Dashboard UI (server.py)"]
```

---

## 🌟 What's New in v4.0 & Core Highlights

| Feature | Description |
| :--- | :--- |
| 🔌 **Native MCP Server** | Built-in Model Context Protocol server (`bridge_mcp_server.py`) with 5 standard tools: `ask_argon`, `ask_carbon`, `check_job`, `bridge_status`, and `extract_latest`. |
| ⚡ **Fast Fire-and-Forget** | Dispatches prompts and local file attachments to the browser in under 2 seconds via Persistent Bridge (`:8765`) without blocking the agent. |
| 🎫 **Atomic Job Tickets** | Persists job states (`latest.status.json`) and answers (`latest.md`) using atomic writes (`UUID + fsync`) to prevent file corruption or stale reads. |
| 📦 **Smart Code Bundler** | Consolidates project files into a single `.txt` bundle with 3 modes (`audit`, `feature`, `diff`), reducing token overhead by **40% – 80%**. |
| 🛡️ **Search & Replace Guardrails** | Supports dynamic markdown code fences (4–5 backticks) so nested code blocks and `<<<<<<< SEARCH / ======= / >>>>>>>` patches never break. |
| 🌐 **Auto-Locale UI (TH/EN)** | Automatically detects browser language (`navigator.language`) with an instant `🇹🇭 TH / 🇬🇧 EN` toggle on the Bridge Control Panel. |
| 🎛️ **Visual Cockpit (Optional)** | Side-by-side green/red diff preview dashboard (`server.py` on port `5005`) with Zero-Trust Read/Write scope separation. |

---

## 📦 Quick Installation

### 1. Prerequisites
- **Python 3.10+**
- **Google Chrome**

### 2. Clone & Install Dependencies
```bash
git clone https://github.com/cirrus2026-dev/vertex-studio-oss.git
cd vertex-studio-oss
pip install -r requirements.txt
```

### 3. Environment Configuration (Optional)
```bash
cp .env.example .env
```

---

## 🚀 Quick Start

### Option 1: Headless AI Bridge (Recommended for Antigravity / Cursor / CLI)
Launch an isolated AI Chrome Profile (eliminating repeated "Allow Connection" popups) alongside the Port `8765` Bridge in one click:
```bat
start_ai_bridge.bat
```
Or launch manually:
1. Start Chrome with Remote Debugging (`9222`):
   ```bash
   chrome.exe --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\.gemini\AI-Chrome-Profile" "https://gemini.google.com/app"
   ```
2. Start the Persistent Bridge Server (`8765`):
   ```bash
   python gemini_bridge_server.py
   ```
   Open the Bridge Control Panel at `http://127.0.0.1:8765` (auto-switches between English & Thai).

---

### Option 2: Connect via Native MCP Server (`bridge_mcp_server.py`)
Add this entry to your AI Coding Agent's `mcp_config.json`:
```json
{
  "mcpServers": {
    "vertex-bridge": {
      "command": "python",
      "args": ["C:/path/to/vertex-studio-oss/bridge_mcp_server.py"]
    }
  }
}
```
**Available MCP Tools:**
- `ask_argon` — Dispatch prompt & files to Gemini Web (Argon)
- `ask_carbon` — Dispatch prompt & files to Google Cloud Studio (Carbon)
- `check_job` — Poll job status and retrieve the saved answer by `job_id`
- `bridge_status` — Inspect Chrome CDP & Bridge health
- `extract_latest` — Extract the latest response directly from the browser DOM

---

### Option 3: Agent Skills (`/ask-argon` & `/ask-carbon`)
Copy `skills/ask-argon` and `skills/ask-carbon` into your agent's skills directory:

| Skill Command | Target Web Interface | Best For |
| :--- | :--- | :--- |
| **`/ask-argon`** | **Gemini Web** (`https://gemini.google.com/app`) | Free everyday web usage, quick code reviews & architecture Q&A |
| **`/ask-carbon`** | **Google Cloud Studio** (`console.cloud.google.com`) | Vertex AI Studio Multimodal / GCP project workflows |

```bash
# Dispatch to Gemini Web (Argon)
python skills/ask-argon/scripts/dispatch.py --prompt "Audit this file for bugs" --files app.py

# Dispatch to Cloud Studio (Carbon)
python skills/ask-carbon/scripts/dispatch_carbon.py --prompt "Design a clean API architecture" --files server.py
```

---

### Option 4: Smart Code Bundler CLI (`bundle_project.py`)
```bash
# 1. Audit Mode: Full implementation (100% code) with whitespace cleanup & secret filtering
python bundle_project.py --dir ./my-project --out bundle.txt --mode audit

# 2. Feature Mode: AST Outline only (Class / Async Def / Function / Docstring) — saves 70-80% tokens
python bundle_project.py --dir ./my-project --out bundle.txt --mode feature

# 3. Diff Mode: Latest Git Diff only — saves up to 95% tokens
python bundle_project.py --dir ./my-project --mode diff
```

---

### Option 5: Visual Cockpit Dashboard (Port 5005)
```bash
python server.py
```
Or double-click `run.bat` on Windows and open `http://localhost:5005`.

---

## 🧩 Extending to Other AI Web Portals (ChatGPT, Claude, AI Pass, etc.)

By default, the Port `8765` Bridge targets **Gemini Web** (`gemini.google.com`) and **Google Cloud Studio**. However, you can extend it to automate **any web-based AI chat interface** in minutes without manually writing selector code—simply instruct your AI Coding Agent (Antigravity / Cursor / Claude Code) to inspect the DOM and wire it up.

<details>
<summary><b>📖 Click to expand: 3-Step Agent Prompt Guide to Add Any Custom AI Web Tab</b></summary>

### Prerequisites
1. Open this repository in your AI Coding Agent.
2. Run `start_ai_bridge.bat` to launch Chrome with CDP enabled (no external DevTools plugin required).
3. Open your target AI web portal (e.g., `https://chatgpt.com`, `https://claude.ai`, or a custom portal) in that Chrome window and **log in until the chat input is visible**.

---

### Step 1: Inspect the Target Web DOM via CDP
Copy and send this prompt to your AI Agent:

> **"Use Chrome CDP to inspect the active tab `[TARGET_URL]` and extract the exact CSS selectors for these 4 elements:**
> 1. **Prompt Input Box** (`textarea` or `div[contenteditable="true"]`)
> 2. **Send Button**
> 3. **File Upload Input** (`input[type="file"]` or attachment trigger button)
> 4. **Latest AI Response Container & Streaming/Stop Indicator** (to detect when generation finishes)"

---

### Step 2: Wire the New Target into the Bridge
Once the Agent identifies the selectors, send this prompt:

> **"Using the extracted CSS selectors, add a new target portal following the exact architecture of `ask_carbon`:**
> 1. Add a dispatch & auto-save handler in `chrome_cdp_fixes.py` using `DOM.setFileInputFiles` for attachments and atomic writes (`UUID + fsync`) to `custom_history/latest.md` and `latest.status.json`.
> 2. Expose a new endpoint `POST /ask_custom` in `gemini_bridge_server.py`.
> 3. Create a matching CLI dispatcher `dispatch_custom.py` (with Smart `.txt` Bundling) and `SKILL.md` under `skills/`."

---

### Step 3: Verify End-to-End Dispatch
> **"Restart the Port `8765` Bridge, then run `dispatch_custom.py` with a short test prompt and 1 sample file (create a temporary `.txt` test file specifically for this test, and inform me afterward if that web portal has restrictions on attaching other file extensions). Finally, check `latest.status.json` and summarize `latest.md`."**

> 💡 **Tip:** If your AI provider offers an OpenAI-compatible or Gemini API Key, you can also configure it directly in the **Visual Cockpit Dashboard (`server.py` on port `5005`)** without modifying any browser selectors.

</details>

---

## 📁 Project Structure

```text
vertex-studio-oss/
├── bridge_mcp_server.py         # Native MCP Server (stdio) with 5 standard tools (v4.0)
├── gemini_bridge_server.py      # Persistent Chrome CDP Bridge Server (Port 8765) + Job API
├── chrome_cdp_fixes.py          # Atomic Auto-Save Watcher, Job Tickets & CDP Hardening
├── chrome_cdp.py                # Core Chrome DevTools Protocol Client + Dynamic Markdown Fence
├── bridge_security.py           # Token Auth & Host/Origin Protection Middleware
├── bundle_project.py            # Smart Code Bundler CLI (Audit, Feature, Diff + Secret Filter)
├── server.py                    # Visual Cockpit Dashboard Server (Port 5005)
├── code_tools.py                # Code Analysis, AST Outline & Mention Parser
├── code_tools_hardening.py      # Atomic Writer, FileLocks, Safe Text & Sensitive Filter
├── index.html                   # Single-Page Visual Cockpit Dashboard UI
├── start_ai_bridge.bat          # One-click AI Chrome Profile + Bridge Port 8765 Launcher (v4.0)
├── run.bat                      # Dashboard Launcher Script
├── skills/                      # Ready-to-use Agent Skills
│   ├── ask-argon/               # Gemini Web Skill (Port 8765 Direct)
│   └── ask-carbon/              # Cloud Studio Multimodal Skill (Port 8765 Direct)
├── .env.example                 # Environment Variable Template
├── requirements.txt             # Python Dependencies
├── CHANGELOG.md                 # Full Release History (up to v4.0.0)
├── README.md                    # English Documentation
└── README.th.md                 # Thai Documentation (เอกสารภาษาไทย)
```

---

## 🛡️ Zero-Trust Security Assurance

- **Localhost Bound:** Binds strictly to `127.0.0.1` — no external network exposure.
- **Middleware Token & Origin Guard:** Protects against CSRF and localhost exploits using `X-Bridge-Token` and `Host/Origin` allowlists.
- **Sensitive Pattern Filter:** Automatically excludes `.env`, `*.key`, `*.pem`, `.bridge_token*`, and `sessions_store.json` from code bundles.
- **Atomic File Writes:** Uses temp UUID files + `os.fsync()` with retry logic to prevent data corruption.
- **Pre-Release Privacy Audited:** Verified clean with zero hardcoded personal paths, project IDs, emails, or credentials.

---

## 📄 License

Released under the **MIT License** — free to use, modify, and distribute.

---

## 🤝 Using with Multi-Agent Swarms

Yes, **Vertex Studio OSS works with multi-agent Swarms** (verified with parallel Argon + Carbon execution). Keep these operational limits in mind:

1. **Cross-Model Parallelism Only:** `ask_argon` (Gemini Web tab) and `ask_carbon` (Cloud Studio tab) use separate Chrome tabs and locks, so they can run **simultaneously in parallel**. Multiple workers targeting the **same** model will queue sequentially on that tab.
2. **Keep 1 Tab per Model Open:** If multiple Cloud Studio or Gemini tabs are open in Chrome, CDP attaches to the first matching tab, which can cause it to send to an unintended tab.
3. **Track by `jobId`, Not `latest.md`:** `latest.md` and `latest.status.json` are overwritten by the newest job. Swarm workers must poll their own `jobId` via `check_job(job_id)` or `GET /jobs/{job_id}?wait=30&includeAnswer=1`.
4. **Use MCP or `dispatch.py`:** Direct HTTP calls without `X-Bridge-Token`, `Host`, and `Origin: http://127.0.0.1:8765` will be rejected with `403 Forbidden`.
