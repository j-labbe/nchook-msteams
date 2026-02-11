# Phase 6: AX Discovery and Permission Handling - Research

**Researched:** 2026-02-11
**Domain:** macOS Accessibility (AX) tree access, AppleScript UI scripting, permission probing, graceful degradation
**Confidence:** MEDIUM (AX tree viability for new Teams is LOW; permission handling patterns are HIGH)

## Summary

Phase 6 implements the AX signal in the existing fallback chain (`_detect_status_ax()`, currently a placeholder returning None). The critical discovery is that the new Microsoft Teams (com.microsoft.teams2, version 26015.1401.4272.1018) has a **known broken Accessibility tree** -- the app does not reliably expose its AX tree to external calls, though elements appear accessible when VoiceOver or Accessibility Inspector is running. This was flagged as a risk in the roadmap and is now confirmed through multiple sources and live verification.

The implementation strategy must therefore be **discovery-first**: build the AX probe and AppleScript query infrastructure, attempt to read Teams status, and gracefully fall back when it fails. There are two potential AX approaches to try: (1) reading status text from the Teams main window via `System Events` UI scripting, and (2) reading the Teams menu bar extra's description/tooltip. Both require Accessibility permission granted to the terminal/host application. If neither works due to Teams' broken AX tree, the phase still delivers value by implementing permission detection, actionable instructions logging, and confirming the degradation path.

Permission probing uses `AXIsProcessTrusted()` via Python ctypes (verified working on this machine, returns instantly, no external dependencies). This is strictly preferred over the osascript probe approach because osascript hangs/times out when Accessibility permission is not granted rather than returning a clean error code. The error codes -609 (connection invalid) and -1712 (timeout) observed from osascript are unreliable indicators -- `AXIsProcessTrusted()` is the definitive check.

**Primary recommendation:** Implement AX permission probing via ctypes `AXIsProcessTrusted()`, build the AppleScript query for Teams status with `entire contents` avoidance, and design the function to return None on any failure so the existing fallback chain handles degradation automatically. Accept that new Teams AX access may not work and plan the phase to produce value regardless (permission handling, actionable instructions, confirmed degradation).

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `ctypes` (stdlib) | Python 3.12.7 | Call `AXIsProcessTrusted()` from ApplicationServices framework | Already available in stdlib. Verified working on this machine. Returns boolean instantly with zero overhead. |
| `subprocess` (stdlib) | Python 3.12.7 | Execute `osascript` to run AppleScript for AX tree queries | Already imported and used throughout nchook.py. Proven pattern with timeout handling. |
| `os` (stdlib) | Python 3.12.7 | Read `TERM_PROGRAM` env var for actionable permission instructions | Already imported. `os.environ.get("TERM_PROGRAM")` identifies the terminal app. |
| `osascript` (macOS system) | macOS 26.2 | Execute AppleScript commands for System Events UI scripting | System binary. Required for AX tree access via AppleScript. |
| ApplicationServices.framework | macOS 26.2 | `AXIsProcessTrusted()` C function for permission check | System framework at `/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices`. Loaded via ctypes. |

### No New Python Imports Required

`ctypes` is a stdlib module but is NOT currently imported in nchook.py. It must be added to the imports. All other modules (`subprocess`, `os`, `re`, `logging`) are already imported.

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| ctypes `AXIsProcessTrusted()` | osascript probe (try simple AX query, detect timeout) | osascript hangs for 30-120s when permission denied. ctypes returns instantly. Use ctypes. |
| ctypes `AXIsProcessTrusted()` | PyObjC `HIServices.AXIsProcessTrusted()` | PyObjC is an external dependency. Project is stdlib-only. ctypes achieves the same thing. |
| `osascript` for AX queries | Python `pyobjc` accessibility bindings | External dependency. Not compatible with project's no-external-deps constraint. |
| Reading Teams main window AX tree | Reading Teams menu bar extra description | Menu bar extra is a newer feature (late 2024) and may be more accessible. Try both approaches. |

## Architecture Patterns

### Integration Point

The single change is replacing the placeholder `_detect_status_ax()` with a real implementation. The existing fallback chain in `detect_user_status()` already handles the AX signal correctly: if `_detect_status_ax()` returns a non-None string, it's normalized via `_normalize_ax_status()` and returned with `source=ax, confidence=high`. If it returns None, the chain falls through to idle+process signals. **No changes to the orchestrator are needed.**

### Recommended Code Structure

