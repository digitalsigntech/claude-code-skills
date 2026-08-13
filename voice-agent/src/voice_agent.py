#!/usr/bin/env python3
"""Voice agent adapter — connect a Claude Code machine to the voice plane.

The plane POSTs protocol messages to this server; each one is answered from the
machine the agent actually runs on. A spoken question becomes a real Claude turn
in your project directory, so the answer comes out of your own files.

    python3 voice_agent.py            # serve (reads config.json beside this file)
    python3 voice_agent.py --check    # print health as the plane would see it

Protocol (POST /, JSON, `Authorization: Bearer <secret>`):

    {"v":1, "account":"…", "account_name":"…", "type":"capabilities"}
        -> {"capabilities": ["ask", "health", "branding", "file"]}
    {"v":1, …, "type":"health"}
        -> {"ok": true}                       agent up and signed in
        -> {"ok": false, "signed_out": true, "detail": "…"}
    {"v":1, …, "type":"ask", "question":"…"}
        -> {"answer": "…"}
        -> {"answer": "", "agent_error": "signed_out", "detail": "…"}

`branding` is the app's identity panel — the user's name, the company, the agent's
own name and logo. It is configuration, not code: an agent that does not set it gets
the app's generic assistant, which is why an install for a company must.

`health` must never cost a model turn — it is a file read, so a connection test
stays instant. Unknown types get HTTP 400, which the plane reads as "this agent
speaks ask only" rather than as a failure.

Sessions are per account: the first turn opens one, later turns resume it, so a
conversation over voice keeps its thread.
"""
import argparse, base64, hashlib, json, mimetypes, os, pathlib, re, secrets, shutil, \
    subprocess, sys, threading, time
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


# ---------------------------------------------------------------- identity
MEDIA = {}                              # token -> absolute path, minted here only
IDENTITY_TTL = 7 * 86400                # re-derive weekly; names change slowly
_identity_lock = threading.Lock()

LOGO_DIRS = ("brand", "branding", "assets", "static", "public", "img", "images",
             "media", "knowledge-base/company/assets", "docs")
LOGO_EXT = (".png", ".jpg", ".jpeg", ".webp")

IDENTITY_PROMPT = """\
Output ONE JSON object and nothing else. No prose, no code fence.

You are being asked to describe yourself for the identity panel of a voice app
that speaks with your user. Answer ONLY from files inside this project directory —
CLAUDE.md, README, company or brand files. Not from your own account, not from
global configuration, not from any conversation you remember, not from general
knowledge. If this project does not state a fact, it is null, even if you believe
you know it: the wrong person's name on a stranger's phone is the failure here.

ONE EXCEPTION, and only for agent_name: your own name is something users say rather
than write down, so the addresses listed at the end of this prompt (taken from this
project's own conversation logs) count as evidence for it. Nothing else may come
from them.

{"agent_name": "the name your user calls YOU. If the candidates listed below are
                present, choose the one that is actually your name and not a person
                you work with; otherwise take it from this project's files. null if
                you have no name of your own here",
 "company_name": "the company or organisation whose work lives here; null if none",
 "user_name": "the full name of the person you work for here; null if unknown",
 "user_email": "their email address; null if unknown",
 "logo_path": "absolute path to this company's logo image on this machine
               (png/jpg/webp, not svg); null if there is none"}

Use null, never a guess or a placeholder. It is better to say null than to invent
a name a real person will see on their phone."""


def scan_logo(workdir):
    """A logo file, found rather than configured. Preferred directories first,
    then anything named like a logo — the file is nearly always sitting in the
    project already, and asking a human to type its path is how it stays unset."""
    root = pathlib.Path(workdir)
    for sub in LOGO_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix.lower() in LOGO_EXT and "logo" in p.name.lower():
                return str(p.resolve())
    try:
        for p in sorted(root.rglob("*logo*")):
            if p.suffix.lower() in LOGO_EXT and ".git" not in p.parts:
                return str(p.resolve())
    except OSError:
        pass
    return ""


SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
             "dist", "build", ".next", "target"}

