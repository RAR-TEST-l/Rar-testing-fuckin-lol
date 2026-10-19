import psycopg2
import random
import threading
import time
from psycopg2 import pool as pg_pool
from psycopg2.extensions import STATUS_READY
from urllib.parse import urlparse
from rar.config import DATABASE_URL, DB_POOL_MAX, DB_STALE_AFTER, GROUP_ANON_BOT_ID, PLAYLIST_LIMIT, RANK_NONE, RECENT_PLAYLISTS_LIMIT, RECENT_PLAYLISTS_TTL, SYS_CACHE_TTL
from rar.state import recent_playlists


# ───── Пул соединений с БД ─────
# Раньше на КАЖДЫЙ запрос открывалось новое SSL-соединение (сотни мс), и всё это блокировало
# event loop бота. Теперь соединения переиспользуются, интерфейс (cursor/commit/close) тот же.
_db_pool = None
_db_pool_lock = threading.Lock()
_db_last_used = {}


def _db_connect_raw():
    result = urlparse(DATABASE_URL)
    return psycopg2.connect(database=result.path[1:], user=result.username,
        password=result.password, host=result.hostname, port=result.port, sslmode='require',
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def _get_pool():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                result = urlparse(DATABASE_URL)
                _db_pool = pg_pool.ThreadedConnectionPool(1, DB_POOL_MAX,
                    database=result.path[1:], user=result.username, password=result.password,
                    host=result.hostname, port=result.port, sslmode='require',
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
    return _db_pool


class _PooledConn:
    """Обёртка: close() возвращает соединение в пул, а не закрывает его."""
    def __init__(self, conn):
        self._conn = conn
        self._released = False

    def cursor(self, *a, **k): return self._conn.cursor(*a, **k)
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()
    def __getattr__(self, name): return getattr(self._conn, name)

    def close(self):
        if self._released: return
        self._released = True
        conn = self._conn
        try:
            if conn.closed:
                _db_pool.putconn(conn, close=True)
            else:
                if conn.status != STATUS_READY:
                    conn.rollback()
                _db_last_used[id(conn)] = time.time()
                _db_pool.putconn(conn)
        except Exception:
            try: _db_pool.putconn(conn, close=True)
            except Exception: pass

    def __del__(self):
        # если где-то забыли close() (например, из-за исключения) — вернём соединение в пул
        try: self.close()
        except Exception: pass


def get_db_connection():
    try:
        pool = _get_pool()
        conn = pool.getconn()
    except pg_pool.PoolError:
        return _db_connect_raw()  # пул занят — запасное прямое соединение
    now = time.time()
    if conn.closed or now - _db_last_used.get(id(conn), 0) > DB_STALE_AFTER:
        try:
            if conn.closed: raise psycopg2.OperationalError("closed")
            cur = conn.cursor(); cur.execute("SELECT 1"); cur.close(); conn.rollback()
        except Exception:
            try: pool.putconn(conn, close=True)
            except Exception: pass
            conn = pool.getconn()
    _db_last_used[id(conn)] = now
    return _PooledConn(conn)


def init_db():
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS channel_music (file_id TEXT PRIMARY KEY, title TEXT NOT NULL, added_by BIGINT, added_at TIMESTAMP DEFAULT NOW())")
    cursor.execute("ALTER TABLE channel_music ADD COLUMN IF NOT EXISTS track_num SERIAL")
    cursor.execute("ALTER TABLE channel_music ADD COLUMN IF NOT EXISTS inline_broken BOOLEAN DEFAULT FALSE")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_music_track_num ON channel_music(track_num)")
    cursor.execute("CREATE TABLE IF NOT EXISTS track_stats (track_num INTEGER PRIMARY KEY REFERENCES channel_music(track_num) ON DELETE CASCADE, plays BIGINT DEFAULT 0, last_played TIMESTAMP DEFAULT NOW())")
    cursor.execute("CREATE TABLE IF NOT EXISTS chat_settings (chat_id BIGINT PRIMARY KEY, greet_enabled BOOLEAN DEFAULT TRUE, farewell_enabled BOOLEAN DEFAULT TRUE, greet_text TEXT DEFAULT 'Добро пожаловать в %чат%, %имя%', farewell_text TEXT DEFAULT 'Пока пока, %имя%, буду скучать!')")
    cursor.execute("ALTER TABLE chat_settings ADD COLUMN IF NOT EXISTS kall_enabled BOOLEAN DEFAULT TRUE")
    cursor.execute("CREATE TABLE IF NOT EXISTS chat_members (chat_id BIGINT PRIMARY KEY, user_ids BIGINT[] DEFAULT '{}')")
    cursor.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS usernames (user_id BIGINT PRIMARY KEY, username TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS chat_admins (chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, rank INTEGER NOT NULL, assigned_by BIGINT, assigned_at TIMESTAMP DEFAULT NOW(), PRIMARY KEY (chat_id, user_id))")
    cursor.execute("CREATE TABLE IF NOT EXISTS sleuths (id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, target_id BIGINT NOT NULL, target_name TEXT, admin_id BIGINT NOT NULL, admin_name TEXT, required_count INTEGER NOT NULL, current_count INTEGER DEFAULT 0, duration_seconds INTEGER NOT NULL, action TEXT DEFAULT 'none', started_at TIMESTAMP DEFAULT NOW(), deadline TIMESTAMP NOT NULL)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleuths_chat ON sleuths(chat_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sleuths_target ON sleuths(chat_id, target_id)")
    conn.commit(); cursor.close(); conn.close()


_username_cache = {}


def save_username(user_id, username):
    if not username: return
    uname = username.lower()
    if _username_cache.get(user_id) == uname: return  # не пишем в БД на каждое сообщение
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("INSERT INTO usernames (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username", (user_id, uname))
        conn.commit(); cursor.close(); conn.close()
        if len(_username_cache) > 20000: _username_cache.clear()
        _username_cache[user_id] = uname
    except Exception: pass


def find_user_by_username(username):
    if not username: return None
    username = username.lstrip("@").lower()
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM usernames WHERE username = %s", (username,))
    row = cursor.fetchone(); cursor.close(); conn.close()
    return row[0] if row else None


def get_admin_rank(chat_id, user_id):
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT rank FROM chat_admins WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
        row = cursor.fetchone(); cursor.close(); conn.close()
        return row[0] if row else RANK_NONE
    except Exception: return RANK_NONE


def set_admin_rank(chat_id, user_id, rank, by_user):
    conn = get_db_connection(); cursor = conn.cursor()
    if rank <= 0:
        cursor.execute("DELETE FROM chat_admins WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    else:
        cursor.execute("INSERT INTO chat_admins (chat_id, user_id, rank, assigned_by, assigned_at) VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (chat_id, user_id) DO UPDATE SET rank = EXCLUDED.rank, assigned_by = EXCLUDED.assigned_by, assigned_at = NOW()", (chat_id, user_id, rank, by_user))
    conn.commit(); cursor.close(); conn.close()


def get_all_admins(chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT user_id, rank FROM chat_admins WHERE chat_id = %s ORDER BY rank DESC", (chat_id,))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def get_kall_enabled(chat_id):
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT kall_enabled FROM chat_settings WHERE chat_id = %s", (chat_id,))
        row = cursor.fetchone(); cursor.close(); conn.close()
        if row is None: return True
        return row[0]
    except Exception: return True


def set_kall_enabled(chat_id, val):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_settings (chat_id, kall_enabled) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET kall_enabled = EXCLUDED.kall_enabled", (chat_id, val))
    conn.commit(); cursor.close(); conn.close()


_sleuth_chats = set()
_sleuth_chats_ts = 0


def sleuth_chat_has_any(chat_id):
    """Есть ли в чате активные слежки (кэш на 60 сек), чтобы не лезть в БД на каждое сообщение."""
    global _sleuth_chats, _sleuth_chats_ts
    now = time.time()
    if now - _sleuth_chats_ts > 60:
        try:
            conn = get_db_connection(); cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT chat_id FROM sleuths")
            _sleuth_chats = {r[0] for r in cursor.fetchall()}
            cursor.close(); conn.close()
            _sleuth_chats_ts = now
        except Exception:
            return True
    return chat_id in _sleuth_chats


def sleuth_create(chat_id, target_id, target_name, admin_id, admin_name, required_count, duration_seconds, action):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, target_id))
    deadline_ts = time.time() + duration_seconds
    cursor.execute("INSERT INTO sleuths (chat_id, target_id, target_name, admin_id, admin_name, required_count, current_count, duration_seconds, action, deadline) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, TO_TIMESTAMP(%s))", (chat_id, target_id, target_name, admin_id, admin_name, required_count, duration_seconds, action, deadline_ts))
    conn.commit(); cursor.close(); conn.close()
    _sleuth_chats.add(chat_id)


def sleuth_get(chat_id, target_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT id, target_id, target_name, admin_id, admin_name, required_count, current_count, duration_seconds, action, deadline FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, target_id))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def sleuth_get_all(chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT id, target_id, target_name, admin_id, admin_name, required_count, current_count, duration_seconds, action, deadline FROM sleuths WHERE chat_id = %s ORDER BY started_at DESC", (chat_id,))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def sleuth_delete(chat_id, target_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, target_id))
    deleted = cursor.rowcount
    conn.commit(); cursor.close(); conn.close()
    return deleted


def sleuth_delete_by_id(sid):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM sleuths WHERE id = %s", (sid,))
    conn.commit(); cursor.close(); conn.close()


def sleuth_get_expired():
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT id, chat_id, target_id, target_name, admin_id, admin_name, required_count, current_count, duration_seconds, action FROM sleuths WHERE deadline <= NOW()")
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def save_track_to_db(file_id, title, added_by):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT title FROM channel_music WHERE file_id = %s OR LOWER(title) = LOWER(%s) LIMIT 1", (file_id, title))
    if cursor.fetchone():
        cursor.close(); conn.close(); return False
    cursor.execute("INSERT INTO channel_music (file_id, title, added_by) VALUES (%s, %s, %s) ON CONFLICT (file_id) DO NOTHING", (file_id, title, added_by))
    conn.commit(); cursor.close(); conn.close(); return True


def search_track_in_db(query):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT file_id, title, track_num FROM channel_music WHERE LOWER(title) LIKE LOWER(%s) LIMIT 1", (f"%{query.strip().lower()}%",))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def search_track_in_db_by_title(title):
    if not title: return None
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT file_id, title, track_num FROM channel_music WHERE LOWER(title) = LOWER(%s) LIMIT 1", (title.strip(),))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def search_tracks_in_db(query, limit=20):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT file_id, title, track_num FROM channel_music WHERE LOWER(title) LIKE LOWER(%s) AND inline_broken = FALSE LIMIT %s", (f"%{query.strip().lower()}%", limit))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def get_all_tracks_from_db(only_ok=False):
    conn = get_db_connection(); cursor = conn.cursor()
    if only_ok: cursor.execute("SELECT file_id, title FROM channel_music WHERE inline_broken = FALSE")
    else: cursor.execute("SELECT file_id, title FROM channel_music")
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def mark_track_broken(file_id):
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("UPDATE channel_music SET inline_broken = TRUE WHERE file_id = %s", (file_id,))
        conn.commit(); cursor.close(); conn.close()
    except Exception as e: print(f"[MARK BROKEN ERROR] {e}")


def get_broken_tracks():
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT track_num, title FROM channel_music WHERE inline_broken = TRUE ORDER BY track_num")
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def delete_track_from_db(file_id, fallback_title=None):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT title, track_num FROM channel_music WHERE file_id = %s", (file_id,))
    row = cursor.fetchone()
    if not row and fallback_title:
        cursor.execute("SELECT title, track_num FROM channel_music WHERE LOWER(title) = LOWER(%s)", (fallback_title,))
        row = cursor.fetchone()
    if row:
        title, tnum = row
        cursor.execute("UPDATE playlists SET track_nums = array_remove(track_nums, %s) WHERE %s = ANY(track_nums)", (tnum, tnum))
        cursor.execute("DELETE FROM channel_music WHERE track_num = %s", (tnum,))
        conn.commit(); cursor.close(); conn.close(); return title
    cursor.close(); conn.close(); return None


def get_track_num(file_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT track_num FROM channel_music WHERE file_id = %s", (file_id,))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row[0] if row else None


def get_track_by_num(track_num):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT file_id, title FROM channel_music WHERE track_num = %s", (track_num,))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def get_tracks_by_nums(track_nums):
    if not track_nums: return []
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT track_num, file_id, title FROM channel_music WHERE track_num = ANY(%s) AND inline_broken = FALSE", (track_nums,))
    rows = cursor.fetchall(); cursor.close(); conn.close()
    mapping = {r[0]: (r[1], r[2]) for r in rows}
    return [mapping[tn] for tn in track_nums if tn in mapping]


def increment_play(file_id):
    try:
        tn = get_track_num(file_id)
        if not tn: return
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("INSERT INTO track_stats (track_num, plays, last_played) VALUES (%s, 1, NOW()) ON CONFLICT (track_num) DO UPDATE SET plays = track_stats.plays + 1, last_played = NOW()", (tn,))
        conn.commit(); cursor.close(); conn.close()
    except Exception as e: print(f"[INCREMENT PLAY ERROR] {e}")


def get_top_tracks(limit=10):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT cm.title, ts.plays FROM track_stats ts JOIN channel_music cm ON cm.track_num = ts.track_num WHERE ts.plays > 0 ORDER BY ts.plays DESC LIMIT %s", (limit,))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def create_playlist(name, owner_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id FROM playlists WHERE LOWER(name) = LOWER(%s)", (name,))
    if cursor.fetchone():
        cursor.close(); conn.close(); return None, "exists"
    try:
        cursor.execute("INSERT INTO playlists (name, owner_id) VALUES (%s, %s) RETURNING playlist_id", (name, owner_id))
        pid = cursor.fetchone()[0]
        cursor.execute("INSERT INTO playlist_stats (playlist_id) VALUES (%s) ON CONFLICT DO NOTHING", (pid,))
        conn.commit(); cursor.close(); conn.close(); return pid, None
    except Exception as e:
        conn.rollback(); cursor.close(); conn.close(); return None, str(e)


def get_playlist_by_name(name):
    if not name: return None
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id, name, owner_id, track_nums FROM playlists WHERE LOWER(name) = LOWER(%s)", (name,))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def get_playlist_by_id(pid):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id, name, owner_id, track_nums FROM playlists WHERE playlist_id = %s", (pid,))
    row = cursor.fetchone(); cursor.close(); conn.close(); return row


def get_user_playlists(owner_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id, name, track_nums FROM playlists WHERE owner_id = %s ORDER BY created_at ASC", (owner_id,))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def delete_playlist(pid):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM playlists WHERE playlist_id = %s", (pid,))
    conn.commit(); cursor.close(); conn.close()


def rename_playlist(pid, new_name):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id FROM playlists WHERE LOWER(name) = LOWER(%s) AND playlist_id != %s", (new_name, pid))
    if cursor.fetchone():
        cursor.close(); conn.close(); return "exists"
    cursor.execute("UPDATE playlists SET name = %s WHERE playlist_id = %s", (new_name, pid))
    conn.commit(); cursor.close(); conn.close(); return None


def add_track_to_playlist(pid, track_num):
    if track_num is None: return "not_found"
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT track_nums FROM playlists WHERE playlist_id = %s", (pid,))
    row = cursor.fetchone()
    if not row:
        cursor.close(); conn.close(); return "not_found"
    nums = row[0] or []
    if track_num in nums:
        cursor.close(); conn.close(); return "already"
    if len(nums) >= PLAYLIST_LIMIT:
        cursor.close(); conn.close(); return "limit"
    cursor.execute("UPDATE playlists SET track_nums = array_append(track_nums, %s) WHERE playlist_id = %s", (track_num, pid))
    conn.commit(); cursor.close(); conn.close(); return "ok"


def remove_track_from_playlist(pid, track_num):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE playlists SET track_nums = array_remove(track_nums, %s) WHERE playlist_id = %s", (track_num, pid))
    conn.commit(); cursor.close(); conn.close()


def increment_playlist_search(pid):
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("INSERT INTO playlist_stats (playlist_id, searches, last_searched) VALUES (%s, 1, NOW()) ON CONFLICT (playlist_id) DO UPDATE SET searches = playlist_stats.searches + 1, last_searched = NOW()", (pid,))
        conn.commit(); cursor.close(); conn.close()
    except Exception as e: print(f"[PLAYLIST SEARCH ERROR] {e}")


def get_top_playlists(limit=10):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT p.name, ps.searches FROM playlist_stats ps JOIN playlists p ON p.playlist_id = ps.playlist_id WHERE ps.searches > 0 ORDER BY ps.searches DESC LIMIT %s", (limit,))
    rows = cursor.fetchall(); cursor.close(); conn.close(); return rows


def get_random_playlist_with_tracks(user_id):
    now = time.time()
    seen = recent_playlists.get(user_id, {})
    seen = {pid: ts for pid, ts in seen.items() if now - ts < RECENT_PLAYLISTS_TTL}
    if len(seen) > RECENT_PLAYLISTS_LIMIT:
        seen = dict(sorted(seen.items(), key=lambda x: x[1])[-RECENT_PLAYLISTS_LIMIT:])
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT playlist_id, name, owner_id, track_nums FROM playlists WHERE cardinality(track_nums) > 0")
    all_pls = cursor.fetchall(); cursor.close(); conn.close()
    if not all_pls:
        recent_playlists[user_id] = seen; return None
    available = [p for p in all_pls if p[0] not in seen]
    if not available: seen = {}; available = all_pls
    chosen = random.choice(available)
    seen[chosen[0]] = now
    recent_playlists[user_id] = seen
    return chosen


def get_chat_settings(chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_settings (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING", (chat_id,))
    conn.commit()
    cursor.execute("SELECT greet_enabled, farewell_enabled, greet_text, farewell_text FROM chat_settings WHERE chat_id = %s", (chat_id,))
    row = cursor.fetchone(); cursor.close(); conn.close()
    return row if row else (True, True, 'Добро пожаловать в %чат%, %имя%', 'Пока пока, %имя%, буду скучать!')


def update_chat_setting(chat_id, field, value):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute(f"INSERT INTO chat_settings (chat_id, {field}) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET {field} = EXCLUDED.{field}", (chat_id, value))
    conn.commit(); cursor.close(); conn.close()


def save_user_to_chat(user_id, chat_id):
    if user_id == GROUP_ANON_BOT_ID: return
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_members (chat_id, user_ids) VALUES (%s, ARRAY[%s::BIGINT]) ON CONFLICT (chat_id) DO UPDATE SET user_ids = (SELECT ARRAY(SELECT DISTINCT unnest(chat_members.user_ids || EXCLUDED.user_ids)))", (chat_id, user_id))
    conn.commit(); cursor.close(); conn.close()


def remove_user_from_chat(user_id, chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE chat_members SET user_ids = array_remove(user_ids, %s::BIGINT) WHERE chat_id = %s", (user_id, chat_id))
    cursor.execute("DELETE FROM chat_members WHERE chat_id = %s AND cardinality(user_ids) = 0", (chat_id,))
    cursor.execute("DELETE FROM chat_admins WHERE chat_id = %s AND user_id = %s", (chat_id, user_id))
    cursor.execute("DELETE FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, user_id))
    conn.commit(); cursor.close(); conn.close()


def remove_users_from_chat_batch(user_ids, chat_id):
    if not user_ids: return
    conn = get_db_connection(); cursor = conn.cursor()
    for uid in user_ids:
        cursor.execute("UPDATE chat_members SET user_ids = array_remove(user_ids, %s::BIGINT) WHERE chat_id = %s", (int(uid), chat_id))
        cursor.execute("DELETE FROM chat_admins WHERE chat_id = %s AND user_id = %s", (chat_id, int(uid)))
        cursor.execute("DELETE FROM sleuths WHERE chat_id = %s AND target_id = %s", (chat_id, int(uid)))
    cursor.execute("DELETE FROM chat_members WHERE chat_id = %s AND cardinality(user_ids) = 0", (chat_id,))
    conn.commit(); cursor.close(); conn.close()


def remove_chat_data(chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_members WHERE chat_id = %s", (chat_id,))
    cursor.execute("DELETE FROM chat_settings WHERE chat_id = %s", (chat_id,))
    cursor.execute("DELETE FROM chat_admins WHERE chat_id = %s", (chat_id,))
    cursor.execute("DELETE FROM sleuths WHERE chat_id = %s", (chat_id,))
    conn.commit(); cursor.close(); conn.close()


def get_chat_members(chat_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT user_ids FROM chat_members WHERE chat_id = %s", (chat_id,))
    row = cursor.fetchone(); cursor.close(); conn.close()
    return [int(uid) for uid in row[0]] if row and row[0] else []


_sys_cache = {}


def get_system_setting(key):
    now = time.time()
    hit = _sys_cache.get(key)
    if hit and now - hit[1] < SYS_CACHE_TTL: return hit[0]
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
        row = cursor.fetchone(); cursor.close(); conn.close()
        value = row[0] if row else None
        _sys_cache[key] = (value, now)
        return value
    except Exception as e:
        print(f"[SYSTEM_SETTINGS] Error: {e}")
        return hit[0] if hit else None


def get_owner_id():
    v = get_system_setting("owner_id"); return int(v) if v else None


def get_storage_chat_id():
    v = get_system_setting("storage_chat_id"); return int(v) if v else None


def get_notify_chat_id():
    v = get_system_setting("notify_chat_id"); return int(v) if v else None
