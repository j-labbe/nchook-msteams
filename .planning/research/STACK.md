# Stack Research: Teams Status Detection

**Domain:** macOS daemon -- Teams presence detection via subprocess (AppleScript, ioreg, pgrep)
**Researched:** 2026-02-11
**Confidence:** MEDIUM (AX tree access on new Teams is a known problem; ioreg/pgrep patterns are HIGH)

## Context

This is an **additive milestone** research. The existing stack (Python stdlib daemon, sqlite3, kqueue, plistlib, urllib.request) is validated and unchanged. This document covers ONLY the new subprocess patterns needed for Teams status detection:

1. AppleScript execution via `osascript` for reading Teams AX tree status
2. `ioreg` parsing for HIDIdleTime (system idle detection)
3. `pgrep` for Teams process liveness check

All patterns use `subprocess.run()` which is already imported in nchook.py (line 23).

---

## Recommended Stack Additions

### Core Technologies (New for This Milestone)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| `subprocess.run()` (stdlib) | ships with Python 3.12 | Execute osascript, ioreg, pgrep | Already imported and used in nchook.py (line 77, `detect_db_path`). `capture_output=True, text=True, timeout=N` is the correct pattern. No new imports needed. |
| `re` (stdlib) | ships with Python 3.12 | Parse ioreg output for HIDIdleTime value | Already imported in nchook.py (line 27). Regex extraction of nanosecond value from ioreg text output. |
| `/usr/bin/osascript` (macOS) | ships with macOS | Execute AppleScript from Python | System binary, always present on macOS. `-e` flag for inline script. Returns stdout text. |
| `/usr/sbin/ioreg` (macOS) | ships with macOS | Query IOKit registry for HIDIdleTime | System binary. `-c IOHIDSystem` filters to HID system class. Output contains `"HIDIdleTime" = <nanoseconds>`. |
| `/usr/bin/pgrep` (macOS) | ships with macOS | Check if Teams process is running | System binary. `-x` for exact name match. Returns PID(s) on stdout, exit code 0 = found, 1 = not found. |

### No New Python Imports Required

The existing imports in nchook.py already cover everything:
- `subprocess` (line 23) -- for `subprocess.run()`
- `re` (line 27) -- for regex parsing of ioreg output
- `logging` (line 16) -- for error logging in detection functions
- `time` (line 20) -- for caching/throttling detection calls

---

## Subprocess Patterns

### Pattern 1: pgrep -- Teams Process Check

**Purpose:** Determine if Microsoft Teams is running. Fastest check, should run first in any detection chain.

```python
def is_teams_running():
    """Check if Microsoft Teams process is alive via pgrep."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Microsoft Teams"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logging.warning("pgrep timed out")
        return False
    except FileNotFoundError:
        logging.error("pgrep not found at expected path")
        return False
```

**Key details:**
- `-x` flag: exact process name match (not substring). Prevents matching "Microsoft Teams Helper" or "Microsoft Teams Helper (Renderer)".
- Exit code semantics: 0 = at least one match, 1 = no match, 2+ = error.
- The process name is `"Microsoft Teams"` for the new Teams (WebKit-based) on macOS. The old Electron-based Teams used the same name.
- Multiple PIDs may be returned (one per line in stdout). We only care about returncode, not the PIDs.
- `capture_output=True` prevents pgrep output from leaking to the daemon's stdout.
- Timeout of 5s is generous; pgrep typically completes in <50ms.

**Process name caveat (MEDIUM confidence):** The new Teams on macOS moved from Electron to a WebKit/Cocoa-based architecture. The main process name remains "Microsoft Teams" but helper processes are named "Microsoft Teams Helper" and "Microsoft Teams Helper (Renderer)". The `-x` exact match prevents false positives from helpers. If Teams changes its process name in a future update, this will silently return False (safe failure mode).

### Pattern 2: ioreg -- System Idle Time

**Purpose:** Read HIDIdleTime from IOKit registry to determine how long since last keyboard/mouse input.

```python
def get_idle_seconds():
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
        logging.error("ioreg not found at expected path")
        return None

    # Parse: look for "HIDIdleTime" = <integer>
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
    if not match:
        logging.warning("HIDIdleTime not found in ioreg output")
        return None

    nanoseconds = int(match.group(1))
    return nanoseconds / 1_000_000_000
```

