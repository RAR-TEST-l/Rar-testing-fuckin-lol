import asyncio
import random
import time
import traceback
import uuid
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from rar.config import DUEL_TIMEOUT, ITEM_NAMES, MAX_CHAT_ROULETTES, MAX_DUPLICATE_ITEMS, RL_NEXT_ROUND_TIMEOUT, ROULETTE_TIMEOUT, ROUND2_ITEM_LIMIT, ROUND2_POOL, ROUND2_RELOAD_BONUS, ROUND3_ITEM_LIMIT, ROUND3_POOL, ROUND3_RELOAD_BONUS
from rar.dialogs import RL_BUTTONS, RL_DIALOGS, RL_FINISH_DIALOGS, RL_START_DIALOGS, ROUND3_START_PHRASES, SETTINGS_DIALOGS, rl_dlg, rl_fmt
from rar.state import _rl_edit_tasks, _rl_events, _rl_penalty, _rl_pending, _roulette_locks, duel_games, roulette_games


def rl_hp_bar(hp, max_hp, zerkalo=False):
    filled = "\u25a0" * hp
    empty = "\u25a1" * (max_hp - hp)
    bar = f"<b>{filled}</b>{empty}"
    if zerkalo: return f"({bar})"
    return bar


def rl_make_magazine(count):
    if count >= 8:
        bullets = random.randint(3, count - 1)
    else:
        bullets = random.randint(2, count - 1)
    blanks = count - bullets
    mag = ["B"] * bullets + ["H"] * blanks
    random.shuffle(mag)
    return mag, bullets, blanks


def rl_generate_items(pool, count, existing=None):
    result = []
    current = list(existing) if existing else []
    for _ in range(count):
        combined = current + result
        available = [it for it in pool if combined.count(it) < MAX_DUPLICATE_ITEMS]
        if not available:
            available = pool
        result.append(random.choice(available))
    return result


def rl_items_text(items):
    if not items: return "(нет)"
    return ", ".join(ITEM_NAMES.get(i, i) for i in items)


def rl_round_config(round_num):
    if round_num == 1: return {"hp": 2, "items": 0, "pool": []}
    if round_num == 2: return {"hp": 4, "items": 2, "pool": ROUND2_POOL}
    return {"hp": 6, "items": 4, "pool": ROUND3_POOL}


def rl_render(game):
    hp_p = rl_hp_bar(game["player_hp"], game["player_max_hp"], game.get("player_zerkalo", False))
    hp_r = rl_hp_bar(game["rar_hp"], game["rar_max_hp"], game.get("rar_zerkalo", False))
    lines = [f"<b>РАУНД {game['round']}</b>", ""]
    lines.append(f"Rar: {hp_r}    Ты: {hp_p}")
    lines.append("")
    lines.append(f"В обойме: {len(game['magazine']) - game['shot_idx']}")
    if game["round"] >= 2:
        lines.append(f"Твои предметы: {rl_items_text(game['player_items'])}")
        lines.append(f"Предметы Rar: {rl_items_text(game['rar_items'])}")
    lines.append("")
    if game.get("log"):
        lines.append(game["log"])
        lines.append("")
    status = game.get("status", "")
    if status:
        active_parts = []
        if game.get("nasadka_active"): active_parts.append("насадка активна")
        if game.get("pila_active"): active_parts.append("ствол срезан")
        if active_parts: status += ", " + ", ".join(active_parts)
        lines.append(f"<i>{status}</i>")
    return "\n".join(lines)


def rl_keyboard(game):
    rows = []
    if game.get("finished"): return None
    if game["turn"] == "player":
        rows.append([
            InlineKeyboardButton(RL_BUTTONS["shoot_self"], callback_data=f"rl_shoot_self_{game['gid']}"),
            InlineKeyboardButton(RL_BUTTONS["shoot_rar"], callback_data=f"rl_shoot_rar_{game['gid']}"),
        ])
        if game["round"] >= 2 and game["player_items"]:
            item_buttons = [InlineKeyboardButton(ITEM_NAMES[it], callback_data=f"rl_item_{it}_{game['gid']}") for it in game["player_items"]]
            rows.append(item_buttons)
    elif game["turn"] == "rar":
        rows.append([InlineKeyboardButton(RL_BUTTONS["rar_turn"], callback_data=f"rl_noop_{game['gid']}")])
    return InlineKeyboardMarkup(rows) if rows else None


def _rl_chat_slots(chat_id, exclude_user=None):
    """Сколько «слотов» рулетки занято в чате: идущие игры и игры, ждущие решения о следующем раунде."""
    n = 0
    for g in roulette_games.values():
        if g["chat_id"] != chat_id: continue
        if exclude_user is not None and g["player_id"] == exclude_user: continue
        if not g.get("finished") or g.get("awaiting_next"): n += 1
    return n


def _rl_interval(chat_id):
    # Один игрок — почти без задержки (красивые анимации). Больше игроков или были
    # flood-control — пауза растёт, лишние промежуточные кадры склеиваются (скип).
    active = _rl_chat_slots(chat_id)
    return 0.45 + 0.5 * max(0, active - 1) + _rl_penalty.get(chat_id, 0)


