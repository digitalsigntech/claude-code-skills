"""LLM extraction: contacts, product/operational knowledge, activity line, and
per-contact CRM summaries.

Backend is swappable. The primary path (`_chat_primary`) calls any OpenAI-compatible
/chat/completions endpoint — a hosted router, or a local llama.cpp / vLLM / Ollama
server. An optional secondary path (`_chat_fallback`) shells out to a CLI-based LLM if
the primary is down, so extraction never silently fails. Neither an endpoint nor a key
is hardcoded: both come from the environment (see README).
"""
import os, sys, time, json, re, subprocess, requests
from kbconf import BODY_CHARS
from conv_clean import clean_conversation

# --- Primary model: an OpenAI-compatible chat endpoint ----------------------
# Structured extraction from an explicit prompt does not need reasoning/chain-of-thought,
# so it is off by default (cheaper + faster); it can be turned on per-call.
PRIMARY_URL   = os.environ.get("EKB_LLM_URL", "https://your-endpoint.example/v1/chat/completions")
PRIMARY_MODEL = os.environ.get("EKB_MODEL", "MODEL_ID_HERE")
# The per-contact summary can use a different (e.g. larger-context) model; defaults to the same.
SUM_MODEL     = os.environ.get("EKB_SUM_MODEL", PRIMARY_MODEL)


def _load_key():
    """API key for the primary endpoint. Prefer the env var; else read a KEY=VALUE line
    from a secrets file (path via EKB_SECRETS_FILE). Never hardcode a key here."""
    k = os.environ.get("EKB_LLM_API_KEY")
    if k:
        return k
    try:
        path = os.path.expanduser(os.environ.get("EKB_SECRETS_FILE", "~/.config/myproject/secrets.env"))
        return next((l.split("=", 1)[1].strip()
                     for l in open(path)
                     if l.startswith("EKB_LLM_API_KEY=")), None)
    except Exception:
        return None


PRIMARY_KEY = _load_key()

# --- Optional secondary (fallback) model: a CLI-based LLM -------------------
# If the primary is unavailable or returns empty, optionally fall back to a local CLI
# LLM so extraction keeps going — and ALERT (throttled) so you know the primary is down.
# Leave EKB_FALLBACK_CMD empty to disable the fallback entirely.
# The default invocation below matches a Claude-Code-style CLI
# (`<cmd> -p "<prompt>" --model <m> --output-format json` returning {"result": "..."}).
# Adjust `_chat_fallback` if your CLI differs.
FALLBACK_CMD    = os.environ.get("EKB_FALLBACK_CMD", "")          # e.g. "/usr/local/bin/llm-cli"
FALLBACK_MODEL  = os.environ.get("EKB_FALLBACK_MODEL", "")
_ALERT_STATE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fallback_alert")
_ALERT_THROTTLE = 3600                            # re-alert at most once/hour while degraded

# --- Optional ops alerting (any chat webhook / bot). All optional. ----------
_ALERT_TOKEN_F = os.environ.get("EKB_ALERT_BOT_TOKEN_FILE", "")   # file holding a bot token, or ""
_ALERT_CHAT    = os.environ.get("EKB_ALERT_CHAT_ID", "123456789")


class LLMTimeout(Exception):
    """Both the primary AND the fallback failed. Signals the caller to DEFER the email
    (retry on a later run) rather than drop its extraction silently."""


def model_id():
    return PRIMARY_MODEL


def _alert(text):
    """Best-effort ops alert (throttled to once/hour). Uses a Telegram-style bot API if a
    token file is configured; otherwise no-ops. Never breaks extraction."""
    if not _ALERT_TOKEN_F or not os.path.exists(_ALERT_TOKEN_F):
        return
    now = time.time()
    try:
        if now - float(open(_ALERT_STATE).read().strip()) < _ALERT_THROTTLE:
            return
    except Exception:
        pass
    try:
        open(_ALERT_STATE, "w").write(str(now))
    except Exception:
        pass
    try:
        token = open(_ALERT_TOKEN_F).read().strip()
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": _ALERT_CHAT, "text": text, "parse_mode": "Markdown",
                            "disable_web_page_preview": "true"}, timeout=20)
    except Exception as e:
        print(f"    [extract] alert failed: {e}", file=sys.stderr)


