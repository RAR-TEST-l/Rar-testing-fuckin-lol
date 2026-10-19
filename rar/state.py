from collections import OrderedDict


roulette_games = {}


_roulette_locks = {}


# Воркеры правок для каждого чата
_rl_edit_tasks = {}


duel_games = {}


_creator_cache = {}


inline_seen = {}


inline_result_cache = {}


recent_playlists = {}


_recent_greets = {}


# ───── Очередь правок рулетки (по одной на чат, с объединением правок) ─────
# Для каждой игры в очереди хранится только ОДНА ожидающая правка (актуальное состояние),
# поэтому анимации с sleep не забивают очередь и не упираются в лимиты Telegram.
_rl_pending = {}   # chat_id -> {gid: [futures]}
_rl_events = {}    # chat_id -> asyncio.Event


_rl_penalty = {}   # chat_id -> добавочная пауза после flood-control от Telegram


saved_users_cache = OrderedDict()
