# Architecture: Status-Aware Notification Gating (v1.1)

**Domain:** Integrating user status detection into existing macOS notification daemon
**Researched:** 2026-02-11
**Confidence:** MEDIUM (AppleScript AX approach has significant uncertainty; ioreg and pgrep are well-understood)

## Existing Architecture (v1.0 Baseline)

### Current Pipeline

```
main()
  |
  +-- argparse (--dry-run)
  +-- load_config()                    # config.json -> dict
  +-- detect_db_path()                 # -> (db_path, wal_path)
  +-- validate_environment(db_path)    # FDA check, schema verify
  +-- signal handlers (SIGINT/SIGTERM)
  +-- run_watcher(db_path, wal_path, state_path, config, dry_run)
        |
        +-- load_state()               # state.json -> last_rec_id
        +-- check_db_consistency()     # purge detection
        +-- print_startup_summary()
        +-- create_wal_watcher()       # kqueue setup
        |
        +-- EVENT LOOP:
              |
              +-- kqueue.control() or time.sleep(poll_interval)
              +-- query_new_notifications(conn, last_rec_id)
              |     |
              |     +-- parse_notification() per row   # binary plist
              |
              +-- FOR EACH notification:
              |     +-- passes_filter(notif, config)   # 4-stage filter
              |     +-- classify_notification(notif)    # DM/channel/mention
              |     +-- build_webhook_payload(notif, msg_type)
              |     +-- post_webhook() or DRY-RUN log
              |
              +-- save_state(last_rec_id)
              +-- WAL recreation handling
```

### Current Functions (849 LOC, single file nchook.py)

| Function | LOC | Modifiable? | Notes |
|----------|-----|-------------|-------|
| `detect_db_path()` | 40 | No | Unchanged for v1.1 |
| `validate_environment()` | 50 | No | Unchanged for v1.1 |
| `parse_notification()` | 30 | No | Unchanged for v1.1 |
| `query_new_notifications()` | 30 | No | Unchanged for v1.1 |
| `save_state()` / `load_state()` | 40 | No | Unchanged for v1.1 |
| `check_db_consistency()` | 15 | No | Unchanged for v1.1 |
| `load_config()` | 35 | **Yes** | Add status config keys |
| `passes_filter()` | 20 | No | Content filter unchanged -- status gate is separate |
| `classify_notification()` | 25 | No | Unchanged for v1.1 |
| `build_webhook_payload()` | 20 | **Yes** | Add status metadata fields |
| `post_webhook()` | 20 | No | Unchanged for v1.1 |
| `print_startup_summary()` | 15 | **Yes** | Show status detection config |
| `run_watcher()` | 170 | **Yes** | Add status check call in loop |
| `main()` | 30 | Minor | Unchanged (config flows through) |
| `_shutdown_handler()` | 5 | No | Unchanged |

**Key insight:** Only 4 existing functions need modification. The status detection system is primarily NEW functions added to the file, not surgery on existing code.

---

## Status Detection Architecture

### Three-Signal Fallback Chain

```
detect_user_status(config)
  |
  +-- Signal 1: AX Status Text (AppleScript)
  |     Reads Teams UI status text via osascript
  |     Returns: "Available", "Away", "Busy", "Do not disturb",
  |              "Be right back", "Appear offline", or None on failure
  |     Requires: Accessibility permission granted to terminal
  |     Confidence: HIGH (when it works) -- direct Teams self-report
  |
  +-- Signal 2: System Idle Time (ioreg)
  |     Reads HIDIdleTime from IOKit registry
  |     Returns: idle_seconds (float)
  |     Maps to: "Away" if idle > threshold, "Available" if <= threshold
  |     Requires: Nothing (no special permissions)
  |     Confidence: MEDIUM -- proxy for user presence, not Teams-specific
  |
  +-- Signal 3: Process Check (pgrep)
        Checks if Teams process is running
        Returns: True/False
        Maps to: "Offline" if not running, falls through if running
        Requires: Nothing (no special permissions)
        Confidence: HIGH -- definitive for Offline detection only
```

### Fallback Chain Logic