**Key details:**
- `ioreg -c IOHIDSystem -d 4`: `-c` filters by class, `-d 4` limits depth to avoid excessive output.
- Output format: `"HIDIdleTime" = 4523987654` -- value is in **nanoseconds**. Divide by 1,000,000,000 for seconds.
- The regex `r'"HIDIdleTime"\s*=\s*(\d+)'` handles the ioreg key-value format. The quotes around `HIDIdleTime` are literal in the output.
- Multiple `HIDIdleTime` entries may appear. `re.search` returns the first, which is the primary HID system entry. This is the correct one.
- Returns `None` on any failure (timeout, parse error, missing data). Callers must handle None.

**Known issue (MEDIUM confidence):** On some headless Mac configurations or when using Screen Sharing without a physical USB input device connected, HIDIdleTime may not reset on keyboard/mouse events. This is a macOS IOKit behavior, not a parsing issue. For a daemon running on a user's desktop Mac with physical or Bluetooth input devices, this is not a concern.

**Known issue (LOW confidence):** There are reports of HIDIdleTime not being reset by third-party input remappers (e.g., Karabiner-Elements). If the user uses such tools, idle detection may be unreliable. This is an edge case to document, not to solve.

### Pattern 3: osascript -- AppleScript AX Tree Status

**Purpose:** Walk the Teams accessibility tree to find the user's status text (Available, Busy, Do Not Disturb, Away, etc.).

```python
# The AppleScript to extract status from Teams AX tree.
# This script MUST be tuned via Accessibility Inspector for the actual
# Teams UI hierarchy. The structure below is a starting template.
TEAMS_STATUS_APPLESCRIPT = '''
tell application "System Events"
    if not (exists process "Microsoft Teams") then
        return "NOT_RUNNING"
    end if
    tell process "Microsoft Teams"
        set frontmost to true
        try
            -- AX tree path must be verified with Accessibility Inspector.
            -- New Teams (WebKit) may require AXManualAccessibility to be set.
            set statusElements to every static text of window 1
            repeat with elem in statusElements
                set elemValue to value of elem
                if elemValue is in {"Available", "Busy", "Do not disturb", "Away", "Be right back", "Appear offline", "In a meeting", "In a call", "Presenting", "Out of office"} then
                    return elemValue
                end if
            end repeat
            return "UNKNOWN"
        on error errMsg
            return "ERROR:" & errMsg
        end try
    end tell
end tell
'''


def get_teams_status_applescript():
    """Read Teams presence status via AppleScript AX tree inspection."""
    try:
        result = subprocess.run(
            ["osascript", "-e", TEAMS_STATUS_APPLESCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logging.warning("osascript failed (rc=%d): %s", result.returncode, stderr)
            return None

        status = result.stdout.strip()
        if status == "NOT_RUNNING":
            return None
        if status.startswith("ERROR:"):
            logging.warning("AppleScript error: %s", status)
            return None
        if status == "UNKNOWN":
            logging.debug("Teams status element not found in AX tree")
            return None
        return status

    except subprocess.TimeoutExpired:
        logging.warning("osascript timed out after 10s")
        return None
    except FileNotFoundError:
        logging.error("osascript not found")
        return None
```

**CRITICAL CAVEAT -- AX Tree Access (LOW confidence on exact hierarchy):**

The AppleScript above is a **template that MUST be validated** with Accessibility Inspector against the actual Teams window. The exact AX element hierarchy varies between:
- Old Teams (Electron): AX tree was accessible but is being deprecated
- New Teams (WebKit/Cocoa): AX tree may NOT be exposed by default

**The new Teams AX tree problem is the biggest technical risk in this milestone.** Research found:

1. A developer reported on Microsoft Tech Community that the new Teams (work or school) does not expose its AX Tree, unlike the old Electron-based Teams.
2. Electron apps have `AXManualAccessibility` attribute that can be set programmatically to enable the AX tree. The new Teams uses WebKit/Cocoa, so this attribute may not apply.
3. The Accessibility Inspector and VoiceOver can trigger the AX tree to become visible, suggesting the tree exists but is gated behind assistive technology detection.
4. The `set-electron-app-accessible` CLI tool works for Electron apps but may not work for the new WebKit-based Teams.

