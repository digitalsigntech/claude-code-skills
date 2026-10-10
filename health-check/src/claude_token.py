"""Background `claude` runs on its own long-lived token, never the shared login.

Why: OAuth refresh tokens are single-use, and every `claude` process on a machine
shares ~/.claude/.credentials.json. When two of them refresh at the same moment
(a chat turn, a voice turn, a cron, someone's terminal) the loser gets
invalid_grant and the login dies for all of them: "OAuth session expired",
everyone has to /login again. A token from `claude setup-token` never refreshes,
so a process holding it in CLAUDE_CODE_OAUTH_TOKEN cannot take part in that race.

Call use() once before spawning `claude` (cheap: one small file read). It loads
CLAUDE_CODE_OAUTH_TOKEN from $CLAUDE_SECRETS_ENV, or else from the first
~/.config/*/secrets.env that has it, into os.environ — so every child inherits
it. The file wins over a token already in the environment, so a renewed token
takes effect on the next turn with no restart. No token anywhere: nothing
changes and use() returns False (callers may warn; install checks do).

One line in the secrets file, chmod 600:
    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
Get it with `claude setup-token` (browser sign-in, valid one year). One token
serves every machine signed in to the same Claude account.

Vendored into every skill that starts Claude turns, so none depends on another.
"""
import glob
import os

VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _files():
    if os.environ.get("CLAUDE_SECRETS_ENV"):
        return [os.path.expanduser(os.environ["CLAUDE_SECRETS_ENV"])]
    return sorted(glob.glob(os.path.expanduser("~/.config/*/secrets.env")))


def token():
    """The background token from the secrets file, or None."""
    for path in _files():
        try:
            with open(path) as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if line.startswith(VAR + "="):
                tok = line.split("=", 1)[1].strip().strip("'\"")
                if tok:
                    return tok
    return None


def use():
    """Put the background token in os.environ. True when one is in effect."""
    tok = token()
    if tok:
        os.environ[VAR] = tok
    return bool(os.environ.get(VAR))


def env(base=None):
    """A copy of `base` (default os.environ) carrying the background token —
    for subprocess calls that pass an explicit env."""
    e = dict(os.environ if base is None else base)
    tok = token()
    if tok:
        e[VAR] = tok
    return e


if __name__ == "__main__":
    import sys
    ok = token() is not None
    print("background token: " + ("configured" if ok else
          "MISSING — run `claude setup-token` and put CLAUDE_CODE_OAUTH_TOKEN=... "
          "in ~/.config/<name>/secrets.env, or every concurrent claude can sign "
          "the machine out"))
    sys.exit(0 if ok else 1)
