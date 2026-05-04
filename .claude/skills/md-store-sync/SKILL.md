---
name: md-store-sync
description: Trigger the local mcp-md-sync Windows scheduled task to force an immediate sync between C:\mcp-md-store-local and the S3 md-store bucket (agent-md-library). Use when the user wants fresh agent-written markdown pulled down now rather than waiting for the 5-minute daemon tick.
---

# md-store-sync

Use this skill when the user says things like:

- "kick off the md-store sync"
- "trigger the local sync with the S3 md-store"
- "pull down the latest summary report"
- "force an md-store sync"

## What it does

Runs `Start-ScheduledTask -TaskName 'mcp-md-sync'` via PowerShell. The task is a persistent daemon (`mcp_md_sync.daemon`) that bidirectionally syncs `C:\mcp-md-store-local` with the `agent-md-library` S3 bucket via the md-store API at `https://jjjtiltcja.execute-api.us-east-1.amazonaws.com/prod/mcp`, using AWS profile `md-sync`. Normal cadence is every ~5 minutes; triggering manually just forces the next tick to happen now.

## How Claude should run it

1. **Start the task**:
   ```
   powershell -NoProfile -Command "Start-ScheduledTask -TaskName 'mcp-md-sync'"
   ```
2. **Report status** using `Get-ScheduledTaskInfo`:
   ```
   powershell -NoProfile -Command "Get-ScheduledTask -TaskName 'mcp-md-sync' | Get-ScheduledTaskInfo | Format-List TaskName,LastRunTime,LastTaskResult,NextRunTime"
   ```
3. **Interpret `LastTaskResult`**:
   - `0` — last run succeeded
   - `267009` (`0x41301`) — currently running (expected for a long-lived daemon) — NOT an error
   - anything else — surface to the user

## Prerequisites

- Windows scheduled task `mcp-md-sync` registered at root path `\` (check with `Get-ScheduledTask -TaskName 'mcp-md-sync'`).
- The daemon's Python venv at `C:\Users\jotw\OneDrive\Documents\Claude\Claude Projects\Personal\mcp-md-store\client\.venv` is the task's `Execute` binary. If the task is missing, the daemon was likely never installed on this machine.

## Gotchas

- **Do not invoke via Git Bash `schtasks /run`** — Git Bash mangles the `/tn` flag into a `C:/Program Files/Git/run` path and the command fails. Always go through PowerShell `Start-ScheduledTask`.
- **This is a daemon, not a one-shot.** `Start-ScheduledTask` against an already-running instance is a no-op at the Windows level; the sync does one cycle and continues its normal loop. If the user wants a genuinely fresh process, stop then start:
  ```
  powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'mcp-md-sync'; Start-ScheduledTask -TaskName 'mcp-md-sync'"
  ```
- **No synchronous "done" signal.** The task returns immediately; files appear in `C:\mcp-md-store-local` over the next few seconds. If the user wants to verify, `ls` the local folder or a specific subpath (e.g. `C:\mcp-md-store-local\TauricTraders\_summary.md`).
