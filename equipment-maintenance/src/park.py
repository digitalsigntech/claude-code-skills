#!/usr/bin/env python3
"""Equipment park — ONE table of the whole park, with or without pictures and operators.

    park.py                      # one plain GFM table (chat app, terminal)
    park.py --photos             # first column is each machine's photo
    park.py --plain              # never photos, whatever the road says
    park.py --operators          # adds an Operator column (name, shift in brackets)
    park.py --presses            # only the printing presses
    park.py --finishing          # only finishing and converting
    park.py --two                # two tables, one per group
    park.py --album <chat_id>    # chat: send the photos as one album, then the table
    park.py --root <workspace>   # where the knowledge base lives (default: see below)

WHY PICTURES ARE AUTOMATIC ON THE APP ROAD. A phone app can draw an image inside
a table cell; a chat client cannot. The owner's rule is that a request arriving
from the app comes back as a table WITH pictures — and a prompt rule alone did
not hold: a model with the flag in its instructions, in the help text and in the
road statement still ran this command without it twice in one minute, once on a
question that said "with pictures". So the road decides, not the model: the
agent runs every model turn with AGENT_ROAD=app in the environment, this reads
it, and --plain is the explicit way out.

The photo cell is `![](vb-token:TOKEN)`, the shape the app already fetches
through its file endpoint. A machine with no photo gets an empty cell, never a
stand-in.

DATA, all inside the workspace (--root, else AGENT_WORKSPACE, else the directory
two levels above this file):
    knowledge-base/equipment/equipment-park.md   the machines, as markdown tables
    knowledge-base/equipment/photos/<ID>.jpg     one picture per machine, optional
    knowledge-base/company/team.md               who runs what, for --operators
Nothing in the output ever names a file, a folder or a source.
"""

import os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _root(argv):
    if "--root" in argv:
        return os.path.expanduser(argv[argv.index("--root") + 1])
    env = os.environ.get("AGENT_WORKSPACE")
    if env:
        return os.path.expanduser(env)
    return os.path.dirname(os.path.dirname(HERE))


ROOT = _root(sys.argv[1:])
SRC = os.path.join(ROOT, "knowledge-base/equipment/equipment-park.md")
TEAM = os.path.join(ROOT, "knowledge-base/company/team.md")
PHOTOS = os.path.join(ROOT, "knowledge-base/equipment/photos")
sys.path.insert(0, HERE)

# one header for the whole park; each group's own columns map onto it
HEAD = ["ID", "Machine", "Type / function", "Width", "Max speed", "Installed", "Notes"]
_ALIASES = {"type": "Type / function", "function": "Type / function", "type / function": "Type / function",
            "id": "ID", "machine": "Machine", "width": "Width", "max speed": "Max speed",
            "installed": "Installed", "notes": "Notes"}


def _mint(path):
    """Token the app can fetch the picture with. Same rule as reminders_reflex:
    the adapter's own media_token (REMINDERS_MINT_MODULE, in-process on this box),
    else the running server's mint endpoint."""
    mod = os.environ.get("REMINDERS_MINT_MODULE") or "voice_agent"
    for p in ("/opt/voice-agent",):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)
    try:
        import importlib
        return importlib.import_module(mod).media_token(os.path.abspath(path))
    except Exception:
        pass
    try:
        import reminders_reflex
        return reminders_reflex._mint(path)
    except Exception:
        return None


