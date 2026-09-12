#!/usr/bin/env python3
"""Draft mail-merge — one Gmail DRAFT per recipient, cloned from a sample draft.

The formatting master is a draft you composed in Gmail (HTML, links, bold, signature).
Each generated draft is a byte-for-byte clone of it with ONLY the greeting name swapped.
Nothing is sent: the drafts land in the mailbox's Drafts folder for a person to review.

Inputs
  --template FILE   text file: first line "Subject: ...", then the body with a [Name]
                    placeholder (gives the subject + the plain-text part)
  --leads FILE      CSV with columns: firstname, recipient
  --sample-subject  subject of the sample draft in Gmail Drafts (default: the template's)
  --sample-message-id  use a specific Gmail message (e.g. the sent copy) as the master
  --example ADDR    also create one "[EXAMPLE]" draft to this address
  --cc ADDR         CC this address on every draft
  --limit N / --dry-run

  Optional Drive mode (needs a Drive-scoped token and a `gdrive` module beside gmailer):
  --doc "<Google Doc name>" --sheet "<Google Sheet name>"   instead of local files.

Mailbox: MAIL_ACCOUNT env var (same as gmailer.py).
"""
import argparse, base64, csv, email, io, os, re, sys, html as H
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'gmail-multi-mailbox', 'src'))
import gmailer  # noqa: E402

EMAIL_RE = re.compile(r"^[\w.+\-']+@[\w\-]+(\.[\w\-]+)+$")
GREET = re.compile(r'(Hi|Hello|Dear)(\s*(?:<[^>]+>\s*)*)([^<,]{1,60}?)(\s*(?:</[^>]+>\s*)*,)', re.I)


def read_template_text(txt):
    lines = [l.rstrip() for l in txt.splitlines()]
    subj = next((l.split(':', 1)[1].strip() for l in lines if l.lower().startswith('subject:')), None)
    if not subj:
        sys.exit("template needs a first line 'Subject: ...'")
    body = '\n'.join(l for l in lines if not l.lower().startswith('subject:')).strip()
    return subj, body


def read_leads_csv(csv_text):
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    cols = {c.lower().strip(): c for c in rows[0].keys()} if rows else {}
    fn = cols.get('firstname') or cols.get('first name') or cols.get('name')
    em = cols.get('recipient') or cols.get('email') or cols.get('e-mail')
    if not (fn and em):
        sys.exit(f"leads need firstname + recipient columns; found {list(cols)}")
    out, bad, seen = [], [], set()
    for i, r in enumerate(rows, start=2):
        name = (r[fn] or '').strip(); addr = ' '.join((r[em] or '').split())
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            bad.append((i, name, addr)); continue
        if addr.lower() in seen:
            bad.append((i, name, addr + '  (duplicate)')); continue
        seen.add(addr.lower()); out.append((name, addr))
    return out, bad


def load_inputs(a):
    if a.doc or a.sheet:
        if not (a.doc and a.sheet):
            sys.exit("--doc and --sheet go together")
        try:
            import gdrive
        except ImportError:
            sys.exit("Drive mode needs a gdrive module (Drive-scoped token) beside gmailer.py")
        d = gdrive.svc()
        def find(name, mime):
            q = f"name contains '{name}' and mimeType = '{mime}' and trashed = false"
            r = d.files().list(q=q, fields='files(id,name)', pageSize=2).execute()['files']
            if not r:
                sys.exit(f"not found in Drive: {name!r}")
            return r[0]['id']
        doc = find(a.doc, 'application/vnd.google-apps.document')
        sheet = find(a.sheet, 'application/vnd.google-apps.spreadsheet')
        tpl = d.files().export(fileId=doc, mimeType='text/plain').execute().decode('utf-8-sig')
        leads = d.files().export(fileId=sheet, mimeType='text/csv').execute().decode('utf-8-sig')
        return tpl, leads
    if not (a.template and a.leads):
        sys.exit("give --template and --leads (local files), or --doc and --sheet (Drive)")
    return open(a.template, encoding='utf-8-sig').read(), open(a.leads, encoding='utf-8-sig').read()


def load_message(gm, mid):
    raw = gm.users().messages().get(userId='me', id=mid, format='raw').execute()['raw']
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + '=='))


