---
phase: 06-ax-discovery-permission-handling
verified: 2026-02-11T23:45:00Z
status: passed
score: 4/4 must-haves verified
---

# Phase 6: AX Discovery and Permission Handling Verification Report

**Phase Goal:** Daemon can read Teams status text directly from the Accessibility tree when permission is granted, and degrades gracefully to idle+process fallback when it is not

**Verified:** 2026-02-11T23:45:00Z

**Status:** passed

**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | With AX permission granted and Teams running, detect_user_status() returns the actual Teams status with source=ax and confidence=high | ✓ VERIFIED | _detect_status_ax() implemented with osascript AppleScript query (lines 753-857). Returns raw status text. detect_user_status() calls _detect_status_ax() (line 895), normalizes result via _normalize_ax_status() (line 898), and returns with source=ax, confidence=high (lines 897-901). Runtime test confirms None returned when permission denied (expected), proving permission gating works. |
| 2 | Raw AX status text (e.g., 'Be Right Back', 'Do not disturb') is normalized to canonical values (BeRightBack, DoNotDisturb, etc.) | ✓ VERIFIED | _AX_STATUS_MAP (lines 860-872) maps raw text to canonical values. _normalize_ax_status() (lines 875-877) performs normalization. Runtime test: _normalize_ax_status('Do not disturb') → 'DoNotDisturb', _normalize_ax_status('Be Right Back') → 'BeRightBack' |
| 3 | Without AX permission, startup logs actionable instructions naming the correct terminal application | ✓ VERIFIED | print_startup_summary() calls _check_ax_permission() (line 370), caches result in _ax_available (line 370). When AX not available, logs actionable instructions (lines 374-380) with terminal app name from _get_terminal_app_name() (lines 676-693). Terminal detection maps TERM_PROGRAM env var. Runtime test: _get_terminal_app_name() → 'Terminal.app' |
| 4 | When AX signal fails or permission is denied, the daemon silently falls back to idle+process detection without user intervention | ✓ VERIFIED | _detect_status_ax() returns None when permission denied (lines 773-774) or on any failure. detect_user_status() fallback chain proceeds to idle signal (line 904) when AX returns None. Self-disable safety net after 3 consecutive failures (lines 818-824, 830-836, 846-852). Runtime test: detect_user_status() → {'detected_status': 'Away', 'status_source': 'idle', 'status_confidence': 'medium'} (proves fallback works) |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| nchook.py: _check_ax_permission() | ctypes AXIsProcessTrusted call | ✓ VERIFIED | Lines 644-661. Loads ApplicationServices framework via ctypes.cdll.LoadLibrary, calls AXIsProcessTrusted() with c_bool return type. Returns True/False. Runtime test confirms: returns False (no AX permission in current env) |
| nchook.py: _detect_status_ax() | Real implementation with osascript | ✓ VERIFIED | Lines 753-857 (106 lines). Contains osascript subprocess call (line 809), AppleScript with two candidate paths (menu bar + window static text), 3s timeout (line 812), permission gate (lines 771-774), safety net (lines 817-852). Function substantive (not stub) |
| nchook.py: _get_terminal_app_name() | TERM_PROGRAM mapping | ✓ VERIFIED | Lines 676-693. Reads os.environ.get("TERM_PROGRAM") (line 683), maps to user-friendly names. Runtime test confirms: returns 'Terminal.app' for Apple_Terminal |
| nchook.py: _ax_available cache | Module-level variable | ✓ VERIFIED | Line 667 declares _ax_available = None. Set in print_startup_summary() (line 370) and checked in _detect_status_ax() (lines 771-773) |
| nchook.py: _ax_consecutive_failures | Module-level counter | ✓ VERIFIED | Line 670 declares _ax_consecutive_failures = 0. Incremented on failure (lines 817, 829, 845), reset on success (line 840) |
| nchook.py: _AX_MAX_FAILURES | Module-level constant | ✓ VERIFIED | Line 673 declares _AX_MAX_FAILURES = 3. Used in safety net checks (lines 818, 830, 846) |
| nchook.py: import ctypes | Import statement | ✓ VERIFIED | Line 28: import ctypes |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| _detect_status_ax() | _check_ax_permission() | permission gate before osascript | ✓ WIRED | Lines 771-774: if _ax_available is None, call _check_ax_permission(). If not _ax_available, return None immediately (no osascript call) |
| _detect_status_ax() | detect_user_status() | existing fallback chain | ✓ WIRED | Line 895: ax_status = _detect_status_ax(). Lines 896-901: if ax_status is not None, normalize and return with source=ax. If None, fall through to line 904 (idle signal) |
| print_startup_summary() | _check_ax_permission() | AX status display and actionable instructions | ✓ WIRED | Line 370: _ax_available = _check_ax_permission(). Lines 371-380: if _ax_available conditional display of status and instructions |
| print_startup_summary() | _get_terminal_app_name() | actionable instructions | ✓ WIRED | Line 375: app_name = _get_terminal_app_name(). Line 378: used in instructions log message |
| _detect_status_ax() | _normalize_ax_status() | existing normalization in detect_user_status | ✓ WIRED | Line 898 in detect_user_status(): _normalize_ax_status(ax_status) called when ax_status is not None |

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| STAT-03: Daemon reads Teams status text from Accessibility tree via osascript | ✓ SATISFIED | None. _detect_status_ax() (lines 753-857) implements osascript AppleScript query with two candidate AX paths |
| STAT-04: Daemon normalizes raw AX status text to canonical values | ✓ SATISFIED | None. _AX_STATUS_MAP (lines 860-872) + _normalize_ax_status() (lines 875-877) normalize raw text. Runtime test confirms correct mapping |
| INTG-04: Daemon probes Accessibility permission at startup and logs actionable instructions | ✓ SATISFIED | None. _check_ax_permission() (lines 644-661) probes permission. print_startup_summary() (lines 370-380) displays status and instructions with terminal app name |
| INTG-05: Daemon gracefully degrades to idle+process fallback when AX permission denied or signal fails | ✓ SATISFIED | None. _detect_status_ax() returns None on permission denial (lines 773-774) or any failure. detect_user_status() fallback chain proceeds to idle signal (line 904). Self-disable safety net (lines 818-852) prevents wasted cycles |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| nchook.py | 894 | Outdated comment: "Signal 1: AX status text (placeholder -- always None until Phase 6)" | ℹ️ Info | Comment refers to Phase 6 as future, but this IS Phase 6 and AX is implemented. Should be updated to remove "placeholder" language. Does not affect functionality. |