# Words that open a sentence and look exactly like a name in vocative position.
NOT_A_NAME = {
    "yes", "no", "ok", "okay", "also", "and", "but", "so", "then", "now", "well",
    "please", "thanks", "thank", "good", "great", "sorry", "actually", "just",
    "still", "next", "first", "second", "last", "times", "note", "todo", "done",
    "fix", "add", "make", "run", "check", "read", "write", "send", "use", "let",
    "can", "could", "would", "should", "did", "does", "is", "are", "was", "the",
    "this", "that", "there", "here", "what", "why", "how", "when", "where", "who",
    "placeholder", "example", "test", "hi", "hey", "hello", "morning", "evening",
    # Header-ish words: pasted mail and logs are full of "Subject:", "From:",
    # "For, ..." and they sit in exactly the position a name sits in.
    "subject", "from", "to", "date", "re", "fwd", "cc", "bcc", "summary", "for",
    "mobile", "phone", "email", "sent", "sincerely", "regards", "best", "dear",
}
VOCATIVE = (
    re.compile(r"^\s*(?:hi|hey|hello|thanks|thank you|ok|okay)[,! ]+([A-Z][a-z]{1,15})\b", re.M),
    # Comma only, never a colon: "Subject: …" and "Note: …" sit in exactly the
    # position a name sits in, and a pasted email would out-vote the real answer.
    re.compile(r"^\s*([A-Z][a-z]{1,15}),\s", re.M),
    re.compile(r"\b(?:thanks|thank you|cheers)[,! ]+([A-Z][a-z]{1,15})\b", re.I),
)
# Being told outright beats being addressed: rare, but decisive when present.
NAMED = (
    re.compile(r"\byou(?:'re| are)\s+(?:called\s+)?([A-Z][a-z]{1,15})\b"),
    re.compile(r"\byour name is\s+([A-Z][a-z]{1,15})\b", re.I),
    re.compile(r"\bwe(?:'ll| will)? call you\s+([A-Z][a-z]{1,15})\b", re.I),
)


def _project_slug(workdir):
    """Claude Code stores a project's sessions under a path-derived directory."""
    return "-" + re.sub(r"[^A-Za-z0-9]+", "-", os.path.abspath(workdir)).strip("-")


def address_candidates(workdir, max_messages=600):
    """Names the USER uses to address this agent, counted from its own sessions.

    An agent's name is the one identity fact that is never written down: it is
    established by being used. "You are Max" appears in no file on Max's machine,
    and asking the operator to add it is asking them to configure the thing we
    said would configure itself. The transcripts are where it does exist.

    Counted here rather than judged by a model: a count is evidence, and it is
    the difference between reading a name and inventing one."""
    root = pathlib.Path.home() / ".claude" / "projects" / _project_slug(workdir)
    if not root.is_dir():
        return {}
    hits, seen = {}, 0
    for f in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            for line in f.open(errors="ignore"):
                if seen >= max_messages:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content
                                       if isinstance(c, dict))
                if not isinstance(content, str) or not content.strip():
                    continue
                if "identity panel of a voice app" in content:
                    # Our own derivation turn, logged like any other user message.
                    # Left in, the names it lists become evidence for the next run
                    # citing itself — a fact that gets truer every week without
                    # anyone ever saying it again.
                    continue
                seen += 1
                for rx, weight in [(r, 1) for r in VOCATIVE] + [(r, 10) for r in NAMED]:
                    for name in rx.findall(content):
                        name = name.strip().title()
                        if name.lower() in NOT_A_NAME:
                            continue
                        h = hits.setdefault(name, {"count": 0, "sessions": set(),
                                                   "samples": []})
                        h["count"] += weight
                        h["sessions"].add(f.name)
                        if len(h["samples"]) < 2:
                            h["samples"].append(" ".join(content.split())[:120])
        except OSError:
            continue
        if seen >= max_messages:
            break
    for h in hits.values():
        h["sessions"] = len(h["sessions"])
    # Spread across sessions before raw count: a name the user comes back to is
    # the agent's; a name that spikes inside one pasted email is a colleague's.
    return dict(sorted(hits.items(),
                       key=lambda kv: (-kv[1]["sessions"], -kv[1]["count"])))


def attested(value, workdir, max_files=2000, max_bytes=2_000_000):
    """Does this string actually appear in the project's own files?

    The check that turns a model's answer into evidence. Cheap, and it only ever
    removes: a name the project never writes down does not go on the panel."""
    needle = value.strip().lower()
    if len(needle) < 2:
        return False
    seen = 0
    for dp, dn, fn in os.walk(workdir):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        for f in fn:
            if seen >= max_files:
                return False
            p = os.path.join(dp, f)
            try:
                if os.path.getsize(p) > max_bytes:
                    continue
                seen += 1
                with open(p, "r", errors="ignore") as fh:
                    if needle in fh.read().lower():
                        return True
            except OSError:
                continue
    return False


