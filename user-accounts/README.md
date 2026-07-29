# User accounts

Registry of known people + per-user privileges, keyed by Telegram user id.
Enforced by injecting a briefing line into every Claude turn (telegram/bridge.py
`_account_line`). the owner's spec, 2026-07-29.

- **Privileges:** `private_info` (may see customers/finances/credentials/internal
  docs), `write_code` (may request code changes and skill installs).
- **Fields:** name, telegram id (key), position (optional).
- **Unknown allowlisted senders = guests:** hard denial line, referred to the owner.
- **Owner (configure your own id) is the baseline** — no line injected.
- Manage: `python3 accounts.py add|set|rm|list|get` — changes only on the owners'
  request (the owner's DM or this box), same policy as the email whitelist.
- The gateway allowlist still decides who can talk AT ALL; accounts decide what
  a talker may get. Hard walls (personal-notes gate, masking, no-send) are
  independent of this and unchanged.
