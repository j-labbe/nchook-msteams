# Teams Notification Interceptor

## What This Is

A macOS daemon that intercepts Microsoft Teams notifications from the macOS Sequoia notification center database and forwards them as structured JSON to a configurable webhook URL. Features status-aware gating that only forwards notifications when the user is Away or Busy, with a three-signal detection chain (Accessibility tree, system idle time, process check) and graceful degradation.

## Core Value

Reliably capture every Teams message notification and deliver it as structured JSON to a webhook — no missed messages, no noise.

## Requirements

### Validated

- ✓ Detect new Teams message notifications by watching the macOS notification center SQLite database — v1.0
- ✓ Filter to Microsoft Teams bundle IDs (com.microsoft.teams2, com.microsoft.teams) — v1.0
- ✓ Extract sender name (title), chat/channel name (subt), and message content (body) from notifications — v1.0
- ✓ Allowlist message patterns: only forward notifications with both a sender and body present — v1.0
- ✓ Filter out non-message noise: system alerts from "Microsoft Teams" itself, reactions, calls, join/leave events, empty bodies — v1.0
- ✓ POST each message notification as JSON to a configurable webhook URL — v1.0
- ✓ JSON config file for webhook URL, poll interval, and other settings — v1.0
- ✓ Persist processed notification IDs (rec_ids) across restarts via state file — v1.0
- ✓ Support macOS Sequoia DB path (~/Library/Group Containers/group.com.apple.usernoted/db2/db) — v1.0
- ✓ Detect and flag truncated message content (~150 char notification preview limit) — v1.0
- ✓ Graceful SIGINT/SIGTERM shutdown with state flush — v1.0
- ✓ --dry-run mode for safe testing without HTTP requests — v1.0
- ✓ Three-signal fallback chain (AX → idle → process) for user status detection — v1.1
- ✓ Status-aware notification gating (forward on Away/Busy/Unknown, suppress on Available/Offline) — v1.1
- ✓ AX permission probe via ctypes with graceful degradation to idle+process fallback — v1.1
- ✓ Status metadata (_detected_status, _status_source, _status_confidence) in webhook payloads — v1.1
- ✓ Config toggle (status_enabled) to enable/disable status-aware gating — v1.1
- ✓ Startup banner with status detection mode and AX permission instructions — v1.1
- ✓ Self-disabling safety net for broken AX trees (3 consecutive failures) — v1.1

### Active

(None — next milestone requirements TBD via `/gsd:new-milestone`)

### Out of Scope

- Microsoft Graph API integration — avoiding API complexity is the whole point
- AI logic / message triage — downstream consumers handle this
- Reply or response capabilities — read-only interception
- GUI — CLI/daemon only
- launchd service management — manual foreground process
- Edit/delete detection — macOS notifications don't surface these
- Retry queue for failed webhooks — log-and-skip on failure
- Offline mode — daemon requires live DB access
- Calendar integration — duplicates what Teams already provides
- Teams log file parsing — log format is unstable and undocumented
- Pixel/screen scraping — fragile, high maintenance burden
- Continuous AX observer — polling per cycle is sufficient
- Microsoft Graph API for status — local approach avoids API complexity

## Context

Shipped v1.1 with 1240 LOC Python (single file: nchook.py).
Tech stack: Python stdlib only (sqlite3, plistlib, select.kqueue, urllib, argparse, ctypes, subprocess).
Architecture: single-file daemon with config.json and state.json alongside.

Known limitations:
- ~150 char notification preview truncation (detected and flagged)
- Foreground suppression: active/focused chats may not generate notifications
- Display names only — no Azure AD user IDs or email addresses
- New Teams (com.microsoft.teams2) AX tree may be broken — daemon self-disables AX after 3 failures

v2 deferred items (from v1.1 research):
- Hysteresis/debounce for status transitions (SREF-01)
- Screen lock / session state detection (SREF-02)
- macOS Focus/DND mode detection (SREF-03)
- Per-status configurable gating rules (SREF-04)
- Configurable idle threshold (SREF-05)

## Constraints

- **OS**: macOS Sequoia (15+) — target platform
- **Language**: Python (stdlib only, no pip dependencies)
- **Config**: JSON file in project directory
- **State**: File-based persistence alongside config
- **Failure mode**: Log-and-skip on webhook delivery failure (no retry queue)
- **Process model**: Manual foreground process (not a launchd service)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single nchook.py module | Matches daemon simplicity, avoids package structure | ✓ Good — 1240 LOC stays manageable |
| Patch nchook for subt extraction | Wrapper needs chat/channel name that nchook doesn't pass through | ✓ Good — subt flows through full pipeline |
| Allowlist message patterns for filtering | More predictable than blocklisting — only forward what looks like a real message | ✓ Good — clean 4-stage pipeline |
| Log-and-skip on webhook failure | Simplicity over reliability — no retry queue complexity | ✓ Good — daemon never hangs |
| Config and state in project directory | Simple deployment, no XDG paths to manage | ✓ Good — zero setup beyond config.json |
| kqueue + fallback polling | Near-real-time via WAL watching, polling catches edge cases | ✓ Good — handles WAL checkpoint race |
| Atomic state writes (tempfile + fsync + os.replace) | Prevents corruption on crash/kill | ✓ Good — state always consistent |
| Signal handler sets flag only | No I/O in signal context, loop exits naturally | ✓ Good — clean shutdown every time |
| argparse before config load | --help works without config.json | ✓ Good — better UX |
| stdlib only (no pip) | Zero dependency management for single-file daemon | ✓ Good — just `python3 nchook.py` |
| Three-signal fallback chain (AX → idle → process) | Layered detection: high confidence when AX works, decent fallback otherwise | ✓ Good — graceful degradation proven |
| ctypes AXIsProcessTrusted over osascript probe | Instant boolean vs 30s+ hang when permission denied | ✓ Good — no startup delay |
| Hardcoded forward statuses (Away, Busy, Unknown) | GATE-01 specifies exact policy; configurable is v2 deferred | ✓ Good — simple, correct |
| AX permission cached at startup | TCC changes require process restart on macOS | ✓ Good — matches OS behavior |
| Self-disable after 3 consecutive AX failures | Known broken AX tree in new Teams; avoids 3s/cycle waste | ✓ Good — handles real-world failure |
| 3s osascript timeout | Must be < 5s poll interval to prevent event loop blocking | ✓ Good — bounded latency |

---
*Last updated: 2026-02-11 after v1.1 milestone*
