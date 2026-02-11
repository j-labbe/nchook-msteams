# Project Research Summary

**Project:** macos-notification-intercept
**Domain:** macOS system daemon for notification interception and webhook forwarding
**Researched:** 2026-02-11
**Confidence:** MEDIUM-HIGH

## Executive Summary

This project builds a zero-dependency Python daemon that intercepts Microsoft Teams notifications from macOS Sequoia's notification center database and forwards them via webhook to downstream agents. The technical approach combines proven patterns from nchook (kqueue-based SQLite WAL file watching) with Teams-specific filtering and JSON payload construction. The entire stack uses Python 3.12+ standard library—no external dependencies, no virtualenv, making it deployable with zero setup on any macOS Sequoia machine.

The recommended architecture maintains clean separation between generic notification watching (patched nchook) and Teams-specific logic (wrapper script). This allows independent testing and keeps complexity isolated. The core data flow is: kqueue detects WAL writes → query DB for new records → decode binary plists → filter for Teams messages → build JSON → POST to webhook. State persistence (last processed rec_id) prevents duplicate deliveries across restarts.

Critical risks center on macOS system integration fragility: SQLite database locking conflicts with the usernoted process, kqueue file descriptor staleness during WAL checkpoints, Full Disk Access permission failures, and notification database schema changes between macOS versions. All of these have proven mitigation strategies (read-only DB access, fallback poll timer, startup validation, explicit FDA checks) that must be implemented from Phase 1. The Teams notification format variations and truncation behavior are manageable through defensive parsing and transparent metadata flags in the webhook payload.

## Key Findings

### Recommended Stack

**Core: Python 3.12+ stdlib only.** The entire stack leverages macOS Sequoia's bundled Python 3.12 with zero external dependencies. This includes `sqlite3` for database access, `select.kqueue` for file system event monitoring, `plistlib` for decoding binary notification payloads, `urllib.request` for webhook delivery, and `json`/`logging` for configuration and observability. No `requirements.txt`, no virtualenv, no dependency management. This is the correct pattern because the problem domain maps perfectly to stdlib capabilities.

**Core technologies:**
- **Python 3.12+ (system)**: Runtime — macOS Sequoia ships Python 3.12 via Xcode CLT; zero external dependencies needed
- **sqlite3 (stdlib)**: Read-only notification DB access — handles WAL mode reads, timeouts for lock contention
- **select.kqueue (stdlib)**: BSD kernel event notification — near-instant detection of DB changes via WAL file monitoring
- **plistlib (stdlib)**: Binary plist decoding — notification payloads stored as bplist blobs in DB
- **urllib.request (stdlib)**: Webhook HTTP POST — sufficient for fire-and-forget delivery with log-on-failure

**Critical version requirements:**
- Python 3.12+ for stable stdlib APIs (sqlite3 URI mode, plistlib binary plist support)
- macOS Sequoia 15+ for target notification DB path and schema

**What NOT to use:**
- `requests` library — adds only external dependency for simple POST; urllib.request is sufficient
- `watchdog` — cross-platform abstraction unnecessary; kqueue is native and proven
- `asyncio` — adds complexity with no benefit; synchronous flow is simpler
- `pyobjc` — massive dependency tree; DB-watching approach avoids Objective-C bridge entirely

### Expected Features

**Must have (table stakes):**
- **Teams bundle ID filtering** — match both `com.microsoft.teams2` (new Teams) and `com.microsoft.teams` (classic); without this, downstream drowns in system noise
- **Sender + message extraction** — map notification fields (title→sender, body→message, subt→channel/chat) into structured data
- **Chat/channel name extraction (subt)** — critical for routing; requires patching nchook to pass subtitle field
- **Allowlist filtering** — require both sender name and body present; rejects reactions, call events, "X is typing", system alerts
- **Webhook POST delivery** — JSON payload to configurable URL; log-and-skip on failure per project design
- **State persistence** — track processed rec_ids to survive restarts without replaying history
- **macOS Sequoia DB path support** — `~/Library/Group Containers/group.com.apple.usernoted/db2/db`
- **Structured JSON payload** — parseable fields: sender, channel, message, timestamp, truncation flag
- **Timestamp extraction** — messages without timestamps are unorderable

