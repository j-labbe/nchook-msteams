# Phase 5: Config, Gating, and Event Loop Integration - Research

**Researched:** 2026-02-11
**Domain:** Notification gating logic, config integration, and event loop modification in a single-file Python daemon
**Confidence:** HIGH

## Summary

Phase 5 wires the Phase 4 `detect_user_status(config)` function into the existing daemon event loop to gate notification forwarding based on detected status. This is primarily an integration phase -- no new external tools, no new dependencies, no new subprocess calls. The work consists of: (1) adding a `status_enabled` config key with default `true`, (2) calling `detect_user_status()` once per poll cycle at the top of the event loop, (3) implementing a gate function that forwards on Away/Busy/Unknown and suppresses on Available/Offline/DoNotDisturb/BeRightBack, (4) ensuring rec_id always advances even when gated, (5) adding status metadata fields to the webhook payload, and (6) extending the startup summary to show status detection mode.

The existing event loop in `run_watcher()` (lines 777-949 of nchook.py) already follows a clean structure: wait for kqueue/poll -> query notifications -> filter each -> build payload -> post webhook -> save state. The gating logic inserts between the wait step and the per-notification processing. The critical design decision is that even when gating suppresses forwarding, the daemon MUST still query notifications and advance the rec_id high-water mark (GATE-03). This prevents stale notification replay when status transitions from Available to Away.

The implementation touches exactly four existing functions (`load_config`, `build_webhook_payload`, `print_startup_summary`, `run_watcher`) and adds one new function (`should_forward_status`). No new imports are needed. Estimated delta is ~60-80 LOC, bringing the total to approximately 1020-1040 LOC.

**Primary recommendation:** Implement gating as a single new function and a focused modification to `run_watcher()`. Keep the gate function pure (status dict in, bool out). Always query and advance rec_id regardless of gating decision. Add status metadata to every forwarded payload using the existing underscore-prefix convention.

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `json` (stdlib) | Python 3.12.7 | Config loading, payload serialization | Already imported line 16 |
| `logging` (stdlib) | Python 3.12.7 | Gating decision logging | Already imported line 17 |
| `time` (stdlib) | Python 3.12.7 | Timestamp formatting in payload | Already imported line 20 |

### No New Imports Required

Every module needed for Phase 5 is already imported in nchook.py. The phase adds pure Python logic that calls the existing `detect_user_status()` from Phase 4 and modifies existing functions with additional dict keys and conditional branches.

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Hardcoded forward/suppress sets | Configurable `forward_statuses` list in config.json | GATE-01 specifies exact policy (Away/Busy forward, rest suppress). Making it configurable is a v2 item (SREF-04). Hardcode now, add config later. |
| Per-cycle status check | Cached status with TTL | Unnecessary complexity. GATE-04 says "once per poll cycle" which is already rate-limited by the 5-second poll interval. The subprocess overhead is ~20ms per cycle (0.4%). No caching needed. |
| Separate gate function | Inline gating logic in run_watcher | Separate function is testable, readable, and matches the existing pattern of small focused functions. |

## Architecture Patterns

### Recommended Project Structure

All new code goes into `nchook.py` (single-file architecture, matching all prior phases):

```
nchook.py (modifications for Phase 5)
  # Configuration section:
  DEFAULT_CONFIG           # Add: "status_enabled": True, "idle_threshold_seconds": 300
  load_config()            # Add: validate status_enabled is bool

  # Status Detection section (between existing functions):
  should_forward_status()  # NEW: gate function (after detect_user_status)

  # Webhook Delivery section:
  build_webhook_payload()  # MODIFY: accept status_result, add 3 metadata fields

  # Startup section:
  print_startup_summary()  # MODIFY: show status detection mode + current status

  # Event Loop section:
  run_watcher()            # MODIFY: add status check + gating logic
```

### Pattern 1: Status Gate as Pure Function

**What:** A pure function that takes a status result dict and config, returns True (forward) or False (suppress). No side effects, no subprocess calls, no logging.

**When to use:** Every poll cycle, after `detect_user_status()` returns and before processing notifications.

