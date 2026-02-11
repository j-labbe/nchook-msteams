# Phase 4: Status Detection Core - Research

**Researched:** 2026-02-11
**Domain:** macOS system idle detection, process detection, and fallback chain orchestration via Python subprocess
**Confidence:** HIGH

## Summary

Phase 4 builds the status detection foundation: three functions that read system signals (`ioreg` for idle time, `pgrep` for Teams process, and a placeholder for AX), an orchestrator that runs them as a fallback chain, and a canonical status result dict. AX discovery is explicitly deferred to Phase 6 -- this phase delivers value using only idle time and process detection.

The technical domain is well-understood. All three subprocess calls use patterns already proven in the codebase (`detect_db_path()` at line 77 uses `subprocess.run()` with `capture_output=True, text=True, timeout=5`). The key risk is not technical complexity but a process name mismatch: **the existing research recommends `pgrep -x "Microsoft Teams"` but the actual main Teams process on this machine is named `MSTeams`** (verified via `ps -c` and `pgrep -x "MSTeams"` returning PID 86160). This would cause STAT-02 to silently fail if implemented as specified. The research below provides the corrected approach.

**Primary recommendation:** Build three signal functions (idle, process, AX stub) and one orchestrator function. Keep all functions pure (no caching, no side effects beyond subprocess calls). Test each signal independently before wiring the chain. Use `pgrep -x "MSTeams"` as the primary process check.

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `subprocess` (stdlib) | Python 3.12.7 | Execute ioreg, pgrep, osascript | Already imported and used in nchook.py line 23. `subprocess.run()` with `capture_output=True, text=True, timeout=N` is the proven pattern. |
| `re` (stdlib) | Python 3.12.7 | Parse HIDIdleTime from ioreg output | Already imported in nchook.py line 27. Simple regex extraction. |
| `logging` (stdlib) | Python 3.12.7 | Log detection results and failures | Already configured in nchook.py. |
| `ioreg` (macOS system) | macOS 26.2 (Tahoe) | Read HIDIdleTime from IOKit registry | System binary at `/usr/sbin/ioreg`. SIP-protected. Verified working on this machine: `ioreg -c IOHIDSystem -d 4` returns HIDIdleTime in 5.7ms. |
| `pgrep` (macOS system) | macOS 26.2 (Tahoe) | Check if Teams process is running | System binary at `/usr/bin/pgrep`. Verified: `pgrep -x "MSTeams"` returns PID 86160 in 14.6ms. |

### No New Python Imports Required

Every stdlib module needed is already imported in nchook.py: `subprocess` (line 23), `re` (line 27), `logging` (line 17), `time` (line 20).

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `ioreg` text parsing with regex | `ioreg -a` (plist XML output) + `plistlib.loads()` | More robust parsing but 3-5x slower due to XML generation overhead. The text regex is sufficient and verified stable. |
| `pgrep -x "MSTeams"` | `pgrep -f "Microsoft Teams.app"` (path match) | Path match is more stable across process name changes but slower and may match helper processes. Use `-x` for the main process name. |
| Synchronous subprocess in event loop | Threading for parallel subprocess calls | Unnecessary complexity. Total worst-case time for idle+process is <20ms. Threading adds race conditions for no measurable benefit. |

## Architecture Patterns

### Recommended Project Structure

All new code goes into `nchook.py` (single-file architecture, matching v1.0 pattern). New functions are added as a logical block between the existing "Webhook Delivery" section and the "kqueue WAL Watcher" section:

```
nchook.py (additions for Phase 4)
  # ---------------------------------------------------------------------------
  # Status Detection
  # ---------------------------------------------------------------------------
  _detect_idle_time()           # Signal 2: ioreg HIDIdleTime
  _detect_teams_process()       # Signal 3: pgrep process check
  _detect_status_ax()           # Signal 1: placeholder (returns None)
  _normalize_ax_status()        # AX status text normalization (for Phase 6)
  detect_user_status()          # Orchestrator: fallback chain -> result dict
```

