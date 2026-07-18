#!/usr/bin/env python3
"""Daily follow-up check: customer inquiries still awaiting a reply.

For each connected mailbox, scan recent inbox threads. A thread NEEDS A REPLY when
its LATEST message is from an external customer (anyone at your own domain counts as
"us" — so a teammate's reply counts as yours). An LLM then keeps only the messages
that genuinely require a response (an open question / quote / pricing / availability /
ordering request). If any are pending, a digest email is sent to the owner.
Designed to run once a day from cron (default 08:00 — see the run script / README).

Dependencies (documented, NOT bundled here):
  * gmailer + config.token_path  -> provided by the `gmail-multi-mailbox` skill
    (the Gmail read/search/send layer + per-account token handling).
  * An OpenAI-compatible chat endpoint for the YES/NO triage (see classify() below).
    The backend is swappable — point LLM_URL/LLM_MODEL at any provider you like.
"""
import os, sys, datetime, base64, html
from email.utils import getaddresses, parsedate_to_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# gmailer.py and config.py come from the `gmail-multi-mailbox` skill. Place them next
# to this file, or add their directory to PYTHONPATH.
import gmailer
from config import token_path

# ---------------------------------------------------------------------------
# Config (edit these or override via environment variables)
# ---------------------------------------------------------------------------
# Account KEYS as defined in your gmail layer's config.ACCOUNTS. The scan runs over
# each connected account and treats a reply from ANY of them as "handled".
MAILBOXES = os.environ.get("FOLLOWUP_MAILBOXES", "primary,secondary").split(",")

# Your own domain — mail from these addresses is "us", never a pending customer.
OWN_DOMAIN = os.environ.get("OWN_DOMAIN", "example.com")

# Where the digest is sent, and the account/address it is sent FROM.
NOTIFY_TO           = os.environ.get("FOLLOWUP_NOTIFY_TO", "owner@example.com")
NOTIFY_FROM         = os.environ.get("FOLLOWUP_NOTIFY_FROM", "agent@example.com")
NOTIFY_FROM_ACCOUNT = os.environ.get("FOLLOWUP_NOTIFY_ACCOUNT", "primary")

LOOKBACK    = "newer_than:10d in:inbox -in:chats"
MAX_THREADS = 40