**Fallback strategies if AX tree is inaccessible:**
- Window title parsing: `name of window 1 of process "Microsoft Teams"` -- may contain contextual info
- Menu bar extra: Teams added a macOS menu bar icon with persistent presence status (rolled out late 2024). This icon's AX properties may be more accessible than the main window
- Graph API: Microsoft Graph `/me/presence` endpoint (requires OAuth, breaks stdlib-only constraint)

---

## Error Handling Patterns

### Subprocess Error Taxonomy

All three subprocess calls share the same error categories:

| Error | Cause | Handling |
|-------|-------|----------|
| `subprocess.TimeoutExpired` | Command hung (osascript is the most likely offender) | Log warning, return None/False |
| `FileNotFoundError` | Binary not found (should never happen on macOS) | Log error, return None/False |
| `returncode != 0` | Command-specific failure | Log with stderr, return None/False |
| Parse failure | Output format changed | Log warning, return None |

### Consistent Return Convention

All detection functions should return:
- **pgrep:** `bool` -- True if running, False otherwise
- **ioreg:** `Optional[float]` -- seconds idle, or None on failure
- **osascript:** `Optional[str]` -- status string, or None on failure

None/False always means "detection failed, do not gate notifications." This ensures the daemon never silently drops notifications due to detection errors.

### Timeout Budget

| Command | Typical Duration | Timeout | Rationale |
|---------|-----------------|---------|-----------|
| pgrep | <50ms | 5s | Extremely fast. 5s is 100x headroom. |
| ioreg | <200ms | 5s | Reads IOKit registry. 5s is generous. |
| osascript | 200ms-6s | 10s | Highly variable. `entire contents` queries on large AX trees can take seconds. System Events restart after idle adds ~2s. 10s covers worst case. |

**Total budget per detection cycle:** ~20s worst case if all commands timeout. In practice, expect <500ms total. Cache results to avoid running on every notification.

---

## Subprocess Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Shell injection | Never use `shell=True`. Always pass args as a list: `["pgrep", "-x", "Microsoft Teams"]`. nchook already follows this pattern (line 78). |
| Path hijacking | Use bare command names (`pgrep`, `ioreg`, `osascript`). macOS system binaries are in `/usr/bin/` and `/usr/sbin/` which are SIP-protected. No need for absolute paths. |
| Zombie processes | `subprocess.run()` waits for completion. The `timeout` parameter kills + waits on timeout. No zombie risk. |
| AppleScript injection | The AppleScript is a hardcoded constant, not user input. No injection vector. |

---

## Prerequisites and Permissions

| Requirement | Needed For | How to Enable |
|-------------|-----------|---------------|
| Accessibility access | osascript AX tree queries | System Settings > Privacy & Security > Accessibility > add Terminal/Python |
| Full Disk Access | Already granted (existing nchook requirement) | Already configured for notification DB access |
| No additional permissions | pgrep, ioreg | These are standard system utilities, no special permissions needed |

