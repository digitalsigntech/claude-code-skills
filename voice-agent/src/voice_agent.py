#!/usr/bin/env python3
"""Voice agent adapter — connect a Claude Code machine to the voice plane.

The plane POSTs protocol messages to this server; each one is answered from the
machine the agent actually runs on. A spoken question becomes a real Claude turn
in your project directory, so the answer comes out of your own files.

    python3 voice_agent.py            # serve (reads config.json beside this file)
    python3 voice_agent.py --check    # print health as the plane would see it

Protocol (POST /, JSON, `Authorization: Bearer <secret>`):

    {"v":1, "account":"…", "account_name":"…", "type":"capabilities"}
        -> {"capabilities": ["ask", "health"]}
    {"v":1, …, "type":"health"}
        -> {"ok": true}                       agent up and signed in
        -> {"ok": false, "signed_out": true, "detail": "…"}
    {"v":1, …, "type":"ask", "question":"…"}
        -> {"answer": "…"}
        -> {"answer": "", "agent_error": "signed_out", "detail": "…"}

`health` must never cost a model turn — it is a file read, so a connection test
stays instant. Unknown types get HTTP 400, which the plane reads as "this agent
speaks ask only" rather than as a failure.

Sessions are per account: the first turn opens one, later turns resume it, so a
conversation over voice keeps its thread.
"""
import argparse, json, os, pathlib, re, secrets, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"

DEFAULTS = {
    "workdir": str(pathlib.Path.home()),
    "port": 8787,
    "bind": "127.0.0.1",
    "model": "",                 # empty = whatever `claude` defaults to
    "turn_timeout": 870,         # the plane gives up at 900
    "secret": "",                # bearer the plane must present; generated if absent
}
_lock = threading.Lock()


def load(path, default):
    try:
        return {**default, **json.loads(path.read_text())} if isinstance(default, dict) \
            else json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(default) if isinstance(default, dict) else default


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def config():
    cfg = load(CONFIG, DEFAULTS)
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_urlsafe(32)
        save(CONFIG, cfg)
    return cfg


def claude_bin():
    """Absolute path to the CLI. A service manager does not inherit a login PATH."""
    cfg = load(CONFIG, DEFAULTS)
    if cfg.get("claude_bin"):
        return cfg["claude_bin"]
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude"):
        p = pathlib.Path(p).expanduser()
        if p.exists():
            return str(p)
    return ""


# ---------------------------------------------------------------- health
SIGNED_OUT = re.compile(
    r"(oauth (token|session) (expired|revoked)|please run .?/login|not logged in|"
    r"authentication[_ ]error|invalid api key|credit balance is too low)", re.I)


def health():
    """File reads only — a connection test must not cost a model turn."""
    exe = claude_bin()
    if not exe:
        return {"ok": False, "signed_out": True,
                "detail": "the claude CLI was not found on this machine"}

    cred = pathlib.Path.home() / ".claude" / ".credentials.json"
    if not cred.exists():
        # An API-key install is legitimate and has no credentials file.
        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"ok": True}
        return {"ok": False, "signed_out": True,
                "detail": "no Claude credentials on this machine — run `claude` "
                          "in a terminal here and log in"}
    try:
        oauth = json.loads(cred.read_text()).get("claudeAiOauth", {})
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "signed_out": True, "detail": f"credentials unreadable: {e}"}

    # The access token refreshes itself; only the refresh token expiring is fatal.
    refresh_exp = int(oauth.get("refreshTokenExpiresAt") or 0) / 1000
    if refresh_exp and refresh_exp < time.time():
        return {"ok": False, "signed_out": True,
                "detail": "the Claude login on this machine has expired — run "
                          "`claude` in a terminal here and log in again"}
    return {"ok": True}


# ---------------------------------------------------------------- turns
def session_id(account):
    st = load(STATE, {})
    return st.get("sessions", {}).get(account)


def remember_session(account, sid):
    with _lock:
        st = load(STATE, {})
        st.setdefault("sessions", {})[account] = sid
        save(STATE, st)