**Should have (competitive):**
- **Truncation detection flag** — macOS truncates previews at ~150 chars; flag `"truncated": true` signals incomplete message
- **Dry-run mode** — print payloads without POSTing; essential for setup/debugging
- **Graceful shutdown** — SIGTERM/SIGINT handler flushes state before exit
- **Notification type classification** — distinguish DM vs channel vs mention; requires pattern matching on subt/title formats
- **Health/heartbeat signal** — distinguish "no messages" from "daemon crashed"

**Defer (v2+):**
- **Startup replay window** — re-process last N minutes on startup (needs careful testing)
- **Content-hash deduplication** — catch rare duplicate notifications with different rec_ids (only if observed in practice)
- **Config reload without restart** — SIGHUP-triggered config re-read (convenience, not necessity)

**Anti-features (explicitly avoid):**
- Microsoft Graph API integration — defeats the purpose (avoiding OAuth complexity)
- Retry queue for failed webhooks — adds state management complexity; log-and-skip is correct
- Reply/response capabilities — requires Graph API; stay read-only
- GUI/menu bar app — adds dependency surface; CLI daemon is correct
- launchd service bundling — document separately; keep tool launchd-agnostic

### Architecture Approach

**Two-component separation:** Patched nchook (generic notification watcher) dispatches via subprocess to a wrapper script (Teams-specific filtering and webhook delivery). This maintains clean boundaries: nchook owns kqueue watching, SQLite reads, binary plist decoding, and rec_id state management. The wrapper owns Teams bundle ID filtering, allowlist logic, JSON construction, and HTTP POST. Process boundary allows independent testing and keeps each component focused.

**Major components:**
1. **kqueue WAL Watcher** — `select.kqueue()` with `KQ_FILTER_VNODE`/`NOTE_WRITE` on `db-wal` file; blocks until DB write, near-zero CPU when idle
2. **DB Reader** — SQLite connection with `?mode=ro` URI; queries `WHERE rec_id > last_seen` for new records; read-only prevents interference with usernoted
3. **Plist Decoder** — `plistlib.loads()` on binary BLOB from `data` column; extracts `titl`, `subt`, `body`, `date`, `app` keys
4. **State Tracker** — high-water mark (single integer last_rec_id) persisted to JSON file; simpler than tracking set of all IDs
5. **Callback Dispatcher** — `subprocess.call([handler, APP, TITLE, BODY, TIME, SUBT])` to wrapper script
6. **Teams Filter** — match against configurable bundle ID set; allowlist requires sender+body present; blocklist for noise patterns
7. **JSON Builder** — construct payload with sender, chat_name, body, timestamp, is_truncated flag
8. **Webhook Poster** — `urllib.request.Request` with timeout; log-and-skip on failure

**Critical data flow:** usernoted writes to SQLite → WAL file modified → kqueue fires → query for new rec_ids → decode plist blobs → subprocess call to handler → filter Teams messages → build JSON → POST to webhook → persist rec_id to state file.

**State management decision:** State lives in nchook (recommended). nchook tracks last_rec_id in memory and persists to file after processing. Handler is stateless—receives args, filters, posts, exits. This avoids race conditions and keeps handler simple. Atomic file writes (write to temp, rename) prevent corruption.

### Critical Pitfalls

1. **SQLite database locking causes silent data loss** — Default sqlite3 connection modes conflict with usernoted's WAL writes. **Avoid:** Always open read-only (`?mode=ro` URI), set `PRAGMA busy_timeout`, never hold transactions open long, handle `SQLITE_BUSY` gracefully. **Phase 1 blocker.**

2. **kqueue on WAL file fires unreliably or misses events** — WAL checkpoints, file recreation, and event coalescing cause missed notifications. **Avoid:** Query for ALL new rec_ids after each event (not just one), implement fallback poll timer (5-10s), detect WAL inode changes and re-register. **Phase 1 blocker.**

3. **Sequoia DB path and schema divergence** — macOS versions change DB location and schema without documentation. **Avoid:** Detect macOS version, validate path exists on startup, introspect schema with `PRAGMA table_info`, fail loudly with clear errors. **Phase 1 blocker.**

4. **Binary plist parsing fragility** — Notification plists have nested structure, abbreviated keys (`titl`/`subt`/`body`), and missing fields. **Avoid:** Use `plistlib.loads()`, always use `.get()` with defaults, log raw plist on parse failures, test with multiple notification types. **Phase 1-2 validation.**

5. **Full Disk Access (FDA) permission not granted** — macOS sandbox silently fails reads of `~/Library/Group Containers/` without FDA. **Avoid:** Startup self-test (attempt DB read), detect zero records when DB exists, print actionable FDA instructions, document in README with screenshots. **Phase 1 blocker.**

