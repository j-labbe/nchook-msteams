# Feature Landscape

**Domain:** macOS notification interception and webhook forwarding (Teams-specific)
**Researched:** 2026-02-11
**Confidence:** MEDIUM (based on training data + detailed project context; no live verification available)

## Table Stakes

Features users expect. Missing = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Teams bundle ID filtering** | The entire point is Teams-only notifications. Without filtering, downstream consumers drown in system noise (Slack, Mail, Calendar). Must match both `com.microsoft.teams2` (new Teams) and `com.microsoft.teams` (classic). | Low | Simple string match against the `app` column in the notification DB. |
| **Sender + message extraction** | A webhook payload without structured sender/message fields is useless JSON. Downstream agents need to know WHO said WHAT. Map `title` -> sender, `body` -> message content. | Low | Direct column reads from the notification DB record table. |
| **Chat/channel name extraction (subt)** | Without this, downstream can't distinguish "same sender in DM vs channel" -- a critical routing distinction. This is the `subt` (subtitle) field that nchook doesn't currently pass through. | Low-Med | Requires patching nchook to read the `subt` column and pass it as a 5th argument. The column exists in the DB; the gap is nchook's extraction logic. |
| **Allowlist filtering (sender + body present)** | Teams generates many non-message notifications: reactions, call events, "X is typing", join/leave, system alerts from "Microsoft Teams" itself. Without allowlisting, webhook gets 30-50% noise. The allowlist pattern (require both sender name and body content) is the simplest reliable heuristic. | Low | Check that title is not empty, not "Microsoft Teams" literally, and body is not empty/null. |
| **Webhook POST delivery** | The output mechanism. No webhook = no bridge. Must POST JSON to a configurable URL with appropriate Content-Type headers. | Low | Standard `urllib.request` or `requests` POST. No auth complexity since the user controls the webhook endpoint. |
| **JSON config file** | Hardcoded webhook URLs and settings make the tool unusable for anyone but the author. Config file is the minimum for portability. | Low | Read a JSON file on startup for webhook URL, poll interval, paths. |
| **State persistence (processed rec_ids)** | Without this, every restart replays ALL historical notifications. On a busy Teams account, that's hundreds of duplicate webhooks on each restart. This is the difference between "works" and "works reliably." | Med | Write processed `rec_id` values to a state file. On startup, load them and skip already-processed records. Must handle the file not existing yet (first run). |
| **macOS Sequoia DB path support** | macOS Sequoia (15+) moved the notification center DB from the pre-Sequoia path to `~/Library/Group Containers/group.com.apple.usernoted/db2/db`. Without this, the tool simply does not work on the target OS. | Low | Path detection/selection. Original nchook only knows the old path. |
| **Structured JSON payload** | The webhook body must be parseable JSON, not raw text. Downstream agents need machine-readable fields: `sender`, `channel`, `message`, `timestamp`, `app_bundle_id`. | Low | `json.dumps()` with a well-defined schema. |
| **Timestamp extraction** | Messages without timestamps are unorderable. Downstream agents need to know WHEN a message arrived, both for display and for detecting staleness. | Low | The notification DB stores delivery timestamps. Pass through to the JSON payload. |

## Differentiators