def find_sample_draft(gm, subject):
    for d in gm.users().drafts().list(userId='me', maxResults=100).execute().get('drafts', []):
        m = gm.users().messages().get(userId='me', id=d['message']['id'], format='metadata',
                                      metadataHeaders=['Subject']).execute()
        s = next((h['value'] for h in m['payload']['headers'] if h['name'] == 'Subject'), '')
        if s.strip() == subject.strip():
            raw = gm.users().messages().get(userId='me', id=d['message']['id'], format='raw').execute()['raw']
            return email.message_from_bytes(base64.urlsafe_b64decode(raw + '=='))
    sys.exit(f"no draft with subject {subject!r} in this mailbox's Drafts")


def parts(msg):
    h = t = None
    for p in msg.walk():
        ct = p.get_content_type()
        if ct == 'text/html' and h is None:
            h = p.get_payload(decode=True).decode(p.get_content_charset() or 'utf-8', 'ignore')
        if ct == 'text/plain' and t is None:
            t = p.get_payload(decode=True).decode(p.get_content_charset() or 'utf-8', 'ignore')
    if not h:
        sys.exit("the sample draft has no HTML part — compose it in Gmail with formatting first")
    return h, t or ''


def personalise(html_src, text_src, first):
    head, tail = html_src[:1500], html_src[1500:]
    m = GREET.search(head)
    if not m:
        sys.exit("no 'Hi <name>,' greeting near the top of the sample draft")
    html_out = head[:m.start(3)] + H.escape(first) + head[m.end(3):] + tail
    text_out = GREET.sub(lambda mm: mm.group(1) + mm.group(2) + first + mm.group(4), text_src, count=1) if text_src else ''
    return html_out, text_out.replace('[Name]', first)


def make_draft(gm, to, subject, html_body, text_body, dry, cc=None):
    msg = MIMEMultipart('alternative'); msg['To'] = to; msg['Subject'] = subject
    if cc:
        msg['Cc'] = cc
    if text_body:
        msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    if dry:
        return 'dry-run'
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return gm.users().drafts().create(userId='me', body={'message': {'raw': raw}}).execute()['id']


def norm_words(s):
    s = re.sub(r'<[^>]+>', ' ', s); s = re.sub(r'https?://\S+', ' ', s)
    return re.findall(r'[a-z0-9%]+', s.lower())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--template'); ap.add_argument('--leads')
    ap.add_argument('--doc'); ap.add_argument('--sheet')
    ap.add_argument('--sample-subject'); ap.add_argument('--sample-message-id', help='use this Gmail message (e.g. a sent copy) as the master'); ap.add_argument('--example'); ap.add_argument('--example-name', default='there')
    ap.add_argument('--cc', help='CC address(es) on every draft')
    ap.add_argument('--limit', type=int, default=0); ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    gm = gmailer.svc()
    tpl_txt, leads_csv = load_inputs(a)
    subject, body = read_template_text(tpl_txt)
    leads, bad = read_leads_csv(leads_csv)
    html_src, text_src = parts(load_message(gm, a.sample_message_id) if a.sample_message_id else find_sample_draft(gm, a.sample_subject or subject))
    text_src = text_src or body
    d_words = ' '.join(norm_words(body.replace('[Name]', 'x'))).split('best regards')[0].split()
    s_words = re.sub(r'^hi \w+', 'hi x', ' '.join(norm_words(text_src)).split('best regards')[0]).split()
    print(f"mailbox: {os.environ.get('MAIL_ACCOUNT', 'primary')} | leads: {len(leads)} ({len(bad)} skipped) | subject: {subject}")
    print("sample draft text matches the template:", "yes" if d_words == s_words else "NO — the draft is used as-is; update the sample draft if the template changed")
    for i, n, e in bad:
        print(f"  skipped row {i}: {n} <{e}>")
    todo = leads[:a.limit] if a.limit else leads
    for first, addr in todo:
        h, t = personalise(html_src, text_src, first)
        did = make_draft(gm, f"{first} <{addr}>" if first else addr, subject, h, t, a.dry_run, a.cc)
        print(f"  draft -> {first} <{addr}>  [{did}]")
    if a.example:
        h, t = personalise(html_src, text_src, a.example_name)
        print(f"  example -> {a.example_name} <{a.example}>  [{make_draft(gm, a.example, '[EXAMPLE] ' + subject, h, t, a.dry_run, a.cc)}]")
    print(f"{'would create' if a.dry_run else 'created'} {len(todo)} draft(s){' + 1 example' if a.example else ''} — nothing sent.")


if __name__ == '__main__':
    main()