async def _rl_do_edit(bot, game):
    chat_id = game["chat_id"]
    for attempt in range(2):
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=game["msg_id"],
                text=rl_render(game), parse_mode="HTML",
                reply_markup=rl_keyboard(game))
            _rl_penalty[chat_id] = max(0.0, _rl_penalty.get(chat_id, 0.0) - 0.05)
            return
        except RetryAfter as e:
            _rl_penalty[chat_id] = min(2.5, _rl_penalty.get(chat_id, 0.0) + 0.4)
            print(f"[RL WORKER {chat_id}] Flood control, ждем {e.retry_after} сек...")
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message can't be edited" in err or "chat not found" in err:
                # сообщение удалили — игра больше не нужна, освобождаем слот в чате
                roulette_games.pop(game["gid"], None)
            elif "message is not modified" not in err:
                print(f"[RL WORKER {chat_id}] Ошибка: {e}")
            return


async def _rl_edit_worker_for_chat(bot, chat_id):
    pending = _rl_pending[chat_id]
    event = _rl_events[chat_id]
    while True:
        futures = []
        try:
            await event.wait()
            if not pending:
                event.clear()
                continue
            gid = next(iter(pending))          # самая давняя ожидающая игра (по кругу)
            futures = pending.pop(gid)
            if not pending:
                event.clear()
            game = roulette_games.get(gid)     # всегда берём АКТУАЛЬНЫЙ объект игры
            if game and not game.get("finished"):
                await _rl_do_edit(bot, game)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[RL WORKER FATAL {chat_id}] {e}")
            await asyncio.sleep(1)
        finally:
            for f in futures:
                if not f.done():
                    f.set_result(None)
        await asyncio.sleep(_rl_interval(chat_id))


def _rl_enqueue(bot, game, future=None):
    chat_id = game["chat_id"]
    if chat_id not in _rl_pending:
        _rl_pending[chat_id] = {}
        _rl_events[chat_id] = asyncio.Event()
        _rl_edit_tasks[chat_id] = asyncio.create_task(_rl_edit_worker_for_chat(bot, chat_id))
    waiters = _rl_pending[chat_id].setdefault(game["gid"], [])
    if future is not None:
        waiters.append(future)
    _rl_events[chat_id].set()


async def rl_edit(context, game):
    game["last_action"] = time.time()
    _rl_enqueue(context.bot, game)


async def rl_edit_wait(context, game):
    game["last_action"] = time.time()
    future = asyncio.get_running_loop().create_future()
    _rl_enqueue(context.bot, game, future)
    await future


async def rl_edit_bot(bot, game):
    game["last_action"] = time.time()
    _rl_enqueue(bot, game)


async def rl_finish(context, game, winner):
    game["finished"] = True
    game["last_action"] = time.time()
    game["awaiting_next"] = (winner == "player" and game["round"] < 3)
    _rl_pending.get(game["chat_id"], {}).pop(game["gid"], None)
    game["turn"] = "finished"
    game["status"] = ""
    if winner == "player":
        game["log"] = RL_FINISH_DIALOGS["player_win"]
    else:
        game["log"] = RL_FINISH_DIALOGS["rar_win"]
    if winner == "player" and game["round"] < 3:
        next_round = game["round"] + 1
        rows = []
        rows.append([InlineKeyboardButton(RL_BUTTONS["next"], callback_data=f"rl_offer{next_round}_{game['gid']}"), InlineKeyboardButton(RL_BUTTONS["close"], callback_data=f"rl_close_{game['gid']}")])
        kb = InlineKeyboardMarkup(rows)
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(RL_BUTTONS["close"], callback_data=f"rl_close_{game['gid']}")]])
    try:
        await context.bot.edit_message_text(chat_id=game["chat_id"], message_id=game["msg_id"], text=rl_render(game), parse_mode="HTML", reply_markup=kb)
    except Exception: pass


async def rl_offer_round2(context, gid, chat_id):
    game = roulette_games.get(gid)
    if not game: return
    game["last_action"] = time.time()
    game["log"] = RL_START_DIALOGS["offer_round2"]
    game["status"] = ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(RL_BUTTONS["round2"], callback_data=f"rl_r2_{gid}"), InlineKeyboardButton(RL_BUTTONS["no"], callback_data=f"rl_close_{gid}")]])
    try:
        await context.bot.edit_message_text(chat_id=game["chat_id"], message_id=game["msg_id"], text=rl_render(game), parse_mode="HTML", reply_markup=kb)
    except Exception: pass


async def rl_offer_round3(context, gid, chat_id):
    game = roulette_games.get(gid)
    if not game: return
    game["last_action"] = time.time()
    game["log"] = RL_START_DIALOGS["offer_round3"]
    game["status"] = ""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(RL_BUTTONS["round3"], callback_data=f"rl_r3_{gid}"), InlineKeyboardButton(RL_BUTTONS["no"], callback_data=f"rl_close_{gid}")]])
    try:
        await context.bot.edit_message_text(chat_id=game["chat_id"], message_id=game["msg_id"], text=rl_render(game), parse_mode="HTML", reply_markup=kb)
    except Exception: pass


async def rl_give_items_with_animation(context, game, who, items):
    target = game["player_items"] if who == "player" else game["rar_items"]
    for it in items:
        target.append(it)
        await rl_edit(context, game)
        await asyncio.sleep(0.5)