6. **Teams bundle ID fragmentation** — Multiple Teams versions (`com.microsoft.teams2`, `com.microsoft.teams`, potential beta/nightly) have different bundle IDs. **Avoid:** Use configurable set of IDs (not single string), log watched IDs on startup, scan DB for unknown "teams" IDs. **Phase 2 filtering.**

7. **rec_id state persistence race conditions** — Crash between processing and persist causes duplicates; crash during file write corrupts state. **Avoid:** Atomic file writes (temp + rename), accept at-least-once delivery semantic (persist after webhook success), use high-water mark (simpler than set). **Phase 1 state management.**

8. **Foreground suppression creates invisible gaps** — macOS/Teams suppress notifications for active/focused chats; messages arrive but no DB record. **Avoid:** Document limitation prominently, include notice in webhook payload, this is fundamental constraint of DB-watching approach. **Phase 3 documentation.**

9. **Notification cleanup/purge causes negative rec_id delta** — macOS purges old notifications or recreates DB; persisted rec_id higher than MAX(rec_id) in DB means query returns zero forever. **Avoid:** On startup, compare persisted rec_id to `MAX(rec_id)`; reset if persisted > max; log warnings. **Phase 1 startup validation.**

10. **Teams notification format variations break allowlist** — Sender in title vs subtitle vs combined; group chat vs channel vs 1:1 formats differ; reactions/meetings look like messages. **Avoid:** Content-based filters (not just field presence), configurable blocklist of noise patterns, log all filter decisions at DEBUG, accept some noise is unavoidable. **Phase 2 filtering.**

## Implications for Roadmap

Based on research, suggested phase structure with strong dependency ordering:

### Phase 1: Core DB Watcher Foundation
**Rationale:** Everything depends on nchook correctly watching the Sequoia DB and extracting the subtitle field. This is the critical path. No other phase can begin until DB watching, plist decoding, and state persistence are proven reliable.

**Delivers:**
- Patched nchook with Sequoia DB path detection
- Binary plist decoding with `subt` extraction
- Callback dispatch with 5 arguments (APP, TITLE, BODY, TIME, SUBT)
- State file persistence (atomic writes, high-water mark)
- Startup validation (FDA check, DB path exists, schema introspection)
- kqueue + fallback poll timer for reliability
- Read-only SQLite access with lock handling

**Addresses features:**
- macOS Sequoia DB path support (table stakes)
- Chat/channel name extraction (table stakes)
- State persistence (table stakes)
- Timestamp extraction (table stakes)

**Avoids pitfalls:**
- Pitfall 1: SQLite locking (read-only mode, busy_timeout)
- Pitfall 2: kqueue unreliability (fallback timer, batch queries)
- Pitfall 3: Sequoia path/schema divergence (detection, validation)
- Pitfall 4: Plist parsing fragility (defensive `.get()`, error handling)
- Pitfall 5: FDA permission failure (startup self-test, actionable error)
- Pitfall 7: State persistence races (atomic writes)
- Pitfall 9: DB purge/rec_id reset (startup MAX(rec_id) check)

**Validation criteria:**
- Test with concurrent writes (no SQLITE_BUSY crashes)
- Kill -9 during operation, verify restart behavior (no state corruption)
- Parse 5+ different notification types without error
- Verify notifications captured during WAL checkpoint window
- Test on fresh macOS user account without FDA (clear error message)

**Needs research:** No—nchook's kqueue/SQLite approach is proven, stdlib APIs are well-documented. Execution is straightforward.

### Phase 2: Teams Filtering and JSON Construction
**Rationale:** With nchook reliably dispatching notifications, build the filtering layer that isolates Teams messages and the JSON formatting layer that structures data for webhook delivery. Filtering must handle Teams notification format variations (group vs channel vs 1:1) and reject noise (reactions, calls, meetings). This phase depends entirely on Phase 1's correct extraction of APP, TITLE, BODY, TIME, SUBT.

**Delivers:**
- Wrapper script (shell or Python) receiving nchook args
- Teams bundle ID filtering (configurable set: `com.microsoft.teams2`, `com.microsoft.teams`)
- Allowlist logic (require sender+body present, not "Microsoft Teams" system alerts)
- Blocklist for noise patterns (reactions, calls, meetings, typing indicators)
- JSON payload construction with schema: `{sender, chat_name, body, timestamp, is_truncated, source}`
- Truncation detection heuristic (length >= 148 chars)
- Config file loading and validation (webhook URL, bundle IDs, log level)