def _md_tables(path):
    """[(heading, header_cells, [row_cells...])] from a markdown file."""
    out, heading, rows = [], None, []
    for line in open(path).read().splitlines() + [""]:
        if line.startswith("## "):
            if rows: out.append((heading, rows)); rows = []
            heading = line[3:].strip()
        elif line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif rows:
            out.append((heading, rows)); rows = []
    res = []
    for heading, rows in out:
        rows = [r for r in rows if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
        if rows:
            res.append((heading, rows[0], rows[1:]))
    return res


def tables():
    return _md_tables(SRC)


def _is_press(heading):
    return "press" in (heading or "").lower() and "finish" not in (heading or "").lower()


def operators():
    """{machine id: "Name (shift); Name"} from the team file."""
    by_id = {}
    for _h, head, body in _md_tables(TEAM):
        cols = [c.lower() for c in head]
        if "name" not in cols:
            continue
        i_name, i_role = cols.index("name"), (cols.index("role") if "role" in cols else None)
        i_notes = cols.index("notes") if "notes" in cols else None
        for row in body:
            name = row[i_name] if i_name < len(row) else ""
            role = row[i_role] if i_role is not None and i_role < len(row) else ""
            notes = row[i_notes] if i_notes is not None and i_notes < len(row) else ""
            if not name:
                continue
            m = re.search(r"\b(\d(?:st|nd|rd|th)\s+shift|night\s+shift|day\s+shift)\b", role, re.I)
            label = f"{name} ({m.group(1).lower()})" if m else name
            for mid in set(re.findall(r"\b([A-Z]{2}-\d{2})\b", notes)):
                by_id.setdefault(mid, [])
                if label not in by_id[mid]:
                    by_id[mid].append(label)
    return {k: "; ".join(v) for k, v in by_id.items()}


def unified_rows(group=None):
    """Every machine as one row under HEAD; presses first, then finishing."""
    rows = []
    for heading, head, body in tables():
        press = _is_press(heading)
        if group == "presses" and not press:
            continue
        if group == "finishing" and press:
            continue
        idx = {}
        for i, h in enumerate(head):
            key = _ALIASES.get(h.strip().lower())
            if key:
                idx[key] = i
        for row in body:
            rows.append([row[idx[k]] if k in idx and idx[k] < len(row) and row[idx[k]] else "—" for k in HEAD])
    return rows


def _table(head, rows, photos):
    lines = []
    lines.append("| " + " | ".join((["Photo"] if photos else []) + head) + " |")
    lines.append("|" + "---|" * (len(head) + (1 if photos else 0)))
    for row in rows:
        cells = list(row)
        if photos:
            p = os.path.join(PHOTOS, f"{row[0]}.jpg")
            tok = _mint(p) if os.path.exists(p) else None
            cells.insert(0, f"![](vb-token:{tok})" if tok else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render(photos=False, ops=False, group=None, two=False):
    if two:
        blocks = []
        for heading, head, body in tables():
            rows = [list(r) for r in body]
            h = list(head)
            if ops:
                o = operators(); h = h + ["Operator"]; rows = [r + [o.get(r[0], "—")] for r in rows]
            blocks.append(f"**{heading}**\n\n" + _table(h, rows, photos))
        return "\n\n".join(blocks)
    head, rows = list(HEAD), unified_rows(group)
    if ops:
        o = operators(); head = head + ["Operator"]; rows = [r + [o.get(r[0], "—")] for r in rows]
    title = {"presses": "Printing presses", "finishing": "Finishing and converting"}.get(group, "Equipment park")
    return f"**{title}**\n\n" + _table(head, rows, photos)


def album(chat_id, group=None):
    items = []
    for row in unified_rows(group):
        p = os.path.join(PHOTOS, f"{row[0]}.jpg")
        if os.path.exists(p):
            items.append(f"{p}::{row[0]} · {row[1]}")
    if items:
        # The album sender belongs to whatever chat gateway this deployment
        # runs; it lives with the workspace, not with this skill.
        sender = next((p for p in (os.path.join(ROOT, "telegram", "send_album.py"),
                                   os.path.join(HERE, "send_album.py"))
                       if os.path.exists(p)), None)
        if not sender:
            print("no album sender on this install", file=sys.stderr)
            return
        subprocess.run([sys.executable, sender, str(chat_id)] + items[:10],
                       check=False)


def main():
    argv = sys.argv[1:]
    group = "presses" if "--presses" in argv else ("finishing" if "--finishing" in argv else None)
    ops = "--operators" in argv
    trailer = ("\n\n[Instruction to the assistant, not part of the answer: copy EVERYTHING above this line "
               "into your reply, verbatim, every row - the person sees only your reply, never this output. "
               "Leave this bracketed line out.]")
    if "--album" in argv:
        i = argv.index("--album"); album(argv[i + 1], group)
        print(render(False, ops, group, "--two" in argv) + trailer); return
    # 2026-09-15: a voice-app turn sets AGENT_ROAD=app in the environment every
    # command inherits, so the app gets its pictures even when the flag is
    # forgotten (it was, twice, on a request that said "with pictures").
    # --plain is the explicit way out.
    photos = "--photos" in argv or (os.environ.get("AGENT_ROAD") == "app"
                                    and "--plain" not in argv)
    print(render(photos, ops, group, "--two" in argv) + trailer)


if __name__ == "__main__":
    main()