### Pattern 1: Fallback Chain with Typed Results

**What:** Each signal function returns a typed result or `None`. The orchestrator tries signals in priority order (AX -> idle -> process), using the first non-None result. Every result carries `detected_status`, `status_source`, and `status_confidence` metadata.

**When to use:** Always. This is the core pattern for Phase 4.

**Example:**
```python
# Source: verified against existing codebase pattern + requirements STAT-05, STAT-06, STAT-07
def detect_user_status(config):
    """
    Three-signal fallback chain for user status detection.

    Signal 1 (AX) is a placeholder that returns None until Phase 6.
    Signal 2 (idle) maps HIDIdleTime >= 300s to Away, < 300s to Available.
    Signal 3 (process) maps Teams not running to Offline.

    Returns dict with: detected_status, status_source, status_confidence
    """
    idle_threshold = config.get("idle_threshold_seconds", 300)

    # Signal 1: AX status text (placeholder -- always None until Phase 6)
    ax_status = _detect_status_ax()
    if ax_status is not None:
        return {
            "detected_status": _normalize_ax_status(ax_status),
            "status_source": "ax",
            "status_confidence": "high",
        }

    # Signal 2: System idle time
    idle_seconds = _detect_idle_time()
    if idle_seconds is not None:
        if idle_seconds >= idle_threshold:
            return {
                "detected_status": "Away",
                "status_source": "idle",
                "status_confidence": "medium",
            }
        # User is active. Check if Teams is running before declaring Available.
        if _detect_teams_process():
            return {
                "detected_status": "Available",
                "status_source": "idle",
                "status_confidence": "medium",
            }
        else:
            return {
                "detected_status": "Offline",
                "status_source": "process",
                "status_confidence": "high",
            }

    # Signal 3: Process check only (idle failed)
    if not _detect_teams_process():
        return {
            "detected_status": "Offline",
            "status_source": "process",
            "status_confidence": "high",
        }

    # All signals failed or inconclusive
    return {
        "detected_status": "Unknown",
        "status_source": "error",
        "status_confidence": "low",
    }
```

### Pattern 2: Subprocess with Mandatory Timeout

**What:** Every `subprocess.run()` call includes `timeout=N`. All exceptions are caught internally. The function returns `None` on any failure.

**When to use:** Every signal function.

**Example:**
```python
# Source: matches existing pattern in detect_db_path() line 77-83
def _detect_idle_time():
    """Read system idle time in seconds from IOKit HIDIdleTime (nanoseconds)."""
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logging.warning("ioreg returned exit code %d", result.returncode)
            return None
    except subprocess.TimeoutExpired:
        logging.warning("ioreg timed out")
        return None
    except FileNotFoundError:
        logging.error("ioreg not found")
        return None

    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not match:
        logging.warning("HIDIdleTime not found in ioreg output")
        return None

    nanoseconds = int(match.group(1))
    return nanoseconds / 1_000_000_000  # Convert ns -> seconds
```

### Pattern 3: Result Dict Shape (STAT-07)

**What:** Every status result is a dict with exactly three fields: `detected_status`, `status_source`, `status_confidence`.

**Example:**
```python
# The canonical result dict shape, per STAT-07
{
    "detected_status": "Away",      # Available | Away | Offline | Unknown
    "status_source": "idle",        # ax | idle | process | error
    "status_confidence": "medium",  # high | medium | low
}
```

### Anti-Patterns to Avoid