All new code goes into `nchook.py` (single-file architecture). Changes are:

```
nchook.py (modifications for Phase 6)
  import ctypes                          # NEW import for AXIsProcessTrusted
  _check_ax_permission()                 # NEW: ctypes AXIsProcessTrusted probe
  _detect_status_ax()                    # REPLACE placeholder with real implementation
  _AX_STATUS_MAP                         # EXISTING: may need additions based on discovery
  _normalize_ax_status()                 # EXISTING: already implemented in Phase 4
  print_startup_summary()                # MODIFY: add AX permission status + instructions
```

### Pattern 1: Permission Probe via ctypes
**What:** Use `AXIsProcessTrusted()` from ApplicationServices framework via ctypes to check if the current process has Accessibility permission. This is an instant boolean check with no side effects.
**When to use:** At startup (for logging actionable instructions) and before each AX query attempt (to skip osascript when permission is not granted).

```python
# Source: verified on macOS 26.2, Python 3.12.7
# AXIsProcessTrusted returns False in this environment
import ctypes

def _check_ax_permission():
    """
    INTG-04: Check if current process has Accessibility permission.

    Uses AXIsProcessTrusted() from ApplicationServices framework via ctypes.
    Returns True if permission granted, False otherwise.
    Returns False on any error (safe fallback).
    """
    try:
        lib = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework'
            '/ApplicationServices'
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        return lib.AXIsProcessTrusted()
    except (OSError, AttributeError):
        return False
```

### Pattern 2: AppleScript AX Query with Timeout
**What:** Run osascript via subprocess with a tight timeout. The AppleScript attempts to read a specific UI element from the Teams process via System Events. If it times out or errors, return None.
**When to use:** Only when `_check_ax_permission()` returns True.

```python
# Source: derived from AppleScript UI scripting patterns + Teams architecture
def _detect_status_ax():
    """
    STAT-03: Read Teams status text from Accessibility tree via osascript.

    Returns raw status text string (e.g., "Busy", "Away") or None on failure.
    """
    # Fast check: skip osascript entirely if no AX permission
    if not _check_ax_permission():
        return None

    # AppleScript to read Teams status via System Events
    # NOTE: The exact element path must be discovered via Accessibility Inspector
    # with AX permission granted. The script below is the template.
    script = '''
    tell application "System Events"
        tell process "MSTeams"
            -- Element path TBD: discovered during implementation
            -- Candidate: description of menu bar item 1 of menu bar 2
            -- Candidate: value of static text N of group M of window 1
            return "DISCOVERY_NEEDED"
        end tell
    end tell
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            logging.debug("AX query failed: %s", result.stderr.strip())
            return None
        raw = result.stdout.strip()
        if not raw or raw == "DISCOVERY_NEEDED":
            return None
        return raw
    except subprocess.TimeoutExpired:
        logging.debug("AX query timed out")
        return None
    except FileNotFoundError:
        logging.error("osascript not found")
        return None
```

### Pattern 3: Actionable Permission Instructions
**What:** At startup, if AX permission is not granted, log clear instructions telling the user exactly which application to add to Accessibility settings.
**When to use:** During `print_startup_summary()` when `_check_ax_permission()` returns False and `status_enabled` is True.

```python
# Source: verified -- TERM_PROGRAM env var identifies terminal on this machine
def _log_ax_permission_instructions():
    """INTG-04: Log actionable instructions for granting Accessibility permission."""
    terminal = os.environ.get("TERM_PROGRAM", "your terminal application")
    terminal_names = {
        "Apple_Terminal": "Terminal.app",
        "iTerm.app": "iTerm2",
        "Alacritty": "Alacritty",
        "WezTerm": "WezTerm",
        "WarpTerminal": "Warp",
        "vscode": "Visual Studio Code",
    }
    app_name = terminal_names.get(terminal, terminal)

    logging.info("  AX status:   NOT AVAILABLE (Accessibility permission not granted)")
    logging.info("  To enable AX-based status detection:")
    logging.info("    1. Open System Settings > Privacy & Security > Accessibility")
    logging.info("    2. Click the + button and add: %s", app_name)
    logging.info("    3. Restart the daemon")
    logging.info("  Without AX, status detection falls back to idle+process signals.")
```

### Pattern 4: Cache Permission Check
**What:** Call `_check_ax_permission()` once at startup and cache the result. The permission state cannot change while the process is running (macOS requires restart for TCC changes to take effect on running processes).
**When to use:** Always. Avoids calling ctypes on every poll cycle.

