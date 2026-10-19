import time
from rar.config import ADMIN_STATUSES, GREET_DEDUP_TTL, GROUP_ANON_BOT_ID, IN_CHAT_STATUSES, LEAVE_STATUSES
from rar.db import get_chat_settings, remove_chat_data, remove_user_from_chat, save_user_to_chat
from rar.dialogs import SETTINGS_DIALOGS
from rar.state import _creator_cache, _recent_greets
from rar.utils import cache_user, substitute_vars, uncache_user


async def handle_chat_member(update, context):
    try:
        result = update.chat_member
        if not result: return
        user = result.new_chat_member.user
        chat_id = result.chat.id
        new_status = result.new_chat_member.status
        old_status = result.old_chat_member.status
        if user.is_bot or chat_id >= 0: return
        if user.id == GROUP_ANON_BOT_ID: return
        greet_enabled, farewell_enabled, greet_text, farewell_text = get_chat_settings(chat_id)
        try:
            chat = await context.bot.get_chat(chat_id)
            chat_title = chat.title or "этот чат"
        except Exception: chat_title = "этот чат"
        user_name = user.first_name or "друг"
        if old_status in LEAVE_STATUSES and new_status in IN_CHAT_STATUSES:
            try: save_user_to_chat(user.id, chat_id); cache_user(user.id, chat_id)
            except Exception: pass
            if greet_enabled:
                try: await context.bot.send_message(chat_id=chat_id, text=substitute_vars(greet_text, user_name, chat_title))
                except Exception: pass
        elif old_status in IN_CHAT_STATUSES and new_status in LEAVE_STATUSES:
            try: remove_user_from_chat(user.id, chat_id); uncache_user(user.id, chat_id)
            except Exception: pass
            if farewell_enabled:
                try: await context.bot.send_message(chat_id=chat_id, text=substitute_vars(farewell_text, user_name, chat_title))
                except Exception: pass
    except Exception as e: print(f"[CHAT_MEMBER FATAL] {e}")


async def handle_my_chat_member(update, context):
    try:
        result = update.my_chat_member
        if not result: return
        chat = result.chat; chat_id = chat.id
        new_status = result.new_chat_member.status
        old_status = result.old_chat_member.status
        if chat_id >= 0: return
        if new_status in LEAVE_STATUSES:
            try: remove_chat_data(chat_id); _creator_cache.pop(chat_id, None)
            except Exception: pass
            return
        if old_status not in ADMIN_STATUSES and new_status in ADMIN_STATUSES:
            now = time.time()
            if chat_id in _recent_greets and now - _recent_greets[chat_id] < GREET_DEDUP_TTL:
                try: await context.bot.send_message(chat_id=chat_id, text=SETTINGS_DIALOGS["thanks"])
                except Exception: pass
                return
            try: await context.bot.send_message(chat_id=chat_id, text=SETTINGS_DIALOGS["thanks"])
            except Exception: pass
            return
        if old_status in ADMIN_STATUSES and new_status not in ADMIN_STATUSES:
            try: await context.bot.send_message(chat_id=chat_id, text=SETTINGS_DIALOGS["lost_admin"])
            except Exception: pass
            return
        if old_status not in IN_CHAT_STATUSES and new_status in IN_CHAT_STATUSES:
            _recent_greets[chat_id] = time.time()
            _creator_cache.pop(chat_id, None)
            try:
                admins = await context.bot.get_chat_administrators(chat_id)
                for admin in admins:
                    if admin.user.is_bot: continue
                    if admin.user.id == GROUP_ANON_BOT_ID: continue
                    save_user_to_chat(admin.user.id, chat_id); cache_user(admin.user.id, chat_id)
            except Exception: pass
            if new_status in ADMIN_STATUSES:
                try: await context.bot.send_message(chat_id=chat_id, text=SETTINGS_DIALOGS["thanks"])
                except Exception: pass
            else:
                text = f"{SETTINGS_DIALOGS['join_no_admin']}\n\n{SETTINGS_DIALOGS['join_no_admin_hint']}\n\n{SETTINGS_DIALOGS['join_commands']}"
                try: await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                except Exception: pass
    except Exception as e: print(f"[MY_CHAT_MEMBER FATAL] {e}")
