---
phase: 06-ax-discovery-permission-handling
plan: 01
subsystem: status-detection
tags: [accessibility, ctypes, applescript, osascript, ax-permission, graceful-degradation]

# Dependency graph
requires:
  - phase: 04-status-detection-core
    provides: "_detect_status_ax() placeholder, _normalize_ax_status(), _AX_STATUS_MAP, detect_user_status() fallback chain"
  - phase: 05-config-gating-event-loop-integration
    provides: "print_startup_summary() with status_enabled gating, event loop status check"
provides:
  - "_check_ax_permission() via ctypes AXIsProcessTrusted for AX permission probing"
  - "_detect_status_ax() real implementation with osascript AppleScript query"
  - "_get_terminal_app_name() for actionable AX permission instructions"
  - "Module-level _ax_available cache with self-disable safety net"
  - "Startup banner AX status display with actionable instructions"
affects: []

# Tech tracking
tech-stack:
  added: [ctypes]
  patterns: [ctypes-framework-loading, applescript-ax-query, permission-cache-pattern, consecutive-failure-safety-net]

key-files:
  created: []
  modified: [nchook.py]

key-decisions:
  - "ctypes AXIsProcessTrusted over osascript probe: instant boolean check vs 30s+ hang when permission denied"
  - "Permission cached at startup in _ax_available: TCC changes require process restart on macOS"
  - "3s osascript timeout (< 5s poll interval): prevents AX query from blocking event loop"
  - "Self-disable after 3 consecutive failures: avoids wasting 3s/cycle on broken AX tree"
  - "Two candidate AX paths (menu bar extension + window static text): covers both new and legacy Teams layouts"

patterns-established:
  - "Permission probe pattern: ctypes -> cache boolean -> gate all dependent calls"
  - "Safety net pattern: consecutive failure counter -> self-disable with INFO log"
  - "Terminal detection pattern: TERM_PROGRAM env var -> user-friendly name mapping"

# Metrics
duration: 4min
completed: 2026-02-11
---

# Phase 6 Plan 1: AX Discovery and Permission Handling Summary

**AX permission probe via ctypes AXIsProcessTrusted, AppleScript Teams status query with two candidate paths, self-disabling safety net, and actionable startup instructions naming the user's terminal app**

## Performance

- **Duration:** 4 min
- **Started:** 2026-02-11T23:30:44Z
- **Completed:** 2026-02-11T23:35:02Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments
- Replaced `_detect_status_ax()` placeholder with real AppleScript-based implementation that reads Teams status via System Events
- Added `_check_ax_permission()` using ctypes to call `AXIsProcessTrusted()` from ApplicationServices framework -- returns boolean instantly
- Startup banner now displays AX permission status and actionable instructions (with terminal app name from TERM_PROGRAM) when Accessibility permission is not granted
- Self-disabling safety net: after 3 consecutive AX failures, `_ax_available` set to False for the session, preventing 3s/cycle waste on broken AX tree

## Task Commits

Each task was committed atomically:

1. **Task 1: AX permission probe, terminal detection, and startup instructions** - `9fda7ac` (feat)
2. **Task 2: Replace AX status detection placeholder with real implementation** - `b84910a` (feat)

**Plan metadata:** [pending] (docs: complete plan)

## Files Created/Modified
- `nchook.py` - Added ctypes import, `_check_ax_permission()`, `_get_terminal_app_name()`, `_ax_available`/`_ax_consecutive_failures`/`_AX_MAX_FAILURES` module-level state, replaced `_detect_status_ax()` placeholder with real AppleScript implementation, added AX status display to `print_startup_summary()`

## Decisions Made
- Used ctypes `AXIsProcessTrusted()` over osascript probe: instant boolean vs 30s+ hang on missing permission
- Cached AX permission at startup: macOS TCC changes require process restart, so single-check-at-startup is correct
- Set osascript timeout to 3s (strictly < 5s poll interval) to prevent event loop blocking
- Self-disable safety net after 3 consecutive failures: handles known broken AX tree in new Teams (com.microsoft.teams2)
- Two candidate AppleScript paths: menu bar extension description (promising for new Teams late 2024+) and window static text (legacy fallback)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required
None - no external service configuration required. AX permission is optional and the daemon degrades gracefully without it.

## Next Phase Readiness
- Phase 6 is the final phase of v1.1 -- all 16 requirements now have implementations
- AX permission is False in the current environment (expected), confirming the graceful degradation path works
- If/when a user grants Accessibility permission and Teams exposes its AX tree, the implementation will attempt to read status text
- The known-broken AX tree for new Teams (com.microsoft.teams2) means AX may not yield useful results even with permission -- the self-disable safety net handles this case

## Self-Check: PASSED

All files exist, all commits verified, all plan artifacts present (10/10 checks passed).

---
*Phase: 06-ax-discovery-permission-handling*
*Completed: 2026-02-11*