def _chat_primary(system, user, max_tokens, temperature, reasoning=False, model=None):
    """Primary path. Returns (content, reason): content='' on any failure/empty, reason
    describes what went wrong (None on success). `reasoning=True` turns reasoning ON for
    calls that need tight instruction-following — costlier/slower, opt-in per call.
    `model` overrides PRIMARY_MODEL for this call."""
    payload = {"model": model or PRIMARY_MODEL, "temperature": temperature,
               "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    # Optional reasoning toggle (honoured by routers that support it; harmless elsewhere).
    payload["reasoning"] = {"enabled": bool(reasoning)}
    # Optional provider pin (router-style): EKB_PROVIDER="ProviderA,ProviderB".
    prov = os.environ.get("EKB_PROVIDER")
    if prov:
        payload["provider"] = {"order": [p.strip() for p in prov.split(",")],
                               "allow_fallbacks": False}
    timed_out, last = False, None
    for attempt in range(3):                      # bounded retries -> no multi-minute storm
        try:
            r = requests.post(PRIMARY_URL, json=payload, timeout=(10, 120),
                              headers={"Authorization": f"Bearer {PRIMARY_KEY}"})
        except requests.exceptions.Timeout:
            timed_out = True; last = "timed out"; continue
        except Exception as e:
            last = type(e).__name__; time.sleep(2 ** attempt); continue
        if r.status_code == 200:
            c = (r.json()["choices"][0]["message"].get("content") or "").strip()
            return c, (None if c else "returned empty")
        if r.status_code in (429, 500, 502, 503):
            last = f"HTTP {r.status_code}"; time.sleep(2 ** attempt); continue
        return "", f"HTTP {r.status_code}: {r.text[:120]}"
    return "", ("timed out after retries" if timed_out else (last or "failed"))


def _chat_fallback(system, user, max_tokens, temperature):
    """Optional fallback path: a CLI-based LLM. Returns '' if disabled or on failure."""
    if not FALLBACK_CMD:
        return ""
    try:
        cmd = [FALLBACK_CMD, "-p", f"{system}\n\n{user}"]
        if FALLBACK_MODEL:
            cmd += ["--model", FALLBACK_MODEL]
        cmd += ["--output-format", "json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            return (json.loads(r.stdout).get("result") or "").strip()
        print(f"    [extract] fallback rc={r.returncode}: {(r.stderr or '')[:160]}",
              file=sys.stderr)
    except Exception as e:
        print(f"    [extract] fallback error: {e}", file=sys.stderr)
    return ""


def _chat(system, user, max_tokens=700, temperature=0.0, reasoning=False, model=None):
    who = model or PRIMARY_MODEL
    content, reason = _chat_primary(system, user, max_tokens, temperature, reasoning, model)
    if content:
        return content
    # primary unavailable / empty -> fall back to the CLI LLM, and alert (throttled).
    fb = _chat_fallback(system, user, max_tokens, temperature)
    if fb:
        _alert(f"⚠️ *Email KB: primary model ({who}) {reason}* — fell back to the CLI LLM. "
               f"Check the primary endpoint.")
        return fb
    # both failed -> defer this email for a later run rather than lose its extraction
    _alert(f"🛑 *Email KB: {who} {reason} AND fallback failed* — deferring emails. "
           f"Extraction is stalled until one recovers.")
    raise LLMTimeout(f"{who} ({reason}) and fallback both failed")


def _json_block(text):
    """Pull the first {...} JSON object out of a model response."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        # tolerate trailing commas
        try:
            return json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:
            return {}


def _json_array(text):
    """Pull the first [...] JSON array out of a model response."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    for candidate in (m.group(0), re.sub(r",\s*([}\]])", r"\1", m.group(0))):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return []