# LLM triage backend — OpenAI-compatible /chat/completions. Bring your own endpoint
# (local llama.cpp/vLLM, or a hosted provider). Nothing here is hardcoded to a vendor.
LLM_URL   = os.environ.get("LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "local-model")
LLM_KEY   = os.environ.get("LLM_API_KEY", "")

# Marketing / social / automated senders: skipped even if they land in the inbox.
# These are generic examples — add whatever noise domains you see.
NOISE_SENDER_DOMAINS = (
    "linkedin.com", "mailchimp.com", "substack.com",
    "fedex.com", "ups.com", "accounts.google.com",
)

CLASSIFY_SYS = (
    "You triage an inbound customer email for a business. The question is NOT 'is this "
    "about sales' — it is: does the LATEST customer message REQUIRE A RESPONSE OR ACTION "
    "FROM US to move forward? Answer YES only if the customer is WAITING ON US: an "
    "unanswered question, a request for a quote/price/availability/compatibility info, a "
    "request to place or arrange an order, or an open issue we must address. "
    "Answer NO when the message needs nothing from us, EVEN IF it mentions buying or "
    "ordering: a thank-you or acknowledgement ('thanks', 'appreciate it', 'got it', "
    "'sounds good'), the customer stating THEIR OWN next step ('I'll order tomorrow', "
    "'will check and get back to you', 'placing the order now'), a confirmation/closing "
    "with no question, or newsletters, marketing, automated notices, shipping/tracking, "
    "payment receipts, internal mail, social, spam. If the message is only a statement of "
    "intent or a pleasantry with no question or request directed at us, it is NO. "
    "Answer ONLY one line, either 'YES: <reason>' or 'NO: <reason>'. "
    "For YES, the reason MUST be a concrete, specific summary of the ask in <=14 words "
    "that names the specific product/model and the action requested — pull the actual "
    "product names, quantities, and the specific request straight from the email. Do NOT "
    "use vague phrases like 'request for quote' or 'product inquiry'. "
    "For NO, give a <=8-word reason.")


def classify(system, user, max_tokens=60):
    """One YES/NO triage call against an OpenAI-compatible chat endpoint."""
    payload = {"model": LLM_MODEL, "temperature": 0.0, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    headers = {"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {}
    r = requests.post(LLM_URL, json=payload, headers=headers, timeout=(10, 120))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def is_noise_sender(addr):
    dom = addr.split("@")[-1].lower().strip(">").strip()
    return any(dom == d or dom.endswith("." + d) for d in NOISE_SENDER_DOMAINS)


def _addr(value):
    a = [x for _, x in getaddresses([value or ""]) if "@" in x]
    return a[0].lower() if a else ""


def _external(addr):
    return addr and not addr.endswith("@" + OWN_DOMAIN)


def _days_waiting(date_hdr):
    try:
        d = parsedate_to_datetime(date_hdr)
        now = datetime.datetime.now(d.tzinfo) if d.tzinfo else datetime.datetime.now()
        return max(0, (now - d).days)
    except Exception:
        return "?"


def _last_epoch(msg):
    try:
        return int(msg.get("internalDate", "0")) // 1000
    except Exception:
        return 0


def scan():
    pending, seen = [], set()
    # Build a service per connected mailbox up front so we can check whether a reply to
    # a customer exists in ANY of our mailboxes (replies often land under a different
    # subject/thread than the customer's question — don't re-flag those).
    svcs = {}
    for acct in MAILBOXES:
        acct = acct.strip()
        if not acct:
            continue
        if not os.path.exists(token_path(acct)):
            print(f"  (skipping {acct} — not connected)")
            continue
        os.environ["MAIL_ACCOUNT"] = acct   # gmail layer selects the account from this
        svcs[acct] = gmailer.svc()

    def replied_after(addr, after_epoch):
        # True if any of our mailboxes sent mail to `addr` after `after_epoch`.
        for a, s in svcs.items():
            try:
                os.environ["MAIL_ACCOUNT"] = a
                q = f"in:sent to:{addr} after:{after_epoch}"
                if s.users().messages().list(userId="me", q=q, maxResults=1
                                             ).execute().get("messages"):
                    return True
            except Exception:
                pass
        return False

    for acct, svc in svcs.items():
        os.environ["MAIL_ACCOUNT"] = acct
        threads = svc.users().threads().list(userId="me", q=LOOKBACK,
                                             maxResults=MAX_THREADS).execute().get("threads", [])
        for t in threads:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            full = svc.users().threads().get(userId="me", id=t["id"], format="full").execute()
            msgs = full.get("messages", [])
            if not msgs:
                continue
            last = msgs[-1]
            frm = gmailer._hdr(last, "From")
            from_addr = _addr(frm)
            # awaiting reply only if the LAST message is from an external customer
            if not _external(from_addr) or is_noise_sender(from_addr):
                continue
            # ...and only if we haven't already replied to them after this message,
            # even under a different subject/thread (cross-thread reply detection).
            if replied_after(from_addr, _last_epoch(last)):
                continue
            subj = gmailer._hdr(msgs[0], "Subject")
            body = gmailer._body_text(last.get("payload", {}))[:5000]
            try:
                verdict = classify(CLASSIFY_SYS, f"Subject: {subj}\nFrom: {frm}\n\n{body}",
                                   max_tokens=60).strip()
            except Exception:
                verdict = "NO: classify error"
            if verdict.upper().startswith("YES"):
                pending.append({
                    "from": from_addr, "name": getaddresses([frm])[0][0] or from_addr,
                    "subject": subj or "(no subject)", "date": gmailer._hdr(last, "Date"),
                    "days": _days_waiting(gmailer._hdr(last, "Date")),
                    "reason": verdict.split(":", 1)[-1].strip(), "mailbox": acct,
                })
    pending.sort(key=lambda p: (p["days"] if isinstance(p["days"], int) else 0), reverse=True)
    return pending


def _plain(pending):
    lines = [f"Good morning. {len(pending)} customer inquir{'y' if len(pending)==1 else 'ies'} "
             f"appear to be awaiting a reply:\n"]
    for p in pending:
        lines.append(f"• {p['name']} <{p['from']}>  —  {p['subject']}")
        lines.append(f"    waiting ~{p['days']} day(s) · arrived in {p['mailbox']} · {p['reason']}")
        lines.append("")
    lines.append("(A teammate's replies count as handled. Reply to clear them from tomorrow's check.)")
    return "\n".join(lines)


def _html(pending):
    e = html.escape
    n = len(pending)
    rows = []
    for i, p in enumerate(pending):
        bg = "#ffffff" if i % 2 == 0 else "#f6f8fa"
        days = p["days"]
        wait = "today" if days == 0 else (f"~{days} day" if days == 1 else f"~{days} days")
        # flag anything sitting 2+ days
        wcolor = "#b91c1c" if isinstance(days, int) and days >= 2 else "#111827"
        rows.append(f"""
      <tr style="background:{bg};">
        <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;vertical-align:top;">
          <div style="font-weight:600;color:#111827;">{e(p['name'])}</div>
          <a href="mailto:{e(p['from'])}" style="color:#2563eb;text-decoration:none;font-size:13px;">{e(p['from'])}</a>
        </td>
        <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;vertical-align:top;color:#111827;">{e(p['subject'])}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;vertical-align:top;color:#374151;">{e(p['reason'])}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;vertical-align:top;white-space:nowrap;color:{wcolor};font-weight:600;">{wait}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;vertical-align:top;white-space:nowrap;color:#6b7280;">{e(p['mailbox'])}</td>
      </tr>""")
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:16px;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827;">
  <div style="max-width:720px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
    <div style="padding:16px 18px;background:#0f172a;color:#ffffff;">
      <div style="font-size:17px;font-weight:600;">Follow-up check</div>
      <div style="font-size:14px;color:#cbd5e1;margin-top:2px;">{n} customer inquir{'y' if n==1 else 'ies'} awaiting a reply</div>
    </div>
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f3f4f6;text-align:left;">
          <th style="padding:9px 12px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">Customer</th>
          <th style="padding:9px 12px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">Subject</th>
          <th style="padding:9px 12px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">What they need</th>
          <th style="padding:9px 12px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">Waiting</th>
          <th style="padding:9px 12px;border-bottom:2px solid #e5e7eb;color:#6b7280;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;">In</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}
      </tbody>
    </table>
    <div style="padding:12px 18px;font-size:12px;color:#6b7280;background:#f9fafb;border-top:1px solid #e5e7eb;">
      A teammate's replies count as handled. Reply to clear an item from tomorrow's check.
    </div>
  </div>
</body></html>"""


def notify(pending):
    os.environ["MAIL_ACCOUNT"] = NOTIFY_FROM_ACCOUNT
    svc = gmailer.svc()
    msg = MIMEMultipart("alternative")
    msg["To"] = NOTIFY_TO
    msg["From"] = NOTIFY_FROM
    msg["Subject"] = f"Follow-up check: {len(pending)} customer inquir" \
                     f"{'y' if len(pending)==1 else 'ies'} awaiting reply"
    msg.attach(MIMEText(_plain(pending), "plain"))
    msg.attach(MIMEText(_html(pending), "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()


def main():
    dry = "--dry" in sys.argv
    pending = scan()
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    if pending:
        if not dry:
            notify(pending)
        print(f"{stamp} {'[DRY] would notify' if dry else 'notified'} {NOTIFY_TO}: "
              f"{len(pending)} pending inquiries")
        for p in pending:
            print(f"   - {p['from']} | {p['subject'][:50]} | {p['days']}d | {p['reason']}")
    else:
        print(f"{stamp} all clear — no unanswered customer inquiries.")


if __name__ == "__main__":
    main()
