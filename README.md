# nchook-msteams

A macOS daemon that watches the Sequoia notification center database for Microsoft Teams messages and forwards them as JSON to a webhook. Detects user status (Away, Available, etc.) to only forward notifications you'd miss.

## Usage

1. Edit `config.json` with your webhook URL
2. Run: `python3 nchook.py` (or `python3 nchook.py --dry-run` to test)

Requires Full Disk Access for your terminal. Optionally grant Accessibility access for direct Teams status reading.

## Attribution

Based on [nchook](https://github.com/Who23/nchook) by [Who23](https://github.com/Who23).
