#!/usr/bin/env python3
"""CLI entry point for the privacy router — what a chat gateway shells out to.

  privacy_route.py "<query>" --json --answer --history-stdin

--answer        skip classification; answer privately via the tool-calling agent
                (the gateway already classified intent with intent.py)
--history-stdin read recent conversation history (plain text) from stdin — REQUIRED
                for a useful answer; without it the model can't resolve references
Output JSON: {"decision": "private", "answer": "...", "files": [{path, caption}, ...]}
"files" lists documents the agent queued via send_file — the CALLER (gateway)
uploads them into the chat after delivering the text answer.

Without --answer it classifies first: {"decision": "public"|"private", "reason": ...}
(private also gets an answer). Fail closed: errors surface as non-zero exit / no
answer — the CALLER must treat that as private-but-unanswered, never cloud-escalate.
"""
import sys, json, argparse

import intent
import private_agent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--answer", action="store_true",
                    help="skip classification; just answer privately via the agent loop")
    ap.add_argument("--history-stdin", action="store_true",
                    help="read recent conversation history (plain text) from stdin")
    ap.add_argument("--sender", default="the owner",
                    help="display name of the person who sent the message")
    ap.add_argument("--chat-id", type=int, default=None,
                    help="chat the question came from (scheduled reminders fire there)")
    a = ap.parse_args()
    history = sys.stdin.read() if a.history_stdin else ""
    if a.answer:
        ans, files = private_agent.run(a.query, history, chat_id=a.chat_id)
        out = {"decision": "private", "reason": "forced-answer",
               "answer": ans, "files": files}
    else:
        priv, why = intent.is_private(a.query)
        out = {"decision": "private" if priv else "public", "reason": why}
        if priv:
            out["answer"], out["files"] = private_agent.run(a.query, history,
                                                            chat_id=a.chat_id)
    print(json.dumps(out) if a.json else json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
