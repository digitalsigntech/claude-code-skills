# Agent install brief

You are an agent installing this voice adapter on the machine you are running on.
The operator handed you this file so they would not have to type the instructions.

Read `README.md` in this directory **in full** first. This file adds the judgement
calls and the traps; it does not replace it.

When you are done, the operator will be able to speak to the app on their phone and
get an answer out of **this machine's files**.

---

## 1. Which directory should the agent work in

`workdir` in `config.json` is where every voice turn runs. It is the whole point of
the install: a turn that runs somewhere empty can only answer from general knowledge,
which the phone could have done without you.

Work it out before you ask — nobody can put it in this file:

```bash
ls -la ~
find ~ -maxdepth 3 -name CLAUDE.md -not -path '*/.*' 2>/dev/null
find ~ -maxdepth 3 -name '.git' -type d 2>/dev/null
```

A directory with a `CLAUDE.md` is a strong signal — read it, it usually says what the
machine is for. Your own session's directory is a good default. **State your choice and
why, and let the operator confirm before you continue.** Do not silently pick.

## 2. Decide the transport — this is where installs fail

The plane calls the agent, so it needs an address that resolves from the public
internet. Check what this machine actually has; do not assume.

```bash
curl -s --max-time 10 https://api.ipify.org ; echo        # our egress address
ip -4 addr show scope global | grep -oP 'inet \K[\d.]+'   # our local addresses
```

- **The two match, and something already terminates HTTPS here** (Caddy, nginx,
  Traefik): use the direct transport. Add a route that proxies to `127.0.0.1:8787`,
  then pair with that URL. Do not open the adapter's port to the world directly —
  keep the bind on localhost and let the proxy do TLS.
- **They differ, or nothing serves HTTPS**: this machine is behind NAT — the common
  case, and fully supported. Use `tunnel.py`: it gets a public HTTPS URL over an
  outbound connection, signs up or re-pairs with it, and keeps doing so every time
  the URL moves. Install `cloudflared` (a single binary) if it is missing — on PATH,
  in `$CLOUDFLARED`, or dropped beside the scripts.

If you are unsure, prefer the tunnel: it works in both cases.

## 3. Get the account — two routes, and the NAT one has an order

**If the operator already has the app and an account**, ask them for the account
token (app Settings, or its pairing QR) and pair with `--token`. You cannot derive
it: it is the credential that says which account this agent belongs to.

**If they have no account yet**, create it from here — that is the normal case, and
it means the operator needs nothing beforehand:

    python3 pair.py --signup --url <your public URL>
    python3 pair.py --qr        # show them this; one scan signs their phone in

You do not need to ask which plane to pair with, and the operator will not know:
`pair.py` defaults to the service behind the app. `--api` overrides it, and the only
reason to pass it is a different deployment.

Order matters and cannot be swapped: the plane **probes the webhook before it
creates anything**, so the adapter must already be serving at a publicly reachable
URL. On a NAT machine you do not run `--signup` by hand at all — `tunnel.py` does
it for you the moment the tunnel URL exists, because until then there is no URL to
sign up with. Then show the QR.

Either way `config.json` ends up holding the token: `chmod 600`, never print it back
into a chat, never commit it. The QR is a different, short-lived thing (~15 min,
one scan) — that one is safe to show, but if you put it in a chat, delete the
message at expiry.

## 4. Check the identity it worked out — do not fill it in

The app's panel (user's name, company, your own name, logo) is derived, not
configured: `pair.py` spends one turn before registering asking you to describe
yourself from this project's files, and caches it. You do not have to do anything
for this to happen. Look at the result:

    python3 voice_agent.py --identity

**A field that came back empty is usually correct.** Every value has to appear in the
project's own files or it is dropped, because an agent asked who its user is will
otherwise answer from the account the CLI is signed in as — a real person with nothing
to do with this install. If a name is missing and the operator wants it shown, the fix
is to write it where it belongs (`CLAUDE.md`, a company file) and re-derive — not to
hand-set it here. That way the next agent to read this project knows it too.

Override in `config.json` (`agent_name`, `company_name`, `user_name`, `user_email`,
`logo`) only for something the files genuinely should not say.

If you change any of it later: restart the adapter, then re-run `pair.py --url <your
URL>` so the plane re-probes capabilities, and give it a minute — it caches both for
about five.

## 5. Run both pieces as services

The adapter, and — on a NAT machine — the tunnel. Templates are in `src/`.

**The trap that catches everyone:** the adapter shells out to `claude`. A service
manager does not inherit the operator's login `PATH`, so if `claude` lives in
`~/.local/bin` the service starts cleanly and then fails on *every* turn with a
command-not-found that is invisible unless you read the logs. Set an absolute `PATH`
in the unit. Verify with `which claude` as the user the service will run as.

Second trap, if the service runs as root: the CLI refuses
`--dangerously-skip-permissions` for root. The adapter handles this (`IS_SANDBOX=1`,
then a prompt-limited retry), but a non-root user is better where the machine allows it.

## 6. Verify from the plane's side, not yours

A local `curl` proving your own server answers proves nothing about whether the plane
can reach it. Run:

    python3 pair.py --test

and read the reason it returns:

- **ok** — done.
- **unreachable** — the plane could not get to you. Wrong URL, tunnel down, proxy
  route wrong, adapter not running.
- **signed_out** — it reached you, and Claude on this machine is logged out. **Fix it
  here**, in a terminal on this machine, by running `claude` and logging in.
  Re-pairing changes nothing.

Do not report success on a running process. Then ask the operator to speak to the app
and confirm the answer came from this machine's files — something only these files
would know is the honest test.

## 7. Report back with

- The `workdir`, and why you chose it
- The transport, and the public URL the plane will call
- Service names, and how to check status, logs and restart
- Where `config.json` lives and that it is `chmod 600`
- The result of `pair.py --test`, verbatim
- The identity it derived, verbatim — and anything it could not, with where you
  would write that fact so it can

---

## Quick sanity checklist

- [ ] `workdir` is the project the operator confirmed
- [ ] Transport matches reality (public address vs NAT), not an assumption
- [ ] Adapter bound to `127.0.0.1`; only the proxy or tunnel is public
- [ ] `config.json` is `chmod 600` and uncommitted
- [ ] Absolute `PATH` in the unit so `claude` resolves
- [ ] On NAT: tunnel runs as a service so a new URL re-pairs by itself
- [ ] Identity derived and checked; blanks understood, not papered over
- [ ] `pair.py --test` returns ok
- [ ] A real spoken question answered from this machine's files
