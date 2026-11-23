---
name: ask-argon
description: Dispatch tasks, code audits, bug triaging, and architecture reviews directly to Gemini 4 Argon Web via the Port 8765 pipeline with file attachments and smart code bundling. Also triggered by the /ask-argon command.
---

# Ask Argon Web Skill

Use this skill whenever the user enters `/ask-argon` or asks to consult, review, audit, check, or dispatch a prompt or files to **Argon**, **อากอน**, **Gemini 4 Argon**, or **Argon Web**.

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
       │   └─ Execute dispatch.py with --prompt and --files
       ▼
[Step 3: Sub-2-Second Immediate Acknowledgement]
       │   └─ Inform user that the prompt has been dispatched to Argon Web
       ▼
[Step 4: Result Summary (When user asks or Job status finishes)]
           └─ Check latest.status.json -> Read latest.md -> Summarize in Thai
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
python "~/.gemini/config/skills/ask-argon/scripts/dispatch.py" --prompt "Instruction content" --files "path/to/file1"
```
- **Strict Prohibition:** Do NOT print the prompt or file contents into chat before dispatching; execute directly.

#### 3. Immediate Acknowledgement (< 2 seconds)
Reply briefly to the user confirming dispatch to Argon Web and that processing is underway.

#### 4. Result Summary
When user prompts that results are ready or when reviewing completed tasks:
1. **Check Job Status:** Read `./argon_history/latest.status.json`.
   - If `state == "generating"`: Notify user that Argon is still generating (never read stale `latest.md`).
   - If `state == "done"`: Read `latest.md`.
2. **Summarize Content:** Provide a concise Thai summary (Bottom-Line First).
3. **Fallback:** If `latest.status.json` is missing or stalled, call `GET http://127.0.0.1:8765/extract_markdown` once to sync the latest output to file.

#### 5. Operational Guardrails (Lessons Learned)
- **Always use `dispatch.py` for cross-drive/multi-file sends:** Never POST raw file paths from outside the bridge working directory directly to `/ask` (it will fail Path Jail). Always use `scripts/dispatch.py` so it bundles files cleanly.
- **Never blind-retry HTTP 500:** If `/ask` returns `HTTP 500`, do NOT retry repeatedly (it will keep switching Chrome tabs). Check `tabUrl` in `/status` (e.g. `/notebook/` vs `/app`), or if `input[type='file']` is missing, ask the user to click `+` once on Gemini Web.
- **Bounded Reads for `latest.md`:** Read `latest.md` in chunks (`StartLine=1, EndLine=150`) to stay within `context-mode` read limits.

---

## 🎯 Smart Intent-Based Bundling Rules

Select bundling mode based on user intent:
1. **Bug Audit / Security Review Mode:**
   - **Input:** Send full implementation (100%). Never truncate functions; logic bugs hide in control flow.
   - **Token reduction:** Strip redundant blank lines and exclude binary/assets/lockfiles only.
   - **Output Requirement:** Require Argon to return targeted **Search & Replace Snippet Blocks** inside fenced code blocks:
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

1. **Direct Web Pipeline:** Dispatches prompts to Gemini 4 Argon Web via Persistent Bridge (Port 8765) using Chrome CDP.
2. **File Attachment Support:** Attaches local files directly to Argon chat context.
3. **Smart Code Bundling (.txt):** Merges multiple code files into a single structured `.txt` bundle (`# === FILE: ... ===`), reducing token overhead by 40-70%.
4. **Deep Thinking:** Configured with `webThinkingLevel: HIGH` for complex reasoning.

## Usage Guide

Execute `scripts/dispatch.py`:
```powershell
python "~/.gemini/config/skills/ask-argon/scripts/dispatch.py" --prompt "Prompt instruction" --files "path/to/file1.py" "path/to/file2.html" --out "path/to/output.md"
```

### Arguments:
- `--prompt` / `-p` (Required): Instruction text for Argon.
- `--files` / `-f` (Optional): Paths to attached files.
- `--bundle` / `-b` (Optional): Forces bundling all attached code files into a single `.txt` file.
- `--thinking` / `-t` (Optional): Thinking level (`HIGH`, `MEDIUM`, `LOW`, default: `HIGH`).
- `--out` / `-o` (Optional): Output markdown file path.
- `--session` / `-s` (Optional): Session ID for tracking.

## Prerequisites
1. Chrome running with remote debugging on port 9222.
2. Persistent Bridge running on `http://127.0.0.1:8765` (`gemini_bridge_server.py`).

#### 6. Multi-Agent / Swarm Rules (Experimental / Lab)
- **Poll by `jobId` (Never `latest.md`):** Always poll `check_job(job_id)` or `GET /jobs/{job_id}?wait=30&includeAnswer=1`.
- **Auth Headers:** Direct HTTP calls require `X-Bridge-Token`, `Host: 127.0.0.1:8765`, and `Origin: http://127.0.0.1:8765` (otherwise `403 Forbidden`).
- **Multi-Tab Routing & Draft Mode:** Use `GET /tabs` to get `target_id`, pass `"target_id"` (or `"tab_url"`) and optional `"draft_only": true` / `"fireAndForget": true` in `POST /ask`. Omit `webModel`/`thinkingLevel` to keep the tab's pre-selected thinking level.
