# Project Research Summary

**Project:** Teams Notification Interceptor (v1.1 -- Status-Aware Gating)
**Domain:** macOS daemon -- user presence detection via subprocess fallback chain
**Researched:** 2026-02-11
**Confidence:** MEDIUM

## Executive Summary

This milestone adds status-aware notification gating to an existing 849 LOC Python daemon that already intercepts Teams notifications from the macOS notification center database. The approach is a three-signal fallback chain: (1) AppleScript AX tree reading for direct Teams status text, (2) ioreg HIDIdleTime for system idle detection, and (3) pgrep for Teams process liveness. All three use `subprocess.run()` which is already imported and used in the codebase. No new Python dependencies are needed. The daemon will forward notifications when the user is Away or Busy, and suppress when Available -- with a fail-open policy that forwards on any detection failure.

The recommended approach builds on existing patterns in the codebase (subprocess calls, config loading, webhook payload building) and requires modifying only 4 existing functions while adding approximately 200-250 LOC of new detection and gating logic. The architecture inserts a status gate before the per-notification processing loop, checks status once per poll cycle (not per notification), and always advances the notification high-water mark even when gating suppresses forwarding. This prevents stale notification replay on status transitions.

The primary risk is the AppleScript AX signal. The new Teams client (com.microsoft.teams2) has a known broken Accessibility tree -- community reports confirm the old Electron AX walking methods no longer work. This signal may be permanently unavailable. The architecture is designed for this: idle time + process check alone deliver approximately 80% of the value (forward when idle for 5+ minutes, suppress when active). AX reading is a high-value enhancement that must be validated empirically with Accessibility Inspector before investing implementation time.

## Key Findings

### Recommended Stack

No new Python imports are needed. The existing `subprocess`, `re`, `logging`, and `time` imports cover all requirements. Three macOS system binaries provide the detection signals: `/usr/bin/pgrep` (process check, <50ms), `/usr/sbin/ioreg` (idle time, <200ms), and `/usr/bin/osascript` (AX status, 200ms-3s). All are SIP-protected system utilities present on every macOS installation.

**Core technologies:**
- `subprocess.run()` with `capture_output=True, text=True, timeout=N`: executes all three detection commands -- already used in codebase (line 77)
- `ioreg -c IOHIDSystem -d 4`: reads HIDIdleTime in nanoseconds from IOKit registry -- well-documented, stable across macOS versions
- `pgrep -x "Microsoft Teams"`: exact-match process name check -- fast, no permissions needed
- `osascript -e <AppleScript>`: walks Teams AX tree for status text -- requires Accessibility permission, may not work with new Teams

### Expected Features

