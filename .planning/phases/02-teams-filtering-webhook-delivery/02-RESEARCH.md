# Phase 2: Teams Filtering & Webhook Delivery - Research

**Researched:** 2026-02-11
**Domain:** Teams notification filtering, JSON webhook delivery, config file loading (Python stdlib)
**Confidence:** HIGH

## Summary

Phase 2 transforms the Phase 1 notification engine from a log-everything daemon into a Teams-specific data pipeline. The work divides into three clear domains: (1) filtering raw notifications to only Teams messages using bundle ID matching, allowlist, and noise rejection rules; (2) constructing structured JSON payloads with all required fields and delivering them via HTTP POST; (3) loading runtime configuration from a JSON config file. All three domains use exclusively Python standard library modules -- no external dependencies are introduced.

The key architectural realization is that Phase 1 produced a single-file daemon (`nchook.py`) with an event loop that currently logs all notifications. Phase 2 does NOT build a separate wrapper script called via subprocess (the original nchook architecture). Instead, Phase 2 adds filtering and webhook delivery functions directly into nchook.py's event loop. The notification processing pipeline becomes: query DB -> parse plist -> filter (bundle ID, allowlist, noise) -> classify type -> build JSON -> POST webhook. This keeps the zero-dependency, single-file architecture intact.

The technical risk is LOW. Filtering is string matching and regex on known fields. Webhook delivery uses `urllib.request` (stdlib) with a 10-second timeout and broad exception handling. Config loading is `json.load()` on a file. The main complexity is in getting the Teams noise patterns right (reactions, calls, join/leave, system alerts) -- these patterns are documented in this research but will need real-world validation since the exact notification text may vary by Teams version and locale.

**Primary recommendation:** Add filtering, webhook, and config modules as functions in nchook.py. Wire them into the existing event loop between `query_new_notifications()` and `save_state()`. Use `urllib.request` for HTTP POST with `timeout=10` and catch-all exception handling for log-and-skip behavior.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `urllib.request` (stdlib) | Python 3.12+ | HTTP POST to webhook | Fire-and-forget JSON POST. No retry logic needed (log-and-skip design). No external dependency. `requests` would add the project's only external dep for no benefit. |
| `urllib.error` (stdlib) | Python 3.12+ | HTTP error handling | `URLError` and `HTTPError` for catching connection failures and HTTP error responses. |
| `json` (stdlib) | Python 3.12+ | Config file parsing + webhook payload serialization | Already used in Phase 1 for state file. Same module for config.json reading and webhook JSON body construction. |
| `re` (stdlib) | Python 3.12+ | Noise pattern matching | Regex for detecting reactions, calls, join/leave patterns in notification body text. Simple patterns, no advanced regex features needed. |
| `time` (stdlib) | Python 3.12+ | ISO 8601 timestamp formatting | Already imported in Phase 1. Used for formatting timestamps in webhook payloads. |
| `logging` (stdlib) | Python 3.12+ | Structured daemon logging | Already configured in Phase 1. Phase 2 adds log messages for filter decisions and webhook results. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `socket` (stdlib) | Python 3.12+ | Timeout exception type reference | `socket.timeout` is an alias for `TimeoutError` since Python 3.10, but imported for clarity in exception handling. |
| `os` (stdlib) | Python 3.12+ | Config file path resolution | Already imported. Used for resolving config file path relative to script location. |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `urllib.request` | `requests` library | `requests` has nicer API, session management, auto-retries. But adds the project's only external dependency for a simple fire-and-forget POST. Not worth it. |
| `re` for noise patterns | Simple `str.startswith()`/`in` checks | Regex gives more precise matching (e.g., word boundaries) but simple string checks may be sufficient. Start with simple string matching, upgrade to regex only if false positives occur. |
| JSON config | TOML (`tomllib` in 3.11+) | TOML has better syntax for humans. But JSON config is explicitly specified in requirements (CONF-01). Stick with JSON. |

