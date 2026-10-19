import asyncio
import random
import re
import time
from html import escape as html_escape
from telegram import ChatPermissions
from telegram.error import RetryAfter
from rar.config import ADMIN_STATUSES, DUEL_ACTIONS, FAREWELL_PREFIXES, GREET_PREFIXES, GROUP_ANON_BOT_ID, PERM_NAMES_RU, PLAYLIST_LIMIT, RANK_LOW, RANK_MID, RANK_NAMES, RANK_NONE, RANK_PERMS, RANK_SENIOR, STATUS_ADMINISTRATOR, STATUS_BANNED, STATUS_CREATOR, STATUS_KICKED, STATUS_LEFT, STATUS_MEMBER, STATUS_RESTRICTED
from rar.db import add_track_to_playlist, create_playlist, delete_playlist, delete_track_from_db, get_admin_rank, get_all_admins, get_all_tracks_from_db, get_broken_tracks, get_chat_members, get_db_connection, get_kall_enabled, get_owner_id, get_playlist_by_name, get_random_playlist_with_tracks, get_storage_chat_id, get_top_playlists, get_top_tracks, get_track_num, get_user_playlists, increment_play, increment_playlist_search, remove_track_from_playlist, remove_users_from_chat_batch, rename_playlist, save_track_to_db, save_username, search_track_in_db, search_track_in_db_by_title, set_admin_rank, set_kall_enabled, sleuth_chat_has_any, sleuth_create, sleuth_delete, sleuth_get, sleuth_get_all, update_chat_setting
from rar.dialogs import ADMIN_DIALOGS, DUEL_FINAL_SOUNDS, DUEL_RAR_WIN_PHRASES, MUSIC_DIALOGS, SETTINGS_DIALOGS, SLEUTH_DIALOGS, answers_coin, answers_does, answers_hi, answers_love, answers_rar, answers_ref
from rar.games.duel import duel_rar_turn, duel_start
from rar.games.roulette import rl_start
from rar.handlers.inline import build_playlist_message
from rar.moderation import check_user_rank, get_chat_creator, resolve_user_from_arg, safe_promote
from rar.state import duel_games
from rar.utils import format_duration, log_to_owner, notify_moderation, parse_duration, uncache_user


rar_replies_history, does_replies_history, ref_replies_history = {}, {}, {}
recent_tracks_history, love_replies_history, hi_replies_history = {}, {}, {}


async def start_command(update, context):
    if update.effective_chat.id >= 0:
        text = (f"{SETTINGS_DIALOGS['start_title']}\n\n"
                f"{SETTINGS_DIALOGS['start_body']}\n\n"
                f"{SETTINGS_DIALOGS['start_hint']}")
        await update.message.reply_text(text, parse_mode="HTML")


