#!/usr/bin/env python3
"""Render the Agent Voice Mode pairing QR for this connector.

Reads url.txt (written by start.sh) + connector.json and emits the payload the
iOS app's scanner understands:

    {"v": 1, "type": "webhook", "url": "https://…/<path>/hook", "secret": "…"}

Default: draws the QR in the terminal (qrencode -t ANSIUTF8) AND writes
pairing-qr.png next to this script, so an agent can also send the PNG to its
user over whatever channel it has. `--payload` prints the JSON only.
"""
import json
import os
import subprocess
import sys

DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    conf = json.load(open(os.path.join(DIR, "connector.json")))
    base = open(os.path.join(DIR, "url.txt")).read().strip().rstrip("/")
    payload = json.dumps({"v": 1, "type": "webhook",
                          "url": f"{base}/{conf['path']}/hook",
                          "secret": conf["secret"]},
                         separators=(",", ":"))
    if "--payload" in sys.argv:
        print(payload)
        return
    png = os.path.join(DIR, "pairing-qr.png")
    subprocess.run(["qrencode", "-o", png, "-s", "8", payload], check=True)
    subprocess.run(["qrencode", "-t", "ANSIUTF8", payload], check=True)
    print(f"\nScan with Agent Voice Mode → Settings → Scan QR.\n"
          f"PNG copy: {png}\n"
          f"(The QR contains the webhook secret — share it only with the "
          f"person pairing.)")


if __name__ == "__main__":
    main()