```python
def detect_user_status(config):
    """
    Three-signal fallback chain for user status detection.

    Returns dict: {
        "status": str,          # "Available", "Away", "Busy", "Offline", "Unknown"
        "source": str,          # "ax", "idle", "process", "error"
        "confidence": str,      # "high", "medium", "low"
        "raw_value": str|None,  # raw value from detection source
    }
    """
    # Signal 1: AppleScript AX (highest fidelity)
    if config.get("status_ax_enabled", True):
        ax_result = _detect_status_ax()
        if ax_result is not None:
            return {
                "status": _normalize_ax_status(ax_result),
                "source": "ax",
                "confidence": "high",
                "raw_value": ax_result,
            }

    # Signal 2: System idle time (medium fidelity)
    idle_seconds = _detect_idle_time()
    if idle_seconds is not None:
        threshold = config.get("idle_threshold_seconds", 300)
        status = "Away" if idle_seconds > threshold else "Available"
        return {
            "status": status,
            "source": "idle",
            "confidence": "medium",
            "raw_value": str(int(idle_seconds)),
        }

    # Signal 3: Process check (low fidelity, only detects Offline)
    teams_running = _detect_teams_process(config.get("bundle_ids", set()))
    if not teams_running:
        return {
            "status": "Offline",
            "source": "process",
            "confidence": "high",
            "raw_value": "not_running",
        }

    # All signals failed or inconclusive
    return {
        "status": "Unknown",
        "source": "error",
        "confidence": "low",
        "raw_value": None,
    }
```

---

## Integration Point: Where Status Gate Goes in the Pipeline

### Decision: Status gate goes BEFORE the per-notification filter loop, not per-notification

**Rationale:**
1. Status is a property of the USER, not the notification. It does not change between notifications in the same batch.
2. The status check involves subprocess calls (osascript, ioreg, pgrep). Running these once per event loop iteration (every 5s) is acceptable. Running per-notification in a burst of 20 would be wasteful.
3. The filter pipeline (`passes_filter`) is content-based. Status gating is context-based. They are orthogonal concerns and should be separate stages.

### Modified Event Loop

```
EVENT LOOP (run_watcher):
  |
  +-- kqueue.control() or time.sleep(poll_interval)
  |
  +-- [NEW] detect_user_status(config)          # Once per loop iteration
  +-- [NEW] should_forward = passes_status_gate(status_result, config)
  |
  +-- IF should_forward:
  |     +-- query_new_notifications(conn, last_rec_id)
  |     +-- FOR EACH notification:
  |     |     +-- passes_filter(notif, config)
  |     |     +-- classify_notification(notif)
  |     |     +-- build_webhook_payload(notif, msg_type, status_result)  # [MODIFIED]
  |     |     +-- post_webhook() or DRY-RUN log
  |     +-- save_state(last_rec_id)
  |
  +-- ELSE (status gate blocks):
  |     +-- query_new_notifications(conn, last_rec_id)  # Still query!
  |     +-- logging.debug("Status gate: dropping %d notifications (status=%s)",
  |     |                  len(notifications), status_result["status"])
  |     +-- IF notifications:
  |     |     +-- last_rec_id = notifications[-1]["rec_id"]
  |     |     +-- save_state(last_rec_id)               # Still advance!
  |
  +-- WAL recreation handling (unchanged)
```

### Critical Design Decision: Always Query, Always Advance State

Even when the status gate blocks forwarding, the daemon MUST:
1. **Query new notifications** -- to get the latest rec_id
2. **Advance the high-water mark** -- so blocked notifications are not replayed when status changes

Without this, transitioning from "Available" (blocked) to "Away" (forwarded) would replay all notifications that accumulated during the Available period. That would flood the webhook with stale messages the user already saw.

---

## New Functions (to add)

### Status Detection Functions

| Function | Purpose | Subprocess? | Expected Duration |
|----------|---------|-------------|-------------------|
| `detect_user_status(config)` | Orchestrates fallback chain | No (calls below) | Sum of sub-calls |
| `_detect_status_ax()` | Run AppleScript to read Teams AX status | Yes: `subprocess.run(["osascript", "-e", ...])` | 200-500ms typical, up to 2s on cold start |
| `_normalize_ax_status(raw)` | Map raw AX text to canonical status | No | Instant |
| `_detect_idle_time()` | Run `ioreg` to get HIDIdleTime | Yes: `subprocess.run(["ioreg", ...])` | 50-150ms typical |
| `_detect_teams_process(bundle_ids)` | Run `pgrep` to check Teams running | Yes: `subprocess.run(["pgrep", ...])` | 10-50ms typical |
| `passes_status_gate(status_result, config)` | Decide forward/drop based on status | No | Instant |

