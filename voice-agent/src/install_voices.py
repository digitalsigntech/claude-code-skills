#!/usr/bin/env python3
"""Install the local tier's named voices — from upstream, per language.

    python3 install_voices.py --langs en,ru        # just those
    python3 install_voices.py --all                # all fourteen (~2.6 GB)
    python3 install_voices.py --langs de --dest /opt/voice-agent/voices

WHY NOT SHIP THEM. The rostered set is forty files and about 2.6 GB. Copying
that from us to each agent measured 1.3 MB/s and half an hour on the first
install; the files are public and sit on a CDN, so the agent fetches its own.
The roster names both the file and its upstream path, so nothing here has to
guess a URL from a filename — a parsing rule that works for `ru_RU-irina-medium`
and quietly mangles `pt_PT-tugão-medium` is the kind of thing that fails in one
language and is found by a customer.

WHY PER LANGUAGE. An agent that answers in two languages has no use for the
other twelve, and the model row reports what is INSTALLED rather than what the
roster promises — so a partial install is honest by construction. Ask for what
you will use.

WHAT PIPER DOES NOT SHIP. Japanese and Chinese need phonemizers that are not
part of piper: `pyopenjtalk` and `g2pW`. Without them the voice loads, produces
no audio, and fails with `wave.Error: # channels not specified` — an error about
the output file that says nothing about the cause. Requesting those languages
installs them, and the run ENDS BY SYNTHESISING with every voice it installed,
because this project has now shipped two counts that read filenames.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# Languages whose phonemizer is a separate package. `piper-tts[zh]` pulls g2pW.
EXTRAS = {"ja": ["pyopenjtalk"], "zh": ["piper-tts[zh]", "unicode_rbnf"]}


def fetch(path, dest):
    url = BASE + urllib.parse.quote(path)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)
    return os.path.getsize(dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dest", default="")
    ap.add_argument("--skip-verify", action="store_true")
    a = ap.parse_args()

    roster = json.load(open(os.path.join(HERE, "roster.json")))
    known = [k for k in roster if not k.startswith("_")]
    want = ([c.strip() for c in a.langs.split(",") if c.strip()]
            if a.langs else (known if a.all else []))
    if not want:
        sys.exit("choose --langs en,ru,... or --all "
                 f"(available: {' '.join(sorted(known))})")
    unknown = [c for c in want if c not in roster]
    if unknown:
        sys.exit(f"no voices rostered for: {' '.join(unknown)}")

    dest = a.dest or os.path.join(HERE, "voices")
    os.makedirs(dest, exist_ok=True)
    seen, total = set(), 0
    for code in want:
        for vid, e in roster[code].items():
            if e.get("alias") or not e.get("path"):
                continue              # an alias, or a Kokoro-only voice
            for path, name in ((e["path"], e["file"]),
                               (e["path"] + ".json", e["file"] + ".json")):
                if name in seen:
                    continue
                seen.add(name)
                out = os.path.join(dest, name)
                if os.path.exists(out) and os.path.getsize(out) > 1000:
                    print(f"  have {name}")
                    continue
                try:
                    n = fetch(path, out)
                except Exception as exc:                      # noqa: BLE001
                    print(f"  FAILED {name}: {exc}", file=sys.stderr)
                    continue
                total += n
                print(f"  got  {name}  {n / 1e6:.0f} MB")
        for pkg in EXTRAS.get(code, []):
            print(f"  pip install {pkg}   (phonemizer for {code})")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg],
                           check=False)

    # Copy the roster in beside the voices: voice_for() reads it from there.
    # MERGED WITH WHAT IS ALREADY THERE. Writing only this run's languages made
    # `--langs it` delete the Swedish entries installed an hour earlier: the
    # files stayed on disk and the picker lost them, which reads exactly like
    # voices that vanished. Caught by installing twice, which is the only way
    # it could have been caught.
    out_roster = os.path.join(dest, "roster.json")
    try:
        with open(out_roster) as f:
            merged = json.load(f)
    except (OSError, ValueError):
        merged = {}
    for k, v in roster.items():
        if k.startswith("_") or k in want:
            merged[k] = v
    with open(out_roster, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"\n{len(seen) // 2} voices in {dest}, {total / 1e6:.0f} MB fetched")

    if a.skip_verify:
        return 0
    # THE INSTALL PROVES ITSELF. A voice that downloaded is not a voice that
    # speaks, which is exactly how Japanese and Chinese were counted for a day.
    sys.path.insert(0, HERE)
    os.environ.setdefault("LQ_PIPER_VOICE", os.path.join(dest, "x.onnx"))
    import local_voice
    res = local_voice.verify_voices(force=True)
    bad = [n for n, v in res.items() if not v["ok"]]
    for n in sorted(res):
        print(("  ok   " if res[n]["ok"] else "  MUTE "), n,
              "" if res[n]["ok"] else res[n]["err"])
    n_lang, codes, why = local_voice.languages()
    print(f"\n{n_lang} languages: {' '.join(codes)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
