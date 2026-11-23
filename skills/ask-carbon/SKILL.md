---
name: ask-carbon
description: Dispatch tasks, deep code reviews, and architectural audits directly to Gemini 4 Carbon via the Google Cloud Studio Multimodal Web Tab with Smart .txt Bundling. Also triggered by the /ask-carbon command.
---

# Ask Carbon Web Skill

Use this skill whenever the user enters `/ask-carbon` or asks to consult, review, audit, check, or dispatch prompts and files to **Gemini 4 Carbon**, **Carbon**, or **Carbon Web** on Google Cloud Studio.

## Target URL
- `https://console.cloud.google.com/agent-platform/studio/multimodal?authuser=0&project=YOUR_GCP_PROJECT_ID&model=gemini-4-carbon&region=global`

---

## 📋 Standard Workflow

Strictly follow these 4 steps in order:

```
[Step 1: Bridge Port 8765 Healthcheck]
       │
       ├─ Port listening (State: Listen) ────────┐
       └─ Port closed ──> Start background task ─┤
                                                 ▼
[Step 2: Fast Fire-and-Forget Dispatch (< 2 seconds)]
       │   └─ Execute dispatch_carbon.py with --prompt and --files
       ▼
[Step 3: Immediate Acknowledgement]
       │   └─ Inform user that the prompt has been dispatched to Carbon Web
       ▼
[Step 4: Result Summary (When user asks or Job status finishes)]
           └─ Check carbon_history\latest.status.json -> Read latest.md -> Summarize in Thai
```

### Step Details:

#### 1. Bridge Port 8765 Healthcheck
Verify whether port 8765 is actively listening:
- `Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue`
- **If listening:** Proceed to Step 2 immediately.
- **If closed:** Start the background service cleanly:
  ```powershell
  schtasks /run /tn "GeminiBridgeService"
  ```

#### 2. Direct Dispatch
Execute the dispatch script immediately:
```powershell
python "~/.gemini/config/skills/ask-carbon/scripts/dispatch_carbon.py" --prompt "Instruction content" --files "path/to/file1"
```
- **Strict Prohibition:** Do NOT print the prompt or file contents into chat before dispatching; execute directly.

#### 3. Immediate Acknowledgement (< 2 seconds)
Reply briefly to the user confirming dispatch to Google Cloud Studio Carbon.

#### 4. Result Summary
When user prompts that results are ready or when reviewing completed tasks:
1. **Check Job Status:** Read `./carbon_history/latest.status.json`.
   - If `state == "generating"`: Notify user that Carbon is still generating (never read stale `latest.md`).
   - If `state == "done"`: Read `latest.md`.
2. **Summarize Content:** Provide a concise Thai summary (Bottom-Line First).
3. **Fallback:** If `latest.status.json` is missing or stalled, call `GET http://127.0.0.1:8765/extract_carbon` once to sync the latest output to file.

#### 5. Operational Guardrails (Lessons Learned)
- **Always use `dispatch_carbon.py` for cross-drive/multi-file sends:** Never POST raw file paths from outside the bridge working directory directly to `/ask_carbon` (it will fail Path Jail). Always use `scripts/dispatch_carbon.py` so it bundles files cleanly.
- **Never blind-retry HTTP 500:** If `/ask_carbon` returns `HTTP 500`, do NOT retry repeatedly (it will keep switching Chrome tabs). Check `tabUrl` in `/status` or verify the Cloud Studio tab is open.
- **Bounded Reads for `latest.md`:** Read `latest.md` in chunks (`StartLine=1, EndLine=150`) to stay within `context-mode` read limits.
- **Auto-reconnect on HTTP 503 (CDP Not Connected):** If `/ask_carbon` or `/extract_carbon` returns `HTTP 503`, immediately trigger **1** reconnect request via MCP `bridge_status(reconnect=true)` or `POST http://127.0.0.1:8765/connect-cdp` (with `X-Bridge-Token` from `.bridge_token_8765.json`, `Host: 127.0.0.1:8765`, `Origin: http://127.0.0.1:8765`), then ask the user to click **Allow** on the Chrome popup within 60 seconds before retrying.

