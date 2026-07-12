#!/usr/bin/env python3
"""Google Drive client (reuses the same OAuth setup as the Gmail tooling).

Usage:
  gdrive.py list   [-q "<drive query>"] [-n 25]      list/search files
  gdrive.py find   "text"                            full-text search
  gdrive.py read   <fileId>                           print a doc as text (Google Docs/Sheets export)
  gdrive.py download <fileId> [dest]                  download a file
  gdrive.py upload <localpath> [--folder <id>] [--name N]
  gdrive.py mkdir  <name> [--folder <parentId>]
  gdrive.py info   <fileId>

Account selected via env MAIL_ACCOUNT (default "primary"). The account's token must
carry a Drive scope (see config.py) — re-run `python auth.py <account>` after adding it.
"""
import os, sys, io, argparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from auth import get_credentials

EXPORT = {  # Google-native types -> export MIME for `read`
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def svc():
    account = os.environ.get("MAIL_ACCOUNT", "primary")
    creds = get_credentials(account, interactive=False)
    if not creds:
        sys.exit(f"Not authorized for '{account}'. Run: python auth.py {account}")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def cmd_list(a):
    kw = {"pageSize": a.n, "fields": "files(id,name,mimeType,modifiedTime,size)",
          "orderBy": "modifiedTime desc"}
    if a.q:
        kw["q"] = a.q
    for f in svc().files().list(**kw).execute().get("files", []):
        sz = f.get("size", "")
        sz = f"{int(sz)//1024}K" if sz else "—"
        kind = f["mimeType"].split(".")[-1]
        print(f"  {f['id']}  {f['modifiedTime'][:10]}  {sz:>6}  {kind:12}  {f['name']}")


def cmd_find(a):
    a.q = f"fullText contains '{a.text}' or name contains '{a.text}'"
    a.n = a.n
    cmd_list(a)


def cmd_read(a):
    s = svc()
    meta = s.files().get(fileId=a.id, fields="name,mimeType").execute()
    mt = meta["mimeType"]
    if mt in EXPORT:
        data = s.files().export(fileId=a.id, mimeType=EXPORT[mt]).execute()
        print(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
    else:
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, s.files().get_media(fileId=a.id))
        done = False
        while not done:
            _, done = dl.next_chunk()
        print(buf.getvalue().decode("utf-8", "replace"))


def cmd_download(a):
    s = svc()
    meta = s.files().get(fileId=a.id, fields="name,mimeType").execute()
    dest = a.dest or meta["name"]
    if meta["mimeType"] in EXPORT:
        data = s.files().export(fileId=a.id, mimeType=EXPORT[meta["mimeType"]]).execute()
        open(dest, "wb").write(data if isinstance(data, bytes) else data.encode())
    else:
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, s.files().get_media(fileId=a.id))
        done = False
        while not done:
            _, done = dl.next_chunk()
        open(dest, "wb").write(buf.getvalue())
    print(f"downloaded -> {dest}")


def cmd_upload(a):
    body = {"name": a.name or os.path.basename(a.path)}
    if a.folder:
        body["parents"] = [a.folder]
    f = svc().files().create(body=body, media_body=MediaFileUpload(a.path),
                             fields="id,name,webViewLink").execute()
    print(f"uploaded: {f['name']}  id={f['id']}  {f.get('webViewLink','')}")


def cmd_mkdir(a):
    body = {"name": a.name, "mimeType": "application/vnd.google-apps.folder"}
    if a.folder:
        body["parents"] = [a.folder]
    f = svc().files().create(body=body, fields="id,name").execute()
    print(f"folder created: {f['name']}  id={f['id']}")


def cmd_info(a):
    f = svc().files().get(fileId=a.id,
                          fields="id,name,mimeType,size,modifiedTime,owners,parents,webViewLink").execute()
    for k, v in f.items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("list"); g.add_argument("-q", default=None); g.add_argument("-n", type=int, default=25); g.set_defaults(func=cmd_list)
    g = sub.add_parser("find"); g.add_argument("text"); g.add_argument("-n", type=int, default=25); g.set_defaults(func=cmd_find)
    g = sub.add_parser("read"); g.add_argument("id"); g.set_defaults(func=cmd_read)
    g = sub.add_parser("download"); g.add_argument("id"); g.add_argument("dest", nargs="?"); g.set_defaults(func=cmd_download)
    g = sub.add_parser("upload"); g.add_argument("path"); g.add_argument("--folder", default=None); g.add_argument("--name", default=None); g.set_defaults(func=cmd_upload)
    g = sub.add_parser("mkdir"); g.add_argument("name"); g.add_argument("--folder", default=None); g.set_defaults(func=cmd_mkdir)
    g = sub.add_parser("info"); g.add_argument("id"); g.set_defaults(func=cmd_info)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
