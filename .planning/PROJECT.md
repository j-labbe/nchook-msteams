# Teams Notification Interceptor

## What This Is

A macOS daemon that intercepts Microsoft Teams notifications from the macOS notification center database and forwards them as JSON to a configurable webhook URL. This lets downstream AI agents triage and respond to Teams messages without needing Microsoft Graph API access.

## Core Value

Reliably capture every Teams message notification and deliver it as structured JSON to a webhook — no missed messages, no noise.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Detect new Teams message notifications by watching the macOS notification center SQLite database
- [ ] Filter to Microsoft Teams bundle IDs (com.microsoft.teams2, com.microsoft.teams)
- [ ] Extract sender name (title), chat/channel name (subt), and message content (body) from notifications
- [ ] Allowlist message patterns: only forward notifications with both a sender and body present
- [ ] Filter out non-message noise: system alerts from "Microsoft Teams" itself, reactions, calls, join/leave events, empty bodies
- [ ] POST each message notification as JSON to a configurable webhook URL
- [ ] JSON config file for webhook URL, poll interval, and other settings
- [ ] Persist processed notification IDs (rec_ids) across restarts via state file
- [ ] Support macOS Sequoia DB path (~/Library/Group Containers/group.com.apple.usernoted/db2/db)
- [ ] Detect and flag truncated message content (~150 char notification preview limit)

### Out of Scope

- Microsoft Graph API integration — avoiding API complexity is the whole point
- AI logic / message triage — downstream consumers handle this
- Reply or response capabilities — read-only interception
- GUI — CLI/daemon only
- Accessibility API usage — DB watching approach only
- launchd service management — manual foreground process
- Edit/delete detection — macOS notifications don't surface these
- Retry queue for failed webhooks — log-and-skip on failure

## Context

**Foundation:** [nchook](https://github.com/who23/nchook) — a Python daemon that watches the macOS notification center SQLite database using kqueue on the WAL file and calls a user script for each new notification with APP, TITLE, BODY, and TIME as arguments.

**Architecture:** Two components working together:
1. **Patched nchook** — forked to add Sequoia DB path support and `subt` (subtitle/chat name) extraction as a 5th argument to the callback script
2. **Wrapper script** — called by nchook, handles Teams-specific filtering (allowlist: sender + body present), JSON formatting, and webhook delivery

**Known limitations of macOS notification interception:**
- ~150 character notification preview truncation (Teams truncates long messages)
- Foreground suppression: active/focused chats may not generate notifications
- Display names only — no Azure AD user IDs or email addresses
- No edit or delete events surfaced through notifications

**nchook gaps being addressed:**
- Pre-Sequoia DB path only → adding Sequoia path detection
- No subtitle/subt extraction → adding as 5th callback argument
- No webhook delivery → wrapper handles HTTP POST
- No Teams-specific filtering → wrapper handles allowlisting
- In-memory rec_ids only → state file persistence
- No config file → JSON config in project directory

## Constraints

- **OS**: macOS Sequoia (15+) — target platform
- **Language**: Python
- **Config**: JSON file in project directory
- **State**: File-based persistence alongside config
- **Failure mode**: Log-and-skip on webhook delivery failure (no retry queue)
- **Process model**: Manual foreground process (not a launchd service)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Wrap nchook (patch + wrapper) rather than rewrite | Leverage proven DB watching logic, minimize new code | — Pending |
| Patch nchook for subt extraction | Wrapper needs chat/channel name that nchook doesn't pass through | — Pending |
| Allowlist message patterns for filtering | More predictable than blocklisting — only forward what looks like a real message | — Pending |
| Log-and-skip on webhook failure | Simplicity over reliability — no retry queue complexity | — Pending |
| Config and state in project directory | Simple deployment, no XDG paths to manage | — Pending |

---
*Last updated: 2026-02-11 after initialization*