# --- Multi-prompt extraction: one FOCUSED prompt per field over the SAME email
# context. This beats one combined prompt on smaller models (esp. operational
# knowledge, which the combined prompt routinely drops). Temperature 0 for fidelity.
# EKB_BUSINESS_DESC lets you describe your business so the prompts have context.
_DOMAIN = os.environ.get("EKB_BUSINESS_DESC",
                         "a company that sells products and related supplies")

CONTACTS_SYS = (
    f"You extract contacts from a business email for {_DOMAIN}. "
    'Return ONLY a JSON array of every real person/organization in the email — '
    "senders, recipients, AND people named in the body (e.g. end-customer technical "
    'contacts) — as objects {"email","name","company","role","phone"}. '
    "Use only values present in the text; leave unknown fields as empty strings. No prose.")
OPS_SYS = (
    f"You extract OPERATIONAL facts from a business email for {_DOMAIN}. "
    "Return ONLY a JSON array of short strings: PO/order/quote/invoice numbers, "
    "shipping, who the END CUSTOMER is, the reseller/sales channel, decisions, next "
    "steps, and other company activities. Empty array if none. Only facts present. No prose.")
PROD_SYS = (
    f"You extract durable PRODUCT knowledge from a business email for {_DOMAIN}. "
    "Return ONLY a JSON array of short factual strings about how products work: models, "
    "specs, technical compatibility, usage instructions, settings, materials. "
    "DO NOT extract marketing or sales content: discounts, promotional offers, event "
    "follow-ups, validity dates, calls-to-action, contact-to-order lines, or list prices "
    "quoted as part of an offer. Empty array if none. Only durable technical facts present. No prose.")
ACT_SYS = ("Summarize in ONE concise sentence what this email is about / what happened, "
           "for a CRM activity log. Plain text only, no preamble.")


def extract(direction, frm, to, cc, subject, body):
    body = (body or "")[:BODY_CHARS]
    ctx = (f"Direction: {direction}\nFrom: {frm}\nTo: {to}\nCc: {cc}\n"
           f"Subject: {subject}\n\nBody:\n{body}")

    def arr(sysp, mx=600):
        try:
            return _json_array(_chat(sysp, ctx, max_tokens=mx))
        except LLMTimeout:
            raise                         # defer the whole email — don't silently drop it
        except Exception:
            return []

    contacts = [c for c in arr(CONTACTS_SYS, 700) if isinstance(c, dict)]
    operational = [s for s in arr(OPS_SYS, 500) if isinstance(s, str) and s.strip()]
    # product knowledge only matters for OUR outbound mail -> skip the call otherwise
    product = ([s for s in arr(PROD_SYS, 500) if isinstance(s, str) and s.strip()]
               if "outbound" in direction else [])
    try:
        activity = _chat(ACT_SYS, ctx, max_tokens=120).strip()
    except LLMTimeout:
        raise
    except Exception:
        activity = ""
    return {"contacts": contacts, "product_knowledge": product,
            "operational_knowledge": operational, "activity": activity}


# --- Writing-style learning (for OUR senders) -------------------------------
STYLE_SYS = (
    "You analyze the writing style of a business email. Consider ONLY the text the SENDER "
    "actually wrote — IGNORE quoted/replied text (lines starting with '>'), the signature block, "
    "and any legal/confidentiality disclaimer. Return ONLY a JSON array of short, concrete style "
    "observations covering: greeting, sign-off, tone/register, typical sentence length, vocabulary, "
    "formatting habits (bullets/bold/options), and characteristic phrases. Empty array if the email "
    "is too short or automated to judge.")


def extract_style(body):
    body = (body or "")[:BODY_CHARS]
    try:
        resp = _chat(STYLE_SYS, f"Email:\n{body}", max_tokens=400)
    except Exception:
        return []
    obs = []
    for item in _json_array(resp):
        if isinstance(item, str) and item.strip():
            obs.append(item.strip())
        elif isinstance(item, dict):
            for k, v in item.items():
                if isinstance(v, str) and v.strip():
                    obs.append(f"{k}: {v.strip()}")
    if obs:
        return obs[:15]
    # fallback: the model returned prose/markdown bullets instead of a JSON array
    out = []
    for ln in resp.splitlines():
        ln = re.sub(r"^[\-\*\d\.\)\s]+", "", ln).strip().strip('"')
        if len(ln) > 3 and not ln.lower().startswith(("here", "json", "```", "the email", "this email")):
            out.append(ln)
    return out[:15]