### Modified Functions

| Function | Change | Scope |
|----------|--------|-------|
| `load_config()` | Add status config keys to defaults + validation | ~10 LOC added |
| `build_webhook_payload()` | Add `_detected_status`, `_status_source`, `_status_confidence` | ~5 LOC added |
| `print_startup_summary()` | Log status detection config | ~5 LOC added |
| `run_watcher()` | Add status check + gate logic before filter loop | ~20 LOC added |

**Estimated total new code:** ~200-250 LOC (bringing total to ~1050-1100 LOC). Stays manageable for single-file architecture.

---

## Detailed Function Specifications

### _detect_status_ax()

```python
# AppleScript to walk the Teams AX tree and find the status text.
# The new Teams (com.microsoft.teams2) has limited AX tree exposure.
# This script uses System Events UI scripting, which requires
# Accessibility permission for the terminal app.
#
# CRITICAL CAVEAT: The new Teams on macOS does NOT reliably expose
# its AX tree to programmatic access. VoiceOver and Accessibility
# Inspector can read it, but osascript may not. This signal is
# HIGH VALUE but LOW RELIABILITY. The fallback chain handles this.

_AX_SCRIPT = '''
tell application "System Events"
    if not (exists process "Microsoft Teams") then
        return "NOT_RUNNING"
    end if
    tell process "Microsoft Teams"
        try
            -- Walk the AX tree to find the status text element.
            -- The exact path depends on Teams version and UI state.
            -- This is the most fragile part of the system.
            set statusText to value of static text 1 of group 1 of ...
            return statusText
        on error
            return "AX_ERROR"
        end try
    end tell
end tell
'''

def _detect_status_ax():
    """
    Attempt to read Teams status via AppleScript AX tree walking.

    Returns status string on success, None on any failure.
    Timeout: 3 seconds (prevents daemon hang if osascript blocks).
    """
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _AX_SCRIPT],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            logging.debug("AX status check failed: %s", result.stderr.strip())
            return None
        raw = result.stdout.strip()
        if raw in ("NOT_RUNNING", "AX_ERROR", ""):
            return None
        return raw
    except subprocess.TimeoutExpired:
        logging.debug("AX status check timed out (3s)")
        return None
    except (FileNotFoundError, OSError):
        logging.debug("osascript not available")
        return None
```

**Confidence: LOW** -- The exact AppleScript to extract Teams status text from the AX tree needs to be discovered empirically using Accessibility Inspector on the target machine. The new Teams may not expose the status text to osascript at all. The function structure and error handling are solid; the AX_SCRIPT content is a placeholder that MUST be validated during implementation.

### _normalize_ax_status(raw)

```python
# Map Teams AX status strings to canonical values.
# Teams displays localized status text. This mapping handles English.
_AX_STATUS_MAP = {
    "available": "Available",
    "busy": "Busy",
    "do not disturb": "Busy",       # Treat DND as Busy
    "in a meeting": "Busy",         # Treat In a Meeting as Busy
    "in a call": "Busy",            # Treat In a Call as Busy
    "away": "Away",
    "be right back": "Away",        # Treat BRB as Away
    "appear offline": "Offline",
    "offline": "Offline",
    "out of office": "Offline",     # Treat OOO as Offline
}

def _normalize_ax_status(raw):
    """Normalize raw AX status text to canonical status."""
    return _AX_STATUS_MAP.get(raw.lower().strip(), "Unknown")
```

**Confidence: MEDIUM** -- The status strings are well-known Teams terminology. The exact text Teams exposes via AX may differ from UI labels. Needs validation.

### _detect_idle_time()

```python
def _detect_idle_time():
    """
    Read system idle time via ioreg HIDIdleTime.

    Returns idle time in seconds (float), or None on failure.
    HIDIdleTime is reported in nanoseconds by IOKit.
    """
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                # Line format: "    |   "HIDIdleTime" = 1234567890"
                parts = line.split("=")
                if len(parts) == 2:
                    try:
                        idle_ns = int(parts[1].strip())
                        return idle_ns / 1_000_000_000
                    except ValueError:
                        pass
        return None
    except subprocess.TimeoutExpired:
        logging.debug("ioreg idle time check timed out")
        return None
    except (FileNotFoundError, OSError):
        logging.debug("ioreg not available")
        return None
```

