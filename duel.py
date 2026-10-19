import asyncio
import random
import time
import uuid
from rar.config import DUEL_ACTIONS, DUEL_COUNTERS
from rar.dialogs import DUEL_FINAL_SOUNDS, DUEL_PLAYER_WIN_PHRASES, DUEL_RAR_WIN_PHRASES, DUEL_START_PHRASES
from rar.state import duel_games


async def duel_start(update, context, hard=False):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    for existing_game in duel_games.values():
        if existing_game["chat_id"] == chat_id and existing_game["user_id"] == user_id and existing_game.get("active"):
            await update.message.reply_text("У тебя уже идёт дуэль.")
            return
    gid = uuid.uuid4().hex[:16]
    rounds = 10 if hard else 5
    timeout = 3 if hard else 4
    game = {"gid": gid, "chat_id": chat_id, "user_id": user_id, "round": 0, "max_rounds": rounds, "timeout": timeout, "active": True, "state": "intro", "last_action": time.time(), "hard": hard, "msg_id": None, "expected": None}
    duel_games[gid] = game
    start_phrase = random.choice(DUEL_START_PHRASES)
    msg = await update.message.reply_text(f"– {start_phrase}", parse_mode="HTML")
    game["msg_id"] = msg.message_id
    await asyncio.sleep(2)
    asyncio.create_task(duel_rar_turn(context, game))


async def duel_rar_turn(context, game):
    if not game.get("active"): return
    await asyncio.sleep(1)
    game["round"] += 1
    game["last_action"] = time.time()
    if game["round"] > game["max_rounds"]:
        game["active"] = False
        duel_games.pop(game["gid"], None)
        try:
            await context.bot.send_message(chat_id=game["chat_id"], text=f"– {random.choice(DUEL_PLAYER_WIN_PHRASES)}", parse_mode="HTML")
        except Exception: pass
        return
    rar_action = random.choice(DUEL_ACTIONS)
    game["expected"] = DUEL_COUNTERS[rar_action]
    game["state"] = "waiting_player"
    game["deadline"] = time.time() + game["timeout"]
    try:
        await context.bot.send_message(chat_id=game["chat_id"], text=f"<b>{rar_action}</b>", parse_mode="HTML")
    except Exception: pass
    asyncio.create_task(duel_timer(context, game, game["round"]))


async def duel_timer(context, game, round_num):
    await asyncio.sleep(game["timeout"] + 0.3)
    if not game.get("active"): return
    if game.get("round") != round_num: return
    if game.get("state") != "waiting_player": return
    game["active"] = False
    duel_games.pop(game["gid"], None)
    final_sound = random.choice(DUEL_FINAL_SOUNDS)
    try:
        await context.bot.send_message(chat_id=game["chat_id"], text=f"<b>{final_sound}</b>\n– {random.choice(DUEL_RAR_WIN_PHRASES)}", parse_mode="HTML")
    except Exception: pass