STYLE_MERGE_SYS = (
    "You maintain a CONCISE writing-style profile for {name} so an assistant can draft emails in "
    "their voice. Merge the NEW observations into the CURRENT profile (if any): refine and "
    "de-duplicate. Output the FULL updated profile in Markdown with sections: Voice & tone, "
    "Greetings, Sign-offs, Sentence & structure, Vocabulary, Formatting, Characteristic phrases. "
    "Keep it TIGHT — at most ~5 bullets per section, no repetition, no preamble. The profile must "
    "not grow without bound: keep only the most characteristic, distinct traits.")


def update_style_profile(name, existing, observations):
    """Merge new observations into the existing bounded profile (never grows unbounded)."""
    if not observations:
        return existing or ""
    user = ((f"CURRENT profile:\n{existing}\n\n" if existing else "")
            + "NEW observations from recent sent emails:\n" + "\n".join(f"- {o}" for o in observations))
    try:
        return _chat(STYLE_MERGE_SYS.replace("{name}", name), user, max_tokens=800)
    except Exception:
        return existing or ""


SUMMARY_SYS = ("You maintain a CRM. Given a dated activity log with one contact, write a brief "
               "2-4 sentence summary of the relationship and key topics/orders. Plain text only.")


def summarize_activity(name, company, activity_log):
    who = f"{name or ''} {('('+company+')') if company else ''}".strip() or "this contact"
    try:
        return _chat(SUMMARY_SYS, f"Contact: {who}\n\nActivity log:\n{activity_log}\n\nSummary:",
                     max_tokens=220)
    except Exception:
        return ""


# Email-grounded rolling summary: built from the contact's actual emails (body_new),
# rebuilt from the WHOLE thread each time — a later email can supersede an earlier fact,
# so incremental folding (which can't revise stale facts) is wrong here. This is the
# per-contact summary stored in the DB.
CONTACT_SUM_SYS = (
    f"You write a CRM relationship summary for ONE contact of {_DOMAIN}, built ONLY from "
    "the emails provided below (oldest to newest). This is a COMPLETE REBUILD: base the "
    "summary solely on the emails shown.\n\n"
    "CRITICAL RULES:\n"
    "1. The thread evolves over time. When a later email changes or supersedes an earlier "
    "fact (a date moves, a price changes, an order is modified, payment goes from pending "
    "to paid), reflect ONLY the LATEST state. Never report a superseded fact as current.\n"
    "2. Distinguish PROPOSED/PENDING/a DEADLINE from CONFIRMED/DONE. Only describe something "
    "as completed (e.g. 'payment received', 'shipped') if an email explicitly confirms it "
    "happened. A due date is not proof of payment.\n"
    "3. Do not assume the contact's gender; use their name or neutral phrasing.\n"
    "4. State each fact once. Do not repeat the same event in multiple paragraphs.\n"
    "5. Only list a product/model as ordered or quoted to THIS contact if an email shows it "
    "on their actual invoice/order/quote. Models merely listed as options or in a price "
    "menu are NOT their order — do not present them as such.\n\n"
    "Write in prose paragraphs grouped by topic (not a bulleted catalog). Favor "
    "COMPLETENESS over brevity: capture every fact the emails support — do NOT drop "
    "detail to keep it short. Length should track how much actually happened.\n"
    "Write SHORT, SINGLE-FACT sentences: one event or claim per sentence. Do NOT chain "
    "many events into one long compound sentence — split them so each sentence stands "
    "on its own and can carry its own source id. E.g. write 'The seller sent the invoice "
    "on May 27. The customer asked to cover multiple units. A revised invoice followed "
    "the same day.' — NOT 'The seller sent the invoice on May 27, the customer asked to "
    "cover multiple units, and a revised invoice followed.'\n\n"
    "6. CITE INLINE — mandatory and checked. Each email above is prefixed with a "
    "'[msg-id: <id>]' tag. END EVERY FACTUAL SENTENCE with the bracketed id(s) that "
    "sentence's fact came from, right there inline. Worked example (note an id after "
    "EACH sentence):\n"
    "  'The seller sent a proforma invoice on May 27 [1a2b3c4d5e6f7a8b]. The customer "
    "then asked to cover multiple units, so a revised invoice followed the same day "
    "[2b3c4d5e6f7a8b9c]. Payment was received on June 22 [3c4d5e6f7a8b9c0d].'\n"
    "TWO failure modes make the output INVALID: (a) collecting ids into a list at the "
    "END of the summary, and (b) omitting ids. Do NEITHER — every factual sentence "
    "carries its own id(s) inline where the fact is stated. If a sentence draws on two "
    "emails, cite both inline: '[id1] [id2].' Use only ids shown verbatim in a "
    "'[msg-id: <id>]' tag above — never invent, guess, reformat, or cite an id range.\n\n"
    "Capture: who they are (company & role), the relationship history, EVERY order/PO/quote/"
    "invoice number with what it was for, products and quantities, pricing discussed, the "
    "end customer / sales channel, current shipping/payment status, open issues or "
    "complaints, decisions made, and outstanding next steps. A few short factual paragraphs "
    "(group facts logically). No filler, no preamble, no speculation.")