**Example:**
```python
# Source: GATE-01, GATE-02 requirements
# Forward statuses: Away, Busy, Unknown
# Suppress statuses: Available, Offline, DoNotDisturb, BeRightBack
_FORWARD_STATUSES = frozenset({"Away", "Busy", "Unknown"})

def should_forward_status(status_result, config):
    """
    GATE-01, GATE-02: Decide whether to forward notifications based on status.

    Forward on Away, Busy (user not actively at computer).
    Forward on Unknown (fail-open: never silently drop).
    Suppress on Available, Offline, DoNotDisturb, BeRightBack.

    When status_enabled is False (INTG-01), always returns True (v1.0 behavior).
    """
    if not config.get("status_enabled", True):
        return True  # Status gating disabled, forward everything
    return status_result["detected_status"] in _FORWARD_STATUSES
```

### Pattern 2: Always-Query-Always-Advance

**What:** Even when the status gate suppresses forwarding, the event loop still queries for new notifications and advances the rec_id high-water mark. Only the webhook POST is skipped.

**When to use:** Always. This is the core correctness requirement (GATE-03).

**Example:**
```python
# Source: GATE-03, GATE-04 requirements
# Inside run_watcher() event loop:

# [NEW] Check status once per poll cycle (GATE-04)
status_result = detect_user_status(config) if config.get("status_enabled", True) else None
forward = should_forward_status(status_result, config) if status_result else True

# Query notifications regardless of gating decision (GATE-03)
notifications = query_new_notifications(conn, last_rec_id)

for notif in notifications:
    # Content filter still runs (orthogonal to status gate)
    if config is not None and not passes_filter(notif, config):
        continue

    if forward:
        # Existing notification processing: log, classify, build payload, post webhook
        msg_type = classify_notification(notif)
        payload = build_webhook_payload(notif, msg_type, status_result)
        post_webhook(payload, config["webhook_url"], ...)
    else:
        logging.debug(
            "Status gate suppressed: status=%s sender=%s",
            status_result["detected_status"], notif["title"],
        )

# Always advance high-water mark (GATE-03)
if notifications:
    last_rec_id = notifications[-1]["rec_id"]
    save_state(last_rec_id, state_path)
```

### Pattern 3: Payload Metadata with Underscore Convention

**What:** Status metadata fields use the underscore prefix (`_detected_status`, `_status_source`, `_status_confidence`) matching the existing `_source` and `_truncated` conventions.

**When to use:** Every forwarded webhook payload.

**Example:**
```python
# Source: INTG-02 requirement, matching existing _source and _truncated fields
def build_webhook_payload(notif, msg_type, status_result=None):
    payload = {
        "senderName": notif.get("title", ""),
        "chatId": notif.get("subtitle", ""),
        "content": notif.get("body", ""),
        "timestamp": ts_formatted,
        "type": msg_type,
        "subtitle": notif.get("subtitle", ""),
        "_source": "macos-notification-center",
        "_truncated": detect_truncation(notif.get("body", "")),
    }
    # v1.1: Status metadata (INTG-02)
    if status_result is not None:
        payload["_detected_status"] = status_result["detected_status"]
        payload["_status_source"] = status_result["status_source"]
        payload["_status_confidence"] = status_result["status_confidence"]
    return payload
```

### Anti-Patterns to Avoid

- **Checking status per notification:** Status is a property of the user, not the notification. GATE-04 explicitly requires checking once per poll cycle. Never call `detect_user_status()` inside the `for notif in notifications` loop.
- **Skipping query when gated:** If the daemon skips `query_new_notifications()` when status is Available, the rec_id never advances. When status transitions to Away, all accumulated notifications replay as a stale burst. GATE-03 prevents this.
- **Using `forward_statuses` config key in Phase 5:** The requirements (GATE-01) specify exact forward/suppress policy. A configurable `forward_statuses` is SREF-04, a v2 deferred item. Hardcode the set in Phase 5.
- **Adding status_result as a run_watcher parameter:** The status check happens INSIDE the event loop, not before it. `run_watcher()` already receives `config` which contains `status_enabled`. The status is checked each iteration, not once at startup.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Status caching/TTL | TTL-based cache with timestamps | Direct call to `detect_user_status()` per cycle | GATE-04 says "once per poll cycle." The 5s poll interval already rate-limits. Subprocess overhead is 20ms per cycle. Caching adds complexity (stale data, sleep/wake issues) for 0.4% CPU savings. |
| Configurable gating rules | Custom forward_statuses config parsing | Hardcoded `_FORWARD_STATUSES` frozenset | Requirements (GATE-01) lock the policy. SREF-04 (configurable) is v2 deferred. |
| Notification replay prevention | Custom replay buffer or deduplication | Always-advance rec_id pattern (GATE-03) | The rec_id high-water mark already handles this. Advancing it when gated is sufficient. |