**Accessibility access is the NEW permission requirement** for this milestone. The terminal (or Python binary) running nchook must be granted Accessibility access in System Settings for AppleScript UI scripting to work. Without it, osascript will fail with: `"System Events got an error: [app] is not allowed assistive access."`.

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `subprocess.run(["pgrep", ...])` | `subprocess.run(["ps", "aux"])` + grep in Python | Never. pgrep is purpose-built for this. `ps aux` returns all processes and requires Python-side filtering. |
| `subprocess.run(["ioreg", ...])` + regex | `Quartz.CoreGraphics` via `pyobjc` | Never for this project. pyobjc is an external dependency. ioreg output is stable and well-documented. |
| `subprocess.run(["osascript", "-e", ...])` | `pyobjc` + `ApplicationServices` framework | If AX tree approach fails entirely and you need native AX API access. Breaks stdlib-only constraint. |
| `subprocess.run(["osascript", "-e", ...])` | JXA (JavaScript for Automation) via `osascript -l JavaScript` | If AppleScript syntax becomes unmanageable. JXA is an alternative scripting language for osascript. Same subprocess pattern, different `-l` flag. No Python-side changes. Consider if AX tree queries need complex logic. |
| Polling subprocess on timer | Running subprocess in thread | Never for this project. Status detection runs in the main event loop between kqueue events. Threading adds complexity for no benefit on a low-frequency polling path. |
| Cache detection results | Query on every notification | Always cache. Detection functions should cache their results for a configurable interval (e.g., 30s). Avoids spawning 3 subprocesses per notification burst. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `pyobjc` / `ApplicationServices` | Massive external dependency tree. Breaks stdlib-only constraint. Would give native AX API access but at enormous cost. | `subprocess.run(["osascript", "-e", ...])` |
| `appscript` / `py-applescript` | External pip packages. appscript is abandoned. py-applescript wraps NSAppleScript but adds a dependency. | `subprocess.run(["osascript", "-e", ...])` -- one-liner equivalent |
| `subprocess.Popen()` | Over-complex for this use case. `subprocess.run()` with `capture_output=True` and `timeout` is the correct high-level API. Popen is for streaming I/O or background processes, neither of which is needed here. | `subprocess.run()` |
| `shell=True` | Security risk (shell injection) and unnecessary. All commands are simple argument lists. nchook already uses list form. | `subprocess.run(["cmd", "arg1", "arg2"])` |
| `os.system()` | Legacy API. No output capture, no timeout, shell injection risk. | `subprocess.run()` |
| `shlex.split()` | Unnecessary when you construct the argument list directly. shlex is for parsing user-provided command strings, which we don't have. | Direct list: `["pgrep", "-x", "Microsoft Teams"]` |
| `threading` / `multiprocessing` | Status detection is fast enough to run synchronously. Adding threads for subprocess calls adds complexity, race conditions, and debugging difficulty for no measurable benefit. | Synchronous `subprocess.run()` in main loop |

---

## Implementation Notes

### Caching Strategy

Status detection should NOT run on every kqueue event. Spawn subprocesses at most once per `status_check_interval` (configurable, default 30s):

```python
_status_cache = {"teams_status": None, "idle_seconds": None, "teams_running": False}
_status_cache_time = 0

def get_cached_status(config):
    """Return cached status, refreshing if stale."""
    global _status_cache, _status_cache_time
    now = time.time()
    interval = config.get("status_check_interval", 30)
    if now - _status_cache_time < interval:
        return _status_cache

    _status_cache["teams_running"] = is_teams_running()
    if _status_cache["teams_running"]:
        _status_cache["idle_seconds"] = get_idle_seconds()
        _status_cache["teams_status"] = get_teams_status_applescript()
    else:
        _status_cache["idle_seconds"] = None
        _status_cache["teams_status"] = None

    _status_cache_time = now
    return _status_cache
```

### Detection Order

Run checks in this order (cheapest to most expensive, with early exit):

1. **pgrep** (~50ms) -- If Teams is not running, skip everything else
2. **ioreg** (~200ms) -- If system is idle beyond threshold, skip AX query
3. **osascript** (~0.5-6s) -- Most expensive, only run if needed

### osascript Performance Gotcha

System Events has a built-in `quit delay` (default 5 minutes). After idling, System Events quits itself. The next osascript call must relaunch it, adding ~2s latency. Mitigation options:
- Accept the occasional 2s delay (simplest)
- Set `quit delay` to 0 in the AppleScript to keep System Events alive (side effect: System Events never quits)
- Neither is critical; the daemon tolerates this latency since status checks are async to notification delivery

### Config Additions

New config keys needed in `config.json`:

```json
{
    "status_check_interval": 30,
    "idle_threshold_seconds": 300,
    "gate_when_status": ["Do not disturb", "Busy", "In a meeting", "In a call", "Presenting"]
}
```

---

## Version Compatibility

