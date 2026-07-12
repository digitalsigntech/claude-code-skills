"""Thin Telegram Bot API client (long-polling, no webhook — works behind NAT)."""
import re
import html
import requests
import tgconf as C


# ---- Markdown -> Telegram HTML ----------------------------------------------
# Claude replies in GitHub-flavored markdown; Telegram only renders its own small
# HTML subset (<b> <i> <u> <s> <code> <pre> <a>). Convert so formatting shows up
# instead of leaking raw ** __ # ` characters as plaintext.
def md_to_html(text):
    stash = []

    def keep(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    # Protect code first (its contents must NOT be markdown- or HTML-processed).
    text = re.sub(r"```[ \t]*[\w+#.-]*\n?(.*?)```",
                  lambda m: keep(f"<pre>{html.escape(m.group(1))}</pre>"),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)

    # Escape the rest, then layer in formatting (brackets/parens survive escaping).
    text = html.escape(text)
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", r"\1• ", text)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


# ---- Rich messages (Bot API 10.1) -------------------------------------------
# Telegram's HTML subset can't render GFM tables (they leak as pipe junk). Bot API
# 10.1 added sendRichMessage, which renders raw markdown tables / task lists as
# native, selectable Telegram tables. We route ONLY table/task-list-bearing replies
# through the rich path and keep ordinary text on the fast HTML path.
_TABLE_RE = re.compile(r"(?m)^[ \t]*\|.+\|[ \t]*\n[ \t]*\|[ \t:|-]+\|[ \t]*$")
_TASKLIST_RE = re.compile(r"(?m)^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+\S")


def needs_rich(text):
    """True when text has a construct only rich messages render (GFM table / task list)."""
    return bool(text) and (bool(_TABLE_RE.search(text)) or bool(_TASKLIST_RE.search(text)))


def _tables_to_text(text):
    """Degrade GFM tables to readable 'Header: value' row groups for the legacy HTML
    fallback path, so a rich-send failure shows clean rows instead of pipe junk."""
    lines = text.split("\n")
    out, i = [], 0
    sep = re.compile(r"^[ \t]*\|[ \t:|-]+\|[ \t]*$")
    row = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$")

    def cells(line):
        return [c.strip() for c in row.match(line).group(1).split("|")]

    while i < len(lines):
        m = row.match(lines[i])
        if m and i + 1 < len(lines) and sep.match(lines[i + 1]):
            headers = cells(lines[i]); i += 2
            while i < len(lines) and row.match(lines[i]):
                vals = cells(lines[i])
                pairs = [f"{headers[j]}: {vals[j]}" for j in range(min(len(headers), len(vals))) if vals[j]]
                out.append("• " + " · ".join(pairs))
                i += 1
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)


def send_rich_message(chat_id, markdown, reply_to=None, reply_markup=None):
    """Send raw GFM markdown via sendRichMessage (native table rendering). Returns the
    API result on success, or None so the caller can fall back to the legacy path."""
    if not markdown or len(markdown) > C.RICH_MAX:
        return None
    params = {"chat_id": chat_id, "rich_message": {"markdown": markdown},
              "disable_web_page_preview": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to}   # 10.1 replaces reply_to_message_id
    if reply_markup:
        params["reply_markup"] = reply_markup
    res = _call("sendRichMessage", **params)
    return res if res.get("ok") else None


def edit_rich(chat_id, msg_id, markdown):
    """Turn an existing (streamed placeholder) message into a rich message in place."""
    if not msg_id or not markdown or len(markdown) > C.RICH_MAX:
        return None
    r = _call("editMessageText", chat_id=chat_id, message_id=msg_id,
              rich_message={"markdown": markdown}, disable_web_page_preview=True)
    return r if (r.get("ok") or "not modified" in str(r.get("error", "")).lower()) else None


# One keep-alive session for all Bot API calls: bare requests.post() opens a fresh
# TLS connection every time (~400-900ms handshake to api.telegram.org); a pooled
# session makes repeat calls pay only the API round-trip (~150-300ms). Thread-safe.
_SESSION = requests.Session()
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4,
                                                         pool_maxsize=16))


def _call(method, _files=None, _timeout=60, **params):
    url = f"{C.API}/{method}"
    for attempt in (0, 1):
        try:
            if _files:
                r = _SESSION.post(url, data=params, files=_files, timeout=_timeout)
            else:
                r = _SESSION.post(url, json=params, timeout=_timeout)
            j = r.json()
            return j if j.get("ok") else {"ok": False, "error": j}
        except requests.exceptions.ConnectionError as e:
            # A pooled keep-alive connection Telegram closed while idle — retry once
            # on a fresh one. Uploads (_files) aren't retried: the stream is consumed.
            if attempt or _files:
                return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def get_me():
    return _call("getMe")


