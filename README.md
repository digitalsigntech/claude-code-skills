# claude-code-skills

A collection of shareable Claude Code skills and reusable components — chat gateways,
local-AI tooling, email/CRM automation, and infrastructure patterns. Each skill is
self-contained with its own `README.md` (what it does + how to install it); secrets,
credentials, populated databases, and personal data are never included — templates
(`*.example.*`) ship instead.

## Data privacy

Several of these skills share a deliberate design principle: **sensitive data is
processed locally, and only masked or non-sensitive text is ever sent to a cloud
LLM.** Three skills implement the two halves of that pattern:

- **Keep it on-box.** `grammar-local` proofreads text against a localhost model and
  never sends it to the cloud — its prompt-submit hook fails safe (blocks the
  submission if the local model is down rather than leaking the text). `local-doc-qa`
  ingests and answers over documents with a local backend, so document content never
  leaves the machine.
- **Mask before the cloud.** `reply-autodraft` has an optional privacy layer
  (`src/privacy.py`) that replaces customer PII with reversible `[[TYPE_N]]` tokens
  before any text reaches the cloud LLM, routes anything too sensitive to a local
  model instead, and enforces a pre-send tripwire that refuses to send if hard PII
  survived masking.
- **Route by privacy label.** `privacy-router` labels data public/private (default
  private, fail closed) and routes chat queries whose intent touches private data to
  a private tool-calling model with full conversation context — the cloud agent never
  sees the underlying records. `telegram-gateway` integrates it as a message gate
  with a `/cloud` escape hatch.

The masking is best-effort, not a guarantee — when detection is uncertain or the
tripwire fires, the message falls back to being drafted entirely on the local model.
See each skill's own `README.md` for configuration.

## Skills

### Interactive / chat
| Skill | What it does | Location |
|-------|--------------|----------|
| **telegram-gateway** | Chat with a headless Claude Code agent over Telegram — real Claude turn per message, one persistent conversation per chat/group, file ingest/analyze/hold, allowlist-locked. Long-polling (works behind NAT). | [`telegram-gateway/`](telegram-gateway/) |
| **realtime-voice** | Live spoken conversation from a phone browser: OpenAI gpt-realtime as mouth/ears (WebRTC direct, ~300 ms), Claude as the brain via tool calls — plus inline media search, an in-page camera Claude can see through, and voice-command chat clearing. No public IP needed (outbound tunnel). | [`realtime-voice/`](realtime-voice/) |
| **grammar-local** | On-box grammar/style fixer that never sends text to the cloud — slash command + CLI + a prompt-submit hook. Bring-your-own local model endpoint. | [`grammar-local/`](grammar-local/) |

### Local AI / knowledge
| Skill | What it does | Location |
|-------|--------------|----------|
| **local-doc-qa** | Private on-box document ingest + RAG Q&A (PDF/CSV/text → citable chunks), plus exact structured-table queries. Swappable embedding + chat endpoints. | [`local-doc-qa/`](local-doc-qa/) |
| **clip-media-search** | Neural image search over a media folder via CLIP — annotation-aware, with a warm localhost server. | [`clip-media-search/`](clip-media-search/) |
| **kb-semantic-index** | Unified semantic search over a knowledge base (`search`/`ask`) plus a grounded tier-1 quick-answer that answers from retrieved chunks or escalates. | [`kb-semantic-index/`](kb-semantic-index/) |
| **privacy-router** | Label data public/private and route private-intent chat queries to a private tool-calling LLM (CRM/email/KB lookup tools, full chat context) instead of the cloud agent. Fail-closed. | [`privacy-router/`](privacy-router/) |

### Email / CRM
| Skill | What it does | Location |
|-------|--------------|----------|
| **gmail-multi-mailbox** | Programmatic access to any number of Gmail mailboxes (profile/list/read/thread/send/draft) with OAuth setup. Drafts by default; sends only when told; per-account draft-only enforcement. | [`gmail-multi-mailbox/`](gmail-multi-mailbox/) |
| **email-idle-watcher** | Event-driven IMAP IDLE push — new mail seen in seconds, enqueued as JSON jobs; flock launcher + watchdog. | [`email-idle-watcher/`](email-idle-watcher/) |
| **email-knowledge-extract** | Cron pipeline that extracts durable facts + contacts from email into a KB/CRM using an LLM, with rolling per-contact summaries and citations. | [`email-knowledge-extract/`](email-knowledge-extract/) |
| **kb-refine-loop** | Self-improving KB: for every reply the owner sends to a customer question, a headless agent answers the question from the KB alone, diffs against the real reply, and patches gaps/conflicts until the KB converges. | [`kb-refine-loop/`](kb-refine-loop/) |
| **reply-autodraft** | Auto-drafts replies to inbound inquiries in the owner's voice, grounded in a KB index, learning from replies actually sent. Never sends (drafts only); optional PII-masking layer. | [`reply-autodraft/`](reply-autodraft/) |
| **crm-contacts** | Local SQLite contact + sent-mail archive (schema + markdown-per-contact templates) for context and dedup. Ships code/templates only, no data. | [`crm-contacts/`](crm-contacts/) |
| **followup-check** | Daily digest email of inbound inquiries still awaiting a reply, with LLM triage and cross-thread reply detection. | [`followup-check/`](followup-check/) |

### Automation / ops
| Skill | What it does | Location |
|-------|--------------|----------|
| **chat-archive** | Searchable SQLite/FTS5 log of chat messages with real-time multi-label project tagging and LLM query-expansion recall. | [`chat-archive/`](chat-archive/) |
| **weekly-reports** | Scheduled research/report generator → HTML/PDF, distributed by email + chat + cloud drive. Bring-your-own prompt files. | [`weekly-reports/`](weekly-reports/) |
| **drive-backup** | Daily project backup to Google Drive with retention (keep-N) and a documented exclusion list for secrets/state. | [`drive-backup/`](drive-backup/) |
| **health-check** | Scheduled self-maintenance routines — weekly: grooms agent memory, audits crons/processes/log bloat, pings only on judgment calls; plus a 10-min auth watchdog that alerts when the Claude Code OAuth session dies (concurrent-process refresh race). Fully config-driven. | [`health-check/`](health-check/) |

### Infrastructure patterns / docs
| Skill | What it does | Location |
|-------|--------------|----------|
| **boot-autostart** | Reliable service autostart pattern: flock single-instance launchers, DNS/network wait, `@reboot` + socket-aware watchdog crons. | [`boot-autostart/`](boot-autostart/) |
| **headless-chrome-scrape** | Read JavaScript-rendered pages a plain fetch can't — render the DOM with headless Chrome, then parse. Retry-with-growing-budget helper included. | [`headless-chrome-scrape/`](headless-chrome-scrape/) |
| **letterhead-doc** | Guide + blank template for pinning a logo to a page corner (ODF/OOXML) so letterhead doesn't drift across viewers. | [`letterhead-doc/`](letterhead-doc/) |

_Each skill folder has its own `README.md` with what it does and how to install it. Several email/report skills share the Google OAuth setup from **gmail-multi-mailbox**; local-AI skills expect a bring-your-own OpenAI-compatible model endpoint._