**Installation:**
```bash
# No installation needed. Entire stack is Python standard library.
# Same as Phase 1: zero external dependencies.
```

## Architecture Patterns

### Current Project Structure (after Phase 2)
```
macos-notification-intercept/
├── nchook.py                  # Main daemon: ALL logic in one file
│                              # Phase 1: DB watching, plist parsing, state persistence
│                              # Phase 2: filtering, webhook delivery, config loading
├── config.json                # Runtime configuration (webhook URL, bundle IDs, etc.)
├── state.json                 # Persisted state (auto-generated, gitignored)
├── .gitignore                 # state.json, __pycache__
└── .planning/                 # Project planning
```

### Pattern 1: Pipeline Processing in Event Loop
**What:** Insert filtering and webhook delivery steps between the existing DB query and state save in `run_watcher()`.
**When to use:** This IS the Phase 2 integration pattern.
**Current event loop flow (Phase 1):**
```
query_new_notifications() -> log each notification -> save_state()
```
**Phase 2 event loop flow:**
```
query_new_notifications() -> filter_notification() -> classify_type() -> build_payload() -> post_webhook() -> save_state()
```
**Example:**
```python
# Source: Adaptation of existing nchook.py run_watcher() loop
for notif in notifications:
    # Phase 2: Filter
    if not passes_filter(notif, config):
        logging.debug("Filtered out: app=%s title=%s", notif["app"], notif["title"])
        continue

    # Phase 2: Classify and build payload
    msg_type = classify_notification(notif)
    payload = build_webhook_payload(notif, msg_type)

    # Phase 2: Deliver
    post_webhook(payload, config["webhook_url"], config.get("webhook_timeout", 10))
```

### Pattern 2: Layered Filtering (Bundle ID -> Allowlist -> Noise Reject)
**What:** Three-stage filter pipeline applied in order of cheapest-to-most-expensive check. Each stage is a separate function for testability.
**When to use:** For every notification before webhook delivery.
**Example:**
```python
# Source: Requirements FILT-01 through FILT-04

def passes_filter(notif, config):
    """Three-stage filter: bundle ID -> allowlist -> noise rejection."""
    # Stage 1: Bundle ID match (FILT-01) -- cheapest check
    if notif["app"] not in config.get("bundle_ids", set()):
        return False

    # Stage 2: Allowlist -- require sender and body (FILT-02)
    if not notif.get("title") or not notif.get("body"):
        return False

    # Stage 3: System alert rejection (FILT-03)
    if notif["title"] == "Microsoft Teams":
        return False

    # Stage 4: Noise pattern rejection (FILT-04)
    if is_noise_notification(notif["body"], notif["title"]):
        return False

    return True
```

### Pattern 3: Fire-and-Forget Webhook with Log-on-Failure
**What:** POST JSON to webhook URL. On ANY failure (timeout, HTTP error, connection refused, DNS failure), log the error and continue processing the next notification. Never crash, never hang, never retry.
**When to use:** For every notification that passes filtering.
**Example:**
```python
# Source: Python 3.12 urllib.request docs, requirements WEBH-01/03

import urllib.request
import urllib.error
import json

def post_webhook(payload, webhook_url, timeout=10):
    """POST JSON payload to webhook. Logs and skips on any failure."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            logging.info("Webhook delivered: %d %s", resp.status, resp.reason)
    except urllib.error.HTTPError as e:
        logging.error("Webhook HTTP error: %d %s", e.code, e.reason)
    except urllib.error.URLError as e:
        logging.error("Webhook URL error: %s", e.reason)
    except TimeoutError:
        logging.error("Webhook timeout after %ds", timeout)
    except Exception as e:
        logging.error("Webhook unexpected error: %s", e)
```

