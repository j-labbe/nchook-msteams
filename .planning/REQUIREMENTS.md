# Requirements: Teams Notification Interceptor

**Defined:** 2026-02-11
**Core Value:** Reliably capture every Teams message notification and deliver it as structured JSON to a webhook — no missed messages, no noise.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### DB Watching & Extraction

- [ ] **DBWT-01**: Daemon auto-detects macOS Sequoia notification center DB path (~Library/Group Containers/group.com.apple.usernoted/db2/db)
- [ ] **DBWT-02**: Daemon monitors the WAL file via kqueue (KQ_FILTER_VNODE + NOTE_WRITE) for near-instant notification detection
- [ ] **DBWT-03**: Daemon falls back to periodic polling when kqueue events are missed (WAL checkpoint edge cases)
- [ ] **DBWT-04**: Daemon opens notification DB in read-only mode (sqlite3 URI mode=ro) to avoid interfering with usernoted
- [ ] **DBWT-05**: Daemon decodes binary plist blobs from the data column to extract notification fields (titl, subt, body, date)
- [ ] **DBWT-06**: Patched nchook passes subtitle (subt) as 5th argument to the callback script

### Teams Filtering

- [ ] **FILT-01**: Wrapper filters notifications to Teams bundle IDs only (configurable set: com.microsoft.teams2, com.microsoft.teams)
- [ ] **FILT-02**: Wrapper requires both sender (title) and body to be present and non-empty (allowlist)
- [ ] **FILT-03**: Wrapper rejects notifications where title is "Microsoft Teams" (system alerts)
- [ ] **FILT-04**: Wrapper rejects known noise patterns: reactions, calls, join/leave events, meeting alerts
- [ ] **FILT-05**: Wrapper classifies notification type (direct_message, channel_message, mention) based on subt/title patterns

### Webhook Delivery

- [ ] **WEBH-01**: Wrapper POSTs each passing notification as JSON to the configured webhook URL
- [ ] **WEBH-02**: JSON payload includes: senderId, senderName, chatId, content, timestamp, _source, _truncated
- [ ] **WEBH-03**: Webhook POST uses a configurable timeout (default 10s) and logs-and-skips on failure
- [ ] **WEBH-04**: Wrapper detects likely truncated messages (~150 char body + no sentence-ending punctuation) and sets _truncated flag

### Configuration & State

- [ ] **CONF-01**: Daemon reads JSON config file from project directory for webhook URL, bundle IDs, poll interval, log level
- [ ] **CONF-02**: Daemon persists last processed rec_id to a state file using atomic writes (write-then-rename)
- [ ] **CONF-03**: State file survives daemon restarts — new session resumes from last rec_id
- [ ] **CONF-04**: Daemon detects DB purge/recreation (max rec_id < persisted rec_id) and resets state with a warning

### Startup & Operations

- [ ] **OPER-01**: On startup, daemon validates Full Disk Access by attempting to read the notification DB and prints actionable error if denied
- [ ] **OPER-02**: On startup, daemon prints summary: DB path, FDA status, bundle IDs, webhook URL, last rec_id
- [ ] **OPER-03**: Daemon handles SIGINT/SIGTERM gracefully — flushes state to disk and exits cleanly
- [ ] **OPER-04**: Daemon supports --dry-run flag that logs what would be POSTed without actually sending

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Enhanced Operations

- **EOPS-01**: Daemon sends periodic heartbeat signal to webhook endpoint
- **EOPS-02**: Daemon alerts after N consecutive webhook delivery failures
- **EOPS-03**: Daemon reloads config on SIGHUP without restart
- **EOPS-04**: Daemon can replay notifications from last N minutes on startup (configurable replay window)

### Enhanced Filtering

- **EFLT-01**: User-configurable blocklist patterns in config file (regex or glob)

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Microsoft Graph API integration | Avoiding API complexity is the entire value proposition |
| AI logic / message triage | Downstream consumers handle intelligence; interceptor captures data |
| Reply or response capabilities | Read-only interception; write access changes security posture entirely |
| GUI / menu bar app | Target user is a developer running in terminal; GUI adds massive deps |
| Accessibility API usage | DB watching is more reliable and lower-privilege |
| launchd service management | Manual foreground process first; launchd plist can be documented separately |
| Edit/delete detection | macOS notifications don't surface these events |
| Retry queue for failed webhooks | Log-and-skip design; complexity not justified for foreground daemon |
| Multi-account support | Single user, single Teams account per machine |
| Webhook authentication (OAuth/HMAC) | Webhook receiver is same-org controlled; auth proxy if needed |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| DBWT-01 | — | Pending |
| DBWT-02 | — | Pending |
| DBWT-03 | — | Pending |
| DBWT-04 | — | Pending |
| DBWT-05 | — | Pending |
| DBWT-06 | — | Pending |
| FILT-01 | — | Pending |
| FILT-02 | — | Pending |
| FILT-03 | — | Pending |
| FILT-04 | — | Pending |
| FILT-05 | — | Pending |
| WEBH-01 | — | Pending |
| WEBH-02 | — | Pending |
| WEBH-03 | — | Pending |
| WEBH-04 | — | Pending |
| CONF-01 | — | Pending |
| CONF-02 | — | Pending |
| CONF-03 | — | Pending |
| CONF-04 | — | Pending |
| OPER-01 | — | Pending |
| OPER-02 | — | Pending |
| OPER-03 | — | Pending |
| OPER-04 | — | Pending |

**Coverage:**
- v1 requirements: 23 total
- Mapped to phases: 0
- Unmapped: 23

---
*Requirements defined: 2026-02-11*
*Last updated: 2026-02-11 after initial definition*