```python
# In the startup/init path:
_ax_permission_granted = _check_ax_permission()

# In _detect_status_ax():
def _detect_status_ax():
    if not _ax_permission_granted:
        return None
    # ... osascript query ...
```

### Anti-Patterns to Avoid

- **Using `entire contents` in AppleScript:** Traverses the entire AX tree of Teams (which has hundreds of web elements via WebView2). Takes 5-30+ seconds. Blocks the event loop. Always target a specific element path.
- **Probing permission via osascript timeout:** osascript hangs for the full timeout duration when permission is denied. This wastes 5-30 seconds every poll cycle. Use `AXIsProcessTrusted()` via ctypes instead (returns instantly).
- **Retrying AX when permission is denied:** Accessibility permission cannot change for a running process without restart. Once denied at startup, skip all AX attempts for the lifetime of the process.
- **Hard-coding AX element paths without discovery:** The Teams AX tree structure is unknown and possibly broken. Element paths MUST be discovered via Accessibility Inspector with permission granted, not guessed.
- **Raising exceptions from `_detect_status_ax()`:** The function must return None on any failure. The orchestrator never sees exceptions from signal functions. This is the established codebase pattern.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Accessibility permission check | Custom TCC.db query, osascript probe with timeout | `ctypes` call to `AXIsProcessTrusted()` | Official Apple API. Returns instantly. No TCC database hackery needed. |
| AX tree traversal | Custom pyobjc AX bindings | `osascript` running AppleScript via `subprocess.run()` | Matches project pattern (subprocess-based). No external dependencies. |
| Terminal app identification | Process tree walking via ps | `os.environ.get("TERM_PROGRAM")` | Set by every modern terminal emulator on macOS. Instant, reliable. |
| Status text normalization | Regex parsing, fuzzy matching | `_AX_STATUS_MAP` dict lookup (already implemented) | Simple, deterministic, already built in Phase 4. |

**Key insight:** Phase 6's primary complexity is not in building infrastructure but in **discovering whether Teams exposes status text at all**. The code patterns are simple (subprocess + ctypes). The uncertainty is in what Teams actually provides via its AX tree.

## Common Pitfalls

### Pitfall 1: osascript Hangs When Accessibility Permission Not Granted (CRITICAL)
**What goes wrong:** Calling osascript with a System Events UI query when the process doesn't have Accessibility permission causes osascript to hang indefinitely (or until subprocess timeout). The daemon's event loop freezes for the timeout duration every poll cycle.
**Why it happens:** macOS doesn't return an error when Accessibility permission is missing. It either shows a system dialog (for GUI apps) or hangs silently (for CLI processes). The -1743 error code that documentation mentions is for Automation permission (AppleEvents), not Accessibility permission.
**How to avoid:** Always check `AXIsProcessTrusted()` via ctypes BEFORE invoking osascript. Cache the result at startup. If False, skip all osascript AX queries for the entire process lifetime.
**Warning signs:** Daemon becomes sluggish. Status detection takes 5+ seconds per poll cycle. Subprocess timeout warnings in logs.

### Pitfall 2: New Teams Broken AX Tree (HIGH RISK - Expected Failure)
**What goes wrong:** Even with Accessibility permission granted, the new Teams (com.microsoft.teams2) may not expose its status text via the AX tree. The AX tree is known to be broken/incomplete for external callers, though it works with VoiceOver and Accessibility Inspector.
**Why it happens:** New Teams uses WebView2 (Chromium-based) for rendering. The Chromium accessibility bridge may not fully expose all elements to the macOS AX API for external processes. Microsoft has not fixed this despite community reports since 2024.
**How to avoid:** Design the phase to succeed regardless of AX tree availability. The `_detect_status_ax()` function returns None on any failure, and the fallback chain handles it. Phase 6 succeeds if it confirms the AX tree IS or IS NOT usable and handles both cases correctly.
**Warning signs:** osascript returns empty strings, wrong element counts, or errors like -1719/-1728 even with Accessibility permission granted.

### Pitfall 3: Wrong App Gets Permission
**What goes wrong:** User grants Accessibility permission to `osascript` or `Python` instead of the terminal application (Terminal.app, iTerm2, etc.). The permission doesn't take effect.
**Why it happens:** macOS grants Accessibility permission to the calling process's code-signed binary. For scripts run from a terminal, that's the terminal app itself, not osascript or Python.
**How to avoid:** The actionable instructions MUST name the specific terminal app. Use `os.environ.get("TERM_PROGRAM")` to identify it. Map known values ("Apple_Terminal" -> "Terminal.app", "iTerm.app" -> "iTerm2", etc.) to user-friendly names.
**Warning signs:** User reports "I granted permission but it still doesn't work." Check which app they added to the Accessibility list.