**Addresses features:**
- Teams bundle ID filtering (table stakes)
- Sender + message extraction (table stakes)
- Allowlist filtering (table stakes)
- Structured JSON payload (table stakes)
- JSON config file (table stakes)
- Truncation detection flag (differentiator)
- Dry-run mode (differentiator)

**Uses stack elements:**
- `json` (config parsing, payload serialization)
- `logging` (filter decisions at DEBUG)

**Avoids pitfalls:**
- Pitfall 6: Bundle ID fragmentation (configurable set)
- Pitfall 10: Format variations (content-based filters, blocklist)

**Validation criteria:**
- Test with both `com.microsoft.teams` and `com.microsoft.teams2`
- Test with group chats, channels, DMs, reactions, calls, meetings
- Verify truncation flag accuracy on long messages
- Dry-run mode prints JSON without POSTing
- Config validation rejects missing webhook URL

**Needs research:** No—filtering logic is heuristic-based, no external APIs or complex patterns. May need tuning after first real-world use, but not pre-planning research.

### Phase 3: Webhook Delivery and Hardening
**Rationale:** With filtering producing clean JSON payloads, connect the output to the webhook endpoint and add operational polish (error handling, graceful shutdown, logging). This phase makes the daemon production-ready.

**Delivers:**
- HTTP POST to webhook URL with JSON body
- Timeout handling (5-10s, log-and-skip on timeout)
- Error handling (log status code, response body on non-2xx)
- Log-and-skip on webhook failure (no retries per project design)
- Entry point script (run.sh) wiring nchook to handler
- Graceful shutdown (SIGTERM/SIGINT handler, flush state)
- Logging levels (INFO for deliveries, WARNING for failures, DEBUG for filter decisions)
- End-to-end testing with real Teams notifications
- Documentation of foreground suppression limitation

**Addresses features:**
- Webhook POST delivery (table stakes)
- Graceful shutdown (differentiator)

**Uses stack elements:**
- `urllib.request` (HTTP POST)
- `signal` (shutdown handler)
- `logging` (structured output)

**Avoids pitfalls:**
- Pitfall 8: Foreground suppression (document in README, mention in payload)

**Validation criteria:**
- Webhook delivers successfully with correct Content-Type headers
- Non-responsive endpoint (timeout) doesn't hang daemon
- Burst of 10+ notifications all delivered
- SIGTERM during operation cleanly saves state
- Error logs include full context (payload, status, error)

**Needs research:** No—HTTP POST with urllib.request is straightforward. Error handling patterns are standard.

### Phase 4: Optional Enhancements
**Rationale:** After core functionality is proven, add operational conveniences that improve UX but are not blockers. These can be built incrementally based on real-world usage patterns.

**Delivers (pick based on priority):**
- Health/heartbeat signal (periodic log message or webhook POST)
- Notification type classification (DM vs channel vs mention via pattern matching)
- Startup replay window (re-process last N minutes on startup)
- Config reload without restart (SIGHUP handler)

**Deferred:**
- Content-hash deduplication (only if duplicates observed)

**Needs research:** Notification type classification may need research if Teams notification text patterns are unclear. Others are standard patterns.

### Phase Ordering Rationale

**Dependency-driven:** Phase 1 is foundational—all components depend on nchook's correct extraction. Phase 2 consumes Phase 1's output (APP/TITLE/BODY/TIME/SUBT args). Phase 3 consumes Phase 2's output (filtered JSON payloads). This is a strict pipeline dependency.

**Pitfall prevention timing:** The critical pitfalls (SQLite locking, kqueue reliability, FDA permissions, DB path detection) must be addressed in Phase 1—they are blockers to any functionality. Teams-specific pitfalls (bundle ID fragmentation, format variations) are Phase 2 concerns. Operational pitfalls (webhook failures, shutdown handling) are Phase 3 concerns.

**Grouping by component boundary:** Phase 1 is "everything in nchook," Phase 2 is "everything in the wrapper script," Phase 3 is "integration and operational hardening." This aligns with the architectural separation and allows focused testing.

**Risk mitigation:** Front-loading the hardest integration work (SQLite/kqueue/macOS permissions) into Phase 1 de-risks the project early. If these fail, the entire approach is wrong. Better to discover this in Phase 1 than after building filtering/webhook logic.

### Research Flags