**Key insight:** Phase 5 is an integration phase, not a features phase. The complexity is in correctly modifying the event loop without breaking existing behavior, not in building new subsystems. Every "don't hand-roll" item is about avoiding premature optimization or scope creep.

## Common Pitfalls

### Pitfall 1: Not Advancing rec_id When Gated (GATE-03 Violation)

**What goes wrong:** When status is Available (suppress), the daemon skips `query_new_notifications()` entirely. rec_id stays at the value from the last forwarded batch. When status transitions to Away, the daemon queries with the stale rec_id and replays all notifications from the Available period -- potentially hundreds of messages the user already read.

**Why it happens:** The natural optimization is "if we're not forwarding, why query?" But the high-water mark MUST advance to prevent replay.

**How to avoid:** Always call `query_new_notifications()`. Always advance `last_rec_id` from the last notification in the batch. Only skip the webhook POST.

**Warning signs:** After status transition to Away, a burst of stale notifications appears in the webhook. Logs show "Notification | app=com.microsoft.teams2" entries with timestamps from hours ago.

### Pitfall 2: Breaking Existing v1.0 Behavior When status_enabled Is False

**What goes wrong:** The `status_enabled: false` config disables gating, but the code path for disabled mode has a bug: it still calls `detect_user_status()` (wasting 20ms), or worse, it accidentally gates because the `should_forward_status()` function is called with a `None` status_result and crashes.

**Why it happens:** The disabled path is not tested. Developers focus on the enabled path.

**How to avoid:** When `status_enabled` is False: do NOT call `detect_user_status()` (skip subprocess overhead entirely). Set `status_result = None` and `forward = True`. The `build_webhook_payload()` function already handles `status_result=None` by omitting the metadata fields. This preserves exact v1.0 payload format.

**Warning signs:** With `status_enabled: false`, the daemon still calls ioreg/pgrep (visible in process table), or payloads include `_detected_status: null` instead of omitting the field.

### Pitfall 3: Status Check Placement in Event Loop

**What goes wrong:** The status check is placed AFTER `query_new_notifications()` instead of BEFORE. This means the daemon queries the DB, finds notifications, then checks status and decides to suppress. The work was wasted, but more importantly, if the status check is slow (unlikely at 20ms, but possible under load), the notifications sit in memory during the check.

**Why it happens:** Developer follows the existing flow linearly and appends the status check after the query.

**How to avoid:** Place `detect_user_status()` immediately after the kqueue/poll wait, BEFORE `query_new_notifications()`. This is correct per GATE-04: "checks status once per poll cycle before processing the notification batch."

**Warning signs:** Logs show query happening before status check. Performance profile shows unnecessary DB access when status is Available.

### Pitfall 4: build_webhook_payload Signature Breaking Existing Call Sites

**What goes wrong:** Adding `status_result` as a required parameter to `build_webhook_payload()` breaks the existing call in `run_watcher()` if not all call sites are updated.

**Why it happens:** The function signature changes but the developer only updates one call site.

**How to avoid:** Add `status_result=None` as a keyword argument with a default value. This makes the parameter optional and preserves backward compatibility. The existing call `build_webhook_payload(notif, msg_type)` continues to work and simply omits status metadata.

**Warning signs:** `TypeError: build_webhook_payload() missing 1 required positional argument` at runtime.

### Pitfall 5: Startup Status Check Blocking

**What goes wrong:** The startup summary calls `detect_user_status()` (INTG-03) during initialization, before the event loop starts. If idle detection or process check hangs (unlikely but possible during boot), startup is delayed.

**Why it happens:** The startup summary is synchronous.

**How to avoid:** Use the same timeout-protected `detect_user_status()` that the event loop uses. The 5-second timeout per subprocess call is already enforced by Phase 4's implementation. Worst case startup adds 10 seconds (two timeouts). Log the status result and continue.

**Warning signs:** Daemon takes >10 seconds to start. Startup summary shows "Unknown" when Teams is running.

## Code Examples

Verified patterns derived from the existing codebase and requirements:

### Config Changes (INTG-01)

```python
# Source: INTG-01, existing DEFAULT_CONFIG at line 366-371
DEFAULT_CONFIG = {
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10,
    # v1.1 status detection
    "status_enabled": True,
    "idle_threshold_seconds": 300,
}
```