def derive_identity(timeout=180):
    """Ask the agent who it is, once, and cache the answer.

    Everything here is knowable on this machine — the agent's own name lives in
    CLAUDE.md, the company in its files, the user in the work they do together —
    so requiring an operator to type any of it is asking for the one thing they
    will skip. The agent reads its own project and answers.

    NEVER on the request path: the plane relays `branding` with a 15s timeout and
    a cold model turn takes longer, so this runs at startup and from pair.py, and
    the panel is served from cache."""
    cfg = config()
    exe = claude_bin()
    if not exe:
        return {}
    workdir = os.path.expanduser(cfg["workdir"])
    env = dict(os.environ)
    if os.geteuid() == 0:
        env.setdefault("IS_SANDBOX", "1")

    # Names the user has actually used to address this agent, counted from its own
    # sessions. Offered as candidates, never as the answer: the model picks which
    # one is its name, and can only pick one that was really said.
    cands = address_candidates(workdir)
    prompt = IDENTITY_PROMPT
    if cands:
        listed = "\n".join(
            f"- {n}: addressed {h['count']}x across {h['sessions']} session(s), "
            f"e.g. {' | '.join(repr(x) for x in h['samples'])}"
            for n, h in list(cands.items())[:5])
        prompt += (
            "\n\nEvidence for agent_name — names used in an address position in this "
            f"project's own conversation logs:\n{listed}\n\n"
            "Every message in those logs was written TO you, so a name in the "
            "greeting position is usually yours. It is NOT yours when the line is "
            "quoted or forwarded text, an email pasted in, or a message about a "
            "colleague rather than to you — read the samples and judge. Recurring "
            "across several sessions is the strongest sign; a single mention inside "
            "pasted content is the weakest. Put your choice in agent_name; this "
            "evidence outranks the files-only rule above. If none of them is you, "
            "agent_name is null.")

    cmd = [exe, "-p", prompt, "--dangerously-skip-permissions"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode != 0 and "cannot be used with root" in (r.stdout or "") + (r.stderr or ""):
            r = subprocess.run([c for c in cmd if c != "--dangerously-skip-permissions"],
                               cwd=workdir, capture_output=True, text=True,
                               timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        got = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}

    ident = {}
    for k in ("agent_name", "company_name", "user_name", "user_email"):
        v = got.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "unknown"):
            v = v.strip()[:120]
            # Attested by the project or dropped. Asked who its user is, an agent
            # with nothing to read will answer from the account the CLI is signed
            # in as — which on a shared build is a real person with no connection
            # to this install. A fact the project cannot show in writing is not a
            # fact about this project.
            #
            # Its own name is the exception, and it has to be: nobody writes down
            # what they call their agent, they just call it that. The transcripts
            # are the record, so a name the user has actually used counts as
            # written down.
            if k == "agent_name" and v in cands:
                ident[k] = v
            elif attested(v, workdir):
                ident[k] = v
            else:
                print(f"[voice-agent] identity: dropping {k}={v!r} — not found in "
                      f"{workdir}", file=sys.stderr, flush=True)
    logo = got.get("logo_path")
    logo = logo if isinstance(logo, str) and os.path.exists(os.path.expanduser(logo or "")) else ""
    # The filesystem is the more reliable witness of its own contents: a model
    # asked for a path will occasionally produce a plausible one that is not there.
    ident["logo"] = os.path.expanduser(logo) if logo else scan_logo(workdir)
    return {k: v for k, v in ident.items() if v}


def cached_identity():
    st = load(STATE, {})
    return st.get("identity") or {}


def ensure_identity(force=False, timeout=180):
    """Derive if missing or stale. Returns what the panel should show."""
    st = load(STATE, {})
    age = time.time() - float(st.get("identity_ts") or 0)
    if not force and st.get("identity") and age < IDENTITY_TTL:
        return st["identity"]
    if not _identity_lock.acquire(blocking=False):
        return st.get("identity") or {}       # a refresh is already running
    try:
        ident = derive_identity(timeout=timeout)
        if ident:
            with _lock:
                st = load(STATE, {})
                st["identity"] = ident
                st["identity_ts"] = time.time()
                save(STATE, st)
            return ident
        return st.get("identity") or {}
    finally:
        _identity_lock.release()


def media_token(path):
    """Stable token for a file: same path, same token, across restarts.

    The plane never receives bytes it did not ask for — it gets a token in the
    branding panel and fetches it back through `file`, like any attachment."""
    cfg = config()
    tok = hashlib.sha256((cfg["secret"] + "|" + os.path.abspath(path))
                         .encode()).hexdigest()[:32]
    MEDIA[tok] = os.path.abspath(path)
    return tok


