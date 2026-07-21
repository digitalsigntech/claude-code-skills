#!/usr/bin/env python3
"""One-shot: remove cross-mailbox duplicate rows that slipped into the archive
while rfc_msgid was still unbackfilled (e.g. adding a second mailbox to an existing archive). For each
rfc_msgid held by >1 row: keep the SENT-labeled copy if exactly one side has it,
else the earliest-fetched row; delete the rest, carrying processed markers.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import db as DB  # noqa: E402


def main():
    c = DB.conn()
    groups = c.execute(
        """SELECT rfc_msgid FROM emails WHERE rfc_msgid IS NOT NULL AND rfc_msgid<>''
           GROUP BY rfc_msgid HAVING COUNT(*) > 1""").fetchall()
    removed = 0
    for (rfc,) in groups:
        rows = c.execute(
            "SELECT id, account, labels, fetched FROM emails WHERE rfc_msgid=?",
            (rfc,)).fetchall()
        sent = [r for r in rows if "SENT" in (r["labels"] or "")]
        keep = sent[0] if len(sent) == 1 else min(rows, key=lambda r: r["fetched"] or "")
        for r in rows:
            if r["id"] == keep["id"]:
                continue
            was = c.execute("SELECT processed_at FROM processed WHERE msg_id=?",
                            (r["id"],)).fetchone()
            if was:
                c.execute("INSERT OR IGNORE INTO processed(msg_id, processed_at) "
                          "VALUES (?,?)", (keep["id"], was[0]))
            c.execute("DELETE FROM emails WHERE id=?", (r["id"],))
            removed += 1
    c.commit()
    print(f"dedup: {len(groups)} duplicated Message-IDs, {removed} rows removed")


if __name__ == "__main__":
    main()