**Phases with standard patterns (skip research-phase):**
- **Phase 1:** kqueue + SQLite + plistlib are well-documented stdlib APIs; nchook's approach is proven
- **Phase 2:** Filtering logic is heuristic-based; config loading is standard JSON parsing
- **Phase 3:** HTTP POST with urllib.request is straightforward; signal handlers are standard patterns

**Phases that may need research if issues arise:**
- **Phase 2 (notification type classification):** If Teams notification text patterns are unclear after initial testing, may need targeted research on Teams notification format variations. Not needed pre-planning—wait for real-world data.
- **Phase 4 (startup replay window):** Time zone handling and DB timestamp formats may need investigation if added.

**No pre-planning research needed.** The research conducted covers the domain thoroughly. Phase-specific research should be triggered only if unexpected issues arise during implementation (e.g., Sequoia schema differs from training data, Teams formats are unrecognizable).

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All technologies are Python stdlib; verified Python 3.12.7 installed; stdlib APIs stable and well-documented |
| Features | HIGH | Directly derived from PROJECT.md requirements and nchook's proven patterns; table stakes list aligns with project goals |
| Architecture | MEDIUM-HIGH | nchook's approach is proven, but exact Sequoia DB schema column names not verified live; plist key names reconstructed from training data |
| Pitfalls | MEDIUM-HIGH | SQLite/kqueue/FDA pitfalls are well-documented; Teams notification format variations are inferred (not observed on Sequoia directly) |

**Overall confidence:** MEDIUM-HIGH

Research is strong on technical approach (kqueue, SQLite, stdlib usage) and known macOS pitfalls (FDA, DB locking, WAL checkpoints). Uncertainty centers on Sequoia-specific details that can't be verified without live access: exact DB schema, exact plist key names, exact Teams notification formats. These are validation concerns for Phase 1 implementation, not roadmap blockers.

### Gaps to Address

**During Phase 1 implementation (verification needed):**
- **Sequoia DB schema:** Verify table/column names with `sqlite3 <db-path> ".schema"`—confirm `record`, `app`, `data`, `rec_id`, `delivered_date` columns exist as expected
- **Plist key names:** Decode a real Teams notification blob and confirm keys are `titl`, `subt`, `body`, `date`, `app`—if different, adjust parser
- **Teams bundle ID:** Check which Teams version is installed (`com.microsoft.teams2` vs `com.microsoft.teams`)—verify with `SELECT DISTINCT identifier FROM app WHERE identifier LIKE '%teams%'`
- **WAL file path:** Confirm `db-wal` exists alongside `db` file at expected location

**During Phase 2 implementation (tuning needed):**
- **Allowlist/blocklist patterns:** Collect sample notifications (DMs, channels, reactions, calls, meetings) and validate filter logic; tune as needed
- **Truncation threshold:** Verify ~148 char limit is accurate for current macOS/Teams; adjust heuristic if needed

**Not gaps—just uncertainty:** These are not missing research, they are "verify assumptions during implementation" items. The architecture and approach are sound regardless; implementation details may need minor adjustment.

**No showstoppers identified.** All gaps are validation/tuning concerns, not fundamental unknowns. Roadmap can proceed with high confidence.

## Sources

### Primary (HIGH confidence)
- `.planning/PROJECT.md` — project requirements, constraints, out-of-scope items
- Python 3.12 stdlib documentation — `sqlite3`, `plistlib`, `select.kqueue`, `urllib.request` (stable APIs, unlikely to change)
- macOS kqueue documentation — BSD kernel API (stable, well-documented)
- SQLite WAL mode documentation — checkpoint behavior, read-only access patterns

### Secondary (MEDIUM confidence)
- nchook project architecture — kqueue-based notification watching, subprocess dispatch pattern (described in PROJECT.md; source code not reviewed)
- macOS notification center DB internals — schema reconstructed from training data (Apple does not document; community reverse-engineering efforts)
- macOS Sequoia notification DB path — confirmed as `~/Library/Group Containers/group.com.apple.usernoted/db2/db` in PROJECT.md

### Tertiary (LOW confidence, needs validation)
- Binary plist key names (`titl`, `subt`, `body`) — training data knowledge; should be verified against live Sequoia DB
- Teams notification format patterns — training data knowledge; may have changed post-cutoff; needs validation with real notifications
- macOS Sequoia Python version (3.12+) — verified 3.12.7 is installed on this machine; assumed via Xcode CLT but not verified explicitly

---
*Research completed: 2026-02-11*
*Ready for roadmap: yes*