# Tune these to your model's context window. We feed base_summary + only the unsealed
# TAIL, so these caps bound a single tail, not the whole thread.
_SUM_PER_EMAIL = 10000     # chars of each email body fed in
_SUM_MAX_EMAILS = 200      # most-recent tail emails packed per call
_SUM_BUDGET = 300000       # hard ceiling of tail context in chars
_SEAL_CHARS = 120000       # seal the tail into base_summary once it grows past this
SUM_MAX_TOKENS = 15000     # output cap — generous so the model can enumerate everything on
                           # fact-dense accounts instead of compressing and dropping the tail


_CITE_RE = re.compile(r"\[\s*(1[0-9a-f]{15})\s*\]")

# Second pass: some models cite at document level (one id-list at the end). This pass
# relocates those ids INLINE onto the sentences they support, WITHOUT touching the
# facts/wording. Editor-only prompt keeps it from re-summarizing or hallucinating.
CITE_PASS_SYS = (
    "You are a citation editor. You are given a CRM SUMMARY and the SOURCE EMAILS it "
    "was built from (each email prefixed with a '[msg-id: <id>]' tag). Your ONLY job "
    "is to attach source ids INLINE. Rules:\n"
    "1. Keep the summary's wording and facts EXACTLY — do not add, remove, reword, or "
    "reorder anything. Insert citations only.\n"
    "2. End each factual sentence with the bracketed id(s) of the email(s) that support "
    "it, inline: 'Payment was received on June 22 [3c4d5e6f7a8b9c0d].' If two emails "
    "support a sentence, cite both '[id1] [id2].'\n"
    "3. If the summary ends with a trailing list of ids, DELETE that list — every id "
    "must move onto the specific sentence it supports.\n"
    "4. Use only ids that appear verbatim in a '[msg-id: <id>]' tag in the source. Never "
    "invent, guess, reformat, or cite a range. If no source email supports a sentence, "
    "leave that sentence without an id.\n"
    "Output ONLY the edited summary text — no preamble, no commentary.")


def _attach_citations(summary, block, valid_ids):
    """Move a trailing id-list to inline per-sentence citations. Returns the inline-cited
    summary, or the ORIGINAL summary on any failure/empty — this pass can never make the
    result worse than the document-level-cited first pass."""
    if not summary or not block:
        return summary
    user = (f"SOURCE EMAILS:\n{block}\n\nSUMMARY to cite inline:\n{summary}\n\n"
            "Edited summary with inline citations:")
    try:
        out = _chat(CITE_PASS_SYS, user, max_tokens=SUM_MAX_TOKENS,
                    temperature=0.0, model=SUM_MODEL).strip()
    except Exception:
        return summary
    return _sanitize_citations(out, valid_ids) if out else summary