Note: `idle_threshold_seconds` is already read by `detect_user_status()` (line 699) with a default of 300. Adding it to `DEFAULT_CONFIG` makes it visible in the config schema and documentable.

### Gate Function (GATE-01, GATE-02)

```python
# Source: GATE-01 (forward/suppress policy), GATE-02 (fail-open)
_FORWARD_STATUSES = frozenset({"Away", "Busy", "Unknown"})

def should_forward_status(status_result, config):
    """
    Decide whether to forward notifications based on detected status.

    GATE-01: Forward on Away or Busy. Suppress on Available, Offline,
             DoNotDisturb, or BeRightBack.
    GATE-02: Forward on Unknown (fail-open policy).
    INTG-01: When status_enabled is False, always forward (v1.0 behavior).

    Returns True to forward, False to suppress.
    """
    if not config.get("status_enabled", True):
        return True
    return status_result["detected_status"] in _FORWARD_STATUSES
```

### Event Loop Modification (GATE-03, GATE-04)

```python
# Source: existing run_watcher() lines 826-891, modified for status gating
# Inside the `while running:` loop, after kqueue/poll wait:

# --- Status gating (GATE-04: once per poll cycle) ---
status_enabled = config.get("status_enabled", True) if config else False
if status_enabled:
    status_result = detect_user_status(config)
    forward = should_forward_status(status_result, config)
    if not forward:
        logging.debug(
            "Status gate: suppressing (status=%s, source=%s)",
            status_result["detected_status"],
            status_result["status_source"],
        )
else:
    status_result = None
    forward = True

# Query for new notifications (GATE-03: always query, even when gated)
notifications = query_new_notifications(conn, last_rec_id)

for notif in notifications:
    if config is not None and not passes_filter(notif, config):
        logging.debug(
            "Filtered: app=%s title=%s body=%.50s",
            notif["app"], notif["title"], notif.get("body", ""),
        )
        continue

    if not forward:
        # Status gate suppresses this notification, skip webhook
        continue

    # --- Existing notification processing (unchanged) ---
    # Log, classify, build payload, post webhook
    msg_type = classify_notification(notif)
    payload = build_webhook_payload(notif, msg_type, status_result)
    if dry_run:
        logging.info("DRY-RUN | Would POST to %s:\n%s", ...)
    else:
        post_webhook(payload, config["webhook_url"], ...)

# GATE-03: Always advance high-water mark, even when gated
if notifications:
    last_rec_id = notifications[-1]["rec_id"]
    save_state(last_rec_id, state_path)
```

### Payload Modification (INTG-02)

```python
# Source: existing build_webhook_payload() lines 547-569, INTG-02 requirement
def build_webhook_payload(notif, msg_type, status_result=None):
    """
    Build JSON-serializable webhook payload from notification.

    INTG-02: When status_result is provided, includes _detected_status,
    _status_source, and _status_confidence metadata fields.
    """
    ts = notif.get("timestamp", 0)
    if ts > 0:
        ts_formatted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    else:
        ts_formatted = None

    payload = {
        "senderName": notif.get("title", ""),
        "chatId": notif.get("subtitle", ""),
        "content": notif.get("body", ""),
        "timestamp": ts_formatted,
        "type": msg_type,
        "subtitle": notif.get("subtitle", ""),
        "_source": "macos-notification-center",
        "_truncated": detect_truncation(notif.get("body", "")),
    }

    # v1.1: Status metadata (INTG-02)
    if status_result is not None:
        payload["_detected_status"] = status_result["detected_status"]
        payload["_status_source"] = status_result["status_source"]
        payload["_status_confidence"] = status_result["status_confidence"]

    return payload
```

### Startup Summary Modification (INTG-03)

```python
# Source: existing print_startup_summary() lines 338-358, INTG-03 requirement
def print_startup_summary(db_path, last_rec_id, config=None, dry_run=False):
    logging.info("=" * 60)
    logging.info("Teams Notification Interceptor")
    logging.info("=" * 60)
    logging.info("  DB path:     %s", db_path)
    logging.info("  FDA status:  OK")
    logging.info("  Last rec_id: %d", last_rec_id)
    if config is not None:
        logging.info("  Webhook URL: %s", config.get("webhook_url", "NOT SET"))
        logging.info("  Bundle IDs:  %s", ", ".join(sorted(config.get("bundle_ids", []))))
        logging.info("  Poll interval: %.1fs", config.get("poll_interval", POLL_FALLBACK_SECONDS))
        logging.info("  Log level:   %s", config.get("log_level", "INFO"))
        # v1.1: Status detection info (INTG-03)
        status_enabled = config.get("status_enabled", True)
        logging.info("  Status gate: %s", "ENABLED" if status_enabled else "DISABLED")
        if status_enabled:
            status_result = detect_user_status(config)
            logging.info(
                "  Current status: %s (source=%s, confidence=%s)",
                status_result["detected_status"],
                status_result["status_source"],
                status_result["status_confidence"],
            )
    if dry_run:
        logging.info("  Mode:        DRY-RUN (no HTTP requests)")
    logging.info("=" * 60)
```

