# Roadmap: Teams Notification Interceptor

## Milestones

- ✅ **v1.0 MVP** — Phases 1-3 (shipped 2026-02-11)
- 🚧 **v1.1 Teams Status Integration** — Phases 4-6 (in progress)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-3) — SHIPPED 2026-02-11</summary>

- [x] Phase 1: DB Watcher & State Engine (2/2 plans) — completed 2026-02-11
- [x] Phase 2: Teams Filtering & Webhook Delivery (2/2 plans) — completed 2026-02-11
- [x] Phase 3: Operational Hardening (1/1 plan) — completed 2026-02-11

</details>

### 🚧 v1.1 Teams Status Integration (In Progress)

**Milestone Goal:** Only forward Teams notifications when the user is Away or Busy -- filter out notifications the user would see directly in Teams.

- [x] **Phase 4: Status Detection Core** — ioreg idle time, pgrep process check, fallback chain orchestrator — completed 2026-02-11
- [ ] **Phase 5: Config, Gating, and Event Loop Integration** — status gate wired into existing daemon with config toggle and payload metadata
- [ ] **Phase 6: AX Discovery and Permission Handling** — AppleScript status reading, AX normalization, permission probing, graceful degradation

## Phase Details

### Phase 4: Status Detection Core
**Goal**: Daemon can detect user presence via system idle time and Teams process state, orchestrated through a fallback chain that produces a canonical status result
**Depends on**: Phase 3 (v1.0 shipped)
**Requirements**: STAT-01, STAT-02, STAT-05, STAT-06, STAT-07
**Success Criteria** (what must be TRUE):
  1. Running `detect_user_status()` when user is idle for 5+ minutes returns status=Away, source=idle, confidence=medium
  2. Running `detect_user_status()` when Teams is not running returns status=Offline, source=process, confidence=high
  3. Running `detect_user_status()` when user is active and Teams is running returns status=Available, source=idle, confidence=medium
  4. Every status result dict contains detected_status, status_source, and status_confidence fields
  5. A subprocess timeout on any signal does not crash the daemon -- the chain falls through to the next signal
**Plans**: 1 plan

Plans:
- [x] 04-01-PLAN.md -- Signal functions (idle, process, AX stub) and fallback chain orchestrator

### Phase 5: Config, Gating, and Event Loop Integration
**Goal**: Daemon uses detected status to gate notification forwarding -- forwarding on Away/Busy, suppressing on Available/Offline -- with config toggle, payload metadata, and correct rec_id advancement
**Depends on**: Phase 4
**Requirements**: GATE-01, GATE-02, GATE-03, GATE-04, INTG-01, INTG-02, INTG-03
**Success Criteria** (what must be TRUE):
  1. When status is Away or Busy, notifications are forwarded to the webhook with `_detected_status`, `_status_source`, and `_status_confidence` in the JSON payload
  2. When status is Available, notifications are suppressed but the rec_id high-water mark still advances (no stale replay on next status transition)
  3. When status is Unknown (all signals failed), notifications are forwarded (fail-open policy)
  4. Setting `status_enabled: false` in config.json disables all gating -- every notification forwards as in v1.0
  5. Startup summary displays whether status detection is enabled and the current detected status
**Plans**: TBD

Plans:
- [ ] 05-01: TBD
- [ ] 05-02: TBD

### Phase 6: AX Discovery and Permission Handling
**Goal**: Daemon can read Teams status text directly from the Accessibility tree when permission is granted, and degrades gracefully to idle+process fallback when it is not
**Depends on**: Phase 5
**Requirements**: STAT-03, STAT-04, INTG-04, INTG-05
**Success Criteria** (what must be TRUE):
  1. With Accessibility permission granted and Teams running, `detect_user_status()` returns the actual Teams status (e.g., Busy, Away, DoNotDisturb) with source=ax and confidence=high
  2. Raw AX status text (e.g., "Be Right Back", "Do not disturb") is normalized to canonical values (BeRightBack, DoNotDisturb, etc.)
  3. Without Accessibility permission, startup logs actionable instructions for granting permission to the correct terminal application
  4. When AX signal fails or permission is denied, the daemon silently falls back to idle+process detection without user intervention
**Plans**: TBD

Plans:
- [ ] 06-01: TBD

## Progress

**Execution Order:** 4 → 5 → 6

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. DB Watcher & State Engine | v1.0 | 2/2 | Complete | 2026-02-11 |
| 2. Teams Filtering & Webhook Delivery | v1.0 | 2/2 | Complete | 2026-02-11 |
| 3. Operational Hardening | v1.0 | 1/1 | Complete | 2026-02-11 |
| 4. Status Detection Core | v1.1 | 1/1 | Complete | 2026-02-11 |
| 5. Config, Gating, and Event Loop Integration | v1.1 | 0/? | Not started | - |
| 6. AX Discovery and Permission Handling | v1.1 | 0/? | Not started | - |