---

## 🎯 Smart Intent-Based Bundling Rules

Select bundling mode based on user intent:
1. **Bug Audit / Security Review Mode:**
   - **Input:** Send full implementation (100%). Never truncate functions; logic bugs hide in control flow.
   - **Token reduction:** Strip redundant blank lines and exclude binary/assets/lockfiles only.
   - **Output Requirement:** Require Carbon to return targeted **Search & Replace Snippet Blocks** inside fenced code blocks:
     ```text
     <<<<<<< SEARCH
     [Original 3-5 lines to replace]
     =======
     [New replacement lines]
     >>>>>>>
     ```
2. **Feature Ideation / Architecture Mode:**
   - **Input:** Outline mode (AST Outline: class names, method signatures, data schemas, endpoints) + system summary.
   - **Token reduction:** Saves 70-80% token budget, focusing attention on high-level architecture.
3. **Bugfix / Incremental Diff Mode:**
   - **Rule:** Send target files or `git diff` only. Request Search & Replace Snippet Blocks.

---

## 🛡️ Anti-Corruption & Architecture Scoping Guardrails

1. **Never Output Naked Search & Replace Blocks:** All `<<<<<<< SEARCH ... ======= ... >>>>>>>` blocks MUST be enclosed inside explicit fenced code blocks (e.g. ````javascript ... ````). Naked `=======` or `>>>>>>>` corrupts Web Markdown parsers.
2. **Never Break Code Fences Inside Template Literals:** When writing UI code with HTML inside JS template literals (`innerHTML = \`...\``), never place Markdown code fences (` ``` `) inside the template string. Emit continuous code within a single outer code block.
3. **Architecture Scoping (Split Large Multi-Tier Tasks):** For large features exceeding 500-800 lines, split requests into distinct rounds (Round 1: Backend/DB/APIs, Round 2: Frontend UI) to prevent formatting drift.
4. **Mandatory Syntax Verification:** Always execute syntax validation (`node --check` for JS/MJS, `python -m py_compile` for Python) before applying generated code to sandbox or production.

---

## Capabilities

1. **Direct Chrome CDP Automation:** Connects directly to the active Chrome tab running Google Cloud Studio Multimodal.
2. **Smart Code Bundling (.txt):** Merges multiple code files into a single clean `.txt` bundle with `# === FILE: ... ===` headers, minimizing token overhead by 40-70% and bypassing upload quotas.
3. **Automated Submission:** Injects prompts, attaches files, and triggers execution without manual copy-pasting.

## Usage Guide

Execute `scripts/dispatch_carbon.py`:
```powershell
python "~/.gemini/config/skills/ask-carbon/scripts/dispatch_carbon.py" --prompt "Prompt instruction" --files "path/to/file1.py" "path/to/file2.py"
```

### Arguments:
- `--prompt` / `-p` (Required): Prompt or instructions for Gemini 4 Carbon.
- `--files` / `-f` (Optional): List of local files to attach (auto-bundled into single `.txt`).
- `--out` / `-o` (Optional): Output markdown file path.
- `--no-bundle` (Optional): Disable automatic bundling.

## Prerequisites
1. Chrome must be open with remote debugging enabled on port 9222.
2. The Google Cloud Studio tab with `gemini-4-carbon` must be open in Chrome.

#### 6. Multi-Agent / Swarm Rules (Experimental / Lab)
- **Poll by `jobId` (Never `latest.md`):** Always poll `check_job(job_id)` or `GET /jobs/{job_id}?wait=30&includeAnswer=1`.
- **Auth Headers:** Direct HTTP calls require `X-Bridge-Token`, `Host: 127.0.0.1:8765`, and `Origin: http://127.0.0.1:8765` (otherwise `403 Forbidden`).
- **Multi-Tab Routing & Draft Mode:** Use `GET /tabs` to get `target_id`, pass `"target_id"` (or `"tab_url"`) and optional `"draft_only": true` in `POST /ask_carbon`.