- **Checking status per notification:** Status is a property of the user, not the notification. Check once per poll cycle, pass the result to the notification processing loop. Never call `detect_user_status()` inside a `for notif in notifications` loop.
- **Using `entire contents` in AppleScript:** Traverses the entire AX tree. Takes 5-30 seconds. Blocks the event loop. Use targeted element paths instead. (Relevant for Phase 6, but the pattern decision affects Phase 4 API design.)
- **Using `shell=True` with subprocess:** Security risk, unnecessary overhead. Always use list form: `["pgrep", "-x", "MSTeams"]`. The codebase already follows this pattern.
- **Not stripping subprocess stdout:** Output includes trailing newlines. Always `.strip()` before comparing or parsing.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| System idle detection | Custom IOKit bindings via ctypes/pyobjc | `subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])` + regex | ioreg is stable, fast (5.7ms), and requires zero dependencies. The regex `r'"HIDIdleTime"\s*=\s*(\d+)'` is verified on this machine. |
| Process detection | Custom `/proc` parsing or `os.kill(pid, 0)` | `subprocess.run(["pgrep", "-x", "MSTeams"])` | pgrep is purpose-built for this. 14.6ms on this machine. Exit code semantics are well-defined. |
| Subprocess timeout handling | Manual `Popen` + `communicate(timeout=)` + kill | `subprocess.run(... timeout=5)` | `subprocess.run()` handles the full lifecycle: spawn, wait, timeout, kill, cleanup. No zombie risk. |
| Nanosecond conversion | Manual division without documentation | Always `nanoseconds / 1_000_000_000` with a comment | The unit confusion (ns vs s vs ms) is the most common ioreg parsing bug. Make the conversion explicit. |

**Key insight:** This phase's functions are thin wrappers around system utilities. The value is in the orchestrator logic (fallback chain, result dict shape) and error handling (timeouts, None propagation), not in complex computation.

## Common Pitfalls

### Pitfall 1: Wrong Teams Process Name (CRITICAL - Corrects Existing Research)

**What goes wrong:** STAT-02 specifies `pgrep -x "Microsoft Teams"` but the actual main Teams process on this macOS 26.2 system is named `MSTeams` (executable at `/Applications/Microsoft Teams.app/Contents/MacOS/MSTeams`). Using `pgrep -x "Microsoft Teams"` returns exit code 1 (not found) even when Teams is running.

**Why it happens:** The new Teams (com.microsoft.teams2) renamed the main executable from "Microsoft Teams" to "MSTeams". The helper processes (WebView, ModuleHost, etc.) still contain "Microsoft Teams" in their names, which is why `pgrep -l "Teams"` shows them with that prefix. But `pgrep -x "Microsoft Teams"` does exact match and misses the main process.

**How to avoid:** Use `pgrep -x "MSTeams"` as the primary check. Include `"Microsoft Teams"` as a fallback for older Teams versions. Verified on this machine:
  - `pgrep -x "MSTeams"` -> PID 86160 (exit code 0)
  - `pgrep -x "Microsoft Teams"` -> nothing (exit code 1)

**Warning signs:** `_detect_teams_process()` always returns False even when Teams is visibly running. Status always falls through to "Offline" on the process signal.

### Pitfall 2: HIDIdleTime Unit Confusion

**What goes wrong:** HIDIdleTime is in nanoseconds. Forgetting to divide by 1,000,000,000 gives "billions of seconds idle" which always exceeds any threshold. The daemon thinks the user is perpetually Away.

**Why it happens:** The ioreg output shows a raw integer like `43827184625` with no unit label. Developers assume seconds or milliseconds.

**How to avoid:** Always divide by `1_000_000_000`. Add a comment: `# HIDIdleTime is in nanoseconds`. Add a sanity check: if idle_seconds > 86400 (24 hours), log a warning.

**Warning signs:** Status is always "Away" regardless of user activity. `idle_seconds` values in the billions.

### Pitfall 3: Subprocess Timeout Missing

**What goes wrong:** `subprocess.run()` without `timeout=` blocks forever if the child process hangs. The daemon's event loop freezes. No notifications are processed.

**Why it happens:** ioreg and pgrep are fast (5-15ms) so developers skip the timeout "because it's fast." But edge cases exist: system under load, ioreg during sleep/wake transition, pgrep when process table is large.