## Existing Code Integration Points

Exact line numbers and functions that need modification (from current nchook.py):

| Function | Current Lines | Change | Impact |
|----------|---------------|--------|--------|
| `DEFAULT_CONFIG` | 366-371 | Add `"status_enabled": True`, `"idle_threshold_seconds": 300` | 2 lines added |
| `load_config()` | 374-411 | No change needed -- `config.update(user_config)` already merges new defaults | 0 lines changed |
| `build_webhook_payload()` | 547-569 | Add `status_result=None` param, add 3 metadata fields | ~8 lines added |
| `print_startup_summary()` | 338-358 | Add status detection display block | ~8 lines added |
| `run_watcher()` | 777-949 | Add status check after kqueue wait, restructure notification loop for gating | ~25 lines added/modified |
| NEW: `should_forward_status()` | After line 746 | New function in Status Detection section | ~15 lines |
| NEW: `_FORWARD_STATUSES` | After line 746 | Constant frozenset | 1 line |

**Total estimated delta:** ~60-80 new/modified LOC

### Critical: run_watcher() Modification Strategy

The event loop modification is the most sensitive change. The current loop (lines 826-891) has this structure:

```
while running:
    [kqueue wait or sleep]
    notifications = query_new_notifications(conn, last_rec_id)
    for notif in notifications:
        [filter -> log -> classify -> build payload -> post webhook]
    if notifications:
        last_rec_id = notifications[-1]["rec_id"]
        save_state(last_rec_id, state_path)
    [WAL recreation handling]
```

The modified loop becomes:

```
while running:
    [kqueue wait or sleep]
    [NEW: status check + gate decision]
    notifications = query_new_notifications(conn, last_rec_id)
    for notif in notifications:
        [filter -> (gate check) -> log -> classify -> build payload -> post webhook]
    if notifications:
        last_rec_id = notifications[-1]["rec_id"]  # ALWAYS advance (GATE-03)
        save_state(last_rec_id, state_path)
    [WAL recreation handling]
```

The key constraint: the `if notifications: last_rec_id = ... save_state(...)` block at the bottom MUST remain unconditional (not wrapped in `if forward:`). This is the GATE-03 compliance point.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| v1.0: Forward all notifications | v1.1: Gate based on detected status | Phase 5 (this phase) | Core feature: suppress notifications when user is at desk |
| ARCHITECTURE.md: `forward_statuses` config | Requirements: hardcoded policy in GATE-01 | Requirements finalized | Simpler implementation; configurable policy is SREF-04 (v2) |
| ARCHITECTURE.md: `raw_value` in status dict | Phase 4: 3-field dict only (detected_status, status_source, status_confidence) | Phase 4 implementation | Leaner dict; payload uses the 3 canonical fields |
| ARCHITECTURE.md: `status` key name | Phase 4: `detected_status` key name | Phase 4 implementation | Matches INTG-02 payload field name exactly |
| ARCHITECTURE.md: `status_ax_enabled` config | Phase 5 scope: only `status_enabled` | Requirements INTG-01 | AX-specific config deferred to Phase 6 |

**Deprecated/outdated from prior research:**
- ARCHITECTURE.md `forward_statuses` config key: Not in Phase 5 requirements (SREF-04 is v2). Use hardcoded `_FORWARD_STATUSES` frozenset.
- ARCHITECTURE.md `raw_value` field in status dict: Phase 4 implemented without it. Do not add it in Phase 5.
- ARCHITECTURE.md `status_ax_enabled` config: Phase 6 scope. Phase 5 only adds `status_enabled`.
- ARCHITECTURE.md status dict key `"status"`: Phase 4 uses `"detected_status"`. All Phase 5 code must use the Phase 4 key names.
- PITFALLS.md TTL caching recommendation: Not needed for Phase 5. GATE-04 says once per poll cycle (already rate-limited at 5s). Caching adds complexity for <1% benefit.