def _sanitize_citations(summary, valid_ids):
    """Deterministic guarantee that no fabricated message-id survives: drop any
    [id] the model emitted that isn't a real email in this thread. Prompt wording
    can't guarantee this; this makes citation-fidelity 100% by construction."""
    def keep(m):
        return m.group(0) if m.group(1) in valid_ids else ""
    out = _CITE_RE.sub(keep, summary)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _pack_tail(emails):
    """Pack emails NEWEST-FIRST into the char budget (so the recent tail always survives),
    then return them oldest->newest as a text block. Returns '' for no emails.

    The thread is first stripped down (clean_conversation): internal-agent chatter
    dropped, repeated signatures/disclaimers deduped across the thread, and trailing
    quoted-reply history cut -- so the model only sees new substance."""
    emails = clean_conversation(emails)
    segs, total = [], 0
    for e in reversed(emails):
        body = (e.get("body_new") or e.get("body") or "")[:_SUM_PER_EMAIL]
        mid = e.get("id") or e.get("threadId") or ""
        seg = (f"[msg-id: {mid}] [{e.get('date','')}] From: {e.get('from','')} "
               f"To: {e.get('to','')}\nSubject: {e.get('subject','')}\n{body}").strip()
        if (total + len(seg) > _SUM_BUDGET or len(segs) >= _SUM_MAX_EMAILS) and segs:
            break
        segs.append(seg); total += len(seg)
    return "\n\n---\n\n".join(reversed(segs))


def should_seal(tail_emails):
    """True once the unsealed tail is large enough to fold into base_summary and advance
    the checkpoint. Measured on the same per-email cap used when packing."""
    total = sum(min(len(e.get("body_new") or e.get("body") or ""), _SUM_PER_EMAIL)
                for e in clean_conversation(tail_emails))
    return total >= _SEAL_CHARS


def summarize_contact_emails(name, company, base_summary, tail_emails):
    """Rolling-checkpoint CRM summary: fold the unsealed TAIL into an existing base_summary.

    We feed the model the already-condensed `base_summary` (everything up to the last sealed
    chunk) plus ONLY the raw tail messages after it — old chunks are never re-read. A later
    email can still supersede an earlier fact, but only within base+tail, which is why the
    caller re-runs this on every new message and re-seals when the tail grows large. With an
    empty base_summary this degrades to a normal full summary of whatever emails are passed.
    Returns '' on model failure.
    """
    if not tail_emails and not base_summary:
        return ""
    who = f"{name or ''} {('('+company+')') if company else ''}".strip() or "this contact"
    block = _pack_tail(tail_emails)
    if base_summary.strip():
        user = (f"Contact: {who}\n\nEXISTING SUMMARY of the earlier conversation "
                f"(already condensed — treat as established fact unless a newer message below "
                f"corrects it):\n{base_summary}\n\nNEWER email messages since then "
                f"(oldest to newest):\n{block}\n\nProduce a single updated summary that "
                f"integrates the newer messages into the existing summary:")
    else:
        user = f"Contact: {who}\n\nComplete email thread (oldest to newest):\n{block}\n\nSummary:"
    try:
        # Reasoning stays OFF: with reasoning ON, some routers leak the chain-of-thought
        # into `content` and burn the token budget before finishing the summary. Inline-
        # citation placement is driven by the prompt (rule 6) plus _sanitize_citations.
        raw = _chat(CONTACT_SUM_SYS, user, max_tokens=SUM_MAX_TOKENS,
                    temperature=0.0, model=SUM_MODEL).strip()
        valid = {e.get("id") for e in tail_emails if e.get("id")}
        first = _sanitize_citations(raw, valid)
        # Second pass: relocate document-level ids to inline per-sentence citations.
        # Falls back to `first` if it fails, so it never degrades the result.
        return _attach_citations(first, block, valid)
    except Exception:
        return ""
