# Roadmap: Teams Notification Interceptor

## Overview

This roadmap delivers a macOS daemon that intercepts Teams notifications from the Sequoia notification center database and forwards them as structured JSON to a webhook. The work follows the architectural boundary between the two components: Phase 1 builds the patched nchook engine (DB watching, extraction, state), Phase 2 builds the wrapper (filtering, JSON, webhook delivery), and Phase 3 hardens the daemon for sustained operation (graceful shutdown, dry-run).

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: DB Watcher & State Engine** - Patched nchook that reliably watches Sequoia DB, extracts all fields, persists state, and validates the environment
- [x] **Phase 2: Teams Filtering & Webhook Delivery** - Wrapper that filters to Teams messages, constructs JSON payloads, and delivers to webhook
- [ ] **Phase 3: Operational Hardening** - Graceful shutdown, dry-run mode, and end-to-end production readiness

## Phase Details

### Phase 1: DB Watcher & State Engine
**Goal**: The notification engine reliably detects every new notification in the macOS Sequoia database, extracts all fields (including subtitle/chat name), persists its position across restarts, and fails loudly with actionable errors when the environment is wrong.
**Depends on**: Nothing (first phase)
**Requirements**: DBWT-01, DBWT-02, DBWT-03, DBWT-04, DBWT-05, CONF-02, CONF-03, CONF-04, OPER-01, OPER-02
**Success Criteria** (what must be TRUE):
  1. Running the daemon prints a startup summary showing DB path, FDA status, watched bundle IDs, and last processed rec_id
  2. When a new notification arrives in the DB, the daemon detects it within seconds and prints/logs the extracted fields (app, title, subtitle, body, timestamp)
  3. Killing and restarting the daemon resumes from the last processed notification without replaying old ones
  4. Running the daemon without Full Disk Access prints a clear error message explaining how to grant FDA in System Settings
  5. If the notification DB has been purged (max rec_id < persisted rec_id), the daemon resets state with a warning instead of silently missing all future notifications
**Plans**: 2 plans

Plans:
- [ ] 01-01-PLAN.md -- Core engine: startup validation, DB access, plist parsing, state persistence
- [ ] 01-02-PLAN.md -- Event loop: kqueue watcher, main entry point, live verification

### Phase 2: Teams Filtering & Webhook Delivery
**Goal**: The complete data pipeline works end-to-end: raw notifications are filtered to Teams messages only, structured as JSON with all required fields, and delivered to the configured webhook URL.
**Depends on**: Phase 1
**Requirements**: DBWT-06, FILT-01, FILT-02, FILT-03, FILT-04, FILT-05, WEBH-01, WEBH-02, WEBH-03, WEBH-04, CONF-01
**Success Criteria** (what must be TRUE):
  1. Only Teams notifications (matching configured bundle IDs) are forwarded; all other app notifications are silently dropped
  2. Noise notifications from Teams (reactions, calls, join/leave, system alerts from "Microsoft Teams") are filtered out; only real messages with a sender and body are forwarded
  3. Each delivered webhook POST contains a JSON payload with sender name, chat/channel name, message body, timestamp, source metadata, and a truncation flag
  4. The daemon reads webhook URL, bundle IDs, poll interval, and log level from a JSON config file in the project directory
  5. When the webhook endpoint is unreachable or returns an error, the daemon logs the failure and continues processing the next notification (no hang, no crash)
  6. Subtitle (subt) field from Phase 1 extraction is included in the callback/webhook dispatch payload
**Plans**: 2 plans

Plans:
- [x] 02-01-PLAN.md -- Config loading, Teams filtering functions, notification classification, truncation detection
- [x] 02-02-PLAN.md -- Webhook delivery functions, event loop integration, startup summary update

### Phase 3: Operational Hardening
**Goal**: The daemon is production-ready for sustained foreground operation with clean lifecycle management and a safe testing mode.
**Depends on**: Phase 2
**Requirements**: OPER-03, OPER-04
**Success Criteria** (what must be TRUE):
  1. Sending SIGINT or SIGTERM to the daemon causes it to flush state to disk and exit cleanly (no data loss, no zombie process)
  2. Running the daemon with --dry-run prints the JSON payloads that would be sent to the webhook without actually making HTTP requests
**Plans**: 1 plan

Plans:
- [ ] 03-01-PLAN.md -- Graceful signal handling, --dry-run CLI flag, post-loop state flush

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. DB Watcher & State Engine | 2/2 | Complete | 2026-02-11 |
| 2. Teams Filtering & Webhook Delivery | 2/2 | Complete | 2026-02-11 |
| 3. Operational Hardening | 0/1 | Not started | - |