**Confidence: HIGH** -- `ioreg -c IOHIDSystem` and HIDIdleTime are well-documented and stable across macOS versions. The nanosecond-to-seconds conversion is straightforward. There is one known edge case: on headless Macs without keyboard/mouse, HIDIdleTime may not reset correctly, but this daemon targets an interactive desktop.

### _detect_teams_process(bundle_ids)

```python
def _detect_teams_process(bundle_ids):
    """
    Check if any Teams process is running via pgrep.

    Returns True if running, False if not running.
    Checks for process names matching known Teams identifiers.
    """
    # Teams process names to check. The new Teams (com.microsoft.teams2)
    # runs as "Microsoft Teams" in the process table. The classic version
    # runs as "Teams" or "Microsoft Teams".
    process_names = ["Microsoft Teams", "Teams"]
    for name in process_names:
        try:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-x", name],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:  # pgrep returns 0 if match found
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return False
```

**Confidence: MEDIUM** -- The process name "Microsoft Teams" for the new Teams is reported in multiple sources. The exact process name should be verified on the target machine using `ps aux | grep -i teams`. The `pgrep -x` flag does exact match, which may miss helper processes but correctly identifies the main app.

### passes_status_gate(status_result, config)

```python
def passes_status_gate(status_result, config):
    """
    Decide whether to forward notifications based on detected status.

    Forward when: Away, Busy (user is not actively at computer or in Teams)
    Drop when: Available (user sees notifications directly in Teams)
    Drop when: Offline, Out of Office (user is not working)
    Forward when: Unknown (fail-open: better to forward than to silently drop)

    Returns True if notifications should be forwarded, False to drop.
    """
    forward_statuses = config.get("forward_statuses", {"Away", "Busy", "Unknown"})
    return status_result["status"] in forward_statuses
```

**Confidence: HIGH** -- The gating logic is a simple set membership check. The default set (Away, Busy, Unknown) is the correct fail-open behavior: forward when uncertain, only drop when positively detecting Available/Offline.

---

## Config Changes

### New Config Keys

```json
{
    "webhook_url": "https://...",
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10,

    "status_enabled": true,
    "status_ax_enabled": true,
    "idle_threshold_seconds": 300,
    "forward_statuses": ["Away", "Busy", "Unknown"]
}
```

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `status_enabled` | bool | `true` | Master switch for status detection. When false, all notifications forwarded (v1.0 behavior). |
| `status_ax_enabled` | bool | `true` | Enable AppleScript AX detection (Signal 1). Disable if Accessibility permission cannot be granted. |
| `idle_threshold_seconds` | int | `300` | Seconds of idle time before status inferred as "Away". Matches Teams' built-in 5-minute idle threshold. |
| `forward_statuses` | list[str] | `["Away", "Busy", "Unknown"]` | Which detected statuses trigger forwarding. |

### DEFAULT_CONFIG Update

```python
DEFAULT_CONFIG = {
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10,
    # v1.1 status detection
    "status_enabled": True,
    "status_ax_enabled": True,
    "idle_threshold_seconds": 300,
    "forward_statuses": ["Away", "Busy", "Unknown"],
}
```

---

## Webhook Payload Changes

### New Fields

```python
def build_webhook_payload(notif, msg_type, status_result=None):
    """Build webhook payload with optional status metadata."""
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
    # v1.1: Add status metadata
    if status_result is not None:
        payload["_detected_status"] = status_result["status"]
        payload["_status_source"] = status_result["source"]
        payload["_status_confidence"] = status_result["confidence"]
    return payload
```

The `_detected_status`, `_status_source`, and `_status_confidence` fields use the underscore prefix convention established by v1.0's `_source` and `_truncated` fields, signaling these are metadata rather than message content.

---

## Data Flow: Status Through the System