def set_commands(commands):
    return _call("setMyCommands", commands=commands)


def get_updates(offset, timeout=C.LONGPOLL):
    return _call("getUpdates", offset=offset, timeout=timeout,
                 allowed_updates=["message", "callback_query"],
                 _timeout=timeout + 15)


def send_chat_action(chat_id, action="typing"):
    return _call("sendChatAction", chat_id=chat_id, action=action)


def _chunks(text, n=C.TG_MAX):
    text = text if text and text.strip() else "(empty reply)"
    while text:
        if len(text) <= n:
            yield text; return
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def send_message(chat_id, text, reply_to=None, reply_markup=None):
    """Send a reply. Table/task-list replies go via sendRichMessage (native rendering);
    everything else uses the fast HTML path. On rich failure, tables are degraded to
    readable rows before falling back so pipe junk never reaches the user."""
    if needs_rich(text) and len(text) <= C.RICH_MAX:
        res = send_rich_message(chat_id, text, reply_to, reply_markup)
        if res:
            return res
        text = _tables_to_text(text)
    return _send_html(chat_id, text, reply_to, reply_markup)


def _send_html(chat_id, text, reply_to=None, reply_markup=None):
    """Send (chunked) as Telegram HTML so markdown renders. reply_markup is
    attached to the LAST chunk only. If a chunk's HTML is rejected (e.g. a tag
    split across the chunk boundary), retry it as plaintext so the reply always
    lands."""
    last = None
    parts = list(_chunks(text))
    for i, part in enumerate(parts):
        params = {"chat_id": chat_id, "disable_web_page_preview": True}
        if i == 0 and reply_to:
            params["reply_to_message_id"] = reply_to
        if i == len(parts) - 1 and reply_markup:
            params["reply_markup"] = reply_markup
        html_part = md_to_html(part)
        res = None
        if len(html_part) <= 4096:
            res = _call("sendMessage", text=html_part, parse_mode="HTML", **params)
        if not res or not res.get("ok"):
            res = _call("sendMessage", text=part, **params)
        last = res
    return last


def message_id(res):
    try:
        return res["result"]["message_id"]
    except Exception:
        return None


def edit_text(chat_id, msg_id, text, as_html=False):
    """Edit a message in place (used for live streaming). HTML attempt with a plain
    fallback; 'not modified' is treated as success (identical consecutive edits)."""
    if not msg_id:
        return None
    text = text if text and text.strip() else "…"
    if len(text) > 4096:
        text = text[:4093] + "…"
    if as_html:
        h = md_to_html(text)
        if len(h) <= 4096:
            r = _call("editMessageText", chat_id=chat_id, message_id=msg_id, text=h,
                      parse_mode="HTML", disable_web_page_preview=True)
            if r.get("ok") or "not modified" in str(r.get("error", "")).lower():
                return r
    r = _call("editMessageText", chat_id=chat_id, message_id=msg_id, text=text,
              disable_web_page_preview=True)
    return r


def deliver_final(chat_id, msg_id, text):
    """Replace the live placeholder with the final formatted reply; overflow past one
    message is sent as additional chunks.

    For table/task-list replies we do NOT upgrade the streamed placeholder in place
    (half-streamed / edited-text rich upgrades render inconsistently). Instead
    we send a fresh sendRichMessage with the COMPLETE markdown, then delete the interim
    placeholder — leaving it only if the delete fails."""
    if needs_rich(text) and len(text) <= C.RICH_MAX:
        if send_rich_message(chat_id, text):
            r = delete_message(chat_id, msg_id)
            if not (r and r.get("ok")):
                edit_text(chat_id, msg_id, "✅", as_html=False)   # delete failed: neutralize placeholder
            return
        text = _tables_to_text(text)   # rich failed: degrade tables, no pipe junk
    parts = list(_chunks(text))
    edit_text(chat_id, msg_id, parts[0], as_html=True)
    for part in parts[1:]:
        send_message(chat_id, part)


def delete_message(chat_id, msg_id):
    if not msg_id:
        return None
    return _call("deleteMessage", chat_id=chat_id, message_id=msg_id)


def answer_callback(cb_id, text=""):
    return _call("answerCallbackQuery", callback_query_id=cb_id, text=text)


def clear_markup(chat_id, message_id):
    return _call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                 reply_markup={"inline_keyboard": []})


def download(file_id, dest):
    """Resolve a Telegram file_id and stream it to dest. Returns dest or None."""
    info = _call("getFile", file_id=file_id)
    if not info.get("ok"):
        return None
    fp = info["result"].get("file_path")
    if not fp:
        return None
    try:
        with requests.get(f"{C.FILE_API}/{fp}", stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        return dest
    except Exception:
        return None