Features that set product apart. Not expected in a minimal tool, but add significant value.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Truncation detection flag** | macOS notification previews are truncated at ~150 characters. Flagging `"truncated": true` in the JSON payload lets downstream agents know the message is incomplete and should be treated differently (e.g., prompt the user to check Teams directly). This is unique to the DB-interception approach and most tools ignore it. | Low | Heuristic: if body length is >= ~140-150 chars and doesn't end with sentence-ending punctuation, flag as likely truncated. Not perfect, but useful signal. |
| **Deduplication beyond rec_id** | The notification DB can occasionally surface the same logical notification with different `rec_id` values (e.g., after a DB compaction or Teams re-posting). Content-hash-based dedup (hash of sender+channel+body+timestamp) catches these edge cases. | Med | Maintain a secondary dedup cache alongside rec_id tracking. Adds state complexity. Consider as a v2 feature if duplicates become a real problem. |
| **Notification type classification** | Beyond allowlist filtering, classify notifications into types: `direct_message`, `channel_message`, `mention`, `reply`. This requires pattern matching on the `subt` field format (DMs have no channel prefix, mentions include "@You" patterns, replies include "replied to" patterns). | Med | Pattern matching against known Teams notification text formats. Fragile to Teams UI string changes but high-value for downstream routing. |
| **Health/heartbeat endpoint or signal** | Downstream systems need to distinguish "no Teams messages in the last hour" from "the interceptor crashed." A periodic heartbeat (even just writing a timestamp to a file or POSTing a heartbeat JSON) solves this. | Low | Periodic POST with `"type": "heartbeat"` or a local file touch. Simple but solves a real operational concern. |
| **Graceful shutdown with state flush** | On SIGINT/SIGTERM, flush current state to disk before exiting. Prevents the "processed 50 notifications but state file only has 45" gap that causes duplicates on restart. | Low | Signal handler that writes state and exits cleanly. |
| **Config reload without restart** | SIGHUP-triggered config reload lets you change the webhook URL or add filters without restarting (and without losing in-memory state). | Low-Med | Signal handler that re-reads config. Must be careful about partial state. |
| **Dry-run / verbose mode** | Print what WOULD be sent without actually POSTing. Essential for debugging filters and verifying the tool sees the right notifications during setup. | Low | CLI flag that logs payloads to stdout instead of (or in addition to) POSTing. |
| **Startup replay window** | On startup, optionally re-process notifications from the last N minutes (instead of only tracking new ones). Useful if the tool was down briefly and you don't want to miss messages from the gap. Bounded by a configurable time window to avoid replaying the entire history. | Med | Query notifications with delivery_date > (now - replay_window), excluding already-processed rec_ids. |

## Anti-Features

Features to deliberately NOT build. Each one has been considered and rejected for good reason.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Microsoft Graph API integration** | The entire value proposition of this tool is avoiding Graph API complexity (OAuth flows, app registrations, tenant admin consent, token refresh, rate limits). Adding Graph API defeats the purpose. | Stick with DB interception. Accept the limitations (truncation, display names only) as acceptable tradeoffs. |
| **Retry queue for failed webhooks** | Retry queues add state management complexity (persistent queue, backoff logic, dead letter handling, ordering guarantees). For a foreground process that logs failures, the operator can see failures and the next notification will succeed if the webhook is back up. | Log failures with full context (payload, HTTP status, error). Let the operator monitor logs. If reliability becomes critical, that's a signal to use a proper message queue system, not to bolt one onto this tool. |
| **Reply / response capabilities** | Writing back to Teams requires Graph API or Accessibility API -- both are explicitly out of scope. Adding write capability changes the security posture entirely (read-only interception vs. impersonation risk). | Stay read-only. Downstream agents that need to reply should use their own Graph API integration. |
| **GUI / menu bar app** | GUI adds an entire dependency surface (PyObjC, Cocoa bindings, or Electron) for a tool that should be a quiet background process. The target user is a developer running it in a terminal. | CLI with good logging. If GUI is ever needed, build it as a separate process that reads the same state file. |
| **launchd service management** | Bundling launchd plist generation/installation adds OS integration complexity and makes debugging harder (launchd log redirection, restart policies, user vs. system agent). The tool should work first as a manual foreground process. | Document how to create a launchd plist manually if users want it. Keep the tool itself launchd-agnostic. |
| **Accessibility API usage** | The Accessibility API approach (hooking into notification center UI) is fragile across macOS versions, requires elevated permissions (Screen Recording or Accessibility), and is slower than DB polling. | DB watching via kqueue on the WAL file is more reliable and lower-privilege. |
| **AI/ML message classification** | Embedding AI logic (sentiment analysis, priority scoring, intent detection) couples the interception layer to a specific downstream use case. Different consumers want different classification. | Forward raw structured data. Let downstream agents apply their own intelligence. The interceptor's job is reliable capture, not interpretation. |
| **Edit/delete detection** | macOS notifications don't surface message edits or deletions. The notification DB only contains the original delivery. Attempting to detect edits would require polling Teams directly (Graph API) or screen scraping. | Document this limitation clearly. Downstream consumers should treat intercepted messages as point-in-time snapshots. |
| **Multi-account support** | Supporting multiple macOS user sessions or multiple Teams accounts on one machine adds session detection complexity for minimal real-world benefit (most users have one Teams account per machine). | Target single-user, single-Teams-account. Document the limitation. |
| **Webhook authentication (OAuth/HMAC)** | Adding outbound webhook auth (signing payloads, OAuth bearer tokens) is premature complexity. The webhook receiver is controlled by the same user/org. If auth is needed, the receiver can validate by IP or the user can put an auth proxy in front. | Support a simple static `Authorization` header in config if anything. Do not build an auth framework. |