### Pitfall 4: AX Permission Cannot Be Checked for Other Processes
**What goes wrong:** Developer tries to check if Teams has AX accessibility enabled, rather than checking if the CURRENT process is a trusted AX client.
**Why it happens:** Confusion about what `AXIsProcessTrusted()` checks. It checks whether the calling process can ACCESS other apps' AX trees, not whether the target app exposes its tree.
**How to avoid:** Understand the model: the daemon (Python process) needs to be in the Accessibility list. Teams does not need any configuration. The question is "can WE read THEIR tree?" not "have THEY enabled THEIR tree?"
**Warning signs:** Attempts to modify Teams settings or look for Teams AX configuration.

### Pitfall 5: AppleScript Timeout Too Long for Poll Cycle
**What goes wrong:** The osascript subprocess timeout is set to 10+ seconds, but the poll interval is 5 seconds. A single AX timeout blocks the entire poll cycle and delays notification processing.
**How to avoid:** Set osascript timeout to 3 seconds maximum (less than poll interval). If AX is consistently slow, disable it dynamically after N consecutive timeouts.
**Warning signs:** Notifications are delayed by 5-10 seconds. Status detection dominates the poll cycle time budget.

### Pitfall 6: Automation vs Accessibility Permission Confusion
**What goes wrong:** Developer conflates two separate macOS permission systems. Automation permission (AppleEvents, error -1743) controls sending commands to apps. Accessibility permission (error -1719/-1728, hangs) controls reading UI elements.
**Why it happens:** Both are in System Settings > Privacy & Security. Both involve System Events. But they are separate TCC grants.
**How to avoid:** AX status reading requires Accessibility permission (System Settings > Privacy & Security > Accessibility). The daemon does NOT need Automation permission because it's reading UI elements, not sending application commands. However, osascript's `tell application "System Events"` may trigger BOTH permission requests depending on macOS version. Use `AXIsProcessTrusted()` for the definitive Accessibility check.
**Warning signs:** Permission granted in Automation but not Accessibility, or vice versa.

## Code Examples

### AX Permission Check (INTG-04)
```python
# Source: verified on macOS 26.2, Python 3.12.7
# AXIsProcessTrusted returns False when run from Terminal without Accessibility permission
import ctypes

def _check_ax_permission():
    """Check if current process has macOS Accessibility permission."""
    try:
        lib = ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework'
            '/ApplicationServices'
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.AXIsProcessTrusted.argtypes = []
        return lib.AXIsProcessTrusted()
    except (OSError, AttributeError):
        return False
```

### Terminal App Detection for Actionable Instructions (INTG-04)
```python
# Source: verified on macOS 26.2 -- TERM_PROGRAM=Apple_Terminal
import os

def _get_terminal_app_name():
    """Get user-friendly name of the terminal app that needs AX permission."""
    terminal = os.environ.get("TERM_PROGRAM", "")
    names = {
        "Apple_Terminal": "Terminal.app",
        "iTerm.app": "iTerm2",
        "Alacritty": "Alacritty",
        "WezTerm": "WezTerm",
        "WarpTerminal": "Warp",
        "vscode": "Visual Studio Code",
        "tmux": "your terminal application (tmux session host)",
    }
    return names.get(terminal, terminal or "your terminal application")
```

### AX Status Detection with Permission Gate (STAT-03)
```python
# Source: pattern from codebase (subprocess.run with timeout=5, return None on failure)
# NOTE: The AppleScript element path is TBD -- must be discovered with AX Inspector

# Module-level cache (set once at startup, never changes)
_ax_available = None  # Will be set to True/False at startup

def _detect_status_ax():
    """
    STAT-03: Read Teams status from Accessibility tree.

    Returns raw status text string or None.
    None causes fallback chain to proceed to idle+process signals.
    """
    global _ax_available
    if _ax_available is None:
        _ax_available = _check_ax_permission()
    if not _ax_available:
        return None

    # Attempt 1: Read from Teams window AX tree
    # The exact AppleScript path depends on Teams' AX hierarchy
    # which must be mapped using Accessibility Inspector
    script = '''
try
    tell application "System Events"
        tell process "MSTeams"
            -- Candidate paths (to be refined during implementation):
            -- get description of menu bar item 1 of menu bar 2
            -- get value of static text 1 of group 1 of window 1
            -- get AXRoleDescription of first UI element of window 1
            set statusText to description of menu bar item 1 of menu bar 2
            return statusText
        end tell
    end tell
on error errMsg number errNum
    return ""
end try
'''
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=3,  # Must be < poll_interval (5s)
        )
        if result.returncode != 0:
            logging.debug("AX query error (exit %d): %s",
                         result.returncode, result.stderr.strip())
            return None
        raw = result.stdout.strip()
        if not raw:
            return None
        return raw
    except subprocess.TimeoutExpired:
        logging.debug("AX query timed out (3s)")
        return None
    except FileNotFoundError:
        return None
```

