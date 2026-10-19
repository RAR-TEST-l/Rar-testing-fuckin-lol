import time
from rar.config import CREATOR_CACHE_TTL, RANK_NONE, STATUS_CREATOR
from rar.db import find_user_by_username, get_admin_rank
from rar.dialogs import ADMIN_DIALOGS
from rar.state import _creator_cache


async def resolve_user_from_arg(context, chat_id, target_arg):
    if not target_arg: return None
    target_arg = target_arg.strip()
    if target_arg.startswith("@"):
        uid = find_user_by_username(target_arg)
        if uid:
            try:
                m = await context.bot.get_chat_member(chat_id, uid); return m.user
            except Exception: pass
        try:
            m = await context.bot.get_chat_member(chat_id, target_arg); return m.user
        except Exception: return None
    try:
        uid = int(target_arg)
        m = await context.bot.get_chat_member(chat_id, uid); return m.user
    except Exception:
        return None


async def safe_promote(context, chat_id, target_id, wanted):
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as e:
        return False, [], [], f"get_chat_member: {e}"

    granted = {}
    skipped = []
    for perm, val in wanted.items():
        if not val:
            granted[perm] = False
            continue
        bot_has = getattr(bot_member, perm, False)
        if bot_has:
            granted[perm] = True
        else:
            granted[perm] = False
            skipped.append(perm)

    try:
        await context.bot.promote_chat_member(chat_id, target_id, **granted)
        return True, [k for k, v in granted.items() if v], skipped, None
    except Exception as e:
        err = str(e)
        try:
            minimal = {"can_delete_messages": True}
            await context.bot.promote_chat_member(chat_id, target_id, **minimal)
            return True, ["can_delete_messages"], skipped + ["(fallback)"], err
        except Exception as e2:
            return False, [], [], f"{err} | fallback: {e2}"


async def get_chat_creator(chat_id, context):
    now = time.time()
    if chat_id in _creator_cache:
        cid, ts = _creator_cache[chat_id]
        if now - ts < CREATOR_CACHE_TTL: return cid
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        for a in admins:
            if a.status == STATUS_CREATOR:
                _creator_cache[chat_id] = (a.user.id, now); return a.user.id
    except Exception: pass
    return None


async def check_user_rank(update, context, chat_id, user_id, required_rank):
    if chat_id >= 0 and required_rank > RANK_NONE:
        await update.message.reply_text(ADMIN_DIALOGS["group_only_long"]); return False
    if chat_id >= 0: return True
    creator = await get_chat_creator(chat_id, context)
    if creator and user_id == creator: return True
    rank = get_admin_rank(chat_id, user_id)
    if rank < required_rank:
        await update.message.reply_text(ADMIN_DIALOGS["no_rights"]); return False
    return True
