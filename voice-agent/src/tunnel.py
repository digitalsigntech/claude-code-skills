#!/usr/bin/env python3
"""Give an agent behind NAT a public HTTPS address, and keep it paired.

Most machines worth talking to — a laptop, a home server, an appliance on an
office LAN — have no public IP and no inbound ports. The plane can only call an
agent it can reach, so those machines need an address that belongs to somebody
else's network.

This runs a Cloudflare quick tunnel: an OUTBOUND connection from this machine to
Cloudflare, which hands back an `https://<random>.trycloudflare.com` URL and
forwards it to the local agent. Nothing listens on a public address here, and no
router or firewall is touched.

    python3 tunnel.py                # tunnel + pair + stay up

A quick tunnel's URL changes every time it restarts, so this watches the tunnel
and re-registers with the plane whenever the address moves. That is the whole
reason this is a supervisor and not a one-line `cloudflared` invocation.

Requires `cloudflared` on PATH (https://developers.cloudflare.com/cloudflare-one/
connections/connect-networks/downloads/). For a stable address, use a named
tunnel instead and pass its hostname to pair.py directly.
"""
import json, pathlib, re, shutil, signal, subprocess, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pair  # noqa: E402

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_stop = threading.Event()


def cfg():
    try:
        return json.loads((HERE / "config.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def agent_alive(port):
    import urllib.error, urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return json.load(r).get("service") == "voice-agent"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def run():
    c = cfg()
    port = int(c.get("port", 8787))
    api, token, secret = c.get("api"), c.get("token"), c.get("secret")
    if not (api and token and secret):
        raise SystemExit("pair.py needs --api and --token once before the tunnel can register")

    if not shutil.which("cloudflared"):
        raise SystemExit(
            "cloudflared not found. Install it (it is a single binary), or expose the "
            "agent yourself and use pair.py --url instead.")

    if not agent_alive(port):
        print(f"[tunnel] WARNING: nothing answering on 127.0.0.1:{port} — "
              f"start voice_agent.py first, or the plane will probe an empty address.",
              file=sys.stderr)

    while not _stop.is_set():
        print("[tunnel] starting cloudflared…", flush=True)
        p = subprocess.Popen(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        url = None
        for line in p.stdout:
            line = line.rstrip()
            if not url:
                m = URL_RE.search(line)
                if m:
                    url = m.group(0)
                    # Record it: the URL is the one thing an operator needs and
                    # the one thing a quick tunnel changes without warning.
                    (HERE / "tunnel_url.txt").write_text(url + "\n")
                    print(f"[tunnel] public URL: {url}", flush=True)
                    try:
                        pair.register(api, token, url, secret)
                        print("[tunnel] paired. Test from the app, or: "
                              "python3 pair.py --test", flush=True)
                    except SystemExit as e:
                        # Registration failing is not fatal to the tunnel: the URL
                        # is up, and a retry on the next restart may succeed.
                        print(f"[tunnel] pairing failed: {e}", file=sys.stderr, flush=True)
            if _stop.is_set():
                break

        p.wait()
        if _stop.is_set():
            break
        print("[tunnel] cloudflared exited — restarting in 5s "
              "(a new URL will be registered automatically)", file=sys.stderr, flush=True)
        time.sleep(5)


def _bye(*_):
    _stop.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)
    run()