### Pattern 4: Config File Loading with Defaults
**What:** Read JSON config file at startup. Merge with sensible defaults. Validate required fields (webhook_url). Config is loaded once and passed to functions, not re-read per notification.
**When to use:** At daemon startup, before entering the event loop.
**Example:**
```python
# Source: Requirement CONF-01

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10,
}

def load_config(config_path=CONFIG_FILE):
    """Load JSON config with defaults. Exits if webhook_url missing."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(config_path, "r") as f:
            user_config = json.load(f)
        config.update(user_config)
    except FileNotFoundError:
        logging.error("Config file not found: %s", config_path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logging.error("Config file invalid JSON: %s", e)
        sys.exit(1)

    if "webhook_url" not in config or not config["webhook_url"]:
        logging.error("Config missing required field: webhook_url")
        sys.exit(1)

    # Convert bundle_ids to set for O(1) lookup
    config["bundle_ids"] = set(config["bundle_ids"])
    return config
```

### Anti-Patterns to Avoid
- **Re-reading config on every notification:** Config file I/O per notification wastes cycles. Load once at startup, pass config dict to functions.
- **Retrying failed webhooks:** Requirements explicitly say log-and-skip. Adding retry logic adds complexity, potential hangs, and violates the spec.
- **Using subprocess to call a wrapper script:** The original nchook architecture dispatched to a subprocess. Phase 1 already moved away from this. Keep filtering and webhook delivery in-process.
- **Blocking on slow webhooks:** Always set a timeout on `urlopen()`. Without timeout, a stalled server hangs the entire daemon forever.
- **Filtering after webhook delivery:** Filter BEFORE constructing the payload. Avoids wasted JSON serialization and HTTP overhead for noise notifications.
- **Hardcoding bundle IDs:** Must come from config for future-proofing (Microsoft changes bundle IDs periodically).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| HTTP POST with JSON | Raw socket HTTP request | `urllib.request.Request` + `urlopen()` | HTTP protocol complexity (chunked encoding, redirects, TLS). stdlib handles it all. |
| JSON serialization | Manual string formatting | `json.dumps()` | Escaping special characters (quotes, backslashes, unicode). One wrong escape corrupts the payload. |
| Timeout handling | Custom alarm/signal timer | `urlopen(req, timeout=N)` | `timeout` parameter handles both connection and read timeouts. No manual timer management. |
| Config file parsing | Custom key=value parser | `json.load()` | JSON parsing with proper string escaping, nested structures, type preservation. |
| URL validation | Regex URL validator | Rely on `urlopen()` raising `URLError` for invalid URLs | URL validation is deceptively complex. Let the HTTP library reject bad URLs at runtime. |

**Key insight:** Phase 2's complexity is in the filtering logic (domain knowledge about Teams notification patterns), not in the HTTP/JSON plumbing. Use stdlib for all plumbing; invest effort in getting filter rules right.

## Common Pitfalls

### Pitfall 1: Webhook Timeout Hangs the Daemon
**What goes wrong:** `urllib.request.urlopen()` without a timeout blocks indefinitely if the server accepts the connection but never responds. The daemon stops processing all notifications.
**Why it happens:** Developers test against localhost or fast servers. In production, the webhook endpoint may be slow, overloaded, or behind a flaky network.
**How to avoid:** ALWAYS pass `timeout=N` to `urlopen()`. The requirement specifies 10 seconds (WEBH-03). Make it configurable via config.json.
**Warning signs:** Daemon appears alive but stops logging new notifications. CPU at 0%. Stuck in a `urlopen()` call.

### Pitfall 2: Exception Hierarchy Confusion in urllib
**What goes wrong:** Code catches `HTTPError` but not `URLError`, or catches `URLError` but timeout slips through. Different failures raise different exception types.
**Why it happens:** `urllib.error` has a specific hierarchy: `HTTPError` is a subclass of `URLError`, which is a subclass of `OSError`. `TimeoutError` is separate (not under `URLError`). Developers catch one and miss the others.
**How to avoid:** Catch in this specific order:
1. `urllib.error.HTTPError` (most specific -- HTTP 4xx/5xx)
2. `urllib.error.URLError` (connection refused, DNS failure, etc.)
3. `TimeoutError` (socket timeout -- separate from URLError since Python 3.10+)
4. `Exception` (catch-all safety net)
**Warning signs:** Unhandled exception crashes the daemon when the webhook endpoint returns a 500 or is unreachable.