## Feature Dependencies

```
macOS Sequoia DB path support --> All other features (nothing works without the right DB)
Teams bundle ID filtering --> Allowlist filtering (filter by app first, then by content)
Sender + message extraction --> Structured JSON payload (extraction feeds the payload)
Chat/channel name extraction (subt) --> Structured JSON payload (subt is a payload field)
Chat/channel name extraction (subt) --> Notification type classification (needs subt patterns)
Timestamp extraction --> Structured JSON payload (timestamp is a payload field)
Structured JSON payload --> Webhook POST delivery (payload is what gets POSTed)
Allowlist filtering --> Webhook POST delivery (only allowlisted messages get POSTed)
JSON config file --> Webhook POST delivery (webhook URL comes from config)
JSON config file --> State persistence (state file path comes from config or convention)
State persistence --> Startup replay window (replay uses state to avoid re-sending)
Truncation detection --> Structured JSON payload (truncation flag is a payload field)
```

Dependency ordering (build in this order):

```
1. DB path detection (Sequoia support)
2. DB reading + notification extraction (sender, message, subt, timestamp)
3. Bundle ID + allowlist filtering
4. JSON config loading
5. Structured JSON payload construction
6. Webhook POST delivery
7. State persistence (rec_id tracking)
8. Truncation detection flag
9. [Differentiators] Dry-run mode, graceful shutdown, heartbeat, etc.
```

## MVP Recommendation

Prioritize (all table stakes, in dependency order):

1. **macOS Sequoia DB path support** -- nothing works without it
2. **Notification extraction** (sender, channel, message, timestamp) -- the core data
3. **Teams bundle ID filtering + allowlist filtering** -- noise reduction
4. **JSON config file** -- portability
5. **Structured JSON payload + webhook POST** -- the output mechanism
6. **State persistence** -- restart reliability
7. **Truncation detection** -- low-cost, high-signal addition

Defer to post-MVP:
- **Notification type classification**: Needs real-world data on Teams notification text patterns before building reliable regex. Ship MVP, collect sample notifications, then build classification.
- **Deduplication beyond rec_id**: Only build if duplicate notifications are observed in practice. Don't solve a theoretical problem.
- **Startup replay window**: Useful but requires careful testing around time zone handling and DB timestamp formats. Add after state persistence is proven solid.
- **Config reload without restart**: Nice-to-have. The tool restarts in under a second, so SIGHUP reload is a convenience, not a necessity.
- **Health/heartbeat**: Add once the tool is running in production and monitoring becomes a real need.

Build immediately but as polish, not blockers:
- **Dry-run mode**: Trivial to implement (CLI flag + print instead of POST) and saves hours of debugging during initial setup. Include in MVP.
- **Graceful shutdown**: A 5-line signal handler. Include in MVP.

## Confidence Notes

| Feature Category | Confidence | Rationale |
|-----------------|------------|-----------|
| Table stakes list | HIGH | Directly derived from PROJECT.md requirements + well-understood domain |
| Allowlist filtering heuristics | MEDIUM | The "sender + body present" heuristic is sound, but exact noise patterns from Teams on Sequoia may vary. May need tuning after first run. |
| Truncation detection threshold | MEDIUM | ~150 char limit is widely reported but may vary by Teams version or notification type. The heuristic approach (length + punctuation check) is pragmatic but imperfect. |
| Notification type classification patterns | LOW | Teams notification text formats (how DMs vs channels vs mentions appear in subt/title/body) need real-world sample data to validate. Training data may be stale. |
| Anti-features list | HIGH | Directly aligned with explicit out-of-scope items in PROJECT.md |
| Feature dependencies | HIGH | Logical ordering based on data flow (extract -> filter -> format -> deliver -> persist) |

## Sources

- Project context: `~/Projects/macos-notification-intercept/.planning/PROJECT.md`
- nchook architecture: [github.com/who23/nchook](https://github.com/who23/nchook) (referenced in PROJECT.md; not fetched due to tool restrictions)
- macOS notification center DB structure: Training data knowledge (MEDIUM confidence -- macOS internals are well-documented in training data but Sequoia-specific changes may exist)
- Teams notification patterns: Training data knowledge (MEDIUM confidence -- Teams notification format may have changed post-training-cutoff)