```
config.json
  |  "status_enabled": true
  |  "idle_threshold_seconds": 300
  |  "forward_statuses": ["Away", "Busy", "Unknown"]
  |
  v
load_config() --> config dict (in memory)
  |
  v
run_watcher(... config ...)
  |
  +-- Each loop iteration:
  |     |
  |     v
  |   detect_user_status(config)
  |     |
  |     +--[try]--> _detect_status_ax()
  |     |             subprocess.run(osascript ...)
  |     |             --> "Away" | None (on failure)
  |     |
  |     +--[try]--> _detect_idle_time()
  |     |             subprocess.run(ioreg ...)
  |     |             --> 342.5 seconds | None
  |     |
  |     +--[try]--> _detect_teams_process(bundle_ids)
  |                   subprocess.run(pgrep ...)
  |                   --> True | False
  |     |
  |     v
  |   status_result = {
  |     "status": "Away",
  |     "source": "idle",
  |     "confidence": "medium",
  |     "raw_value": "342"
  |   }
  |     |
  |     v
  |   passes_status_gate(status_result, config)
  |     --> True (Away is in forward_statuses)
  |     |
  |     v
  |   [forward path]
  |     query_new_notifications(...)
  |     for notif in notifications:
  |       passes_filter(notif, config)       # existing content filter
  |       classify_notification(notif)       # existing classification
  |       build_webhook_payload(notif, type, status_result)  # status injected
  |       post_webhook(payload, ...)
  |     |
  |     v
  |   Webhook JSON includes:
  |     {
  |       "senderName": "Alice",
  |       "content": "Hey, check this out",
  |       "_detected_status": "Away",
  |       "_status_source": "idle",
  |       "_status_confidence": "medium",
  |       ...
  |     }
```

---

## Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **Config loader** (`load_config`) | Parse status config keys, set defaults | Provides config dict to all components |
| **Status detector** (`detect_user_status`) | Orchestrate fallback chain, return canonical result | Calls three signal functions, returns to event loop |
| **AX signal** (`_detect_status_ax`) | AppleScript subprocess for Teams status text | External: osascript process. Returns to detector. |
| **Idle signal** (`_detect_idle_time`) | ioreg subprocess for HIDIdleTime | External: ioreg process. Returns to detector. |
| **Process signal** (`_detect_teams_process`) | pgrep subprocess for Teams process check | External: pgrep process. Returns to detector. |
| **Status gate** (`passes_status_gate`) | Forward/drop decision based on status | Reads status result + config. Returns bool to event loop. |
| **Payload builder** (`build_webhook_payload`) | Include status metadata in webhook JSON | Receives status result from event loop. |
| **Event loop** (`run_watcher`) | Orchestrate status check -> gate -> query -> filter -> deliver | Central coordinator. Modified, not rewritten. |

---

## Patterns to Follow

### Pattern 1: Fallback Chain with Typed Results

**What:** Each signal function returns a typed result or None. The orchestrator tries signals in order, using the first non-None result. Every result carries source and confidence metadata.

**Why:** Graceful degradation. If Accessibility permission is denied, AX returns None and idle time takes over. If ioreg fails (unlikely), process check provides minimal signal. The system never crashes due to a failed status check.

**Implementation principle:** Each signal function handles its OWN errors internally and returns None on ANY failure. The orchestrator never catches exceptions from signal functions.

### Pattern 2: Status Check Once Per Loop Iteration, Not Per Notification

**What:** Call `detect_user_status()` once at the top of each event loop iteration. Pass the result into the per-notification processing.

**Why:** Status detection involves subprocess calls (osascript: 200-500ms, ioreg: 50-150ms, pgrep: 10-50ms). Running these per-notification in a burst of 20 notifications would add 5-10 seconds of overhead. Running once per loop iteration (every 5s poll interval) adds at most 500ms per cycle.

### Pattern 3: Always Advance State, Even When Gated

**What:** When the status gate blocks forwarding, still query notifications and advance the rec_id high-water mark.

**Why:** Prevents replay of stale notifications when status transitions. The user was Available, saw messages directly in Teams, status changes to Away -- the daemon should NOT replay the messages from the Available period.

### Pattern 4: Fail-Open on Unknown Status

**What:** When all status signals fail, return "Unknown" status with "low" confidence. "Unknown" is in the default `forward_statuses` set, so notifications are forwarded.

**Why:** Silent dropping is worse than occasional duplicates. If the status detection system breaks, the daemon falls back to v1.0 behavior (forward everything). The downstream consumer receives the `_status_confidence: "low"` signal and can handle it.

### Pattern 5: Subprocess Timeout on Every External Call

**What:** Every `subprocess.run()` call includes `timeout=3`. Timeout exceptions are caught and treated as signal failure (return None).

