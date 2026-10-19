import asyncio
import random
from rar.db import get_all_tracks_from_db, get_db_connection, get_notify_chat_id, get_owner_id, sleuth_delete_by_id, sleuth_get_expired
from rar.dialogs import SLEUTH_DIALOGS
from rar.utils import format_duration


async def sleuth_deadline_loop(app):
    await asyncio.sleep(90)
    while True:
        try:
            rows = sleuth_get_expired()
            for r in rows:
                sid, chat_id, target_id, tname, admin_id, aname, req, cur, dur, action = r
                sleuth_delete_by_id(sid)
                t_display = f"@{tname}" if tname else f"ID {target_id}"
                a_display = f"@{aname}" if aname else f"ID {admin_id}"
                dur_str = format_duration(dur)
                msg = SLEUTH_DIALOGS["done_deadline"].format(цель=t_display, N=cur, M=req, срок=dur_str, админ=a_display)
                if action == "kick":
                    try:
                        await app.bot.ban_chat_member(chat_id, target_id)
                        await app.bot.unban_chat_member(chat_id, target_id)
                        msg += "\n" + SLEUTH_DIALOGS["kicked"]
                    except Exception as e:
                        msg += "\n" + SLEUTH_DIALOGS["no_kick_rights"]
                        print(f"[SLEUTH KICK ERROR] {e}")
                elif action == "ban":
                    try:
                        await app.bot.ban_chat_member(chat_id, target_id)
                        msg += "\n" + SLEUTH_DIALOGS["banned"]
                    except Exception as e:
                        msg += "\n" + SLEUTH_DIALOGS["no_ban_rights"]
                        print(f"[SLEUTH BAN ERROR] {e}")
                try: await app.bot.send_message(chat_id=chat_id, text=msg)
                except Exception: pass
                try:
                    owner_id = get_owner_id()
                    if owner_id:
                        await app.bot.send_message(chat_id=owner_id, text=f"<b>LOG</b>\n<code>[SLEUTH END] {msg}</code>", parse_mode="HTML")
                except Exception: pass
        except Exception as e:
            print(f"[SLEUTH LOOP ERROR] {e}")
        await asyncio.sleep(60)


async def daily_track_loop(app):
    await asyncio.sleep(60)
    while True:
        try:
            notify = get_notify_chat_id()
            if notify:
                tracks = get_all_tracks_from_db(only_ok=True)
                if tracks:
                    fid, title = random.choice(tracks)
                    try: await app.bot.send_audio(chat_id=notify, audio=fid, caption=f"Ежедневный трек:\n{title}")
                    except Exception: pass
        except Exception: pass
        await asyncio.sleep(86400)


async def keep_database_alive():
    await asyncio.sleep(30)
    while True:
        try:
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("SELECT 1;"); cursor.fetchone()
            cursor.close(); conn.close()
        except Exception as e: print(f"=== [PING ERROR] {e} ===")
        await asyncio.sleep(21600)