**How to avoid:** Every `subprocess.run()` call MUST include `timeout=5`. Catch `subprocess.TimeoutExpired` and return `None`. This is non-negotiable.

**Warning signs:** Daemon stops processing notifications. Stuck in a subprocess call that never returns.

### Pitfall 4: Fallback Chain Doesn't Fall Through on Timeout

**What goes wrong:** The orchestrator catches the `TimeoutExpired` exception but doesn't continue to the next signal. Instead it returns an error result immediately.

**Why it happens:** The signal function raises `TimeoutExpired`, the orchestrator catches it and returns `{"status": "Unknown"}` without trying remaining signals.

**How to avoid:** Each signal function handles its OWN errors internally and returns `None`. The orchestrator never sees exceptions from signal functions. `None` means "try the next signal."

**Warning signs:** Status is always "Unknown" when one signal times out, even though other signals would work.

### Pitfall 5: Process Check Returns True for Helper Processes Only

**What goes wrong:** Teams crashed but helper processes (WebView, ModuleHost) are still running as zombies. `pgrep -f "Teams"` matches these helpers and reports "running." But the main Teams app is dead and the user cannot see notifications.

**Why it happens:** `pgrep` without `-x` does substring matching. Helper processes contain "Teams" in their names.

**How to avoid:** Use `pgrep -x "MSTeams"` for exact match on the main process only. The main process PID is the parent of all helpers. If it dies, helpers eventually terminate too, but there may be a delay.

**Warning signs:** Teams is not visible in Dock or app switcher but process check says "running."

## Code Examples

Verified patterns from this machine's actual runtime:

### Idle Time Detection (STAT-01)

```python
# Source: verified on macOS 26.2, Python 3.12.7
# ioreg -c IOHIDSystem -d 4 returns HIDIdleTime in 5.7ms
def _detect_idle_time():
    """
    Read system idle time in seconds from IOKit HIDIdleTime.

    Returns float (seconds) or None on any failure.
    HIDIdleTime is in nanoseconds -- divide by 1e9.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logging.warning("ioreg returned exit code %d", result.returncode)
            return None
    except subprocess.TimeoutExpired:
        logging.warning("ioreg timed out")
        return None
    except FileNotFoundError:
        logging.error("ioreg not found at expected path")
        return None

    # Parse: "HIDIdleTime" = <nanoseconds>
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not match:
        logging.warning("HIDIdleTime not found in ioreg output")
        return None

    nanoseconds = int(match.group(1))
    return nanoseconds / 1_000_000_000  # Convert nanoseconds to seconds
```

### Teams Process Detection (STAT-02)

```python
# Source: verified on macOS 26.2 with Teams running
# CRITICAL: Main process is "MSTeams", NOT "Microsoft Teams"
# pgrep -x "MSTeams" returns PID 86160 in 14.6ms
def _detect_teams_process():
    """
    Check if Microsoft Teams main process is running.

    Returns True if running, False otherwise.
    Checks both new Teams ("MSTeams") and legacy ("Microsoft Teams").
    """
    for process_name in ["MSTeams", "Microsoft Teams"]:
        try:
            result = subprocess.run(
                ["pgrep", "-x", process_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return False
```

### AX Status Placeholder (for Phase 6)

```python
# Source: Phase 4 design decision -- AX is deferred to Phase 6
def _detect_status_ax():
    """
    Placeholder for AX status detection (Phase 6).

    Always returns None, causing the fallback chain to proceed
    to idle time detection. Phase 6 will implement the actual
    AppleScript AX tree walking.
    """
    return None


# Status normalization mapping (used by Phase 6, defined now for API stability)
_AX_STATUS_MAP = {
    "available": "Available",
    "busy": "Busy",
    "do not disturb": "DoNotDisturb",
    "in a meeting": "Busy",
    "in a call": "Busy",
    "presenting": "Busy",
    "away": "Away",
    "be right back": "BeRightBack",
    "appear offline": "Offline",
    "offline": "Offline",
    "out of office": "Offline",
}


def _normalize_ax_status(raw):
    """Normalize raw AX status text to canonical status value."""
    return _AX_STATUS_MAP.get(raw.lower().strip(), "Unknown")
```