## Requirement-to-Code Mapping

| Requirement | Implementation | Function | Verification |
|-------------|---------------|----------|--------------|
| GATE-01 | `_FORWARD_STATUSES = {"Away", "Busy", "Unknown"}`, suppress all others | `should_forward_status()` | Forward on Away/Busy, suppress on Available/Offline |
| GATE-02 | Unknown is in `_FORWARD_STATUSES` | `should_forward_status()` | Verify Unknown causes forward |
| GATE-03 | `if notifications: last_rec_id = ... save_state(...)` outside `if forward:` | `run_watcher()` | rec_id advances when status is Available |
| GATE-04 | `detect_user_status()` called once at top of while loop, before notification processing | `run_watcher()` | Status check is outside the per-notification for loop |
| INTG-01 | `config.get("status_enabled", True)` guards status check | `should_forward_status()`, `run_watcher()` | With `status_enabled: false`, all notifications forward |
| INTG-02 | `payload["_detected_status"] = ...` (3 fields) | `build_webhook_payload()` | Forwarded payloads include 3 status metadata fields |
| INTG-03 | Status detection mode + current status in startup banner | `print_startup_summary()` | Startup output shows ENABLED/DISABLED and current status |

## Open Questions

1. **Should gating log at INFO or DEBUG level when suppressing?**
   - What we know: The existing filter pipeline logs filtered notifications at DEBUG level (line 847). Gating suppression is a similar "didn't forward" event.
   - What's unclear: Whether operators want to see suppression counts without enabling DEBUG.
   - Recommendation: Log individual suppressions at DEBUG. Log a summary count at INFO at the end of each suppressed batch: `"Status gate: suppressed %d notifications (status=%s)"`. This keeps INFO clean while providing operational visibility.

2. **Should config.json on disk be updated to include status_enabled?**
   - What we know: The current config.json has 5 keys. DEFAULT_CONFIG provides defaults for missing keys.
   - What's unclear: Whether to leave the user's config.json untouched (relying on defaults) or update it.
   - Recommendation: Do NOT modify the user's config.json file. The defaults in `DEFAULT_CONFIG` handle the new keys. Users can add `status_enabled: false` when they want to disable gating. This follows the existing pattern where `webhook_timeout` has a default and is optional in the config file.

3. **Should the startup status check share the same code path as the event loop status check?**
   - What we know: Both call `detect_user_status(config)`.
   - What's unclear: Whether to factor out a shared helper or just call the function twice.
   - Recommendation: Call `detect_user_status(config)` directly in both places. The function is already self-contained. No wrapper needed.

## Sources

### Primary (HIGH confidence)
- **nchook.py source code** (997 lines, verified 2026-02-11) - All line numbers, function signatures, and code patterns cited in this research are from the current file.
- **Phase 4 RESEARCH.md** - `detect_user_status()` API, return dict shape (`detected_status`, `status_source`, `status_confidence`), performance budget (20ms per cycle).
- **Phase 4 VERIFICATION.md** - Confirmed all 5 Phase 4 must-haves are satisfied. `detect_user_status(config)` is verified working and ready for integration.
- **REQUIREMENTS.md** - GATE-01 through GATE-04, INTG-01 through INTG-03 requirements text. Authoritative source for what Phase 5 must implement.
- **ARCHITECTURE.md** - Event loop modification pattern, always-query-always-advance design, payload metadata convention. Note: some details superseded by Phase 4 implementation (key names, dict shape).

### Secondary (MEDIUM confidence)
- **PITFALLS.md** - Pitfalls 1 (subprocess blocking), 10 (gating edge cases), 13 (payload metadata). Informed pitfall analysis but some recommendations (caching, configurable forward_statuses) are v2 scope.
- **PROJECT.md** - Constraints (stdlib only, single file, log-and-skip philosophy). Confirmed no new dependencies needed.

### Tertiary (LOW confidence)
- None. Phase 5 is an integration phase working entirely with known, verified code. No external research was needed beyond the existing codebase and planning artifacts.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - No new dependencies. All code uses existing imports and patterns.
- Architecture: HIGH - Integration points are well-defined. Event loop structure is clear. Phase 4 API is verified.
- Pitfalls: HIGH - Primary risk (GATE-03 rec_id advancement) is well-understood and explicitly called out in requirements and ARCHITECTURE.md.

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (30 days -- this is integration of stable components, no external dependencies that could change)
