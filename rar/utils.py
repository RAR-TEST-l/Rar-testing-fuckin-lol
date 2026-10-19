import random
import re
import time
import traceback
from rar.config import INLINE_RESULT_CACHE_TTL, INLINE_SEEN_LIMIT, INLINE_SEEN_TTL, MODERATION_CHAT_ID, SAVED_USERS_CACHE_LIMIT
from rar.db import get_all_tracks_from_db, get_owner_id, mark_track_broken
from rar.state import inline_result_cache, inline_seen, saved_users_cache


def substitute_vars(text, user_name, chat_title):
    text = text.replace("%имя%", user_name).replace("%user%", user_name)
    return text.replace("%чат%", chat_title).replace("%chat%", chat_title)


def clean_title(full_title):
    if " - " in full_title: return full_title.split(" - ", 1)[1].strip()
    return full_title.strip()


def parse_duration(text):
    if not text: return None
    total = 0; found = False
    for m in re.finditer(r"(\d+)\s*(с|сек|м|мин|ч|час|д|дн|день|дней)", text.lower()):
        val = int(m.group(1)); unit = m.group(2)
        if unit in ("с", "сек"): total += val
        elif unit in ("м", "мин"): total += val * 60
        elif unit in ("ч", "час"): total += val * 3600
        elif unit in ("д", "дн", "день", "дней"): total += val * 86400
        found = True
    return total if found else None


def format_duration(seconds):
    if seconds < 60: return f"{seconds} сек"
    if seconds < 3600: return f"{seconds // 60} мин"
    if seconds < 86400: return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
    return f"{seconds // 86400} д {seconds % 86400 // 3600} ч"


async def log_to_owner(context, text):
    try:
        owner_id = get_owner_id()
        if owner_id:
            safe = text.replace("<", "&lt;").replace(">", "&gt;")
            if len(safe) > 3500: safe = safe[:3500] + "...[обрезано]"
            await context.bot.send_message(chat_id=owner_id, text=f"<b>LOG</b>\n<code>{safe}</code>", parse_mode="HTML")
    except Exception as e: print(f"[LOG ERROR] {e}")


async def notify_moderation(context, text):
    try:
        await context.bot.send_message(chat_id=MODERATION_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e: print(f"[MODERATION NOTIFY ERROR] {e}")


async def safe_answer_inline(update, context, results, cache_time=0):
    if not results:
        try: await update.inline_query.answer([], cache_time=cache_time)
        except Exception: pass
        return
    tried = list(results); removed_ids = []
    while tried:
        try:
            await update.inline_query.answer(tried, cache_time=cache_time)
            if removed_ids:
                await log_to_owner(context, f"[INLINE REMOVED] {len(removed_ids)}:\n" + "\n".join(removed_ids))
            return
        except Exception as e:
            err = str(e)
            if "content_type_invalid" in err.lower() or "Audio_content_type" in err:
                removed = tried.pop()
                fid_removed = None
                if hasattr(removed, 'id') and removed.id in inline_result_cache:
                    fid_removed, _ = inline_result_cache[removed.id]
                if fid_removed: mark_track_broken(fid_removed)
                removed_ids.append(getattr(removed, 'id', '?'))
            else:
                await log_to_owner(context, f"[INLINE FATAL] {err}\n{traceback.format_exc()[:800]}")
                try: await update.inline_query.answer([], cache_time=cache_time)
                except Exception: pass
                return
    try: await update.inline_query.answer([], cache_time=cache_time)
    except Exception: pass


def cache_user(user_id, chat_id):
    key = (user_id, chat_id)
    if key in saved_users_cache: saved_users_cache.move_to_end(key)
    saved_users_cache[key] = True
    if len(saved_users_cache) > SAVED_USERS_CACHE_LIMIT: saved_users_cache.popitem(last=False)


def uncache_user(user_id, chat_id):
    saved_users_cache.pop((user_id, chat_id), None)


def is_user_cached(user_id, chat_id):
    return (user_id, chat_id) in saved_users_cache


def pick_random_with_antirepeat(user_id, limit):
    now = time.time()
    seen = inline_seen.get(user_id, {})
    seen = {fid: ts for fid, ts in seen.items() if now - ts < INLINE_SEEN_TTL}
    if len(seen) > INLINE_SEEN_LIMIT:
        seen = dict(sorted(seen.items(), key=lambda x: x[1])[-INLINE_SEEN_LIMIT:])
    all_tracks = get_all_tracks_from_db(only_ok=True)
    if not all_tracks:
        inline_seen[user_id] = seen; return []
    available = [t for t in all_tracks if t[0] not in seen]
    if len(available) < limit: seen = {}; available = all_tracks
    random.shuffle(available)
    chosen = available[:limit]
    for fid, _ in chosen: seen[fid] = now
    inline_seen[user_id] = seen
    return chosen


def cleanup_inline_result_cache():
    now = time.time()
    to_delete = [rid for rid, (_, ts) in inline_result_cache.items() if now - ts > INLINE_RESULT_CACHE_TTL]
    for rid in to_delete: del inline_result_cache[rid]