### Full Orchestrator (STAT-05, STAT-06, STAT-07)

```python
# Source: requirements STAT-05 (fallback chain), STAT-06 (mapping rules), STAT-07 (result shape)
def detect_user_status(config):
    """
    Three-signal fallback chain producing canonical status result.

    Signal 1: AX text (placeholder, Phase 6) -- returns None -> skip
    Signal 2: ioreg idle time -- >= threshold -> Away, < threshold -> Available
    Signal 3: pgrep process -- not running -> Offline

    Every return value has: detected_status, status_source, status_confidence
    """
    idle_threshold = config.get("idle_threshold_seconds", 300)

    # Signal 1: AX (placeholder -- always falls through)
    ax_status = _detect_status_ax()
    if ax_status is not None:
        return {
            "detected_status": _normalize_ax_status(ax_status),
            "status_source": "ax",
            "status_confidence": "high",
        }

    # Signal 2: System idle time
    idle_seconds = _detect_idle_time()
    if idle_seconds is not None:
        if idle_seconds >= idle_threshold:
            return {
                "detected_status": "Away",
                "status_source": "idle",
                "status_confidence": "medium",
            }
        # User is active (idle < threshold). Need process check to distinguish
        # Available (Teams running) from Offline (Teams not running).
        if _detect_teams_process():
            return {
                "detected_status": "Available",
                "status_source": "idle",
                "status_confidence": "medium",
            }
        return {
            "detected_status": "Offline",
            "status_source": "process",
            "status_confidence": "high",
        }

    # Signal 3: Process check only (idle signal failed)
    if not _detect_teams_process():
        return {
            "detected_status": "Offline",
            "status_source": "process",
            "status_confidence": "high",
        }

    # All signals failed or inconclusive -- Teams is running but idle is unknown
    return {
        "detected_status": "Unknown",
        "status_source": "error",
        "status_confidence": "low",
    }
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `pgrep -x "Microsoft Teams"` | `pgrep -x "MSTeams"` (primary) with `"Microsoft Teams"` fallback | New Teams (com.microsoft.teams2) renamed the main executable | **CRITICAL**: existing research is wrong on this point. Verified on this machine 2026-02-11. |
| Teams Electron app with accessible AX tree | Teams WebView2/Edge-based app with limited AX tree | Teams rewrite in 2024 | AX signal is unreliable; Phase 4 skips it entirely, Phase 6 investigates |
| `ioreg -c IOHIDSystem` (full output, ~2700 lines) | `ioreg -c IOHIDSystem -d 4` (depth-limited, ~140 lines) | Optimization, same data | 4x faster (5.7ms vs 23.3ms), same regex works |

**Deprecated/outdated:**
- `pgrep -x "Microsoft Teams"`: Does NOT match the main Teams process on macOS 26.2 with new Teams. Use `"MSTeams"` instead.
- Teams log file parsing for status: Dead end. New Teams no longer writes presence to local log files.
- `subprocess.check_output()`: Legacy API. Use `subprocess.run(capture_output=True)` which is the current Python idiom.

## Performance Budget

Verified on this machine (macOS 26.2, Apple Silicon):

| Signal | Command | Measured Duration | Timeout | Notes |
|--------|---------|-------------------|---------|-------|
| Idle (ioreg) | `ioreg -c IOHIDSystem -d 4` | 5.7ms | 5s | Extremely fast. Output is 141 lines. |
| Process (pgrep) | `pgrep -x "MSTeams"` | 14.6ms | 5s | Fast. Single process table scan. |
| **Total per cycle** | **Both signals** | **~20ms** | **10s max** | **Well within the 5s poll interval** |

Without AX, the overhead is negligible: 20ms per 5000ms poll cycle = 0.4% CPU time spent on status detection.

## Open Questions

1. **Should `idle_threshold_seconds` default be in config for Phase 4 or Phase 5?**
   - What we know: The threshold (300s) is needed by `detect_user_status()` which is built in Phase 4. But config changes are scoped to Phase 5 per the roadmap.
   - What's unclear: Whether Phase 4 should hardcode 300 or read from config.
   - Recommendation: Phase 4 should accept `config` dict and read `idle_threshold_seconds` with a default of 300. This avoids a code change in Phase 5 just to parameterize an already-parameterized value. Phase 5 adds the config key to `DEFAULT_CONFIG` and validates it.

2. **Should the result dict include `raw_value` for debugging?**
   - What we know: The existing research (ARCHITECTURE.md) includes a `raw_value` field in the result dict. STAT-07 only requires `detected_status`, `status_source`, `status_confidence`.
   - What's unclear: Whether the planner should include `raw_value` or keep the dict minimal per STAT-07.
   - Recommendation: Keep it minimal for Phase 4 (only the three required fields). `raw_value` can be added in Phase 5 when payload integration happens, if downstream consumers need it.

3. **Process name stability across Teams updates**
   - What we know: The main process is currently `MSTeams`. This could change in a future Teams update.
   - What's unclear: Whether Microsoft will keep this name or rename again.
   - Recommendation: Check both `"MSTeams"` and `"Microsoft Teams"` in the process detection function. Log which name matched at DEBUG level. This provides forward and backward compatibility.

## Sources

### Primary (HIGH confidence)
- **Live system verification** (macOS 26.2, Python 3.12.7, Apple Silicon) - ioreg output format, HIDIdleTime regex, pgrep process names, subprocess timing. All code examples verified on this machine.
- **nchook.py existing subprocess usage** (line 77-83, `detect_db_path`) - Proven `subprocess.run()` pattern with `capture_output=True, text=True, timeout=5`.
- [Python 3.12 subprocess documentation](https://docs.python.org/3.12/library/subprocess.html) - `subprocess.run()` API, timeout behavior, `TimeoutExpired` exception.
- [Apple IOKit Registry docs](https://developer.apple.com/library/archive/documentation/DeviceDrivers/Conceptual/IOKitFundamentals/TheRegistry/TheRegistry.html) - IOHIDSystem class, ioreg tool.

### Secondary (MEDIUM confidence)
- [DSSW: Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/) - HIDIdleTime output format, nanosecond units. Consistent with our live verification.
- [Helge Klein: Identifying MS Teams Application Instances](https://helgeklein.com/blog/identifying-ms-teams-application-instances-counting-app-starts/) - Teams process architecture. Partially outdated -- process names have changed, but helper process pattern is confirmed.
- [Karabiner-Elements issue #385](https://github.com/pqrs-org/Karabiner-Elements/issues/385) - HIDIdleTime edge case with third-party input remappers.
- Project research: `.planning/research/STACK.md`, `ARCHITECTURE.md`, `PITFALLS.md` - v1.1 milestone research. **NOTE:** STACK.md's process name recommendation (`"Microsoft Teams"`) is incorrect per live verification. Updated in this research.

### Tertiary (LOW confidence)
- [Microsoft Tech Community: AX Tree in new Teams](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) - New Teams AX tree issues. Relevant for Phase 6, not Phase 4.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All tools (ioreg, pgrep, subprocess) verified on this machine with measured timings
- Architecture: HIGH - Fallback chain pattern is well-established, success criteria are deterministic and testable
- Pitfalls: HIGH - Critical process name issue discovered via live verification, all other pitfalls from existing research validated

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (30 days -- ioreg and pgrep patterns are stable; Teams process name should be re-verified after any Teams update)