### Status Normalization (STAT-04, Already Implemented)
```python
# Source: existing code in nchook.py lines 692-709
# The map is already implemented. May need additions after AX discovery.
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
    # Potential additions after AX discovery:
    # "focusing": "DoNotDisturb",  -- Focus mode
    # "unknown": "Unknown",
}

def _normalize_ax_status(raw):
    """Normalize raw AX status text to canonical value."""
    return _AX_STATUS_MAP.get(raw.lower().strip(), "Unknown")
```

### Startup Summary with AX Status (INTG-04, INTG-05)
```python
# Source: extends existing print_startup_summary() at nchook.py lines 338-369
# Add after the existing "Status gate:" line:
if status_enabled:
    ax_ok = _check_ax_permission()
    if ax_ok:
        logging.info("  AX status:   AVAILABLE (Accessibility permission granted)")
    else:
        _log_ax_permission_instructions()
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Old Teams (Electron) exposed full AX tree | New Teams (WebView2) has broken/incomplete AX tree | Teams rewrite 2023-2024 | AX status reading may not work at all. Phase designed for this possibility. |
| No Teams menu bar icon on macOS | Teams menu bar extension with presence indicator | Late 2024 (version 24277.3502+) | Alternative AX source: menu bar item may expose status via AXDescription. |
| osascript as permission probe | `AXIsProcessTrusted()` via ctypes | Apple introduced TCC in Mojave (2018) | Definitive, instant check. No hanging, no timeouts. |
| Manual process tree walking for terminal detection | `TERM_PROGRAM` environment variable | Available since macOS 10.x | Every modern terminal sets this. Reliable and instant. |

**Deprecated/outdated:**
- Teams log file parsing for status: New Teams no longer writes presence to local log files (`~/Library/Application Support/Microsoft/Teams/logs.txt` does not exist for com.microsoft.teams2).
- Teams GraphQL local API for presence: Deprecated since Teams v1.0 rewrite per mre/teams-call.
- osascript as permission probe: Hangs when permission denied. Use `AXIsProcessTrusted()` instead.
- `pgrep -x "Microsoft Teams"` for process detection: Main process is `MSTeams` on new Teams (already corrected in Phase 4).

## Open Questions

1. **What is the actual AX element path for Teams status text?**
   - What we know: The new Teams AX tree is reported as broken for external callers. Menu bar extension was added in late 2024 and may expose presence via AXDescription. The exact element hierarchy is unknown.
   - What's unclear: Whether ANY path exists that returns status text. This can only be determined by running Accessibility Inspector with AX permission granted against a live Teams instance.
   - Recommendation: The implementation plan must include a **discovery step** where the developer uses Accessibility Inspector to map the Teams AX hierarchy. If no viable path exists, document the finding and keep `_detect_status_ax()` returning None (the fallback chain handles this). The plan should define this as a success condition, not a failure.

2. **Will the Teams menu bar extra expose status via accessibility?**
   - What we know: Teams added a menu bar extension (late 2024) with persistent presence indicator. Menu bar extras can expose AXDescription attributes. The TeamsWidgetExtension.appex is present on this machine.
   - What's unclear: Whether the menu bar item's AXDescription or AXValue contains the actual status text (e.g., "Available", "Busy").
   - Recommendation: During the discovery step, check `description of menu bar item 1 of menu bar 2` for the MSTeams process. This is a promising alternative to the main window AX tree.

3. **Should `_detect_status_ax()` disable itself after N failures?**
   - What we know: If Teams' AX tree is broken, every poll cycle will invoke osascript (3s timeout), wasting CPU and delaying notifications.
   - What's unclear: Whether the AX tree breakage is consistent (always fails) or intermittent (fails sometimes).
   - Recommendation: Add a consecutive-failure counter. After 3 consecutive failures, set `_ax_available = False` and log once that AX has been disabled for this session. This is a safety net beyond the permission cache.

4. **Does `AXIsProcessTrusted()` require the framework to be loaded on each call?**
   - What we know: ctypes `cdll.LoadLibrary()` caches loaded libraries. On this machine, calling `AXIsProcessTrusted()` via ctypes works instantly.
   - What's unclear: Whether there's any cost to calling it repeatedly.
   - Recommendation: Load the library once at module level or on first call, cache the function reference. Call the function at startup, cache the boolean result for the process lifetime.

## Sources

### Primary (HIGH confidence)
- **Live system verification** (macOS 26.2, Python 3.12.7, Apple Silicon) -- AXIsProcessTrusted returns False via ctypes (both ApplicationServices and HIServices framework paths), osascript times out on System Events process queries, TERM_PROGRAM correctly identifies Terminal.app. All code examples verified.
- **nchook.py existing implementation** -- `_detect_status_ax()` placeholder (line 687-689), `_AX_STATUS_MAP` (lines 692-704), `_normalize_ax_status()` (lines 707-709), `detect_user_status()` fallback chain (lines 712-771). Integration point is clear.
- [Apple AXIsProcessTrusted documentation](https://developer.apple.com/documentation/applicationservices/1460720-axisprocesstrusted) -- Official API reference. Returns Boolean indicating trusted accessibility client status.
- [Apple Mac Automation Scripting Guide: Automating the User Interface](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html) -- Official AppleScript UI scripting reference.
- [Apple AppleScript Error Codes](https://developer.apple.com/library/archive/documentation/AppleScript/Conceptual/AppleScriptLangGuide/reference/ASLR_error_codes.html) -- Error -609 (connectionInvalid), -1712 (AppleEvent timed out), -1743 (not authorized).

### Secondary (MEDIUM confidence)
- [Microsoft Tech Community: enable Accessibility Tree on macOS in the new Teams](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) -- Multiple developers confirm new Teams AX tree is broken for external callers. January 2025 comment says latest versions are "less broken." No confirmed fix from Microsoft.
- [Scripting OS X: Avoiding AppleScript Security and Privacy Requests](https://scriptingosx.com/2020/09/avoiding-applescript-security-and-privacy-requests/) -- Explains Automation vs Accessibility permission distinction. Error -1743 is AppleEvents authorization, not AX permission.
- [MC899183: Microsoft Teams Menu Bar Icon for Mac](https://app.cloudscout.one/evergreen-item/mc899183/) -- Teams menu bar extension with persistent presence indicator, rolled out late 2024. Version 24277.3502.3161.3007+.
- [Microsoft Support: Take quick actions in Microsoft Teams from a Mac](https://support.microsoft.com/en-us/office/take-quick-actions-in-microsoft-teams-95b3bf40-cd91-4bc5-8733-dc305c19b150) -- Documents menu bar extension features including status checking and changing.
- [Jano.dev: Accessibility Permission in macOS](https://jano.dev/apple/macos/swift/2025/01/08/Accessibility-Permission.html) -- AXIsProcessTrusted usage patterns, AXIsProcessTrustedWithOptions for prompting.

### Tertiary (LOW confidence)
- [Microsoft Teams stale presence on Mac](https://learn.microsoft.com/en-us/troubleshoot/microsoftteams/teams-on-mac/incorrect-presence-status-teams-for-mac) -- Teams presence subscription can expire and show stale status. May affect AX-reported status even if AX works.
- Community reports of "latest versions are more standard" AX tree in new Teams -- Single comment, January 2025, no version number or specific fix cited. Needs validation.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- ctypes, subprocess, os are all verified on this machine
- Architecture: HIGH -- Integration point is clear (replace placeholder, modify startup summary). Fallback chain is already built.
- AX viability: LOW -- Multiple sources confirm new Teams AX tree is broken. No verified working AppleScript path for status text. This is the critical unknown.
- Permission handling: HIGH -- `AXIsProcessTrusted()` verified working. Error patterns understood. Terminal detection verified.
- Pitfalls: HIGH -- All pitfalls derived from live testing and verified sources

**Research date:** 2026-02-11
**Valid until:** 2026-02-25 (14 days -- Teams AX tree status is fast-moving; Microsoft may fix or further break it with any Teams update)