async def rl_reload_with_items(context, game, bonus):
    count = random.randint(3, 8)
    mag, bullets, blanks = rl_make_magazine(count)
    game["magazine"] = mag
    game["shot_idx"] = 0
    game["status"] = RL_START_DIALOGS["reload_start"]
    game["log"] = ""
    await rl_edit(context, game)
    await asyncio.sleep(2)
    game["log"] = rl_fmt("reload_info", "player", N=blanks, M=bullets)
    await rl_edit_wait(context, game)
    await asyncio.sleep(1)

    if game["round"] == 2:
        limit = ROUND2_ITEM_LIMIT
        pool = ROUND2_POOL
    elif game["round"] == 3:
        limit = ROUND3_ITEM_LIMIT
        pool = ROUND3_POOL
    else:
        return

    p_need = min(bonus, max(0, limit - len(game["player_items"])))
    r_need = min(bonus, max(0, limit - len(game["rar_items"])))

    if p_need > 0:
        game["log"] = RL_DIALOGS["items_for_you"]["player"]
        await rl_edit(context, game); await asyncio.sleep(0.5)
        p_items = rl_generate_items(pool, p_need, existing=game["player_items"])
        await rl_give_items_with_animation(context, game, "player", p_items)

    if r_need > 0:
        game["log"] = RL_DIALOGS["items_for_me"]["player"]
        await rl_edit(context, game); await asyncio.sleep(0.5)
        r_items = rl_generate_items(pool, r_need, existing=game["rar_items"])
        await rl_give_items_with_animation(context, game, "rar", r_items)

    game["log"] = ""
    await rl_edit(context, game)
    await asyncio.sleep(0.5)


async def rl_reload(context, game):
    await rl_reload_with_items(context, game, 0)


async def rl_start(update, context):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in _roulette_locks:
        _roulette_locks[chat_id] = asyncio.Lock()
    async with _roulette_locks[chat_id]:
        # Лимит рулеток в чате (свою старую игру игрок может заменить новой)
        if _rl_chat_slots(chat_id, exclude_user=user_id) >= MAX_CHAT_ROULETTES:
            await update.message.reply_text(f"В этом чате уже идут {MAX_CHAT_ROULETTES} рулетки. Дождитесь окончания одной из них.")
            return

        old_games = [(gid, g) for gid, g in roulette_games.items() if g["chat_id"] == chat_id and g["player_id"] == user_id and (not g.get("finished") or g.get("awaiting_next"))]
        for old_gid, old_game in old_games:
            try:
                await context.bot.delete_message(chat_id=old_game["chat_id"], message_id=old_game["msg_id"])
            except Exception: pass
            roulette_games.pop(old_gid, None)

        gid = uuid.uuid4().hex[:16]
        cfg = rl_round_config(1)
        count = random.randint(3, 8)
        mag, bullets, blanks = rl_make_magazine(count)
        game = {"gid": gid, "chat_id": chat_id, "player_id": user_id, "msg_id": None, "round": 1, "player_hp": cfg["hp"], "rar_hp": cfg["hp"], "player_max_hp": cfg["hp"], "rar_max_hp": cfg["hp"], "magazine": mag, "shot_idx": 0, "player_items": [], "rar_items": [], "player_zerkalo": False, "rar_zerkalo": False, "nasadka_active": False, "pila_active": False, "turn": "player", "log": "", "status": "Твой ход", "finished": False, "last_action": time.time()}
        try:
            msg = await update.message.reply_text(rl_render(game), parse_mode="HTML")
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                msg = await update.message.reply_text(rl_render(game), parse_mode="HTML")
            except Exception:
                return
        except Exception:
            return
        game["msg_id"] = msg.message_id
        game["log"] = rl_fmt("reload_info", "player", N=blanks, M=bullets)
        roulette_games[gid] = game
        await rl_edit(context, game)


async def rl_round2_start(context, gid, chat_id):
    old = roulette_games.get(gid)
    if not old: return
    player_id = old["player_id"]; msg_id = old["msg_id"]
    cfg = rl_round_config(2)
    count = random.randint(3, 8)
    mag, bullets, blanks = rl_make_magazine(count)
    p_items = rl_generate_items(cfg["pool"], cfg["items"])
    r_items = rl_generate_items(cfg["pool"], cfg["items"])
    game = {"gid": gid, "chat_id": chat_id, "player_id": player_id, "msg_id": msg_id, "round": 2, "player_hp": cfg["hp"], "rar_hp": cfg["hp"], "player_max_hp": cfg["hp"], "rar_max_hp": cfg["hp"], "magazine": mag, "shot_idx": 0, "player_items": [], "rar_items": [], "player_zerkalo": False, "rar_zerkalo": False, "nasadka_active": False, "pila_active": False, "turn": "player", "log": "", "status": "Подготовка...", "finished": False, "last_action": time.time()}
    roulette_games[gid] = game
    game["log"] = RL_START_DIALOGS["box_appears"]
    await rl_edit(context, game); await asyncio.sleep(0.5)
    game["log"] = RL_START_DIALOGS["round2_items"]
    await rl_edit(context, game); await asyncio.sleep(0.5)
    game["player_items"].append(p_items[0]); await rl_edit(context, game); await asyncio.sleep(0.5)
    game["player_items"].append(p_items[1]); await rl_edit(context, game); await asyncio.sleep(0.5)
    game["rar_items"].append(r_items[0]); await rl_edit(context, game); await asyncio.sleep(0.5)
    game["rar_items"].append(r_items[1]); await rl_edit(context, game); await asyncio.sleep(0.5)
    game["log"] = RL_START_DIALOGS["round2_finish"]
    await rl_edit(context, game); await asyncio.sleep(1.5)
    game["log"] = rl_fmt("reload_info", "player", N=blanks, M=bullets)
    game["status"] = "Твой ход"
    await rl_edit_wait(context, game)


