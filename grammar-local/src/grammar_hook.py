#!/usr/bin/env python3
"""UserPromptSubmit hook: keep grammar checks OFF the cloud.

Fires before a prompt is sent to the cloud model. If the prompt is a grammar-check
request, run it on the LOCAL model and BLOCK the submission — so the text is never
sent to the cloud provider. Otherwise do nothing and let the prompt proceed normally.

Wire it in your Claude Code settings.json as a UserPromptSubmit command hook, e.g.:

  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command",
                  "command": "python3 /path/to/grammar-local/src/grammar_hook.py"}]}
    ]
  }
"""
import os, sys, json, re, subprocess

# The bash wrapper sits next to this file.
GRAMMAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grammar")

# Precise triggers only, so normal prompts are never intercepted.
TRIGGER = re.compile(
    r"(check|fix|correct|proofread|review)\s+(the\s+|my\s+)?grammar\b"
    r"|grammar\s+(check|fix|correction)\b"   # \b so "grammar checker" (the tool) does NOT match
    r"|^\s*/grammar\b",
    re.I)

# Some turns carry relayed external content (e.g. a message injected into the chat by an
# automation) that is a cloud turn by design and never a grammar request — even when it
# incidentally mentions "grammar". If your setup injects such content, mark it with a
# known prefix and set GRAMMAR_HOOK_SKIP_MARKER so those turns are never blocked.
# Defaults to a sentinel that never matches normal text.
INJECTION_MARKER = os.environ.get("GRAMMAR_HOOK_SKIP_MARKER", "\x00__no_match__")


def _block(text):
    print(json.dumps({"decision": "block", "reason": text}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)                       # malformed input -> don't interfere
    prompt = data.get("prompt", "") or ""
    if INJECTION_MARKER in prompt:
        sys.exit(0)                       # relayed content -> never intercept; let the turn run
    if not TRIGGER.search(prompt):
        sys.exit(0)                       # not a grammar request -> proceed to cloud as normal
    try:
        r = subprocess.run([GRAMMAR, "--meta", prompt], capture_output=True,
                           text=True, timeout=300)
        out = r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        out = ""
        err = str(e)
    else:
        err = (r.stderr or "").strip()
    if not out:
        # Fail SAFE: never silently fall through to the cloud. Block and explain.
        _block("⚠️ Grammar check could NOT run locally (the on-box model may be down), so I "
               "did NOT send your text to the cloud.\n\n"
               f"Detail: {err[:300] or 'no output'}\n\n"
               "Start the local model server and try again, or use the CLI: "
               f"{GRAMMAR} \"<text>\"")
    _block("🔒 Grammar checked LOCALLY — your text was NOT sent to the cloud:\n\n" + out)


if __name__ == "__main__":
    main()
