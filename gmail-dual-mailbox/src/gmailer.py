#!/usr/bin/env python3
"""gmailer — read / search / send / draft for a Gmail mailbox via the Gmail API.

Usage:
  gmailer.py profile
  gmailer.py list   [-q "<gmail query>"] [-n 15] [--label INBOX]
  gmailer.py read   <message_id>
  gmailer.py thread <thread_id>
  gmailer.py send   --to a@b.com [--cc ..] [--bcc ..] --subject "..." --body "..." [--body-file f] [--reply-to <msg_id>]
  gmailer.py draft  --to a@b.com [--cc ..] --subject "..." --body "..." [--body-file f] [--reply-to <msg_id>]

Pick the mailbox with the MAIL_ACCOUNT env var (default: primary).
Gmail query examples: 'is:unread', 'from:bob newer_than:7d', 'subject:invoice'.
"""
import sys, argparse, base64
from email.mime.text import MIMEText
from email.utils import parseaddr
from googleapiclient.discovery import build
from auth import get_credentials


def svc():
    import os
    account = os.environ.get("MAIL_ACCOUNT", "primary")
    creds = get_credentials(account, interactive=False)
    if not creds:
        sys.exit(f"Not authorized for '{account}'. Run: python auth.py {account}")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _hdr(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload):
    """Recursively pull text/plain (fallback text/html stripped) from a payload."""
    if payload.get("mimeType", "").startswith("multipart"):
        out = []
        for p in payload.get("parts", []):
            out.append(_body_text(p))
        return "\n".join(x for x in out if x)
    data = payload.get("body", {}).get("data")
    if not data:
        return ""
    text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
    if payload.get("mimeType") == "text/html":
        import re
        text = re.sub(r"<[^>]+>", "", text)
    return text


def cmd_profile(a, s):
    p = s.users().getProfile(userId="me").execute()
    print(f"Account: {p['emailAddress']}")
    print(f"Total messages: {p.get('messagesTotal')}  |  Threads: {p.get('threadsTotal')}")


def cmd_list(a, s):
    kw = {"userId": "me", "maxResults": a.n}
    if a.q: kw["q"] = a.q
    if a.label: kw["labelIds"] = [a.label]
    res = s.users().messages().list(**kw).execute()
    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        print("(no messages)"); return
    for mid in ids:
        m = s.users().messages().get(userId="me", id=mid, format="metadata",
                                     metadataHeaders=["From", "Subject", "Date"]).execute()
        unread = "U" if "UNREAD" in m.get("labelIds", []) else " "
        print(f"[{unread}] {mid}  {_hdr(m,'Date')[:25]:25}  {parseaddr(_hdr(m,'From'))[1][:28]:28}  {_hdr(m,'Subject')[:50]}")


def cmd_read(a, s):
    m = s.users().messages().get(userId="me", id=a.id, format="full").execute()
    print(f"From:    {_hdr(m,'From')}")
    print(f"To:      {_hdr(m,'To')}")
    print(f"Date:    {_hdr(m,'Date')}")
    print(f"Subject: {_hdr(m,'Subject')}")
    print(f"Thread:  {m.get('threadId')}   Labels: {', '.join(m.get('labelIds', []))}")
    print("-" * 70)
    print(_body_text(m.get("payload", {})).strip())


def cmd_thread(a, s):
    t = s.users().threads().get(userId="me", id=a.id, format="full").execute()
    for m in t.get("messages", []):
        print(f"\n=== {m['id']} | {_hdr(m,'From')} | {_hdr(m,'Date')} ===")
        print(_body_text(m.get("payload", {})).strip()[:2000])


def _mk_message(a, s):
    body = open(a.body_file).read() if getattr(a, "body_file", None) else (a.body or "")
    mime = MIMEText(body)
    mime["To"] = a.to
    if a.cc: mime["Cc"] = a.cc
    if getattr(a, "bcc", None): mime["Bcc"] = a.bcc
    mime["Subject"] = a.subject or ""
    thread_id = None
    if getattr(a, "reply_to", None):
        orig = s.users().messages().get(userId="me", id=a.reply_to, format="metadata",
                                        metadataHeaders=["Message-ID", "Subject"]).execute()
        msgid = _hdr(orig, "Message-ID")
        if msgid:
            mime["In-Reply-To"] = msgid
            mime["References"] = msgid
        thread_id = orig.get("threadId")
        if not a.subject:
            mime.replace_header("Subject", "Re: " + _hdr(orig, "Subject"))
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id: payload["threadId"] = thread_id
    return payload


def cmd_send(a, s):
    import os
    from config import NO_SEND_ACCOUNTS, DEFAULT_ACCOUNT
    account = os.environ.get("MAIL_ACCOUNT", DEFAULT_ACCOUNT)
    if account in NO_SEND_ACCOUNTS:
        sys.exit(f"REFUSED: sending as '{account}' is forbidden (drafts only). Use `draft` instead.")
    res = s.users().messages().send(userId="me", body=_mk_message(a, s)).execute()
    print(f"Sent  id={res['id']}  thread={res.get('threadId')}")


def cmd_draft(a, s):
    res = s.users().drafts().create(userId="me", body={"message": _mk_message(a, s)}).execute()
    print(f"Draft saved  draft_id={res['id']}  msg_id={res['message']['id']}")


def main():
    ap = argparse.ArgumentParser(description="Gmail client (select mailbox with MAIL_ACCOUNT env var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile").set_defaults(func=cmd_profile)
    g = sub.add_parser("list"); g.add_argument("-q", default=None); g.add_argument("-n", type=int, default=15); g.add_argument("--label", default=None); g.set_defaults(func=cmd_list)
    g = sub.add_parser("read"); g.add_argument("id"); g.set_defaults(func=cmd_read)
    g = sub.add_parser("thread"); g.add_argument("id"); g.set_defaults(func=cmd_thread)
    for name, fn in (("send", cmd_send), ("draft", cmd_draft)):
        g = sub.add_parser(name)
        g.add_argument("--to", required=True); g.add_argument("--cc", default=None)
        if name == "send": g.add_argument("--bcc", default=None)
        g.add_argument("--subject", default=None); g.add_argument("--body", default=None)
        g.add_argument("--body-file", dest="body_file", default=None)
        g.add_argument("--reply-to", dest="reply_to", default=None)
        g.set_defaults(func=fn)
    a = ap.parse_args()
    a.func(a, svc())


if __name__ == "__main__":
    main()