**Why:** Prevents the daemon from hanging if osascript blocks (e.g., Accessibility permission dialog), ioreg stalls (unlikely but possible), or pgrep deadlocks. The 3-second timeout is generous (most calls complete in <500ms) but prevents infinite hangs.

---

## Anti-Patterns to Avoid

### Anti-Pattern 1: Status Check Per Notification

**What people do:** Call `detect_user_status()` inside the `for notif in notifications` loop.

**Why bad:** A burst of 20 notifications means 20 subprocess calls to osascript (200ms each = 4 seconds added). Status does not change between notifications in a single batch.

**Instead:** Check once at the top of the loop iteration. Pass the result through.

### Anti-Pattern 2: Blocking on Accessibility Permission Dialog

**What people do:** Run osascript without a timeout. If Accessibility permission is not granted, macOS shows a dialog and osascript blocks indefinitely waiting for user response.

**Why bad:** Daemon hangs. No notifications processed until user dismisses dialog.

**Instead:** `subprocess.run(... timeout=3)`. If osascript blocks, the TimeoutExpired exception fires, signal returns None, fallback chain continues with idle time.

### Anti-Pattern 3: Not Advancing State When Gated

**What people do:** Skip `query_new_notifications()` entirely when status gate says "don't forward."

**Why bad:** rec_id never advances. When status changes to Away, all accumulated Available-period notifications replay. User gets flooded with messages they already read hours ago.

**Instead:** Always query, always advance rec_id, just skip the webhook POST.

### Anti-Pattern 4: Caching Status Across Multiple Loop Iterations

**What people do:** Cache the status result and reuse it for N iterations to "reduce subprocess overhead."

**Why bad:** Status can change at any moment (user goes AFK, starts a meeting, opens Teams). A 30-second stale cache means up to 30 seconds of incorrect gating. The 5-second poll interval already provides natural rate limiting.

**Instead:** Check every iteration. The ~500ms cost per 5-second cycle is acceptable (10% overhead, no user impact).

### Anti-Pattern 5: Using PyObjC for AX Access

**What people do:** Import PyObjC to call the Accessibility API directly from Python, avoiding the osascript subprocess.

**Why bad:** Violates the project constraint of stdlib-only. PyObjC is an external dependency. The performance gain (avoiding fork+exec) is not worth breaking the zero-dependency principle.

**Instead:** Use `subprocess.run(["/usr/bin/osascript", ...])`. Accept the subprocess overhead. It is within budget.

---

## Build Order (v1.1 Phase Dependencies)

### Step 1: Status Detection Core (Foundation)

Build the three signal functions and the orchestrator. Test each independently.

**New functions:**
- `_detect_idle_time()` -- simplest, most reliable, build and test first
- `_detect_teams_process()` -- second simplest, build and test second
- `_detect_status_ax()` -- most complex and fragile, build last
- `_normalize_ax_status()` -- mapping function, trivial
- `detect_user_status()` -- orchestrator, wires the above together

**Depends on:** Nothing (new code, no existing function modifications)
**Why this order:** Start with ioreg (guaranteed to work, no permissions needed), then pgrep (guaranteed to work), then AX (may not work, needs empirical discovery). This lets you test the fallback chain with working signals before tackling the uncertain AX signal.

### Step 2: Status Gating Logic

Build the gate function and wire it into the event loop.

**New functions:**
- `passes_status_gate()` -- simple set membership check

**Modified functions:**
- `run_watcher()` -- add status check + gate + always-advance logic

**Depends on:** Step 1 (needs `detect_user_status()` to exist)

### Step 3: Config and Payload Integration

Add config keys, payload fields, and startup display.

**Modified functions:**
- `load_config()` -- add status defaults
- `build_webhook_payload()` -- add status metadata
- `print_startup_summary()` -- show status config

**Depends on:** Step 1 + Step 2 (needs status result dict shape to be finalized)

### Step 4: AppleScript AX Discovery (Empirical)

Use Accessibility Inspector to map the Teams AX tree on the target machine. Write the actual AppleScript. This step cannot be completed without interactive access to a running Teams instance.

**Modified functions:**
- `_detect_status_ax()` -- replace placeholder AX_SCRIPT with real script

**Depends on:** Step 1 (placeholder exists), interactive macOS session with Teams running
**Risk:** This step may reveal that the new Teams does NOT expose status via AX at all. If so, the AX signal is permanently disabled and the idle time + process check provide the fallback. The architecture handles this gracefully because the fallback chain already works without AX.