**Must have (table stakes):**
- System idle detection via HIDIdleTime (most reliable signal, zero permissions)
- Process-level Teams detection (if Teams is not running, always forward)
- Configurable idle threshold (default 300s, matching Teams' own idle threshold)
- Status-aware gating logic (forward/suppress decision engine)
- Fallback chain assembly (AX -> idle -> process, graceful degradation)
- Status metadata in webhook payload (detected_status, status_source, status_confidence)
- "Always forward" escape hatch (config flag to disable gating entirely)
- Graceful degradation without Accessibility permission

**Should have (differentiators):**
- Teams AX status text reading (direct Teams status, highest fidelity -- but highest risk)
- Teams menu bar icon status reading (potential AX alternative, unverified)
- Status change event logging (trivial to implement, essential for debugging)
- Per-status gating rules (configurable forward/suppress per status value)

**Defer (v2+):**
- Screen lock / session state detection (requires Core Graphics, not stdlib)
- macOS Focus/DND mode detection (plist path varies by OS version)
- Hysteresis / debounce for status transitions (only build if users report flapping)
- Calendar integration (duplicates what Teams already provides)
- Microsoft Graph API (breaks stdlib-only constraint and adds enormous complexity)

### Architecture Approach

The status detection system integrates into the existing event loop as a pre-processing step. One call to `detect_user_status()` per poll cycle produces a status result dict with `status`, `source`, `confidence`, and `raw_value` fields. The `passes_status_gate()` function makes a forward/drop decision based on this result and the configured `forward_statuses` set. When gating suppresses forwarding, the daemon still queries notifications and advances the rec_id high-water mark to prevent replay. The webhook payload gains three underscore-prefixed metadata fields following the existing `_source` and `_truncated` convention.

**Major components:**
1. **Status detector** (`detect_user_status`) -- orchestrates the three-signal fallback chain, returns canonical status result dict
2. **Signal functions** (`_detect_status_ax`, `_detect_idle_time`, `_detect_teams_process`) -- each handles its own errors internally, returns typed value or None
3. **Status normalizer** (`_normalize_ax_status`) -- maps raw AX text to canonical status strings
4. **Status gate** (`passes_status_gate`) -- forward/drop decision based on status and configured forward_statuses set
5. **Config extension** (`load_config`) -- new keys: `status_enabled`, `status_ax_enabled`, `idle_threshold_seconds`, `forward_statuses`
6. **Payload extension** (`build_webhook_payload`) -- adds `_detected_status`, `_status_source`, `_status_confidence`

### Critical Pitfalls

1. **subprocess.run() blocks the kqueue event loop** -- AX queries take 500ms-3s and compound during notification bursts. Prevention: check status once per poll cycle (not per notification), always use `timeout=` on every subprocess call, check status before processing batch.

2. **osascript hangs indefinitely when Teams window is unavailable** -- AppleScript timeout semantics do not apply to System Events AX access. Prevention: always pass `timeout=5` to subprocess.run(), catch TimeoutExpired, pre-check Teams process before running osascript.

3. **Accessibility permission grants to the wrong app** -- macOS grants AX permission to the parent terminal, not osascript. Testing in one terminal succeeds, running from another fails silently. Prevention: probe AX at startup, detect parent terminal via TERM_PROGRAM, print actionable instructions.

4. **New Teams (com.microsoft.teams2) has a broken AX tree** -- parent-child relationships are inconsistent, the old Electron walking method is dead. Prevention: validate AX results against known status strings, design the system to work well WITHOUT AX, treat AX as a nice-to-have enhancement.

5. **Gating logic silently drops notifications on UNKNOWN status** -- if all signals fail and UNKNOWN maps to "drop", the daemon silently loses messages. Prevention: fail-open policy -- UNKNOWN is in the default `forward_statuses` set. Better to get a duplicate than miss a message.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Status Detection Core

**Rationale:** Build the three signal functions and fallback chain orchestrator first. These are independent of the existing event loop and can be tested in isolation. Start with ioreg (guaranteed to work, no permissions), then pgrep (guaranteed to work), then AX (may not work). This order validates the fallback chain with working signals before tackling the uncertain AX signal.

**Delivers:** `_detect_idle_time()`, `_detect_teams_process()`, `_detect_status_ax()` (placeholder AX script), `_normalize_ax_status()`, `detect_user_status()` orchestrator.

**Addresses:** System idle detection, process-level Teams detection, fallback chain assembly, graceful AX degradation.

**Avoids:** Pitfall 1 (blocks event loop) by establishing the "check once" pattern from the start. Pitfall 5 (ioreg parsing) by implementing correct nanosecond conversion immediately. Pitfall 7 (pgrep wrong match) by using `-x` exact match.

### Phase 2: Config, Gating Logic, and Event Loop Integration

**Rationale:** With detection functions in place, wire the config keys, gating decision, and event loop integration. This phase modifies the 4 existing functions (`load_config`, `run_watcher`, `build_webhook_payload`, `print_startup_summary`). The critical design decision -- always query and advance state even when gating suppresses -- must be implemented here.

**Delivers:** `passes_status_gate()`, modified event loop with status check before batch processing, config keys (`status_enabled`, `status_ax_enabled`, `idle_threshold_seconds`, `forward_statuses`), webhook payload with status metadata, startup summary showing status config.

**Addresses:** Status-aware gating logic, status metadata in webhook, "always forward" escape hatch, per-status gating rules.

**Avoids:** Pitfall 10 (drops on UNKNOWN) by implementing fail-open from the start. Anti-Pattern 3 (not advancing state when gated) by always querying and advancing rec_id.

### Phase 3: AX Discovery and Permission Handling

**Rationale:** This phase requires interactive access to a running Teams instance and Accessibility Inspector. It cannot be planned purely from research. The AX script is a placeholder until empirically validated. This phase also adds the startup AX probe with actionable permission instructions.

**Delivers:** Real AppleScript for Teams AX status reading (or confirmed inability to read it), startup AX permission probe with user-friendly instructions, AX backoff logic (disable after consecutive failures, re-probe periodically).

**Addresses:** Teams AX status text reading, Accessibility permission handling, menu bar icon as alternative AX target.

**Avoids:** Pitfall 2 (osascript hangs) via timeout enforcement. Pitfall 3 (wrong permission target) via startup probe. Pitfall 4 (broken AX tree) via validation against known status strings. Pitfall 9 (entire contents slow) via targeted element path.

### Phase 4: Hardening and Polish

**Rationale:** After core functionality works, add resilience features that prevent edge-case failures. These are refinements on top of working signal collection and gating.

**Delivers:** Status change event logging, hysteresis for status transitions (if needed), sleep/wake cache invalidation, consecutive AX failure tracking, ioreg format change detection with warning logs.

**Addresses:** Status change logging, fallback chain consistency during transitions, cache freshness after sleep/wake.

**Avoids:** Pitfall 6 (inconsistent status during transitions) via hysteresis. Pitfall 8 (stale cache after sleep) via monotonic time and gap detection. Pitfall 12 (ioreg format changes) via defensive logging.

### Phase Ordering Rationale

- **Phases 1-2 before Phase 3:** The system must work without AX before investing in AX discovery. Research shows AX may be permanently broken for new Teams. Building idle+process detection first means the daemon delivers value even if Phase 3 discovers AX is non-viable.
- **Phase 2 before Phase 3:** The gating logic and event loop integration define the contract for status results. AX discovery plugs into a tested framework rather than requiring simultaneous integration.
- **Phase 4 last:** Hardening features (hysteresis, sleep/wake, logging) are refinements that require observing the system running. Building them before core functionality wastes effort if the core design changes.
- **Dependencies are strict:** Phase 2 depends on Phase 1 (needs detection functions). Phase 3 depends on Phase 2 (needs the config/gating framework). Phase 4 depends on all previous phases (refines working system).

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3 (AX Discovery):** The AppleScript AX tree path cannot be determined from research alone. Requires interactive Accessibility Inspector session with running Teams. LOW confidence on whether this signal is viable at all. Consider the Teams menu bar extension as an alternative AX target.

Phases with standard patterns (skip research-phase):
- **Phase 1 (Detection Core):** ioreg and pgrep patterns are well-documented with HIGH confidence. subprocess.run() is already used in the codebase. Standard implementation.
- **Phase 2 (Config/Gating/Integration):** Extends existing config/payload/loop patterns. The "always advance state" pattern is clearly defined. Standard implementation.
- **Phase 4 (Hardening):** Hysteresis, logging, and cache management are standard software patterns. No domain-specific research needed.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All tools are stdlib or macOS system binaries. subprocess.run() pattern is proven in the codebase. No new dependencies. |
| Features | MEDIUM | Table stakes and gating logic are well-understood. AX reading (the primary differentiator) has LOW confidence due to new Teams AX tree issues. |
| Architecture | HIGH | Fallback chain pattern is established. Integration points are clearly identified. Only 4 existing functions need modification. ~200-250 new LOC. |
| Pitfalls | HIGH | 13 pitfalls documented with clear prevention strategies. The critical ones (subprocess timeout, AX permission, broken AX tree) have well-defined mitigations. |

**Overall confidence:** MEDIUM -- the system design and non-AX signals are solid (HIGH), but the highest-value signal (AX status text) has the lowest confidence. The architecture's fallback design means this risk is managed, not blocking.

### Gaps to Address

- **New Teams AX tree viability:** Must be validated with Accessibility Inspector on the actual installed Teams version before writing the AppleScript. If the AX tree is non-functional, Phase 3 scope shrinks to "confirm AX is not viable, document findings, ensure idle+process fallback is sufficient."
- **Teams menu bar extension AX properties:** The menu bar presence indicator (GA since Nov 2024) may expose status via AX description attribute. Completely unverified. Could be an alternative to main window AX walking if the main window tree is broken.
- **Teams process name on target machine:** "Microsoft Teams" is the expected name for pgrep, but this must be verified once on the target machine. The process name is configurable in the proposed config, so runtime discovery is straightforward.
- **time.monotonic() behavior during macOS sleep:** Python docs say monotonic clock "may or may not include time during sleep" and this varies by platform. Must verify on macOS Sequoia whether monotonic time advances during sleep. This affects cache invalidation correctness.
- **Karabiner-Elements HIDIdleTime edge case:** Known issue where keyboard input may not reset HIDIdleTime when using Karabiner. Document as a known limitation rather than solving it.

## Sources

### Primary (HIGH confidence)
- [Python 3.12 subprocess documentation](https://docs.python.org/3.12/library/subprocess.html) -- subprocess.run() API, timeout, capture_output
- [Apple Mac Automation Scripting Guide](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/AutomatetheUserInterface.html) -- UI scripting, Accessibility requirements
- [Apple IOKit Registry docs](https://developer.apple.com/library/archive/documentation/DeviceDrivers/Conceptual/IOKitFundamentals/TheRegistry/TheRegistry.html) -- ioreg structure, IOHIDSystem
- [DSSW: Inactivity and Idle Time on OS X](https://www.dssw.co.uk/blog/2015-01-21-inactivity-and-idle-time/) -- HIDIdleTime nanosecond format, parsing patterns
- [Scripting OS X: AppleScript Security and Privacy](https://scriptingosx.com/2020/09/avoiding-applescript-security-and-privacy-requests/) -- AX permission model
- Existing nchook.py subprocess usage (line 77-83, `detect_db_path`) -- proven pattern in codebase

### Secondary (MEDIUM confidence)
- [Microsoft Community: Teams AX tree issue](https://techcommunity.microsoft.com/discussions/teamsdeveloper/enable-accessibility-tree-on-macos-in-the-new-teams-work-or-school/4033014) -- New Teams AX tree is broken
- [Karabiner-Elements issue #385](https://github.com/pqrs-org/Karabiner-Elements/issues/385) -- HIDIdleTime edge case
- [Microsoft: Teams Menu Bar Icon for Mac](https://websites.uta.edu/oit/2024/10/16/microsoft-teams-menu-bar-icon-for-mac-devices/) -- Menu bar presence indicator
- [Helge Klein: Identifying MS Teams Application Instances](https://helgeklein.com/blog/identifying-ms-teams-application-instances-counting-app-starts/) -- Teams process names
- [GitHub: TeamsStatusMacOS](https://github.com/RobertD502/TeamsStatusMacOS) -- Alternative status detection approaches

### Tertiary (LOW confidence, needs validation)
- [Apple Developer Forums: AX elements only exposed with VoiceOver active](https://developer.apple.com/forums/thread/756895) -- AX tree gating behavior
- [Electron issue #7206](https://github.com/electron/electron/issues/7206) -- AXManualAccessibility (likely does not apply to new WebKit Teams)
- [Apple Developer Forums: HIDIdleTime not reset on headless Mac](https://developer.apple.com/forums/thread/721530) -- Edge case for non-desktop setups

---
*Research completed: 2026-02-11*
*Ready for roadmap: yes*