### Human Verification Required

#### 1. AX Permission Grant and Real Teams Status Reading

**Test:** 
1. Grant Accessibility permission to Terminal.app (or user's terminal app):
   - Open System Settings > Privacy & Security > Accessibility
   - Click the + button and add the terminal app
   - Restart the daemon
2. Launch Microsoft Teams (com.microsoft.teams2 or com.microsoft.teams)
3. Set Teams status to a known value (e.g., "Busy", "Do not disturb", "Away")
4. Check daemon logs or call detect_user_status() to see the detected status

**Expected:** 
- detect_user_status() returns detected_status matching the Teams status
- status_source is "ax"
- status_confidence is "high"

**Why human:** 
AX permission requires System Settings UI interaction. Teams status setting requires running the Teams app. Current environment has no AX permission (returns False), so this path can't be tested programmatically without user interaction.

#### 2. AX Tree Broken for New Teams (Self-Disable Safety Net)

**Test:** 
1. Grant AX permission (as above)
2. Launch Microsoft Teams (com.microsoft.teams2)
3. Run the daemon and observe logs
4. If AX tree is broken (known issue per research), osascript will return empty or error
5. After 3 consecutive failures (15 seconds at 5s poll interval), daemon should log: "AX query failed/returned empty/timed out 3 consecutive times; disabling AX for this session. Falling back to idle+process signals."
6. Subsequent polls should NOT call osascript (fast path via _ax_available = False)

**Expected:** 
- After 3 consecutive AX failures, single INFO log about disabling AX
- No further osascript calls (no 3s timeout waste per cycle)
- Status detection continues via idle+process signals

**Why human:** 
Requires running Teams and observing real-time behavior. AX tree viability for com.microsoft.teams2 is unknown and may vary by Teams version. Safety net is critical for production use but can't be verified without a running Teams instance.

#### 3. Startup Instructions Display

**Test:**
1. Run the daemon without AX permission
2. Check startup banner logs for AX status section

**Expected:**
```
  AX status:   NOT AVAILABLE (Accessibility permission not granted)
  To enable AX-based status detection:
    1. Open System Settings > Privacy & Security > Accessibility
    2. Click the + button and add: Terminal.app
    3. Restart the daemon
  Without AX, status detection falls back to idle+process signals.
```

**Why human:** 
Visual confirmation of log output format and actionable instructions. Automated check can verify lines exist but not that they are clear and actionable for end users.

---

## Summary

**All must-haves verified.** Phase 6 goal achieved.

**Artifacts:** All 7 artifacts exist, are substantive (not stubs), and are wired correctly.

**Key links:** All 5 key links verified wired.

**Requirements:** All 4 requirements (STAT-03, STAT-04, INTG-04, INTG-05) satisfied.

**Anti-patterns:** 1 informational (outdated comment). No blockers.

**Runtime tests:** All passed:
- Import: No errors
- _check_ax_permission(): Returns False (expected, no AX permission)
- _get_terminal_app_name(): Returns 'Terminal.app'
- _detect_status_ax(): Returns None (expected, no AX permission)
- detect_user_status(): Returns Away via idle signal (proves fallback chain works)
- _normalize_ax_status(): Correctly normalizes 'Do not disturb' → 'DoNotDisturb', 'Be Right Back' → 'BeRightBack'

**Commits:** Both task commits verified:
- 9fda7ac: Task 1 (AX permission probe, terminal detection, startup instructions)
- b84910a: Task 2 (Replace AX status detection placeholder with real implementation)

**Human verification needed:** 3 items — AX permission grant + real Teams status reading, self-disable safety net with broken AX tree, startup instructions display. These require system UI interaction and running Teams, which cannot be automated.

---

_Verified: 2026-02-11T23:45:00Z_

_Verifier: Claude (gsd-verifier)_