| Component | Compatible With | Notes |
|-----------|-----------------|-------|
| `subprocess.run()` | Python 3.5+ (capture_output added 3.7) | nchook targets 3.12+, no concerns. `capture_output` and `text` params are available. |
| `pgrep -x` | macOS 10.x through Sequoia 15+ | BSD pgrep, ships with macOS. `-x` flag for exact match is standard. |
| `ioreg -c IOHIDSystem` | macOS 10.x through Sequoia 15+ | IOKit registry is a core macOS subsystem. HIDIdleTime has been present since at least 10.6. |
| `osascript -e` | macOS 10.x through Sequoia 15+ | AppleScript execution binary, always present. Requires Accessibility permission for UI scripting. |
| HIDIdleTime (nanoseconds) | macOS Intel + Apple Silicon | Value format is consistent across architectures. Known edge case: may not reset on headless Macs without physical HID devices. |
| System Events AX scripting | macOS 10.x through Sequoia 15+ | Requires Accessibility permission. New Teams (WebKit) may not expose AX tree without workaround. |

---

## Sources

### HIGH Confidence
- [Python 3.12 subprocess documentation](https://docs.python.org/3.12/library/subprocess.html) -- subprocess.run() API, timeout behavior, capture_output
- [pgrep(1) man page](https://www.man7.org/linux/man-pages/man1/pgrep.1.html) -- -x exact match flag, exit code semantics
- [Apple Mac Automation Scripting Guide](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html) -- UI scripting via System Events, accessibility requirements
- [Apple IOKit Registry docs](https://developer.apple.com/library/archive/documentation/DeviceDrivers/Conceptual/IOKitFundamentals/TheRegistry/TheRegistry.html) -- ioreg structure, IOHIDSystem class
- nchook.py existing subprocess usage (line 77-83, `detect_db_path`) -- proven pattern in codebase

### MEDIUM Confidence
- [Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/) -- HIDIdleTime output format, nanosecond units, ioreg parsing patterns
- [Karabiner-Elements issue #385](https://github.com/pqrs-org/Karabiner-Elements/issues/385) -- HIDIdleTime reset behavior with third-party input remappers
- [Microsoft Teams: Menu Bar Icon for Mac](https://websites.uta.edu/oit/2024/10/16/microsoft-teams-menu-bar-icon-for-mac-devices/) -- Teams menu bar presence indicator rollout (late 2024)
- [Teams Helper Process CPU discussion](https://learn.microsoft.com/en-us/answers/questions/4415259/teams-helper-process-using-100-cpu-on-silicon-mac) -- Teams process names: "Microsoft Teams", "Microsoft Teams Helper", "Microsoft Teams Helper (Renderer)"
- [osascript performance on Sonoma](https://github.com/guidepup/guidepup/issues/87) -- osascript latency issues on recent macOS versions

### LOW Confidence (Needs Validation)
- [Microsoft Tech Community: AX Tree in new Teams](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) -- New Teams does NOT expose AX tree by default. Critical finding. Needs hands-on validation with Accessibility Inspector.
- [Electron issue #7206](https://github.com/electron/electron/issues/7206) -- AXManualAccessibility attribute for enabling AX in Electron apps. May not apply to new WebKit-based Teams.
- [set-electron-app-accessible](https://github.com/JonathanGawrych/set-electron-app-accessible) -- CLI tool for enabling Electron app accessibility. Likely does not work with new WebKit Teams.
- [HIDIdleTime not reset on headless Mac](https://developer.apple.com/forums/thread/721530) -- Edge case: HIDIdleTime may not reset without physical USB HID device. Not relevant for desktop use but worth noting.

### Confidence Notes

| Claim | Confidence | Basis |
|-------|------------|-------|
| subprocess.run() with capture_output/text/timeout works | HIGH | Python official docs + existing nchook usage |
| pgrep -x "Microsoft Teams" detects the main process | MEDIUM | Multiple sources confirm process name; -x prevents helper matches |
| ioreg HIDIdleTime is in nanoseconds | HIGH | Multiple sources, consistent documentation across years |
| ioreg output contains `"HIDIdleTime" = <int>` | HIGH | Multiple parsing examples use this exact format |
| New Teams exposes AX tree for status reading | LOW | Reports say it does NOT. This is the biggest risk. |
| AppleScript entire contents on Teams window is reliable | LOW | Depends on AX tree exposure. May need fallback strategy. |
| System Events quit delay causes ~2s startup latency | MEDIUM | Documented in Apple resources, community confirmed |
| Accessibility permission required for osascript UI scripting | HIGH | Apple official documentation |

---
*Stack research for: Teams status detection via subprocess (AppleScript, ioreg, pgrep)*
*Researched: 2026-02-11*
