"""Onboarding helper: print the user/chat IDs of whoever recently messaged the bot.

Run this AFTER you message the bot once, to discover your Telegram user ID, then add
it to telegram/allowlist.json. Safe to run while the gateway is stopped.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tgconf as C
import tg_api as TG

if not C.TOKEN:
    sys.exit("No bot token. Put it in telegram/bot_token or set TG_BOT_TOKEN.")
me = TG.get_me()
if not me.get("ok"):
    sys.exit(f"getMe failed: {me.get('error')}")
print(f"Bot: @{me['result'].get('username')}\n")
res = TG.get_updates(0, timeout=0)
if not res.get("ok"):
    sys.exit(f"getUpdates failed: {res.get('error')}")
seen = {}
for upd in res.get("result", []):
    m = upd.get("message") or upd.get("callback_query", {}).get("message", {})
    frm = (upd.get("message") or upd.get("callback_query", {})).get("from", {})
    if frm.get("id"):
        seen[frm["id"]] = (frm.get("username") or frm.get("first_name") or "?",
                           m.get("chat", {}).get("id"), m.get("chat", {}).get("type"))
if not seen:
    print("No recent messages. Send your bot a message, then re-run this.")
else:
    print("Recent senders (add the right user_id to allowlist.json):")
    for uid, (name, chat_id, ctype) in seen.items():
        print(f"  user_id={uid}  name={name}  chat_id={chat_id}  chat_type={ctype}")
