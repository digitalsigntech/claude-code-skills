"""Agent profile — one description of THIS deployment, read by every skill.

the owner, 2026-08-13: "instead of building a perfect Claude agent, we should have
built a universal transferrable system for other agents. It should be agnostic of
the usernames, company names, and other private data."

The rule this file exists to enforce: a skill knows ROLES, a profile supplies
VALUES. Code says `owner`, never a person's first name; `org.short`, never a
company's initials; `host.label`, never a machine's name. This box becomes one
profile among several instead of the default every other install deviates from.

Resolution order, first hit wins:
  1. env override    (AGENT_<SECTION>_<KEY>, e.g. AGENT_AGENT_NAME)
  2. the profile     ($AGENT_PROFILE, else agent-profile.json beside the
                      workspace root, else next to this file)
  3. the default passed by the caller

A missing profile is NOT an error. Every accessor takes a default, so a skill
installed on a bare machine runs with generic values instead of dying — which is
the failure mode that made Max's install a day of hand-patching.

Vendored, not imported across skills: each skill ships its own copy so it has no
dependency on any other skill being present. sync_exports.py keeps them identical.
"""
import json, os

SCHEMA_VERSION = 1

_CACHE = {}


def _candidates():
    env = os.environ.get("AGENT_PROFILE")
    if env:
        # An EXPLICIT profile is exclusive. Falling through to the search when it
        # is missing would silently run one deployment under another's identity —
        # you would think you were testing Max and be answering as us.
        yield os.path.expanduser(env)
        return
    here = os.path.dirname(os.path.abspath(__file__))
    # the skill usually sits one level under the workspace root
    for base in (os.path.dirname(here), here, os.path.expanduser("~")):
        yield os.path.join(base, "agent-profile.json")


def load(path=None):
    """The profile as a dict, cached. Empty dict if there is none."""
    key = path or "<default>"
    if key in _CACHE:
        return _CACHE[key]
    data = {}
    for cand in ([os.path.expanduser(path)] if path else _candidates()):
        try:
            with open(cand) as fh:
                data = json.load(fh)
            data["_path"] = cand
            break
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (json.JSONDecodeError, OSError) as e:
            # A malformed profile is worth a loud complaint but not a dead skill.
            print(f"agentprofile: ignoring {cand}: {e}")
            continue
    _CACHE[key] = data
    return data


def get(dotted, default=None):
    """`get("org.short", "workspace")` — env override, then profile, then default.

    The env name is the dotted path upper-cased with AGENT_ in front, so
    `org.short` is `AGENT_ORG_SHORT`. That is the whole override convention;
    skills do not invent their own variable names any more.
    """
    envname = "AGENT_" + dotted.replace(".", "_").upper()
    if os.environ.get(envname):
        return os.environ[envname]
    node = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def person(role, field=None, default=None):
    """People by ROLE — "owner", "second_owner", "friend", never by name.

    Returns the whole record for a role, or one field of it. Roles the profile
    does not define return the default, so `if not P.person("friend"):` is how a
    skill turns off a tier rather than testing for a hard-coded address.
    """
    people = load().get("people") or {}
    rec = people.get(role) or {}
    if field is None:
        return rec or default
    envname = f"AGENT_PERSON_{role}_{field}".upper()
    if os.environ.get(envname):
        return os.environ[envname]
    val = rec.get(field)
    return default if val is None else val


def roles():
    """Every role this deployment defines. Lets a skill iterate owners without
    knowing how many there are — two here, one on a single-operator install."""
    return sorted((load().get("people") or {}).keys())


def has(capability):
    """True if this machine actually provides a capability.

    Max's install notes are a list of features that assumed our services existed:
    a semantic answer cache that needs an embedding server, a photo reflex that
    needs a CLIP server, a privacy router that needs a local model and FAILS
    CLOSED. Each of those is now a question asked before the feature arms itself.
    """
    caps = load().get("capabilities") or {}
    envname = "AGENT_CAP_" + capability.replace(".", "_").upper()
    if os.environ.get(envname):
        return os.environ[envname] not in ("0", "false", "no", "")
    cap = caps.get(capability)
    if isinstance(cap, dict):
        return bool(cap.get("enabled", True))
    return bool(cap)


def capability(name, field=None, default=None):
    """Where a capability lives — endpoint, path, model — once `has()` is true."""
    cap = (load().get("capabilities") or {}).get(name)
    if not isinstance(cap, dict):
        return default
    if field is None:
        return cap
    envname = f"AGENT_CAP_{name}_{field}".replace(".", "_").upper()
    if os.environ.get(envname):
        return os.environ[envname]
    val = cap.get(field)
    return default if val is None else val


def workspace(sub=None, default=None):
    """Absolute path into the workspace tree. The layout is per-deployment: ours
    is ~/the workspace with email/ and crm/, Max's is /root/summit-label with jobs/ and
    purchasing/. Skills ask for a ROLE of directory, not a path."""
    root = get("workspace.root") or default or os.path.expanduser("~")
    root = os.path.expanduser(root)
    if sub is None:
        return root
    named = (load().get("workspace") or {}).get("dirs") or {}
    return os.path.join(root, named.get(sub, sub))


def describe():
    """One-line summary for logs and health checks — which profile is live."""
    p = load()
    if not p:
        return "no profile (defaults)"
    return (f"{get('agent.name', '?')} for {get('org.short', '?')} "
            f"on {get('host.label', '?')} [{p.get('_path', '?')}]")
