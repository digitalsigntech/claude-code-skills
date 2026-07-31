#!/usr/bin/env python3
"""Voice agent connector — lets the Agent Voice Mode iOS app talk to YOUR agent.

The hosted voice service relays spoken questions to this webhook; this
connector runs your local agent (Claude Code by default) and returns its
answer. No OpenAI/voice code lives here — the voice session itself is minted
and billed by the hosted service; this piece only answers questions.

Protocol (what the voice service sends):
    POST /<path>/hook
    Authorization: Bearer <secret>
    {"v": 1, "type": "ask", "question": "..."}
Response: {"answer": "..."}   (answer within ~110 s)

Config: ./connector.json — created on first run:
    {"secret": "<64 hex>", "path": "<32 hex>",
     "agent_cmd": ["claude", "--continue", "-p", "{question}"],
     "agent_cmd_fresh": ["claude", "-p", "{question}"]}
`agent_cmd` runs first; if it exits non-zero (e.g. Claude Code's --continue
with no prior conversation), `agent_cmd_fresh` is the fallback. Swap the
commands to plug in any other agent — the contract is just: argv with
`{question}` substituted, answer on stdout.

Run `./start.sh` instead of this file directly — it also brings up the HTTPS
tunnel and prints the pairing QR.
"""
import hmac
import json
import os
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.path.join(DIR, "connector.json")
PORT = int(os.environ.get("CONNECTOR_PORT", "8484"))
AGENT_TIMEOUT_S = 110          # the voice service gives up at 120 s


def load_config():
    if not os.path.exists(CONF_PATH):
        conf = {"secret": secrets.token_hex(32),
                "path": secrets.token_hex(16),
                "agent_cmd": ["claude", "--continue", "-p", "{question}"],
                "agent_cmd_fresh": ["claude", "-p", "{question}"]}
        with open(CONF_PATH, "w") as f:
            json.dump(conf, f, indent=1)
        os.chmod(CONF_PATH, 0o600)
        print("[connector] new config + secret written to connector.json",
              flush=True)
    return json.load(open(CONF_PATH))


CONF = load_config()


def run_agent(question):
    def attempt(argv):
        argv = [a.replace("{question}", question) for a in argv]
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=AGENT_TIMEOUT_S, cwd=DIR)
    p = attempt(CONF["agent_cmd"])
    if p.returncode != 0 and CONF.get("agent_cmd_fresh"):
        p = attempt(CONF["agent_cmd_fresh"])
    if p.returncode != 0:
        raise RuntimeError(f"agent exited {p.returncode}: "
                           f"{(p.stderr or '')[:200]}")
    return p.stdout.strip()[:8000]


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.rstrip("/") != f"/{CONF['path']}/hook":
            return self._send(404, {"error": "not found"})
        got = (self.headers.get("Authorization") or "")[7:]
        if not hmac.compare_digest(CONF["secret"], got):
            self.log_message("REJECTED (bad secret)")
            return self._send(401, {"error": "unauthorized"})
        n = min(int(self.headers.get("Content-Length", 0)), 65536)
        try:
            d = json.loads(self.rfile.read(n))
        except Exception:
            return self._send(400, {"error": "bad body"})
        t = d.get("type", "ask")
        if t == "capabilities":
            # Handshake: the voice service asks what this connector supports
            # and the app shows only those features. A CLI agent is ask-only;
            # richer connectors may add groups/attachments/photo/file — see
            # the protocol notes in README.md.
            return self._send(200, {"capabilities": ["ask"]})
        if t != "ask":
            return self._send(400, {"error": f"unsupported type {t!r}"})
        q = str(d.get("question", ""))[:4000]
        if not q:
            return self._send(400, {"error": "no question"})
        self.log_message("ask: %.80r", q)
        try:
            self._send(200, {"answer": run_agent(q)})
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "agent timed out"})
        except Exception as e:
            self.log_message("agent FAILED: %s", e)
            self._send(502, {"error": "agent failed"})

    def log_message(self, fmt, *args):
        print("[connector]", fmt % args, flush=True)


if __name__ == "__main__":
    if "--print-config" in sys.argv:      # used by qr.py / start.sh
        print(json.dumps({"path": CONF["path"], "secret": CONF["secret"]}))
        sys.exit(0)
    print(f"[connector] listening on 127.0.0.1:{PORT} "
          f"path=/{CONF['path']}/hook", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