---

## Performance Budget

| Operation | Duration | Frequency | Impact |
|-----------|----------|-----------|--------|
| AX status (osascript) | 200-500ms | Every 5s | Adds 4-10% to loop cycle time |
| Idle time (ioreg) | 50-150ms | Every 5s (fallback) | Adds 1-3% to loop cycle time |
| Process check (pgrep) | 10-50ms | Every 5s (fallback) | Adds <1% to loop cycle time |
| **Worst case (all three)** | **260-700ms** | **Every 5s** | **5-14% overhead** |
| **Typical case (AX succeeds)** | **200-500ms** | **Every 5s** | **4-10% overhead** |
| **Typical case (AX fails, idle succeeds)** | **50-150ms** | **Every 5s** | **1-3% overhead** |

The fallback chain is self-optimizing: the most expensive signal (AX) is tried first but also the most likely to fail (producing a fast None return). When AX fails, the cheaper signals handle detection.

If AX is permanently unavailable, disabling it via `status_ax_enabled: false` in config eliminates the 200-500ms cost entirely.

---

## Scaling Considerations

| Concern | Impact | Mitigation |
|---------|--------|------------|
| Subprocess fork overhead | 3 fork+exec per loop iteration max | Each is <500ms; pool is fixed at 3 max |
| osascript cold start | First call ~1-2s, subsequent ~200ms | Not a problem for daemon that runs continuously |
| HIDIdleTime accuracy | Value is system-wide, not Teams-specific | Acceptable proxy; Teams itself uses the same 5-min idle threshold |
| Status during screen lock | ioreg still works; AX may not | ioreg reports high idle time -> correct "Away" |
| Status during sleep | Daemon paused by OS during sleep | On wake, first loop iteration detects current status correctly |
| Multiple Teams windows | AX may read wrong window's status | Normalize to worst case (if any window shows Available, user is Available) |

---

## Sources

### AppleScript / AX Status Detection
- [Microsoft Community Hub: enable Accessibility Tree on macOS in the new Teams](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) -- Confirms new Teams does NOT reliably expose AX tree. **MEDIUM confidence.**
- [Apple Developer Forums: AX Elements in some apps only exposed when VoiceOver active](https://developer.apple.com/forums/thread/756895) -- Confirms AX tree availability depends on assistive technology state. **MEDIUM confidence.**
- [Apple Mac Automation Scripting Guide: Automating the User Interface](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html) -- Official guide for System Events UI scripting. **HIGH confidence.**
- [n8henrie: A Strategy for UI Scripting in AppleScript](https://n8henrie.com/2013/03/a-strategy-for-ui-scripting-in-applescript/) -- Practical guide for discovering AX element hierarchies. **HIGH confidence.**

### ioreg / HIDIdleTime
- [DSSW: Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/) -- Explains HIDIdleTime in IOKit registry, nanosecond units. **HIGH confidence.**
- [Karabiner-Elements issue #385: HIDIdleTime not reset with keyboard](https://github.com/pqrs-org/Karabiner-Elements/issues/385) -- Documents known edge case with HIDIdleTime. **HIGH confidence.**
- [Apple Developer Forums: HIDIdleTime not being reset](https://developer.apple.com/forums/thread/721530) -- Confirms HIDIdleTime is the standard approach but has edge cases on headless systems. **MEDIUM confidence.**

### Process Detection
- [Helge Klein: Identifying MS Teams Application Instances](https://helgeklein.com/blog/identifying-ms-teams-application-instances-counting-app-starts/) -- Documents Teams process names and helper processes. **MEDIUM confidence.**
- [mre/teams-call on GitHub](https://github.com/mre/teams-call) -- Shell/Python script detecting Teams call status via log files. Alternative approach if AX fails. **MEDIUM confidence.**

### subprocess / Performance
- [Python 3 subprocess documentation](https://docs.python.org/3/library/subprocess.html) -- Official docs for subprocess.run, timeout behavior. **HIGH confidence.**
- [Apple Community: osascript performance slower than Script Editor](https://discussions.apple.com/thread/8612365) -- Confirms osascript subprocess overhead. **MEDIUM confidence.**

---
*Architecture research for: Status-aware notification gating integration (v1.1)*
*Researched: 2026-02-11*