def branding():
    """The identity panel the app shows: who is speaking, for whom, and the logo.

    Derived, not configured. Without it the app shows a blank name and a generic
    assistant — the honest look for a machine nobody has set up, and the wrong one
    for an install that had every fact available on disk the whole time. config.json
    still wins where it is set, for the operator who wants a different answer than
    the true one."""
    cfg = config()
    ident = cached_identity()
    b = {}
    for key, field in (("agent_name", "bot_name"), ("company_name", "company_name"),
                       ("user_name", "user_name"), ("user_email", "user_email")):
        val = str(cfg.get(key) or ident.get(key) or "").strip()
        if val:
            b[field] = val
    logo = os.path.expanduser(str(cfg.get("logo") or ident.get("logo") or "").strip())
    if logo and os.path.exists(logo):
        b["logo_token"] = media_token(logo)
    return b


def capabilities():
    """Declared from what this install can actually answer. `branding` is claimed
    whenever a panel exists — including one derived a moment ago, which is why the
    derivation runs before pair.py registers rather than after."""
    caps = ["ask", "health"]
    b = branding()
    if b:
        caps.append("branding")
    if b.get("logo_token"):
        caps.append("file")
    return caps


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
            return self._send(200, {"capabilities": capabilities()})
        if kind == "health":
            return self._send(200, health())
        if kind == "branding":
            b = branding()
            if not b:
                # No identity configured. Answering with an empty panel would
                # have the app render blanks; the plane's 404 makes it fall back
                # to its own plain panel, which is the honest look for "unset".
                return self._send(404, {"error": "no branding"})
            return self._send(200, b)
        if kind == "file":
            # Only paths this process minted a token for are servable — the
            # plane asking for a file is not authority to read arbitrary ones.
            path = MEDIA.get(str(d.get("token") or ""))
            if not path or not os.path.exists(path):
                return self._send(404, {"error": "no such token"})
            if os.path.getsize(path) > 25 * 1024 * 1024:
                return self._send(413, {"error": "file too large for relay"})
            with open(path, "rb") as f:
                blob = f.read()
            return self._send(200, {
                "b64": base64.b64encode(blob).decode(),
                "content_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "filename": os.path.basename(path)})
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


def _identity_worker():
    """Keep the panel current in the background. It costs a model turn, so it
    never runs while the plane is waiting on a request — the panel is always
    served from cache, and this is what fills the cache."""
    ident = ensure_identity()
    if ident:
        print(f"[voice-agent] identity: {ident.get('agent_name') or '?'} "
              f"at {ident.get('company_name') or '?'} "
              f"for {ident.get('user_name') or '?'}"
              + (f", logo {os.path.basename(ident['logo'])}" if ident.get("logo") else ""),
              flush=True)
    else:
        print("[voice-agent] identity: could not derive one — the app will show its "
              "generic panel. Set agent_name/company_name/user_name in config.json "
              "to override.", flush=True)


def _qr_sweeper():
    """Take posted QR codes back out of the chat when they expire.

    The adapter is the one thing on this machine that is definitely still running
    a quarter of an hour after an install — the installing agent has moved on and
    pair.py exited long ago. So the deletion belongs here, not in whatever posted
    the image."""
    sys.path.insert(0, str(HERE))
    try:
        import qr_send
    except ImportError:
        return
    while True:
        try:
            qr_send.sweep()
        except Exception as e:
            print(f"[voice-agent] qr sweep failed: {e}", file=sys.stderr, flush=True)
        time.sleep(30)


def serve():
    cfg = config()
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    print(f"[voice-agent] listening on {cfg['bind']}:{cfg['port']}  "
          f"workdir={os.path.expanduser(cfg['workdir'])}", flush=True)
    h = health()
    if not h["ok"]:
        print(f"[voice-agent] WARNING: {h.get('detail')}", flush=True)
    threading.Thread(target=_identity_worker, daemon=True).start()
    threading.Thread(target=_qr_sweeper, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="print health and exit")
    ap.add_argument("--identity", action="store_true",
                    help="derive the identity panel now and print it")
    a = ap.parse_args()
    if a.check:
        print(json.dumps({**health(), "claude": claude_bin(),
                          "workdir": os.path.expanduser(config()["workdir"]),
                          "branding": branding()}, indent=2))
    elif a.identity:
        print(json.dumps(ensure_identity(force=True), indent=2))
    else:
        serve()
