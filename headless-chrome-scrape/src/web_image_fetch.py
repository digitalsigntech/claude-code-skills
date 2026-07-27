#!/usr/bin/env python3
"""Fetch images from WAF-protected websites via headless Chrome (CDP).

curl/wget get blocked by TLS-fingerprint WAFs (e.g. cpcworldwide.com); this
loads the page in real Chrome and captures matching image bytes off the wire
(Network.getResponseBody), so CORS and fingerprinting don't matter.

Usage:
  web_image_fetch.py <page-url> <match-substring> <out.jpg> [extra-image-url]

  match-substring : case-insensitive substring of the image URL to capture
  extra-image-url : optional full-size image URL to force-load after the page
"""
import json, subprocess, time, base64, sys, urllib.request
import websocket

def main():
    if len(sys.argv) < 4:
        print(__doc__); sys.exit(1)
    page, match, out = sys.argv[1], sys.argv[2].upper(), sys.argv[3]
    extra = sys.argv[4] if len(sys.argv) > 4 else None
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    chrome = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--remote-debugging-port=9222", "--remote-allow-origins=*",
         f"--user-agent={UA}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(30):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
                if tabs: break
            except Exception:
                time.sleep(0.5)
        ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=90)
        mid, events = [0], []
        def cmd(method, params=None):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid[0]:
                    return m.get("result", {})
                events.append(m)
        def drain(seconds):
            ws.settimeout(seconds)
            try:
                while True:
                    events.append(json.loads(ws.recv()))
            except Exception:
                pass
            ws.settimeout(90)
        cmd("Network.enable"); cmd("Page.enable")
        cmd("Page.navigate", {"url": page})
        drain(15)
        if extra:
            cmd("Runtime.evaluate", {"expression":
                f"var i=new Image(); i.src={json.dumps(extra)}; document.body.appendChild(i); 1"})
            drain(8)
        best = None
        for e in events:
            if e.get("method") == "Network.responseReceived":
                u = e["params"]["response"]["url"]
                if match in u.upper():
                    try:
                        b = cmd("Network.getResponseBody", {"requestId": e["params"]["requestId"]})
                        if not b.get("base64Encoded"):
                            continue
                        d = base64.b64decode(b["body"])
                        # accept JPEG/PNG/WebP magic only
                        if (d[:3] == b"\xff\xd8\xff" or d[:8] == b"\x89PNG\r\n\x1a\n"
                                or d[8:12] == b"WEBP") and (best is None or len(d) > len(best[0])):
                            best = (d, u)
                    except Exception:
                        pass
        if not best:
            print("FAILED: no image response matched", match); sys.exit(2)
        open(out, "wb").write(best[0])
        print("OK", len(best[0]), "bytes", best[1], "->", out)
    finally:
        chrome.terminate()

if __name__ == "__main__":
    main()
