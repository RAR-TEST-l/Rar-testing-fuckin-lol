import random
import re
import time
import traceback
from html import escape as html_escape
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InlineQueryResultCachedAudio, InputTextMessageContent
from rar.config import INLINE_RESULT_CACHE_TTL, RAR_LOGO_URL
from rar.db import get_db_connection, get_playlist_by_id, get_playlist_by_name, get_track_by_num, get_tracks_by_nums, increment_play, increment_playlist_search, search_tracks_in_db
from rar.dialogs import answers_coin
from rar.state import inline_result_cache
from rar.utils import clean_title, cleanup_inline_result_cache, log_to_owner, pick_random_with_antirepeat, safe_answer_inline


async def inline_query_handler(update, context):
    query = update.inline_query.query.strip().lower()
    user_id = update.inline_query.from_user.id
    results = []
    cleanup_inline_result_cache()
    try:
        if query == "":
            tracks = pick_random_with_antirepeat(user_id, limit=10)
            for i, (fid, title) in enumerate(tracks):
                results.append(InlineQueryResultCachedAudio(id=f"empty_{i}_{fid[:20]}", audio_file_id=fid, caption=clean_title(title)))
            results.append(InlineQueryResultArticle(id="shuffle_empty", title="Новый набор песен", description="Показать другие случайные треки", thumbnail_url=RAR_LOGO_URL, input_message_content=InputTextMessageContent("Новый набор"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Новый набор", switch_inline_query_current_chat="дай песню")]])))
            await safe_answer_inline(update, context, results, cache_time=0)
        elif query in ["дай песню", "песня", "музыка"]:
            tracks = pick_random_with_antirepeat(user_id, limit=20)
            for i, (fid, title) in enumerate(tracks):
                results.append(InlineQueryResultCachedAudio(id=f"rand_{i}_{fid[:20]}", audio_file_id=fid, caption=clean_title(title)))
            results.append(InlineQueryResultArticle(id="shuffle", title="Новый набор песен", description="Показать другие случайные треки", thumbnail_url=RAR_LOGO_URL, input_message_content=InputTextMessageContent("Новый набор"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Новый набор", switch_inline_query_current_chat="дай песню")]])))
            await safe_answer_inline(update, context, results, cache_time=0)
        elif query.startswith("плейлист "):
            pl_name = query[9:].strip()
            if not pl_name:
                await update.inline_query.answer([], cache_time=0); return
            pl = get_playlist_by_name(pl_name)
            if not pl:
                await update.inline_query.answer([], cache_time=0); return
            pid, pname, owner_id, track_nums = pl
            if not track_nums:
                await update.inline_query.answer([], cache_time=0); return
            increment_playlist_search(pid)
            tracks = get_tracks_by_nums(track_nums)
            for i, (fid, title) in enumerate(tracks):
                rid = f"pl_{pid}_{i}_{int(time.time()*1000)}"
                inline_result_cache[rid] = (fid, time.time())
                results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=fid, caption=clean_title(title)))
            await safe_answer_inline(update, context, results, cache_time=0)
        elif query.startswith("найди ") or query.startswith("трек "):
            search = query[6:].strip()
            if not search:
                await update.inline_query.answer([], cache_time=0); return
            tracks = search_tracks_in_db(search, limit=20)
            for i, (fid, title, tnum) in enumerate(tracks):
                rid = f"find_{i}_{int(time.time()*1000)}_{fid[:10]}"
                inline_result_cache[rid] = (fid, time.time())
                results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=fid, caption=clean_title(title)))
            await safe_answer_inline(update, context, results, cache_time=0)
        elif query.startswith("монетка") or query.startswith("подкинь монетку"):
            coin_text = "Эээ... монетка встала ребром..." if random.randint(1, 50) == 50 else random.choice(answers_coin)
            results.append(InlineQueryResultArticle(id="coin", title="Бросить монетку", thumbnail_url=RAR_LOGO_URL, input_message_content=InputTextMessageContent(coin_text)))
            await safe_answer_inline(update, context, results, cache_time=0)
        else:
            tracks = search_tracks_in_db(query, limit=20)
            for i, (fid, title, tnum) in enumerate(tracks):
                rid = f"auto_{i}_{int(time.time()*1000)}_{fid[:10]}"
                inline_result_cache[rid] = (fid, time.time())
                results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=fid, caption=clean_title(title)))
            await safe_answer_inline(update, context, results, cache_time=0)
    except Exception as e:
        await log_to_owner(context, f"[INLINE OUTER] q='{query}'\n{e}\n{traceback.format_exc()[:800]}")
        try: await update.inline_query.answer([], cache_time=0)
        except Exception: pass


async def chosen_inline_result_handler(update, context):
    try:
        result = update.chosen_inline_result
        if not result: return
        rid = result.result_id
        if rid in inline_result_cache:
            fid, ts = inline_result_cache[rid]
            if time.time() - ts < INLINE_RESULT_CACHE_TTL:
                if rid.startswith("find_") or rid.startswith("auto_") or rid.startswith("pl_"):
                    increment_play(fid)
            del inline_result_cache[rid]
    except Exception as e:
        print(f"[CHOSEN INLINE ERROR] {e}")


def build_playlist_message(pid, index):
    pl = get_playlist_by_id(pid)
    if not pl: return None
    pid_, name, owner_id, track_nums = pl
    if not track_nums: return None
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT track_num, file_id, title FROM channel_music WHERE track_num = ANY(%s) AND inline_broken = FALSE", (track_nums,))
    rows = cursor.fetchall(); cursor.close(); conn.close()
    if not rows: return None
    ok_nums = [r[0] for r in rows]
    total = len(ok_nums); index = index % total
    tn = ok_nums[index]
    track = get_track_by_num(tn)
    if not track: return None
    fid, title = track
    text = f"Плейлист: <b>{html_escape(name)}</b>\n| {index+1}/{total} | {html_escape(clean_title(title))}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=f"pl_{pid}_{(index-1) % total}"), InlineKeyboardButton("Далее", callback_data=f"pl_{pid}_{(index+1) % total}")]])
    return text, fid, kb


async def playlist_nav_handler(update, context):
    query = update.callback_query
    try: await query.answer()
    except Exception: pass
    m = re.match(r"^pl_(\d+)_(\d+)$", query.data)
    if not m: return
    pid = int(m.group(1)); index = int(m.group(2))
    built = build_playlist_message(pid, index)
    if not built:
        try: await query.edit_message_text("Плейлист пуст или удалён")
        except Exception: pass
        return
    text_msg, fid, kb = built
    try: await query.message.delete()
    except Exception: pass
    try: await context.bot.send_audio(chat_id=query.message.chat_id, audio=fid, caption=text_msg, reply_markup=kb, parse_mode="HTML")
    except Exception as e: await log_to_owner(context, f"[PL NAV ERROR] {e}")