### Pitfall 3: Teams Noise Patterns are Locale-Dependent
**What goes wrong:** Filtering rules hardcoded in English ("Liked", "is calling you") don't match non-English Teams installations where these strings are localized.
**Why it happens:** Teams localizes notification text to the user's system language.
**How to avoid:** For v1, target English-language Teams installations (the user's setup). Document the locale assumption. For noise patterns, prefer structural heuristics (empty body, title == "Microsoft Teams") over text content matching where possible. Text-based noise patterns (reactions, calls) should be documented as English-only and configurable in future versions.
**Warning signs:** Non-English Teams users report that noise notifications are not being filtered.

### Pitfall 4: Truncation Detection False Positives
**What goes wrong:** Short messages that happen to be near 150 characters are flagged as truncated. Or messages ending with "..." (user-typed ellipsis) are flagged.
**Why it happens:** The truncation heuristic (body length >= ~150 chars + no sentence-ending punctuation) is approximate. macOS does not provide a "truncated" flag.
**How to avoid:** Use a conservative threshold. The macOS notification preview limit is approximately 150 characters, but the exact limit varies. Use `len(body) >= 150` AND check that body does NOT end with `.`, `!`, `?`, or `"`. Set `_truncated` as a likelihood flag, not a guarantee. Document this in the payload schema.
**Warning signs:** Downstream consumers treat `_truncated: true` as definitive and make wrong decisions for non-truncated messages.

### Pitfall 5: Config File Missing or Invalid Stops the Daemon
**What goes wrong:** User forgets to create config.json, or edits it with a syntax error. Daemon crashes on startup with an unhelpful traceback.
**Why it happens:** JSON syntax is unforgiving (trailing commas, single quotes, comments all cause parse errors).
**How to avoid:** On config load failure, print a clear error message with the specific problem and an example valid config. Provide sensible defaults for everything except `webhook_url` (which MUST be user-provided). Validate that webhook_url starts with `http://` or `https://`.
**Warning signs:** User reports "daemon won't start" after editing config.

### Pitfall 6: Bundle ID Mismatch After Teams Update
**What goes wrong:** Microsoft updates Teams and changes the bundle ID. The config has old bundle IDs. All Teams notifications silently pass through the bundle ID filter as non-matching and get dropped.
**Why it happens:** Microsoft has changed Teams bundle IDs before (classic `com.microsoft.teams` to new `com.microsoft.teams2`). They may do so again.
**How to avoid:** Make bundle IDs configurable (FILT-01 requires this). On startup, log which bundle IDs are being watched. Periodically (or at debug level), log notifications from ALL apps so the user can see if Teams notifications are arriving but being filtered by the wrong bundle ID.
**Warning signs:** Daemon runs, processes notifications, but webhook never fires. Log shows "Filtered out" for all notifications.

## Code Examples

Verified patterns from official sources:

### Complete Webhook POST with Full Error Handling
```python
# Source: Python 3.12 urllib.request docs + urllib.error docs
# Verified: URLError <- OSError, HTTPError <- URLError, TimeoutError (builtin, alias of socket.timeout since 3.10)

import urllib.request
import urllib.error
import json
import logging

def post_webhook(payload, webhook_url, timeout=10):
    """
    POST JSON payload to webhook URL.

    Logs and skips on any failure (WEBH-03: no hang, no crash).
    Returns True on success, False on failure.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            logging.info(
                "Webhook delivered: HTTP %d (%d bytes sent)", resp.status, len(data)
            )
            return True
    except urllib.error.HTTPError as e:
        logging.error(
            "Webhook HTTP error: %d %s (url=%s)", e.code, e.reason, webhook_url
        )
    except urllib.error.URLError as e:
        logging.error("Webhook connection error: %s (url=%s)", e.reason, webhook_url)
    except TimeoutError:
        logging.error(
            "Webhook timed out after %ds (url=%s)", timeout, webhook_url
        )
    except Exception as e:
        logging.error("Webhook unexpected error: %s (url=%s)", e, webhook_url)
    return False
```

### Complete Notification Filter Chain
```python
# Source: Requirements FILT-01 through FILT-04
# Confidence: HIGH for structure, MEDIUM for noise patterns (need real-world validation)

def passes_bundle_id_filter(notif, bundle_ids):
    """FILT-01: Only Teams bundle IDs pass."""
    return notif["app"] in bundle_ids


def passes_allowlist_filter(notif):
    """FILT-02: Require both sender (title) and body to be present and non-empty."""
    return bool(notif.get("title", "").strip()) and bool(notif.get("body", "").strip())


def is_system_alert(notif):
    """FILT-03: Reject notifications where title is 'Microsoft Teams'."""
    return notif.get("title", "").strip() == "Microsoft Teams"


# Known noise patterns in Teams notification body text (English locale)
# These are notification types that are NOT real chat messages.
NOISE_PATTERNS = [
    # Reactions: "Liked", "Loved", "Laughed at", "Was surprised by", "Was sad at"
    # These appear as the body when someone reacts to a message
    "Liked",
    "Loved",
    "Laughed at",
    "Was surprised by",
    "Was sad at",
    # Call notifications
    "is calling you",
    "Missed call from",
    "Incoming call",
    # Meeting notifications
    "joined the meeting",
    "left the meeting",
    "Meeting started",
    "is presenting",
    # Join/leave events
    "has been added",
    "has left",
    "has joined",
    # Typing indicators (if they surface as notifications)
    "is typing",
]


def is_noise_notification(body, title):
    """FILT-04: Reject known noise patterns."""
    body_stripped = body.strip()
    for pattern in NOISE_PATTERNS:
        if body_stripped.startswith(pattern) or body_stripped == pattern:
            return True
    return False


def passes_filter(notif, config):
    """Complete filter chain: bundle ID -> allowlist -> system alert -> noise."""
    if not passes_bundle_id_filter(notif, config["bundle_ids"]):
        return False
    if not passes_allowlist_filter(notif):
        return False
    if is_system_alert(notif):
        return False
    if is_noise_notification(notif.get("body", ""), notif.get("title", "")):
        return False
    return True
```

### Notification Type Classification
```python
# Source: Requirement FILT-05
# Confidence: MEDIUM -- Teams notification subtitle/title patterns need real-world validation

def classify_notification(notif):
    """
    FILT-05: Classify notification type based on subtitle/title patterns.

    Returns one of: "direct_message", "channel_message", "mention"

    Classification heuristic (English locale):
    - If subtitle is empty or absent: likely a direct_message (1:1 chat shows no subt,
      or subt is the chat name which equals a person's name)
    - If body or subtitle contains "@" mention pattern: "mention"
    - If subtitle contains a channel-like pattern (e.g., "General", "Team Name | Channel"):
      "channel_message"
    - Default: "direct_message"

    NOTE: These patterns need real-world validation. The classification is a
    best-effort heuristic, not a guaranteed categorization.
    """
    subtitle = notif.get("subtitle", "").strip()
    body = notif.get("body", "")

    # Check for @mention patterns in body
    # Teams mentions appear as text in the body, not as special formatting
    if "@" in body:
        return "mention"

    # If there's a subtitle with a separator pattern, likely a channel message
    # Teams channel notifications often have subtitle like "Channel Name" or "Team > Channel"
    if subtitle and ("|" in subtitle or ">" in subtitle):
        return "channel_message"

    # If subtitle is present and differs from title, likely a group/channel context
    title = notif.get("title", "").strip()
    if subtitle and subtitle != title:
        return "channel_message"

    # Default: direct message (1:1 chat or unclassifiable)
    return "direct_message"
```

### Truncation Detection
```python
# Source: Requirement WEBH-04
# Confidence: MEDIUM -- 150-char threshold is approximate; macOS truncation varies

# Sentence-ending punctuation that suggests the message is complete
SENTENCE_ENDINGS = frozenset(".!?\"')")

def detect_truncation(body):
    """
    WEBH-04: Detect likely truncated messages.

    Heuristic: body is >= 150 characters AND does not end with sentence-ending
    punctuation. macOS notification preview truncates long messages at approximately
    150 characters without adding an ellipsis.

    Returns True if likely truncated, False otherwise.
    """
    if len(body) < 150:
        return False
    # If it ends with sentence-ending punctuation, probably not truncated
    if body and body[-1] in SENTENCE_ENDINGS:
        return False
    return True
```

### Complete Webhook Payload Construction
```python
# Source: Requirement WEBH-02
# Verified field list: senderId, senderName, chatId, content, timestamp, _source, _truncated

import time

def build_webhook_payload(notif, msg_type):
    """
    WEBH-02: Build structured JSON payload from filtered notification.

    DBWT-06: Includes subtitle field in payload.
    WEBH-04: Includes truncation detection flag.
    """
    # Format timestamp as ISO 8601
    ts = notif.get("timestamp", 0)
    if ts > 0:
        ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    else:
        ts_str = None

    return {
        "senderName": notif.get("title", ""),
        "chatId": notif.get("subtitle", ""),
        "content": notif.get("body", ""),
        "timestamp": ts_str,
        "type": msg_type,
        "subtitle": notif.get("subtitle", ""),  # DBWT-06: explicit subtitle pass-through
        "_source": "macos-notification-center",
        "_truncated": detect_truncation(notif.get("body", "")),
    }
```

### Config File Example
```json
{
    "webhook_url": "https://example.com/webhook",
    "bundle_ids": ["com.microsoft.teams2", "com.microsoft.teams"],
    "poll_interval": 5.0,
    "log_level": "INFO",
    "webhook_timeout": 10
}
```

## Teams Notification Pattern Reference

This section documents known Teams macOS notification patterns based on research and domain knowledge. **These patterns need real-world validation** -- the exact text may vary by Teams version, locale, and notification settings.

### Real Message Notifications (PASS filter)

| Type | title (titl) | subtitle (subt) | body | Classification |
|------|-------------|-----------------|------|----------------|
| Direct message | "John Smith" | "" or "John Smith" | "Hey, can you review..." | direct_message |
| Channel message | "John Smith" | "General" or "Team > General" | "The meeting is at 3pm" | channel_message |
| Group chat | "John Smith" | "Project Alpha Chat" | "Let's sync up" | direct_message or channel_message |
| Mention | "John Smith" | "General" | "@You check this out" | mention |

### Noise Notifications (REJECT by filter)

| Type | title (titl) | body | Why Rejected |
|------|-------------|------|-------------|
| System alert | "Microsoft Teams" | "You have new activity" | FILT-03: title is "Microsoft Teams" |
| Reaction | "John Smith" | "Liked" or "Loved" or "Laughed at..." | FILT-04: noise pattern |
| Call | "John Smith" | "is calling you" | FILT-04: noise pattern |
| Missed call | "Microsoft Teams" | "Missed call from John" | FILT-03 + FILT-04 |
| Join event | "Microsoft Teams" | "John has joined the meeting" | FILT-03 + FILT-04 |
| Empty body | "John Smith" | "" | FILT-02: body empty |

**Confidence: MEDIUM** -- These patterns are reconstructed from Teams notification behavior knowledge. The exact text strings need to be validated by running the Phase 1 daemon with DEBUG logging and observing real Teams notifications.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Separate wrapper script via subprocess | In-process filtering + webhook in nchook.py | Phase 1 implementation decision | No subprocess overhead. Single-file architecture. Config loaded once, not per-notification. |
| `socket.timeout` exception type | `TimeoutError` (builtin) | Python 3.10 | `socket.timeout` became alias of `TimeoutError`. Catch `TimeoutError` directly. |
| `requests` library for HTTP POST | `urllib.request` (stdlib) | Project constraint | Zero external dependencies. `urllib.request.urlopen(req, timeout=N)` is sufficient for fire-and-forget POST. |
| Hardcoded bundle IDs | Configurable via config.json | Requirements (FILT-01, CONF-01) | Future-proofs against Microsoft changing bundle IDs. |

**Deprecated/outdated:**
- Subprocess wrapper dispatch: The original nchook architecture called a handler script via `subprocess.call()`. This project's nchook.py handles everything in-process.
- `socket.timeout` as separate exception: Since Python 3.10, `socket.timeout` is `TimeoutError`. Catching `TimeoutError` is sufficient.

## Integration with Phase 1

### Functions to Add to nchook.py
```
Existing (Phase 1):
  detect_db_path()
  validate_environment()
  parse_notification()
  query_new_notifications()
  save_state() / load_state()
  check_db_consistency()
  print_startup_summary()
  create_wal_watcher()
  run_watcher()
  main()

New (Phase 2):
  load_config()                  # CONF-01: Read config.json
  passes_filter()                # FILT-01..04: Complete filter chain
  passes_bundle_id_filter()      # FILT-01: Bundle ID check
  passes_allowlist_filter()      # FILT-02: Sender + body required
  is_system_alert()              # FILT-03: Title == "Microsoft Teams"
  is_noise_notification()        # FILT-04: Known noise patterns
  classify_notification()        # FILT-05: Message type classification
  detect_truncation()            # WEBH-04: Truncation heuristic
  build_webhook_payload()        # WEBH-02 + DBWT-06: JSON payload construction
  post_webhook()                 # WEBH-01 + WEBH-03: HTTP POST with timeout
```

### Modifications to Existing Functions
- **`run_watcher()`:** Must accept config dict. Replace logging-only notification processing with filter -> classify -> build -> post pipeline.
- **`main()`:** Must call `load_config()` before `run_watcher()`. Pass config to `run_watcher()`. Update startup summary to include webhook URL and bundle IDs.
- **`print_startup_summary()`:** Add webhook URL and bundle IDs to the output banner.
- **`POLL_FALLBACK_SECONDS`:** Should be read from `config["poll_interval"]` instead of being a module-level constant.

### Event Loop Modification
```python
# In run_watcher(), replace the current notification processing block:

# BEFORE (Phase 1):
for notif in notifications:
    logging.info("Notification | app=%s | ...", notif["app"], ...)

# AFTER (Phase 2):
for notif in notifications:
    if not passes_filter(notif, config):
        logging.debug("Filtered: app=%s title=%s", notif["app"], notif["title"])
        continue
    msg_type = classify_notification(notif)
    payload = build_webhook_payload(notif, msg_type)
    post_webhook(payload, config["webhook_url"], config.get("webhook_timeout", 10))
```

## Open Questions

1. **Exact Teams noise notification text patterns**
   - What we know: Teams generates notifications for reactions, calls, join/leave, system alerts. The general patterns are documented above.
   - What's unclear: The EXACT strings used in current Teams versions on macOS Sequoia/Tahoe. Strings may be localized.
   - Recommendation: Run Phase 1 daemon with DEBUG logging for 1-2 hours of active Teams usage. Capture all notification fields. Use this real data to refine noise patterns. Ship with the documented patterns and iterate.

2. **Truncation threshold precision**
   - What we know: macOS notification previews are reported to truncate at approximately 150 characters. The exact limit may vary by notification style (banner vs alert) and macOS version.
   - What's unclear: Whether the limit is exactly 150, or varies. Whether macOS adds "..." or simply cuts off.
   - Recommendation: Use 150 as the threshold. The heuristic (length >= 150 AND no sentence-ending punctuation) is conservative enough to avoid most false positives. Document it as approximate.

3. **WEBH-02 field "senderId" -- what value to use?**
   - What we know: The requirement lists "senderId" as a payload field. macOS notifications only provide display names (title = sender name), not Azure AD user IDs or email addresses.
   - What's unclear: What value should senderId contain? The display name? A hash? An empty string?
   - Recommendation: Omit `senderId` or set it to an empty string. The `senderName` field carries the display name. Document that Azure AD user IDs are not available through notification interception. This is a known limitation per PROJECT.md ("Display names only -- no Azure AD user IDs or email addresses").

4. **Config file path resolution**
   - What we know: CONF-01 says "JSON config file from project directory." The state file currently uses a relative path ("state.json" in CWD).
   - What's unclear: Should config.json be resolved relative to the script location or relative to CWD?
   - Recommendation: Resolve relative to the script file's directory (`os.path.dirname(os.path.abspath(__file__))`). This ensures the daemon finds config.json regardless of which directory it's launched from.

## Sources

### Primary (HIGH confidence)
- **Python 3.12 urllib.request docs** - https://docs.python.org/3/library/urllib.request.html - Verified Request class signature, urlopen() timeout parameter, return type.
- **Python 3.12 urllib.error docs** - https://docs.python.org/3/library/urllib.error.html - Verified exception hierarchy: HTTPError <- URLError <- OSError. TimeoutError is separate.
- **Python CPython issue #86579** - https://github.com/python/cpython/issues/86579 - Verified socket.timeout became alias of TimeoutError in Python 3.10.
- **Phase 1 nchook.py source** - Local file, read directly. Verified current event loop structure, function signatures, import list.
- **Phase 1 RESEARCH.md** - Local file. Verified plist structure, DB schema, stdlib patterns.

### Secondary (MEDIUM confidence)
- **Microsoft Teams notification docs** - https://support.microsoft.com/en-us/office/first-things-to-know-about-notifications-in-microsoft-teams - Verified notification categories: @mentions, direct messages, channel activity, calls.
- **macOS Sequoia notification DB** - https://forum.latenightsw.com/t/parsing-notifications-in-macos-sequoia/5001 - Confirmed DB location at Group Containers path. Confirmed binary plist storage in record table.

### Tertiary (LOW confidence -- needs real-world validation)
- **Teams noise notification patterns** - Reconstructed from domain knowledge. The exact text strings for reactions ("Liked", "Loved"), calls ("is calling you"), and join/leave events need validation against real notifications captured by the Phase 1 daemon.
- **Notification type classification heuristics** - The subtitle patterns for distinguishing direct_message vs channel_message vs mention are approximate. Need real Teams notification data to validate.
- **Truncation threshold (150 chars)** - Widely reported but not officially documented by Apple. The exact cutoff may vary.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All stdlib, same as Phase 1. urllib.request API verified from official docs.
- Architecture: HIGH - Direct extension of Phase 1's nchook.py. Integration points are clear.
- Filtering logic: MEDIUM - Filter structure is solid (layered pipeline). Specific noise patterns need real-world validation.
- Webhook delivery: HIGH - urllib.request POST pattern is standard and well-documented. Exception hierarchy verified.
- Pitfalls: HIGH - Timeout handling, exception hierarchy, and config validation are well-understood problems with documented solutions.

**Research date:** 2026-02-11
**Valid until:** 60 days (stable domain -- stdlib APIs frozen, Teams notification patterns unlikely to change dramatically)