async def rl_round3_start(context, gid, chat_id):
    old = roulette_games.get(gid)
    if not old: return
    player_id = old["player_id"]; msg_id = old["msg_id"]
    cfg = rl_round_config(3)
    count = random.randint(3, 8)
    mag, bullets, blanks = rl_make_magazine(count)
    p_items = rl_generate_items(cfg["pool"], cfg["items"])
    r_items = rl_generate_items(cfg["pool"], cfg["items"])
    game = {"gid": gid, "chat_id": chat_id, "player_id": player_id, "msg_id": msg_id, "round": 3, "player_hp": cfg["hp"], "rar_hp": cfg["hp"], "player_max_hp": cfg["hp"], "rar_max_hp": cfg["hp"], "magazine": mag, "shot_idx": 0, "player_items": [], "rar_items": [], "player_zerkalo": False, "rar_zerkalo": False, "nasadka_active": False, "pila_active": False, "turn": "player", "log": "", "status": "Подготовка...", "finished": False, "last_action": time.time()}
    roulette_games[gid] = game
    start_phrase = random.choice(ROUND3_START_PHRASES)
    game["log"] = f"– {start_phrase}"
    await rl_edit(context, game); await asyncio.sleep(1.5)
    game["log"] = RL_START_DIALOGS["box_appears"]
    await rl_edit(context, game); await asyncio.sleep(0.5)
    game["log"] = RL_START_DIALOGS["round3_items"]
    await rl_edit(context, game); await asyncio.sleep(0.5)
    for i in range(4):
        game["player_items"].append(p_items[i]); await rl_edit(context, game); await asyncio.sleep(0.5)
    for i in range(4):
        game["rar_items"].append(r_items[i]); await rl_edit(context, game); await asyncio.sleep(0.5)
    game["log"] = rl_fmt("reload_info", "player", N=blanks, M=bullets)
    game["status"] = "Твой ход"
    await rl_edit_wait(context, game)


async def rl_rar_turn(context, game):
    if game.get("_rar_running"): return   # защита от двойного хода Rar
    game["_rar_running"] = True
    try:
        await _rl_rar_turn_inner(context, game)
    except Exception as e:
        print(f"[RL RAR TURN FATAL] {e}")
        traceback.print_exc()
        if not game.get("finished"):
            game["turn"] = "player"
            game["status"] = "Твой ход"
            game["log"] = "<b>Rar запнулась. Ход снова твой.</b>"
            try:
                await rl_edit(context, game)
            except Exception: pass
    finally:
        game["_rar_running"] = False


