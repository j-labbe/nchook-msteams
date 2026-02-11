# Requirements: Teams Notification Interceptor

**Defined:** 2026-02-11
**Core Value:** Reliably capture every Teams message notification and deliver it as structured JSON to a webhook -- no missed messages, no noise.

## v1.1 Requirements

Requirements for v1.1 Teams Status Integration. Each maps to roadmap phases.

### Status Detection

- [ ] **STAT-01**: Daemon reads system idle time via `ioreg -c IOHIDSystem` and converts HIDIdleTime from nanoseconds to seconds
- [ ] **STAT-02**: Daemon detects whether Microsoft Teams is running via `pgrep -x "Microsoft Teams"`
- [ ] **STAT-03**: Daemon reads Teams status text from the Accessibility tree via AppleScript (`osascript`)
- [ ] **STAT-04**: Daemon normalizes raw AX status text to canonical values: Available, Busy, Away, DoNotDisturb, BeRightBack, Offline, Unknown
- [ ] **STAT-05**: Daemon orchestrates a three-signal fallback chain (AX → idle → process) with subprocess timeout enforcement on each signal
- [ ] **STAT-06**: When AX is unavailable, idle time >= 300s maps to Away; < 300s with Teams running maps to Available; Teams not running maps to Offline
- [ ] **STAT-07**: Status result includes detected_status, status_source (ax/idle/process), and status_confidence (high/medium/low)

### Notification Gating

- [ ] **GATE-01**: Daemon forwards notifications when detected status is Away or Busy; suppresses when Available, Offline, DoNotDisturb, or BeRightBack
- [ ] **GATE-02**: Daemon forwards notifications when status is Unknown (fail-open policy -- never silently drop messages)
- [ ] **GATE-03**: Daemon always advances the rec_id high-water mark even when gating suppresses forwarding (prevents stale replay on status transition)
- [ ] **GATE-04**: Daemon checks status once per poll cycle before processing the notification batch, not per individual notification

### Integration

- [ ] **INTG-01**: Config supports `status_enabled` boolean to enable/disable status-aware gating entirely (default: true)
- [ ] **INTG-02**: Webhook JSON payload includes `_detected_status`, `_status_source`, and `_status_confidence` metadata fields
- [ ] **INTG-03**: Startup summary displays status detection mode (enabled/disabled) and current detected status
- [ ] **INTG-04**: Daemon probes Accessibility permission at startup and logs actionable instructions if AX signal is unavailable
- [ ] **INTG-05**: Daemon gracefully degrades to idle+process fallback when AX permission is not granted or AX signal fails

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Status Refinements

- **SREF-01**: Hysteresis/debounce for status transitions (build only if flapping reported)
- **SREF-02**: Screen lock / session state detection via Core Graphics
- **SREF-03**: macOS Focus/DND mode detection
- **SREF-04**: Per-status configurable gating rules (forward_statuses set)
- **SREF-05**: Configurable idle threshold (currently hardcoded at 300s)

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Microsoft Graph API | Avoiding API complexity is the whole point of the local approach |
| Calendar integration | Duplicates what Teams already provides |
| Teams log file parsing | Dead end -- log format is unstable and undocumented |
| Pixel/screen scraping | Fragile, high maintenance burden |
| Continuous AX observer | Polling per cycle is sufficient; observer adds complexity |
| Retry queue for status detection | Log-and-skip matches v1.0 failure philosophy |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| STAT-01 | — | Pending |
| STAT-02 | — | Pending |
| STAT-03 | — | Pending |
| STAT-04 | — | Pending |
| STAT-05 | — | Pending |
| STAT-06 | — | Pending |
| STAT-07 | — | Pending |
| GATE-01 | — | Pending |
| GATE-02 | — | Pending |
| GATE-03 | — | Pending |
| GATE-04 | — | Pending |
| INTG-01 | — | Pending |
| INTG-02 | — | Pending |
| INTG-03 | — | Pending |
| INTG-04 | — | Pending |
| INTG-05 | — | Pending |

**Coverage:**
- v1.1 requirements: 16 total
- Mapped to phases: 0
- Unmapped: 16 (pending roadmap creation)

---
*Requirements defined: 2026-02-11*
*Last updated: 2026-02-11 after initial definition*