async def handle_message(update, context):
    global rar_replies_history, does_replies_history, recent_tracks_history, ref_replies_history, hi_replies_history
    if not update.message or not update.effective_user: return
    if update.effective_user.is_bot: return
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    username = update.effective_user.username
    save_username(user_id, username)
    owner_id = get_owner_id()
    incoming = ""
    if update.message.text: incoming = update.message.text.lower().strip()
    elif update.message.caption: incoming = update.message.caption.lower().strip()
    text = update.message.text or ""
    clean = text.lower().strip()

    if chat_id < 0:
        txt_check = (update.message.text or "").strip().lower()
        is_command = txt_check.startswith("рар ") or txt_check.startswith("rar ") or txt_check == "рар" or txt_check == "rar"
        has_photo_only = update.message.photo and not update.message.caption
        if not is_command and not has_photo_only and sleuth_chat_has_any(chat_id):
            try:
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("UPDATE sleuths SET current_count = current_count + 1 WHERE chat_id = %s AND target_id = %s", (chat_id, user_id))
                conn.commit()
                cursor.execute("SELECT required_count, current_count, target_name, admin_name, target_id, admin_id FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, user_id))
                row = cursor.fetchone(); cursor.close(); conn.close()
                if row and row[1] >= row[0] and sleuth_delete(chat_id, user_id):
                    req, cur, tname, aname, tid, aid = row
                    t_display = f"@{tname}" if tname else f"ID {tid}"
                    a_display = f"@{aname}" if aname else f"ID {aid}"
                    try:
                        msg_text = SLEUTH_DIALOGS["done_chat"].format(цель=t_display, N=cur, M=req, админ=a_display)
                        await context.bot.send_message(chat_id=chat_id, text=msg_text)
                    except Exception: pass
                    await log_to_owner(context, f"[SLEUTH DONE] {t_display} {cur}/{req} chat={chat_id}")
            except Exception as e:
                print(f"[SLEUTH COUNT ERROR] {e}")

    if incoming.upper() in DUEL_ACTIONS:
        handled = False
        for gid, game in list(duel_games.items()):
            if game["chat_id"] != chat_id or game["user_id"] != user_id: continue
            if not game.get("active"): continue
            if game.get("state") != "waiting_player": continue
            expected = game.get("expected")
            if incoming.upper() != expected:
                game["active"] = False
                duel_games.pop(gid, None)
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"<b>{random.choice(DUEL_FINAL_SOUNDS)}</b>\n– {random.choice(DUEL_RAR_WIN_PHRASES)}", parse_mode="HTML")
                except Exception: pass
                handled = True
                break
            game["state"] = "answered"
            game["last_action"] = time.time()
            asyncio.create_task(duel_rar_turn(context, game))
            handled = True
            break
        if handled: return

    rl_match = re.match(r"^(рар|rar)[,.]?\s+рулетка\s*$", text, re.IGNORECASE)
    if rl_match:
        await rl_start(update, context); return

    duel_match = re.match(r"^(рар|rar)[,.]?\s+(дуэль|перестрелка)(?:\s+(хард))?\s*$", text, re.IGNORECASE)
    if duel_match:
        hard = bool(duel_match.group(3))
        await duel_start(update, context, hard=hard); return

    sleuth_match = re.match(r"^(рар|rar)[,.]?\s+слежка\s+(.+)$", text, re.IGNORECASE)
    if sleuth_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_MID): return
        args_raw = sleuth_match.group(2).strip()
        actor_creator = await get_chat_creator(chat_id, context)
        actor_rank = get_admin_rank(chat_id, user_id)
        actor_is_creator = (actor_creator and user_id == actor_creator)
        target_user = None; rest = args_raw
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        else:
            parts = args_raw.split(None, 1)
            if parts and parts[0].startswith("@"):
                target_user = await resolve_user_from_arg(context, chat_id, parts[0])
                rest = parts[1] if len(parts) > 1 else ""
        if not target_user:
            target_arg_check = args_raw.split()[0] if args_raw.split() and args_raw.split()[0].startswith("@") else "пользователя"
            await update.message.reply_text(
                f"Не нашла {html_escape(target_arg_check)}.\n\n"
                f"Возможно, он ещё ни разу не писал в чат после добавления Rar, "
                f"или указан неверно.\n\n"
                f"👉 Попроси его написать любое сообщение в чат, или используй <b>reply</b> на его сообщение."
            , parse_mode="HTML"); return
        target_creator = await get_chat_creator(chat_id, context)
        if target_creator and target_user.id == target_creator:
            await update.message.reply_text(SLEUTH_DIALOGS["protect_creator"]); return
        target_rank = get_admin_rank(chat_id, target_user.id)
        if target_rank >= RANK_SENIOR and not actor_is_creator:
            await update.message.reply_text(SLEUTH_DIALOGS["protect_senior"]); return
        dur_match = re.search(r"((?:\d+\s*[дчмсДЧМС]\s*)+)", rest, re.IGNORECASE)
        if not dur_match:
            await update.message.reply_text(SLEUTH_DIALOGS["no_duration"]); return
        duration_seconds = parse_duration(dur_match.group(1))
        if not duration_seconds:
            await update.message.reply_text(SLEUTH_DIALOGS["bad_duration"]); return
        rest_after_dur = rest[dur_match.end():].strip()
        count_match = re.search(r"(\d+)", rest_after_dur)
        if not count_match:
            await update.message.reply_text(SLEUTH_DIALOGS["no_count"]); return
        required = int(count_match.group(1))
        action_part = rest_after_dur[count_match.end():].strip().lower()
        action = "none"
        if "бан" in action_part: action = "ban"
        elif "кик" in action_part: action = "kick"
        if action in ("ban", "kick") and not actor_is_creator and actor_rank < RANK_SENIOR:
            await update.message.reply_text(SLEUTH_DIALOGS["no_rights_action"]); return
        sleuth_create(chat_id, target_user.id, target_user.username, user_id, username, required, duration_seconds, action)
        t_display = f"@{target_user.username}" if target_user.username else f'<a href="tg://user?id={target_user.id}">{target_user.first_name}</a>'
        dur_str = format_duration(duration_seconds)
        msg_text = SLEUTH_DIALOGS["started"].format(цель=t_display, N=required, срок=dur_str)
        await update.message.reply_text(msg_text, parse_mode="HTML")
        await log_to_owner(context, f"[SLEUTH START] target={target_user.id} by={user_id} req={required} dur={duration_seconds} action={action} chat={chat_id}")
        return

    unsleuth_match = re.match(r"^(рар|rar)[,.]?\s+(?:снять|сними)\s+слежку\s*(.*)$", text, re.IGNORECASE)
    if unsleuth_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        target_arg = unsleuth_match.group(2).strip(); target_user = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        elif target_arg:
            target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target_reply"]); return
        row = sleuth_get(chat_id, target_user.id)
        if not row:
            await update.message.reply_text(SLEUTH_DIALOGS["no_active_on_user"]); return
        admin_id = row[3]
        creator = await get_chat_creator(chat_id, context)
        if user_id != admin_id and not (creator and user_id == creator):
            await update.message.reply_text("Снять слежку может только тот, кто её повесил, или создатель чата."); return
        sleuth_delete(chat_id, target_user.id)
        t_display = f"@{target_user.username}" if target_user.username else f'<a href="tg://user?id={target_user.id}">{target_user.first_name}</a>'
        msg_text = SLEUTH_DIALOGS["cancelled"].format(цель=t_display)
        await update.message.reply_text(msg_text, parse_mode="HTML")
        await log_to_owner(context, f"[SLEUTH CANCEL] target={target_user.id} by={user_id} chat={chat_id}")
        return

    if clean in ["рар слежки", "rar слежки", "рар, слежки", "rar, слежки"]:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_MID): return
        rows = sleuth_get_all(chat_id)
        if not rows:
            await update.message.reply_text(SLEUTH_DIALOGS["no_active"]); return
        lines = [SLEUTH_DIALOGS["list_header"]]
        for r in rows:
            sid, tid, tname, aid, aname, req, cur, dur, action, deadline = r
            left = deadline.timestamp() - time.time() if deadline else 0
            left_str = format_duration(int(max(0, left)))
            t_display = f"@{tname}" if tname else f"ID {tid}"
            act_str = f" [{action}]" if action != "none" else ""
            lines.append(SLEUTH_DIALOGS["list_item"].format(цель=t_display, N=cur, M=req, action=act_str, время=left_str))
        await update.message.reply_text("\n".join(lines), parse_mode="HTML"); return

    if clean in ["рар вкл калл", "rar вкл калл", "рар, вкл калл", "rar, вкл калл", "рар выкл калл", "rar выкл калл", "рар, выкл калл", "rar, выкл калл"]:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        val = "вкл" in clean
        set_kall_enabled(chat_id, val)
        await update.message.reply_text(ADMIN_DIALOGS["kall_on"] if val else ADMIN_DIALOGS["kall_off"]); return

    admin_match = re.match(r"^(рар|rar)[,.]?\s+админ\b(?:\s+(\+\+|--|\+|-))?\s*(.*)$", text, re.IGNORECASE)
    if admin_match:
        action = admin_match.group(2); target_arg = admin_match.group(3).strip()
        target_user = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        elif target_arg:
            target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target_msg"]); return
        if action is None:
            rank = get_admin_rank(chat_id, target_user.id)
            creator = await get_chat_creator(chat_id, context)
            if creator and target_user.id == creator:
                await update.message.reply_text(ADMIN_DIALOGS["is_creator"].format(имя=target_user.first_name)); return
            try:
                tg_member = await context.bot.get_chat_member(chat_id, target_user.id)
                tg_is_admin = tg_member.status in ADMIN_STATUSES
            except Exception: tg_is_admin = False
            line = ADMIN_DIALOGS["rank_line"].format(имя=target_user.first_name, ранг=RANK_NAMES.get(rank, 'не админ'))
            if tg_is_admin and rank == RANK_NONE:
                line += "\n" + ADMIN_DIALOGS["sync_hint"]
            await update.message.reply_text(line, parse_mode="HTML"); return
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_long"]); return
        actor_creator = await get_chat_creator(chat_id, context)
        actor_rank = get_admin_rank(chat_id, user_id)
        actor_is_creator = (actor_creator and user_id == actor_creator)
        if not actor_is_creator and actor_rank < RANK_SENIOR:
            await update.message.reply_text(ADMIN_DIALOGS["no_rights"]); return
        target_creator = await get_chat_creator(chat_id, context)
        if target_creator and target_user.id == target_creator:
            await update.message.reply_text(ADMIN_DIALOGS["protect_creator"]); return
        if target_user.id == user_id:
            await update.message.reply_text(ADMIN_DIALOGS["self"]); return
        target_rank = get_admin_rank(chat_id, target_user.id)
        if not actor_is_creator and target_rank >= RANK_SENIOR:
            await update.message.reply_text(ADMIN_DIALOGS["protect_senior"]); return
        new_rank = target_rank
        if action == "+":
            if target_rank == RANK_NONE: new_rank = RANK_LOW
            else: await update.message.reply_text(ADMIN_DIALOGS["already_admin"]); return
        elif action == "++":
            if target_rank == RANK_NONE: await update.message.reply_text(ADMIN_DIALOGS["not_admin"]); return
            elif target_rank < RANK_SENIOR: new_rank = target_rank + 1
            else: await update.message.reply_text(ADMIN_DIALOGS["already_senior"]); return
        elif action == "-":
            if target_rank > RANK_LOW: new_rank = target_rank - 1
            else: await update.message.reply_text(ADMIN_DIALOGS["already_low"]); return
        elif action == "--": new_rank = RANK_NONE

        wanted = RANK_PERMS.get(new_rank, RANK_PERMS[RANK_NONE])
        ok, granted, skipped, err = await safe_promote(context, chat_id, target_user.id, wanted)

        set_admin_rank(chat_id, target_user.id, new_rank, user_id)

        if not ok or skipped:
            target_disp = f"{html_escape(target_user.first_name)} [ID: {target_user.id}]"
            if not ok:
                skipped_ru = "\n".join(f"• {PERM_NAMES_RU.get(p, p)}" for p in skipped if p != "(fallback)") if skipped else ""
                lines_ls = [
                    f"<b>Ошибка повышения</b>",
                    f"Цель: {target_disp}",
                    f"Ранг в БД: {RANK_NAMES.get(new_rank, '?')}",
                    f"Telegram-права не выданы.",
                    f"Ошибка:",
                    f"<code>{html_escape(err[:800])}</code>",
                ]
                if skipped_ru:
                    lines_ls.append(f"\n<b>Не выданы права</b> (у бота их нет):\n{skipped_ru}")
                try:
                    await context.bot.send_message(chat_id=user_id, text="\n".join(lines_ls), parse_mode="HTML")
                except Exception: pass
                await log_to_owner(context, f"[PROMOTE ERROR] chat={chat_id} target={target_user.id} err={err}")
            else:
                skipped_ru = "\n".join(f"• {PERM_NAMES_RU.get(p, p)}" for p in skipped if p != "(fallback)")
                if skipped_ru:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(f"<b>Повышение с ограничениями</b>\n"
                                  f"Цель: {target_disp}\n"
                                  f"Ранг в БД: {RANK_NAMES.get(new_rank, '?')}\n\n"
                                  f"<b>Не выданы права</b> (у бота их нет):\n{skipped_ru}"),
                            parse_mode="HTML")
                    except Exception: pass
                await log_to_owner(context, f"[PROMOTE PARTIAL] chat={chat_id} target={target_user.id} skipped={skipped}")

        await update.message.reply_text(ADMIN_DIALOGS["rank_changed"].format(имя=html_escape(target_user.first_name), ранг=RANK_NAMES.get(new_rank, 'не админ')))
        return

    if incoming in ["рар админы", "rar админы", "рар, админы", "rar, админы"]:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        admins = get_all_admins(chat_id)
        creator = await get_chat_creator(chat_id, context)
        lines = [ADMIN_DIALOGS["admins_header"]]
        if creator:
            try:
                u = await context.bot.get_chat(creator); lines.append(ADMIN_DIALOGS["creator_line"].format(имя=u.first_name))
            except Exception: lines.append(ADMIN_DIALOGS["creator_line"].format(имя=f"ID {creator}"))
        for uid, rank in admins:
            try:
                u = await context.bot.get_chat(uid); lines.append(ADMIN_DIALOGS["rank_line"].format(имя=u.first_name, ранг=RANK_NAMES.get(rank, '?')))
            except Exception: lines.append(ADMIN_DIALOGS["rank_line"].format(имя=f"ID {uid}", ранг=RANK_NAMES.get(rank, '?')))
        if len(lines) == 1: lines.append(ADMIN_DIALOGS["admins_empty"])
        await update.message.reply_text("\n".join(lines)); return

    ban_match = re.match(r"^(рар|rar)[,.]?\s+бан\s*(.*)$", text, re.IGNORECASE)
    if ban_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        target_user = None; reason = ban_match.group(2).strip()
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        elif reason:
            parts = reason.split(None, 1); target_arg = parts[0]
            reason = parts[1] if len(parts) > 1 else ""
            target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target"]); return
        creator = await get_chat_creator(chat_id, context)
        if creator and target_user.id == creator:
            await update.message.reply_text(ADMIN_DIALOGS["cant_ban_creator"]); return
        try:
            await context.bot.ban_chat_member(chat_id, target_user.id)
            msg_text = ADMIN_DIALOGS["banned_reason"].format(имя=target_user.first_name, причина=reason) if reason else ADMIN_DIALOGS["banned"].format(имя=target_user.first_name)
            await update.message.reply_text(msg_text)
            await log_to_owner(context, f"[BAN] {target_user.first_name} [ID: {target_user.id}] Чат: {chat_id}. Причина: {reason or '-'}")
        except Exception as e:
            await update.message.reply_text(ADMIN_DIALOGS["no_ban_rights"])
            await log_to_owner(context, f"[BAN ERROR] {e}")
        return

    unban_match = re.match(r"^(рар|rar)[,.]?\s+разбан\s*(.*)$", text, re.IGNORECASE)
    if unban_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        target_arg = unban_match.group(2).strip(); target_user = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        elif target_arg:
            target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target"]); return
        try:
            await context.bot.unban_chat_member(chat_id, target_user.id, only_if_banned=True)
            await update.message.reply_text(ADMIN_DIALOGS["unbanned"].format(имя=target_user.first_name))
        except Exception as e:
            await update.message.reply_text(ADMIN_DIALOGS["unban_fail"])
            await log_to_owner(context, f"[UNBAN ERROR] {e}")
        return

    kick_match = re.match(r"^(рар|rar)[,.]?\s+кик\s*(.*)$", text, re.IGNORECASE)
    if kick_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        target_user = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        else:
            target_arg = kick_match.group(2).strip()
            if target_arg:
                target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target"]); return
        creator = await get_chat_creator(chat_id, context)
        if creator and target_user.id == creator:
            await update.message.reply_text(ADMIN_DIALOGS["cant_kick_creator"]); return
        try:
            await context.bot.ban_chat_member(chat_id, target_user.id)
            await context.bot.unban_chat_member(chat_id, target_user.id)
            await update.message.reply_text(ADMIN_DIALOGS["kicked"].format(имя=target_user.first_name))
        except Exception as e:
            await update.message.reply_text(ADMIN_DIALOGS["kick_fail"])
            await log_to_owner(context, f"[KICK ERROR] {e}")
        return

    mute_match = re.match(r"^(рар|rar)[,.]?\s+мут\s+(.+)$", text, re.IGNORECASE)
    if mute_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_MID): return
        args = mute_match.group(2).strip(); target_user = None; duration_seconds = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
            duration_seconds = parse_duration(args)
        else:
            parts = args.split(None, 1); target_arg = parts[0]
            dur_text = parts[1] if len(parts) > 1 else ""
            duration_seconds = parse_duration(dur_text)
            target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target"]); return
        if duration_seconds is None: duration_seconds = 600
        until = int(time.time()) + duration_seconds
        try:
            await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
            await update.message.reply_text(ADMIN_DIALOGS["muted"].format(имя=target_user.first_name, срок=format_duration(duration_seconds)))
        except Exception as e:
            await update.message.reply_text(ADMIN_DIALOGS["mute_fail"])
            await log_to_owner(context, f"[MUTE ERROR] {e}")
        return

    unmute_match = re.match(r"^(рар|rar)[,.]?\s+размут\s*(.*)$", text, re.IGNORECASE)
    if unmute_match:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_MID): return
        target_user = None
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            target_user = update.message.reply_to_message.from_user
        else:
            target_arg = unmute_match.group(2).strip()
            if target_arg:
                target_user = await resolve_user_from_arg(context, chat_id, target_arg)
        if not target_user:
            await update.message.reply_text(ADMIN_DIALOGS["no_target"]); return
        try:
            await context.bot.restrict_chat_member(chat_id, target_user.id, permissions=ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True))
            await update.message.reply_text(ADMIN_DIALOGS["unmuted"].format(имя=target_user.first_name))
        except Exception as e:
            await update.message.reply_text(ADMIN_DIALOGS["unmute_fail"])
            await log_to_owner(context, f"[UNMUTE ERROR] {e}")
        return

    add_to_pl = re.match(r"^(рар|rar)[,.]?\s+добав(ь|ить)\s+в\s+(.+)$", text, re.IGNORECASE)
    if add_to_pl:
        pl_name = add_to_pl.group(3).strip(); target = None
        if update.message.reply_to_message and update.message.reply_to_message.audio:
            target = update.message.reply_to_message.audio
        elif update.message.audio: target = update.message.audio
        if not target:
            await update.message.reply_text(MUSIC_DIALOGS["attach_audio_pl"].format(имя=pl_name)); return
        pl = get_playlist_by_name(pl_name)
        if not pl:
            await update.message.reply_text(MUSIC_DIALOGS["pl_not_found"].format(имя=html_escape(pl_name)), parse_mode="HTML"); return
        pid, pname, pl_owner, track_nums = pl
        if user_id != pl_owner and user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["pl_not_yours"]); return
        perf = target.performer.strip() if target.performer else ""
        title = target.title.strip() if target.title else ""
        track_title = f"{perf} - {title}" if perf and title else (target.file_name or "Неизвестный трек")
        tn = get_track_num(target.file_id)
        if not tn:
            existing = search_track_in_db_by_title(track_title)
            if existing: tn = existing[2]
            else:
                save_track_to_db(target.file_id, track_title, user_id); tn = get_track_num(target.file_id)
        if not tn:
            await update.message.reply_text(MUSIC_DIALOGS["something_wrong"]); return
        result = add_track_to_playlist(pid, tn)
        if result == "already": await update.message.reply_text(MUSIC_DIALOGS["track_already"])
        elif result == "limit": await update.message.reply_text(MUSIC_DIALOGS["pl_limit"].format(N=PLAYLIST_LIMIT))
        elif result == "ok": await update.message.reply_text(MUSIC_DIALOGS["pl_added"].format(имя=html_escape(pname)), parse_mode="HTML")
        return

    if re.match(r"^(рар|rar)[,.]?\s+добав(ь|ить)$", incoming) or incoming in ["добавь", "добавить"]:
        target = None
        if update.message.reply_to_message and update.message.reply_to_message.audio:
            target = update.message.reply_to_message.audio
        elif update.message.audio: target = update.message.audio
        if target:
            perf = target.performer.strip() if target.performer else ""
            title = target.title.strip() if target.title else ""
            track_title = f"{perf} - {title}" if perf and title else (target.file_name or "Неизвестный трек")
            if save_track_to_db(target.file_id, track_title, user_id):
                storage = get_storage_chat_id()
                if storage:
                    try:
                        cap = f"{track_title}\n\nДобавил: @{username}" if username else track_title
                        if len(cap) > 1020: cap = cap[:1017] + "..."
                        await context.bot.send_audio(chat_id=storage, audio=target.file_id, caption=cap)
                    except Exception: pass
                try:
                    await context.bot.send_audio(chat_id=chat_id, audio=target.file_id, caption=f"{MUSIC_DIALOGS['track_added']}\n\n{MUSIC_DIALOGS['track_db_name'].format(имя=track_title)}")
                except Exception: pass
            else: await update.message.reply_text(MUSIC_DIALOGS["track_saved"].format(имя=track_title))
        else: await update.message.reply_text(MUSIC_DIALOGS["attach_audio"])
        return

    if not text: return

    if clean in ["рар команды", "rar команды", "рар, команды", "rar, команды"]:
        await update.message.reply_text(
            "<b>Команды Rar:</b>\n\n<b>Музыка:</b>\n"
            "• <code>Рар добавь</code> / <code>Рар добавить</code>\n"
            "• <code>Рар дай песню</code>\n"
            "• <code>Рар найди</code> / <code>Рар найти</code> [название]\n"
            "• <code>Рар топ песен</code>\n\n<b>Плейлисты:</b>\n"
            "• <code>Рар создай плейлист</code> [название]\n"
            "• <code>Рар добавь в</code> [плейлист]\n"
            "• <code>Рар удали</code> [песня] <code>из</code> [плейлист]\n"
            "• <code>Рар удали</code> [плейлист]\n"
            "• <code>Рар переименуй</code> [X] <code>в</code> [Y]\n"
            "• <code>Рар мои плейлисты</code>\n"
            "• <code>Рар дай плейлист</code>\n"
            "• <code>Рар найди плейлист</code> [название]\n"
            "• <code>Рар топ плейлистов</code>\n\n<b>Игры:</b>\n"
            "• <code>Рар рулетка</code>\n"
            "• <code>Рар дуэль</code> / <code>Рар перестрелка</code>\n"
            "• <code>Рар дуэль хард</code>\n\n<b>Слежка:</b>\n"
            "• <code>Рар слежка</code> @user [срок] [норма] [кик/бан]\n"
            "• <code>Рар снять слежку</code> @user\n"
            "• <code>Рар слежки</code>\n\n<b>Админ-система:</b>\n"
            "• <code>Рар вкл/выкл калл</code> (ст.)\n"
            "• <code>Рар админ</code> @user\n"
            "• <code>Рар админ +</code> @user (ст.)\n"
            "• <code>Рар админ ++</code> / <code>Рар админ -</code> / <code>Рар админ --</code>\n"
            "• <code>Рар админы</code>\n"
            "• <code>Рар бан</code> @user [причина] (ст.)\n"
            "• <code>Рар разбан</code> @user (ст.)\n"
            "• <code>Рар кик</code> @user (ст.)\n"
            "• <code>Рар мут</code> @user [время] (ср.)\n"
            "• <code>Рар размут</code> @user (ср.)\n\n<b>Настройки (ст. админы):</b>\n"
            "• <code>Рар вкл/выкл приветствие</code>\n"
            "• <code>Рар вкл/выкл прощание</code>\n"
            "• <code>Рар измени приветствие/прощание</code> [текст]\n\n"
            "<b>Инлайн:</b> <code>@ChRarBot</code> / <code>дай песню</code> / <code>найди X</code> / <code>плейлист X</code> / <code>монетка</code>",
            parse_mode="HTML"); return

    if clean in ["рар проблемные", "rar проблемные"]:
        if user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["creator_only"]); return
        broken = get_broken_tracks()
        if not broken:
            await update.message.reply_text(MUSIC_DIALOGS["no_broken"]); return
        lines = [MUSIC_DIALOGS["broken_header"].format(N=len(broken))]
        for tn, title in broken[:30]: lines.append(MUSIC_DIALOGS["broken_line"].format(N=tn, трек=title))
        if len(broken) > 30: lines.append(MUSIC_DIALOGS["broken_more"].format(N=len(broken) - 30))
        await update.message.reply_text("\n".join(lines)); return

    create_pl = re.match(r"^(рар|rar)[,.]?\s+созда(й|ть)\s+плейлист\s+(.+)$", text, re.IGNORECASE)
    if create_pl:
        pl_name = create_pl.group(3).strip()
        pid, err = create_playlist(pl_name, user_id)
        if err == "exists":
            await update.message.reply_text(MUSIC_DIALOGS["name_exists"]); return
        if err:
            await update.message.reply_text(MUSIC_DIALOGS["error"].format(ошибка=err)); return
        await update.message.reply_text(MUSIC_DIALOGS["pl_created"].format(имя=html_escape(pl_name)), parse_mode="HTML")
        uname = f"@{username}" if username else (update.effective_user.first_name or "друг")
        await notify_moderation(context, f"Создан плейлист\n{html_escape(pl_name)}\nКто: {html_escape(uname)} [ID: {user_id}]")
        return

    if clean in ["рар мои плейлисты", "rar мои плейлисты", "рар, мои плейлисты", "rar, мои плейлисты"]:
        pls = get_user_playlists(user_id)
        if not pls:
            await update.message.reply_text(MUSIC_DIALOGS["no_playlists"], parse_mode="HTML"); return
        lines = [MUSIC_DIALOGS["your_playlists"]]
        for pid, name, track_nums in pls: lines.append(MUSIC_DIALOGS["playlist_line"].format(имя=name, N=len(track_nums or [])))
        await update.message.reply_text("\n".join(lines)); return

    if clean in ["рар дай плейлист", "rar дай плейлист", "рар, дай плейлист", "rar, дай плейлист"]:
        result = get_random_playlist_with_tracks(user_id)
        if not result:
            await update.message.reply_text(MUSIC_DIALOGS["no_pl_with_tracks"]); return
        pid, pname, pl_owner, track_nums = result
        built = build_playlist_message(pid, 0)
        if not built:
            await update.message.reply_text(MUSIC_DIALOGS["error"].format(ошибка="empty")); return
        text_msg, fid, kb = built
        try: await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=text_msg, reply_markup=kb, parse_mode="HTML")
        except Exception as e: await log_to_owner(context, f"[PL SEND ERROR] {e}")
        return

    find_pl = re.match(r"^(рар|rar)[,.]?\s+най(ди|ти)\s+плейлист\s+(.+)$", text, re.IGNORECASE)
    if find_pl:
        pl_name = find_pl.group(3).strip()
        pl = get_playlist_by_name(pl_name)
        if not pl:
            await update.message.reply_text(MUSIC_DIALOGS["not_found"].format(имя=html_escape(pl_name)), parse_mode="HTML"); return
        pid, pname, pl_owner, track_nums = pl
        if not track_nums:
            await update.message.reply_text(MUSIC_DIALOGS["pl_empty"]); return
        increment_playlist_search(pid)
        built = build_playlist_message(pid, 0)
        if not built:
            await update.message.reply_text(MUSIC_DIALOGS["error"].format(ошибка="empty")); return
        text_msg, fid, kb = built
        try: await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=text_msg, reply_markup=kb, parse_mode="HTML")
        except Exception as e: await log_to_owner(context, f"[PL SEND ERROR] {e}")
        return

    if clean in ["рар топ плейлистов", "rar топ плейлистов"]:
        top = get_top_playlists(limit=10)
        if not top:
            await update.message.reply_text(MUSIC_DIALOGS["top_empty"]); return
        lines = [MUSIC_DIALOGS["top_pl_header"]]
        for i, (name, searches) in enumerate(top, 1):
            lines.append(MUSIC_DIALOGS["top_pl_line"].format(N=i, имя=name, поиски=searches))
        await update.message.reply_text("\n".join(lines)); return

    del_from = re.match(r"^(рар|rar)[,.]?\s+удал(и|ить)\s+(.+?)\s+из\s+(?:плейлист[а]?\s+)?(.+)$", text, re.IGNORECASE)
    if del_from:
        track_query = del_from.group(3).strip(); pl_name = del_from.group(4).strip()
        pl = get_playlist_by_name(pl_name)
        if not pl:
            await update.message.reply_text(MUSIC_DIALOGS["not_found"].format(имя=html_escape(pl_name)), parse_mode="HTML"); return
        pid, pname, pl_owner, track_nums = pl
        if user_id != pl_owner and user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["not_yours_strict"]); return
        if not track_nums:
            await update.message.reply_text(MUSIC_DIALOGS["pl_empty_short"]); return
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT track_num, title FROM channel_music WHERE track_num = ANY(%s) AND LOWER(title) LIKE LOWER(%s)", (track_nums, f"%{track_query}%"))
        found = cursor.fetchone(); cursor.close(); conn.close()
        if not found:
            await update.message.reply_text(MUSIC_DIALOGS["track_not_in_pl"].format(запрос=track_query, плейлист=pl_name)); return
        found_tn, found_title = found
        remove_track_from_playlist(pid, found_tn)
        await update.message.reply_text(MUSIC_DIALOGS["track_removed"].format(трек=found_title, плейлист=pl_name)); return

    del_pl = re.match(r"^(рар|rar)[,.]?\s+удал(и|ить)\s+(?:плейлист\s+)?(.+)$", text, re.IGNORECASE)
    if del_pl:
        pl_name = del_pl.group(3).strip()
        pl = get_playlist_by_name(pl_name)
        if not pl:
            await update.message.reply_text(MUSIC_DIALOGS["not_found"].format(имя=html_escape(pl_name)), parse_mode="HTML"); return
        pid, pname, pl_owner, track_nums = pl
        if user_id != pl_owner and user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["not_yours_strict"]); return
        delete_playlist(pid)
        await update.message.reply_text(MUSIC_DIALOGS["pl_deleted"].format(имя=html_escape(pname)), parse_mode="HTML")
        uname = f"@{username}" if username else (update.effective_user.first_name or "друг")
        await notify_moderation(context, f"Удалён плейлист\n{pname}\nКто: {uname}")
        return

    ren_pl = re.match(r"^(рар|rar)[,.]?\s+переимену(й|ть)\s+(?:плейлист\s+)?(.+?)\s+в\s+(.+)$", text, re.IGNORECASE)
    if ren_pl:
        old_name = ren_pl.group(3).strip(); new_name = ren_pl.group(4).strip()
        pl = get_playlist_by_name(old_name)
        if not pl:
            await update.message.reply_text(MUSIC_DIALOGS["not_found"].format(имя=html_escape(old_name)), parse_mode="HTML"); return
        pid, pname, pl_owner, track_nums = pl
        if user_id != pl_owner and user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["not_yours_strict"]); return
        err = rename_playlist(pid, new_name)
        if err == "exists":
            await update.message.reply_text(MUSIC_DIALOGS["name_exists"]); return
        await update.message.reply_text(MUSIC_DIALOGS["renamed"].format(старое=html_escape(old_name), новое=html_escape(new_name)), parse_mode="HTML")
        uname = f"@{username}" if username else (update.effective_user.first_name or "друг")
        await notify_moderation(context, f"Переименован плейлист\n{old_name} -> {new_name}\nКто: {uname}")
        return

    if clean in ["рар удали трек", "rar удали трек"]:
        if user_id != owner_id:
            await update.message.reply_text(MUSIC_DIALOGS["creator_only"]); return
        if not update.message.reply_to_message or not update.message.reply_to_message.audio:
            await update.message.reply_text(MUSIC_DIALOGS["reply_audio"]); return
        audio = update.message.reply_to_message.audio
        perf = audio.performer.strip() if audio.performer else ""
        title = audio.title.strip() if audio.title else ""
        fallback = f"{perf} - {title}" if perf and title else (audio.file_name or None)
        deleted = delete_track_from_db(audio.file_id, fallback)
        if deleted: await update.message.reply_text(MUSIC_DIALOGS["track_deleted"].format(имя=deleted))
        else: await update.message.reply_text(MUSIC_DIALOGS["not_found_short"])
        return

    if clean in ["рар топ песен", "rar топ песен", "рар топ песни", "rar топ песни"]:
        top = get_top_tracks(limit=10)
        if not top:
            await update.message.reply_text(MUSIC_DIALOGS["top_empty"]); return
        lines = [MUSIC_DIALOGS["top_tracks_header"]]
        for i, (title, plays) in enumerate(top, 1):
            lines.append(MUSIC_DIALOGS["top_track_line"].format(N=i, трек=title, прослушивания=plays))
        await update.message.reply_text("\n".join(lines)); return

    toggle_map = {
        "рар вкл приветствие": ("greet_enabled", True, SETTINGS_DIALOGS["greet_on"]),
        "рар выкл приветствие": ("greet_enabled", False, SETTINGS_DIALOGS["greet_off"]),
        "rar вкл приветствие": ("greet_enabled", True, SETTINGS_DIALOGS["greet_on"]),
        "rar выкл приветствие": ("greet_enabled", False, SETTINGS_DIALOGS["greet_off"]),
        "рар вкл прощание": ("farewell_enabled", True, SETTINGS_DIALOGS["farewell_on"]),
        "рар выкл прощание": ("farewell_enabled", False, SETTINGS_DIALOGS["farewell_off"]),
        "rar вкл прощание": ("farewell_enabled", True, SETTINGS_DIALOGS["farewell_on"]),
        "rar выкл прощание": ("farewell_enabled", False, SETTINGS_DIALOGS["farewell_off"]),
    }
    if clean in toggle_map:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        field, val, msg = toggle_map[clean]
        update_chat_setting(chat_id, field, val)
        await update.message.reply_text(msg); return

    matched_greet = next((p for p in GREET_PREFIXES if clean == p or clean.startswith(p + " ")), None)
    matched_fare = next((p for p in FAREWELL_PREFIXES if clean == p or clean.startswith(p + " ")), None)
    if matched_greet or matched_fare:
        if chat_id >= 0:
            await update.message.reply_text(ADMIN_DIALOGS["group_only_short"]); return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_SENIOR): return
        matched = matched_greet or matched_fare
        new_text = text.strip()[len(matched):].strip()
        if not new_text:
            await update.message.reply_text(SETTINGS_DIALOGS["write_text"]); return
        if matched_greet:
            update_chat_setting(chat_id, "greet_text", new_text)
            await update.message.reply_text(f"{SETTINGS_DIALOGS['greet_updated']}\n\n{new_text}")
        else:
            update_chat_setting(chat_id, "farewell_text", new_text)
            await update.message.reply_text(f"{SETTINGS_DIALOGS['farewell_updated']}\n\n{new_text}")
        return

    if clean in ["rar", "рар"]:
        if chat_id not in rar_replies_history: rar_replies_history[chat_id] = []
        avail = [a for a in answers_rar if a not in rar_replies_history[chat_id]] or answers_rar
        r = random.choice(avail)
        rar_replies_history[chat_id].append(r)
        if len(rar_replies_history[chat_id]) > 2: rar_replies_history[chat_id].pop(0)
        await update.message.reply_text(r); return

    if clean in ["рар, подкинь монетку","rar, подкинь монетку","рар подкинь монетку","rar подкинь монетку","рар, монетка","rar, монетка","рар монетка","rar монетка"]:
        if random.randint(1, 50) == 50:
            await update.message.reply_text("Эээ... монетка встала ребром..."); return
        await update.message.reply_text(random.choice(answers_coin)); return

    if clean in ["rar, привет","rar привет","рар, привет","рар привет"]:
        if chat_id not in hi_replies_history: hi_replies_history[chat_id] = []
        avail = [a for a in answers_hi if a not in hi_replies_history[chat_id]] or answers_hi
        r = random.choice(avail)
        hi_replies_history[chat_id].append(r)
        if len(hi_replies_history[chat_id]) > 2: hi_replies_history[chat_id].pop(0)
        await update.message.reply_text(r); return

    if clean in ["we.all.love.rar", "we.all.love.rar."]:
        if chat_id not in love_replies_history: love_replies_history[chat_id] = []
        avail = [a for a in answers_love if a not in love_replies_history[chat_id]] or answers_love
        r = random.choice(avail)
        love_replies_history[chat_id].append(r)
        if len(love_replies_history[chat_id]) > 2: love_replies_history[chat_id].pop(0)
        await update.message.reply_text(r); return

    if clean in ["rar, дай отсылку","rar дай отсылку","rar, отсылка","rar отсылка","рар, дай отсылку","рар дай отсылку","рар, отсылка","рар отсылка"]:
        if chat_id not in ref_replies_history: ref_replies_history[chat_id] = []
        avail = [a for a in answers_ref if a not in ref_replies_history[chat_id]] or answers_ref
        r = random.choice(avail)
        ref_replies_history[chat_id].append(r)
        if len(ref_replies_history[chat_id]) > 15: ref_replies_history[chat_id].pop(0)
        await update.message.reply_text(r); return

    if clean in ["rar, что делаешь?","рар, что делаешь?","rar что делаешь?","рар что делаешь?"]:
        if chat_id not in does_replies_history: does_replies_history[chat_id] = []
        avail = [a for a in answers_does if a not in does_replies_history[chat_id]] or answers_does
        r = random.choice(avail)
        does_replies_history[chat_id].append(r)
        if len(does_replies_history[chat_id]) > 2: does_replies_history[chat_id].pop(0)
        await update.message.reply_text(r); return

    if clean in ["rar дай песню","рар дай песню","rar дай музыку","рар дай музыку"]:
        try:
            tracks = get_all_tracks_from_db(only_ok=True)
            if not tracks:
                await update.message.reply_text(MUSIC_DIALOGS["collection_empty"]); return
            if chat_id not in recent_tracks_history or not isinstance(recent_tracks_history[chat_id], list):
                recent_tracks_history[chat_id] = []
            avail = [t for t in tracks if t not in recent_tracks_history[chat_id]] or tracks
            if not avail: recent_tracks_history[chat_id] = []; avail = tracks
            fid, ttitle = random.choice(avail)
            recent_tracks_history[chat_id].append(fid)
            lim = max(5, len(tracks) // 3)
            if len(recent_tracks_history[chat_id]) > lim: recent_tracks_history[chat_id].pop(0)
            try: await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=f"{MUSIC_DIALOGS['here_song']}\n\n{ttitle}")
            except Exception as e: print(f"[SEND AUDIO] {e}")
        except Exception as e: await update.message.reply_text(MUSIC_DIALOGS["error"].format(ошибка=e))
        return

    if clean == "калл":
        if chat_id >= 0:
            return
        if not get_kall_enabled(chat_id):
            return
        if not await check_user_rank(update, context, chat_id, user_id, RANK_MID): return
        try:
            user_ids = get_chat_members(chat_id)
            if not user_ids:
                await update.message.reply_text(ADMIN_DIALOGS["empty_list"]); return
            valid = [STATUS_MEMBER, STATUS_ADMINISTRATOR, STATUS_CREATOR, STATUS_RESTRICTED]
            tags, to_remove = [], []
            sem = asyncio.Semaphore(3)

            async def _check_member(uid):
                async with sem:
                    for attempt in range(2):
                        try:
                            return uid, await context.bot.get_chat_member(chat_id, uid), ""
                        except RetryAfter as e:
                            await asyncio.sleep(e.retry_after + 0.5)
                        except Exception as e:
                            return uid, None, str(e).lower()
                    return uid, None, "flood"

            ids = [int(m) for m in user_ids if int(m) not in (int(context.bot.id), GROUP_ANON_BOT_ID)]
            results = await asyncio.gather(*(_check_member(i) for i in ids))
            for m_id_int, member, err in results:
                if member is None:
                    if "not found" in err or "participant not found" in err: to_remove.append(m_id_int)
                    continue
                if member.status == STATUS_LEFT: to_remove.append(m_id_int); continue
                if member.status in (STATUS_KICKED, STATUS_BANNED): continue
                if member.status not in valid: continue
                uname = member.user.username
                fname = member.user.first_name or "друг"
                display = f"@{uname}" if uname else fname
                tags.append(f'<a href="tg://user?id={m_id_int}">{html_escape(display)}</a>')
            if to_remove:
                try:
                    remove_users_from_chat_batch(to_remove, chat_id)
                    for uid in to_remove: uncache_user(uid, chat_id)
                except Exception: pass
            if not tags:
                await update.message.reply_text(ADMIN_DIALOGS["no_active_members"]); return
            for i in range(0, len(tags), 6):
                chunk = tags[i:i+6]
                try: await update.message.reply_text(f"<b>{ADMIN_DIALOGS['kall_header']}</b>\n\n" + "\n".join(chunk), parse_mode="HTML")
                except Exception: pass
        except Exception as e: await log_to_owner(context, f"[КАЛЛ FATAL] {e}")
        return

    find_track = re.match(r"^(рар|rar)[,.]?\s+най(ди|ти)\s+(.+)$", text, re.IGNORECASE)
    if find_track:
        query = find_track.group(3).strip()
        if not query:
            await update.message.reply_text(SETTINGS_DIALOGS["write_text"]); return
        status_msg = await update.message.reply_text(MUSIC_DIALOGS["searching"])
        local_track = search_track_in_db(query)
        if local_track:
            fid, ttitle, tnum = local_track
            await status_msg.delete()
            try:
                await context.bot.send_audio(chat_id=chat_id, audio=fid, caption=f"{MUSIC_DIALOGS['found'].format(трек=ttitle)}\n\n{MUSIC_DIALOGS['query'].format(запрос=query)}")
                increment_play(fid)
            except Exception as e: print(f"[FIND SEND] {e}")
            return
        else: await status_msg.edit_text(MUSIC_DIALOGS["not_found_song"])