async def _rl_rar_turn_inner(context, game):
    if game.get("finished"): return
    if game.get("rar_skip"):
        game["rar_skip"] = False
        game["log"] = rl_dlg("naruch_skip", "rar")
        game["turn"] = "player"
        game["status"] = "Твой ход"
        await rl_edit(context, game)
        await asyncio.sleep(1)
        return

    await asyncio.sleep(1)
    game["turn"] = "rar"
    game["log"] = rl_dlg("rar_take_gun", "rar")
    game["status"] = "Ход Rar"
    await rl_edit(context, game)
    await asyncio.sleep(1)

    remaining = len(game["magazine"]) - game["shot_idx"]
    if remaining == 1:
        game["rar_knows"] = game["magazine"][game["shot_idx"]]

    if game["round"] >= 2 and game["rar_items"]:
        if "tabletki" in game["rar_items"]:
            hp = game["rar_hp"]
            should_use = (hp <= game["rar_max_hp"] - 2) or (hp == 1 and len(game["rar_items"]) == 1)
            if should_use:
                game["rar_items"].remove("tabletki")
                game["log"] = rl_dlg("use_tabletki", "rar")
                await rl_edit(context, game); await asyncio.sleep(0.5)
                if random.randint(1, 2) == 1:
                    game["rar_hp"] = min(game["rar_max_hp"], game["rar_hp"] + 2)
                    game["log"] = rl_dlg("tabletki_good", "rar")
                else:
                    game["rar_hp"] -= 1
                    game["log"] = rl_dlg("tabletki_bad", "rar")
                game["status"] = "Ход Rar"
                await rl_edit(context, game); await asyncio.sleep(1)
                if game["rar_hp"] <= 0:
                    await rl_finish(context, game, "player"); return

        if "telefon" in game["rar_items"]:
            game["rar_items"].remove("telefon")
            game["log"] = rl_dlg("use_telefon", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["log"] = rl_dlg("telefon_think", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1.5)
            remaining_list = game["magazine"][game["shot_idx"]:]
            if remaining_list:
                idx = random.randint(0, len(remaining_list) - 1)
                game["rar_knows"] = remaining_list[idx]

        if "lupa" in game["rar_items"] and not game.get("rar_knows"):
            game["rar_items"].remove("lupa")
            game["log"] = rl_dlg("use_lupa", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["rar_knows"] = game["magazine"][game["shot_idx"]]
            game["log"] = rl_dlg("lupa_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "sigara" in game["rar_items"] and game["rar_hp"] < game["rar_max_hp"]:
            game["rar_items"].remove("sigara")
            game["log"] = rl_dlg("use_sigara", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["rar_hp"] += 1
            game["log"] = rl_dlg("sigara_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "bpatron" in game["rar_items"]:
            game["rar_items"].remove("bpatron")
            game["log"] = rl_dlg("use_bpatron", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["magazine"].insert(game["shot_idx"], "B")
            game["rar_knows"] = "B"
            game["log"] = rl_dlg("bpatron_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "nasadka" in game["rar_items"] and not game.get("nasadka_active"):
            game["rar_items"].remove("nasadka")
            game["log"] = rl_dlg("use_nasadka", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["nasadka_active"] = True
            game["log"] = rl_dlg("nasadka_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "zerkalo" in game["rar_items"] and not game.get("rar_zerkalo"):
            game["rar_items"].remove("zerkalo")
            game["log"] = rl_dlg("use_zerkalo", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["rar_zerkalo"] = True
            game["log"] = rl_dlg("zerkalo_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "pila" in game["rar_items"] and not game.get("pila_active") and game.get("rar_knows") == "B":
            game["rar_items"].remove("pila")
            game["log"] = rl_dlg("use_pila", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["pila_active"] = True
            game["log"] = rl_dlg("pila_use", "rar")
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

        if "adrenalin" in game["rar_items"]:
            stealable = [it for it in game["player_items"] if it != "adrenalin"]
            if stealable:
                stolen = random.choice(stealable)
                game["player_items"].remove(stolen)
                game["rar_items"].remove("adrenalin")
                game["log"] = rl_dlg("use_adrenalin", "rar")
                await rl_edit(context, game); await asyncio.sleep(0.5)
                game["log"] = rl_fmt("adrenalin_take", "rar", предмет=ITEM_NAMES[stolen])
                game["status"] = "Ход Rar"
                await rl_edit(context, game); await asyncio.sleep(0.5)
                if stolen == "lupa" and not game.get("rar_knows"):
                    game["rar_knows"] = game["magazine"][game["shot_idx"]]
                elif stolen == "sigara" and game["rar_hp"] < game["rar_max_hp"]:
                    game["rar_hp"] += 1
                elif stolen == "bpatron":
                    game["magazine"].insert(game["shot_idx"], "B")
                    game["rar_knows"] = "B"
                elif stolen == "nasadka":
                    game["nasadka_active"] = True
                elif stolen == "zerkalo":
                    game["rar_zerkalo"] = True
                elif stolen == "tabletki":
                    if random.randint(1, 2) == 1:
                        game["rar_hp"] = min(game["rar_max_hp"], game["rar_hp"] + 2)
                    else:
                        game["rar_hp"] -= 1
                elif stolen == "pila":
                    game["pila_active"] = True
                elif stolen == "naruch":
                    game["player_skip"] = True
                elif stolen == "telefon":
                    remaining_list = game["magazine"][game["shot_idx"]:]
                    if remaining_list:
                        idx = random.randint(0, len(remaining_list) - 1)
                        game["rar_knows"] = remaining_list[idx]
                await rl_edit(context, game); await asyncio.sleep(0.5)

        if game["rar_hp"] <= 0:
            await rl_finish(context, game, "player"); return

        if "naruch" in game["rar_items"]:
            game["rar_items"].remove("naruch")
            game["log"] = rl_dlg("use_naruch", "rar")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["player_skip"] = True
            game["log"] = "<b>На ваши кисти надеты наручники</b>"
            game["status"] = "Ход Rar"
            await rl_edit(context, game); await asyncio.sleep(1)

    knows = game.get("rar_knows")
    under_mirror = game.get("rar_zerkalo", False)
    if knows == "B":
        target = "player"
    elif knows == "H":
        target = "rar"
    else:
        if under_mirror:
            target = "player" if random.randint(1, 2) == 1 else "rar"
        else:
            if game["round"] == 3:
                target = "player" if random.randint(1, 100) <= 80 else "rar"
            else:
                target = "player" if random.randint(1, 100) <= 70 else "rar"

    game["log"] = rl_dlg("aim_player", "rar") if target == "player" else rl_dlg("aim_self", "rar")
    game["status"] = "Ход Rar"
    await rl_edit(context, game)
    await asyncio.sleep(1)

    shot = game["magazine"][game["shot_idx"]]
    game["shot_idx"] += 1
    dmg = 2 if (game.get("nasadka_active") or game.get("pila_active")) else 1

    if shot == "B":
        if target == "player":
            if game.get("player_zerkalo"):
                game["player_zerkalo"] = False
                if dmg == 1:
                    game["rar_hp"] -= 1
                    game["log"] = rl_dlg("zerkalo_crack", "rar")
                else:
                    game["player_hp"] -= 1
                    game["rar_hp"] -= 2
                    game["log"] = rl_dlg("zerkalo_break", "rar")
            else:
                game["player_hp"] -= dmg
                game["log"] = rl_dlg("hit", "rar")
        else:
            if game.get("rar_zerkalo"):
                game["rar_zerkalo"] = False
                if dmg == 1:
                    game["player_hp"] -= 1
                    game["log"] = rl_dlg("zerkalo_crack", "rar")
                else:
                    game["rar_hp"] -= 1
                    game["player_hp"] -= 2
                    game["log"] = rl_dlg("zerkalo_break", "rar")
            else:
                game["rar_hp"] -= dmg
                game["log"] = rl_dlg("hit", "rar")
        if game.get("pila_active"):
            game["pila_active"] = False
    else:
        game["log"] = rl_dlg("blank", "rar")
        if game.get("nasadka_active"):
            game["nasadka_active"] = False
            game["rar_hp"] -= 1
            game["log"] = rl_dlg("nasadka_burst", "rar")
        if game.get("pila_active"):
            game["pila_active"] = False

    game["rar_knows"] = None

    if game["player_hp"] <= 0: await rl_finish(context, game, "rar"); return
    if game["rar_hp"] <= 0: await rl_finish(context, game, "player"); return

    game["status"] = "Ход Rar"
    await rl_edit(context, game); await asyncio.sleep(1)

    reloaded = False
    if game["shot_idx"] >= len(game["magazine"]):
        bonus = ROUND2_RELOAD_BONUS if game["round"] == 2 else (ROUND3_RELOAD_BONUS if game["round"] == 3 else 0)
        await rl_reload_with_items(context, game, bonus)
        reloaded = True

    if shot == "H" and target == "rar":
        asyncio.create_task(rl_rar_turn(context, game)); return

    if game.get("player_skip"):
        game["player_skip"] = False
        game["log"] = rl_dlg("player_skip", "player")
        game["status"] = "Ход Rar"
        await rl_edit(context, game); await asyncio.sleep(1)
        asyncio.create_task(rl_rar_turn(context, game)); return

    game["turn"] = "player"
    if not reloaded: game["log"] = ""
    game["status"] = "Твой ход"
    if game["player_hp"] == 1 and game["rar_hp"] == 1 and not reloaded:
        game["log"] = rl_dlg("low_hp_both", "player")
    elif game["rar_hp"] == 1 and not reloaded:
        game["log"] = rl_dlg("low_hp_rar", "player")
    await rl_edit(context, game)


async def rl_apply_stolen_item(context, game, item):
    if item == "lupa":
        game["player_knows"] = game["magazine"][game["shot_idx"]]
        label = "боевой" if game["player_knows"] == "B" else "холостой"
        game["log"] = rl_fmt("lupa_result", "player", тип=label)
    elif item == "pila":
        game["pila_active"] = True
        game["log"] = rl_dlg("pila_result", "player")
    elif item == "naruch":
        game["rar_skip"] = True
        game["log"] = rl_dlg("adr_take_naruch", "player")
    elif item == "sigara":
        if game["player_hp"] < game["player_max_hp"]:
            game["player_hp"] += 1
            game["log"] = rl_dlg("adr_take_sigara_good", "player")
        else:
            game["log"] = rl_dlg("adr_take_sigara_max", "player")
    elif item == "tabletki":
        if random.randint(1, 2) == 1:
            game["player_hp"] = min(game["player_max_hp"], game["player_hp"] + 2)
            game["log"] = rl_dlg("adr_take_tabletki_good", "player")
        else:
            game["player_hp"] -= 1
            game["log"] = rl_dlg("adr_take_tabletki_bad", "player")
    elif item == "telefon":
        remaining = game["magazine"][game["shot_idx"]:]
        if remaining:
            idx = random.randint(0, len(remaining) - 1)
            p = remaining[idx]
            label = "боевой" if p == "B" else "холостой"
            game["player_knows"] = p
            game["log"] = rl_fmt("adr_take_telefon", "player", N=idx+1, тип=label)
        else:
            game["log"] = rl_dlg("adr_take_telefon_empty", "player")
    elif item == "bpatron":
        game["magazine"].insert(game["shot_idx"], "B")
        game["log"] = rl_dlg("adr_take_bpatron", "player")
    elif item == "nasadka":
        game["nasadka_active"] = True
        game["log"] = rl_dlg("adr_take_nasadka", "player")
    elif item == "zerkalo":
        game["player_zerkalo"] = True
        game["log"] = rl_dlg("adr_take_zerkalo", "player")
    elif item == "adrenalin":
        game["player_hp"] -= 1
        game["log"] = rl_dlg("adr_take_adrenalin", "player")


async def rl_callback(update, context):
    q = update.callback_query
    data = q.data
    user_id = q.from_user.id

    async def _answer(text=None, alert=False):
        # на callback можно ответить только ОДИН раз — поэтому отвечаем ровно один раз
        try:
            await q.answer(text, show_alert=alert)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try: await q.answer(text, show_alert=alert)
            except Exception: pass
        except Exception: pass

    _gid = data.split("_")[-1]
    _game = roulette_games.get(_gid)
    if data.startswith("rl_noop_"):
        await _answer(); return
    if _game is None:
        if data.startswith("rl_close_"):
            # игры уже нет в памяти (перезапуск/таймаут) — просто убираем сообщение
            await _answer()
            try: await q.message.delete()
            except Exception: pass
            return
        await _answer("Эта игра уже закончилась. Начни новую: «Рар рулетка».", True); return
    if _game["player_id"] != user_id:
        await _answer("Это не твоя игра.", True); return
    await _answer()

    if data.startswith("rl_close_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if game and game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        roulette_games.pop(gid, None)
        try: await q.message.delete()
        except Exception: pass
        return
    if data.startswith("rl_offer2_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if not game: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        await rl_offer_round2(context, gid, q.message.chat_id); return
    if data.startswith("rl_offer3_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if not game: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        await rl_offer_round3(context, gid, q.message.chat_id); return
    if data.startswith("rl_r2_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if not game: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        await rl_round2_start(context, gid, q.message.chat_id); return
    if data.startswith("rl_r3_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if not game: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        await rl_round3_start(context, gid, q.message.chat_id); return

    if data.startswith("rl_adr_cancel_"):
        gid = data.split("_")[-1]
        game = roulette_games.get(gid)
        if game: await rl_edit(context, game)
        return
    if data.startswith("rl_adr_"):
        parts = data.split("_")
        if len(parts) < 4: return
        item = parts[2]; gid = parts[3]
        game = roulette_games.get(gid)
        if not game or game["player_id"] != user_id: return
        
        # Защита от спама
        if game.get("_action_lock"): return
        game["_action_lock"] = True
        
        try:
            if item not in game["rar_items"]: return
            if "adrenalin" not in game["player_items"]: return
            game["player_items"].remove("adrenalin")
            game["rar_items"].remove(item)
            game["log"] = rl_dlg("use_adrenalin", "player")
            await rl_edit(context, game); await asyncio.sleep(0.5)
            game["log"] = rl_fmt("adrenalin_take", "player", предмет=ITEM_NAMES[item])
            await rl_edit(context, game); await asyncio.sleep(0.5)
            await rl_apply_stolen_item(context, game, item)
            if game["player_hp"] <= 0: await rl_finish(context, game, "rar"); return
            if game["rar_hp"] <= 0: await rl_finish(context, game, "player"); return
            await rl_edit(context, game)
        finally:
            game["_action_lock"] = False
        return

    if data.startswith("rl_shoot_"):
        parts = data.split("_")
        target = parts[2]
        gid = parts[3]
        game = roulette_games.get(gid)
        if not game or game["finished"]: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        if game["turn"] != "player": return
        if game.get("_action_lock"): return
        game["_action_lock"] = True

        try:
            game["turn"] = "rar"
            game["last_action"] = time.time()

            shot = game["magazine"][game["shot_idx"]]
            game["shot_idx"] += 1
            dmg = 2 if (game.get("nasadka_active") or game.get("pila_active")) else 1

            if shot == "B":
                if target == "self":
                    if game.get("player_zerkalo"):
                        game["player_zerkalo"] = False
                        if dmg == 1:
                            game["rar_hp"] -= 1
                            game["log"] = rl_dlg("zerkalo_crack", "player")
                        else:
                            game["player_hp"] -= 1
                            game["rar_hp"] -= 2
                            game["log"] = rl_dlg("zerkalo_break", "player")
                    else:
                        game["player_hp"] -= dmg
                        game["log"] = rl_dlg("hit", "player")
                else:
                    if game.get("rar_zerkalo"):
                        game["rar_zerkalo"] = False
                        if dmg == 1:
                            game["player_hp"] -= 1
                            game["log"] = rl_dlg("zerkalo_crack", "player")
                        else:
                            game["rar_hp"] -= 1
                            game["player_hp"] -= 2
                            game["log"] = rl_dlg("zerkalo_break", "player")
                    else:
                        game["rar_hp"] -= dmg
                        game["log"] = rl_dlg("hit", "player")
                if game.get("pila_active"):
                    game["pila_active"] = False
            else:
                game["log"] = rl_dlg("blank", "player")
                if game.get("nasadka_active"):
                    game["nasadka_active"] = False
                    if game.get("player_zerkalo"):
                        game["player_zerkalo"] = False
                        game["rar_hp"] -= 1
                        game["log"] = rl_dlg("nasadka_burst", "player") + "\n" + rl_dlg("zerkalo_crack", "player")
                    else:
                        game["player_hp"] -= 1
                        game["log"] = rl_dlg("nasadka_burst", "player")
                if game.get("pila_active"):
                    game["pila_active"] = False

            if game["player_hp"] <= 0: await rl_finish(context, game, "rar"); return
            if game["rar_hp"] <= 0: await rl_finish(context, game, "player"); return

            reload_needed = game["shot_idx"] >= len(game["magazine"])
            reloaded = False
            if reload_needed:
                bonus = ROUND2_RELOAD_BONUS if game["round"] == 2 else (ROUND3_RELOAD_BONUS if game["round"] == 3 else 0)
                game["turn"] = "rar"
                game["status"] = rl_dlg("reloading", "player")
                try:
                    await rl_edit(context, game)
                except Exception as e:
                    print(f"[RL SHOOT EDIT] {e}")
                await rl_reload_with_items(context, game, bonus)
                reloaded = True

            if shot == "H" and target == "self":
                game["turn"] = "player"
                game["status"] = "Твой ход снова"
                if not reloaded: game["log"] = ""
                try:
                    await rl_edit(context, game)
                except Exception: pass
                return

            game["status"] = "Ход Rar"
            try:
                await rl_edit(context, game)
            except Exception as e:
                print(f"[RL SHOOT EDIT2] {e}")
            try:
                asyncio.create_task(rl_rar_turn(context, game))
            except Exception as e:
                print(f"[RL SHOOT TASK] {e}")
                game["turn"] = "player"
                game["status"] = "Твой ход"
                game["log"] = "<b>Rar запнулась. Ход снова твой.</b>"
                try:
                    await rl_edit(context, game)
                except Exception: pass
        finally:
            game["_action_lock"] = False
        return

    if data.startswith("rl_item_"):
        parts = data.split("_")
        if len(parts) < 4: return
        item = parts[2]; gid = parts[3]
        game = roulette_games.get(gid)
        if not game or game["finished"]: return
        if game["player_id"] != user_id:
            try: await q.answer("Это не твоя игра.", show_alert=True)
            except Exception: pass
            return
        if game["turn"] != "player": return
        if game.get("_action_lock"): return
        game["_action_lock"] = True

        try:
            if item not in game["player_items"]: return
            use_key = f"use_{item}"
            if use_key in RL_DIALOGS:
                game["player_items"].remove(item)
                game["log"] = rl_dlg(use_key, "player")
                await rl_edit(context, game); await asyncio.sleep(0.5)

            if item == "lupa":
                game["player_knows"] = game["magazine"][game["shot_idx"]]
                label = "боевой" if game["player_knows"] == "B" else "холостой"
                game["log"] = rl_fmt("lupa_result", "player", тип=label)
            elif item == "pila":
                game["pila_active"] = True
                game["log"] = rl_dlg("pila_result", "player")
            elif item == "naruch":
                game["rar_skip"] = True
                game["log"] = "<b>Rar пропустит ход.</b>"
            elif item == "sigara":
                if game["player_hp"] < game["player_max_hp"]:
                    game["player_hp"] += 1; game["log"] = rl_dlg("sigara_puff", "player")
                else: game["log"] = "<b>Уже максимум</b>"
            elif item == "tabletki":
                if random.randint(1, 2) == 1:
                    game["player_hp"] = min(game["player_max_hp"], game["player_hp"] + 2)
                    game["log"] = rl_dlg("tabletki_good", "player")
                else:
                    game["player_hp"] -= 1
                    game["log"] = rl_dlg("tabletki_bad", "player")
            elif item == "telefon":
                remaining = game["magazine"][game["shot_idx"]:]
                if remaining:
                    idx = random.randint(0, len(remaining) - 1)
                    p = remaining[idx]
                    label = "боевой" if p == "B" else "холостой"
                    pos = idx + 1
                    game["log"] = rl_fmt("telefon_anim", "player", N=pos, тип=label)
                    await rl_edit(context, game); await asyncio.sleep(1.5)
                    game["log"] = rl_dlg("telefon_dead", "player")
                    await rl_edit(context, game); await asyncio.sleep(1)
                    game["player_knows"] = p
                else:
                    game["log"] = rl_dlg("empty", "player")
            elif item == "bpatron":
                game["magazine"].insert(game["shot_idx"], "B")
                game["log"] = rl_dlg("bpatron_use", "player")
            elif item == "nasadka":
                game["nasadka_active"] = True
                game["log"] = rl_dlg("nasadka_use", "player")
            elif item == "zerkalo":
                game["player_zerkalo"] = True
                game["log"] = rl_dlg("zerkalo_use", "player")
            elif item == "adrenalin":
                stealable = [it for it in game["rar_items"] if it != "adrenalin"]
                if not stealable:
                    game["log"] = rl_dlg("rar_no_items", "player")
                    await rl_edit(context, game); return
                rows = []
                for it in stealable:
                    rows.append([InlineKeyboardButton(ITEM_NAMES[it], callback_data=f"rl_adr_{it}_{gid}")])
                rows.append([InlineKeyboardButton(RL_BUTTONS["cancel"], callback_data=f"rl_adr_cancel_{gid}")])
                try:
                    await context.bot.edit_message_text(
                        chat_id=game["chat_id"], message_id=game["msg_id"],
                        text=rl_render(game) + "\n\n<b>" + RL_START_DIALOGS["pick_item"] + "</b>", parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(rows))
                except Exception: pass
                return
            if game["player_hp"] <= 0:
                await rl_finish(context, game, "rar"); return
            await rl_edit(context, game)
        finally:
            game["_action_lock"] = False
        return


async def rl_timeout_loop(app):
    await asyncio.sleep(60)
    while True:
        try:
            now = time.time()
            for gid, game in list(roulette_games.items()):
                limit = RL_NEXT_ROUND_TIMEOUT if game.get("awaiting_next") else ROULETTE_TIMEOUT
                if now - game["last_action"] > limit:
                    try:
                        await app.bot.edit_message_text(
                            chat_id=game["chat_id"], message_id=game["msg_id"],
                            text=SETTINGS_DIALOGS["timeout_game"],
                            reply_markup=None, parse_mode="HTML")
                    except Exception: pass
                    roulette_games.pop(gid, None)
            for gid, game in list(duel_games.items()):
                if now - game.get("last_action", 0) > DUEL_TIMEOUT:
                    game["active"] = False
                    duel_games.pop(gid, None)
        except Exception: pass
        await asyncio.sleep(30)


async def rl_stuck_watchdog(app):
    await asyncio.sleep(120)
    while True:
        try:
            now = time.time()
            for gid, game in list(roulette_games.items()):
                if game.get("finished"): continue
                if game.get("turn") != "rar": continue
                last = game.get("last_action", 0)
                # если ход Rar реально ещё идёт (например, flood-control), даём ему больше времени
                if now - last > (180 if game.get("_rar_running") else 60):
                    game["_rar_running"] = False
                    print(f"[RL WATCHDOG] game {gid} stuck on rar turn, fixing")
                    game["turn"] = "player"
                    game["status"] = "Твой ход"
                    game["log"] = "<b>Rar запнулась. Ход снова твой.</b>"
                    try:
                        await rl_edit_bot(app.bot, game)
                    except Exception: pass
        except Exception as e:
            print(f"[RL WATCHDOG ERROR] {e}")
        await asyncio.sleep(30)