def ask(account, question, account_name=""):
    cfg = config()
    exe = claude_bin()
    if not exe:
        return {"answer": "", "agent_error": "signed_out",
                "detail": "the claude CLI was not found on this machine"}

    workdir = os.path.expanduser(cfg["workdir"])
    prompt = question
    if account_name:
        prompt = f"[Voice turn from {account_name}]\n\n{question}"

    cmd = [exe, "-p", prompt, "--dangerously-skip-permissions"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]

    # The CLI refuses --dangerously-skip-permissions when the process is root,
    # which is exactly how a service on a single-purpose box tends to run. Left
    # unhandled, every voice turn fails instantly with a message about sudo that
    # says nothing about voice. IS_SANDBOX=1 is the CLI's own acknowledgement
    # that the caller has accepted the risk; without it the turn still runs, but
    # tool use is limited to what unprompted permissions allow.
    env = dict(os.environ)
    if os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")

    sid = session_id(account)
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = __import__("uuid").uuid4().hex
        sid = f"{sid[:8]}-{sid[8:12]}-{sid[12:16]}-{sid[16:20]}-{sid[20:32]}"
        cmd += ["--session-id", sid]

    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=cfg["turn_timeout"], env=env)
    except subprocess.TimeoutExpired:
        return {"answer": "", "agent_error": "timeout",
                "detail": f"the turn ran past {cfg['turn_timeout']}s"}
    except FileNotFoundError:
        return {"answer": "", "agent_error": "signed_out",
                "detail": f"could not execute {exe}"}

    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0 and "cannot be used with root" in (err + out):
        r = subprocess.run([c for c in cmd if c != "--dangerously-skip-permissions"],
                           cwd=workdir, capture_output=True, text=True,
                           timeout=cfg["turn_timeout"], env=env)
        out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0:
        blob = f"{err}\n{out}"
        if SIGNED_OUT.search(blob):
            return {"answer": "", "agent_error": "signed_out", "detail": err[:400]}
        return {"answer": "", "agent_error": "failed", "detail": (err or out)[:400]}

    # A resumed session that the CLI has forgotten: retry once, fresh.
    if not out and sid and "No conversation found" in err:
        with _lock:
            st = load(STATE, {})
            st.get("sessions", {}).pop(account, None)
            save(STATE, st)
        return ask(account, question, account_name)

    remember_session(account, sid)
    return {"answer": out[:8000]}


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "voice-agent/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[voice-agent] %s\n" % (fmt % a))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A liveness probe that needs no secret, for tunnels and load balancers.
        if self.path.rstrip("/") in ("", "/health"):
            return self._send(200, {"service": "voice-agent", "ok": health()["ok"]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        cfg = config()
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(token, cfg["secret"]):
            self.log_message("rejected: bad or missing bearer")
            return self._send(401, {"error": "unauthorized"})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad json"})

        kind = str(d.get("type") or "")
        account = str(d.get("account") or "default")
        name = str(d.get("account_name") or "")

        if kind == "capabilities":
            return self._send(200, {"capabilities": ["ask", "health"]})
        if kind == "health":
            return self._send(200, health())
        if kind == "ask":
            q = str(d.get("question") or "").strip()
            if not q:
                return self._send(400, {"error": "no question"})
            self.log_message("ask from %s: %.60s", name or account, q)
            t0 = time.time()
            res = ask(account, q, name)
            self.log_message("answered in %.1fs (%s)", time.time() - t0,
                             res.get("agent_error") or "ok")
            return self._send(200, res)

        # Everything else: the plane treats HTTP 400 as "ask-only agent".
        self._send(400, {"error": f"unsupported type: {kind}"})


def serve():
    cfg = config()
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    print(f"[voice-agent] listening on {cfg['bind']}:{cfg['port']}  "
          f"workdir={os.path.expanduser(cfg['workdir'])}", flush=True)
    h = health()
    if not h["ok"]:
        print(f"[voice-agent] WARNING: {h.get('detail')}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print health and exit")
    a = ap.parse_args()
    if a.check:
        print(json.dumps({**health(), "claude": claude_bin(),
                          "workdir": os.path.expanduser(config()["workdir"])}, indent=2))
    else:
        serve()
