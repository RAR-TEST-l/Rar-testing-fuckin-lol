import os


TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
GROUP_ANON_BOT_ID = 1087968824
SAVED_USERS_CACHE_LIMIT = 2000
RAR_LOGO_URL = "https://raw.githubusercontent.com/romanmisarin71-maker/Rar_bot/main/RarFaceBulka.png"
MODERATION_CHAT_ID = int(os.environ.get("MODERATION_CHAT_ID", "-1003907890387"))

STATUS_CREATOR = "creator"
STATUS_ADMINISTRATOR = "administrator"
STATUS_MEMBER = "member"
STATUS_RESTRICTED = "restricted"
STATUS_LEFT = "left"
STATUS_KICKED = "kicked"
STATUS_BANNED = "banned"
ADMIN_STATUSES = {STATUS_ADMINISTRATOR, STATUS_CREATOR}
IN_CHAT_STATUSES = {STATUS_MEMBER, STATUS_RESTRICTED, STATUS_ADMINISTRATOR, STATUS_CREATOR}
LEAVE_STATUSES = {STATUS_LEFT, STATUS_KICKED, STATUS_BANNED}


RANK_NONE = 0; RANK_LOW = 1; RANK_MID = 2; RANK_SENIOR = 3
RANK_NONE = 0; RANK_LOW = 1; RANK_MID = 2; RANK_SENIOR = 3
RANK_NONE = 0; RANK_LOW = 1; RANK_MID = 2; RANK_SENIOR = 3
RANK_NONE = 0; RANK_LOW = 1; RANK_MID = 2; RANK_SENIOR = 3
RANK_NAMES = {0: "не админ", 1: "мл. админ", 2: "ср. админ", 3: "ст. админ"}


RANK_PERMS = {
    RANK_NONE: {
        "can_delete_messages": False, "can_restrict_members": False,
        "can_invite_users": False, "can_manage_chat": False,
        "can_promote_members": False, "can_change_info": False,
        "can_pin_messages": False, "can_manage_topics": False,
    },
    RANK_LOW: {
        "can_delete_messages": True, "can_restrict_members": False,
        "can_invite_users": False, "can_manage_chat": False,
        "can_promote_members": False, "can_change_info": False,
        "can_pin_messages": False, "can_manage_topics": False,
    },
    RANK_MID: {
        "can_delete_messages": True, "can_restrict_members": True,
        "can_invite_users": True, "can_manage_chat": False,
        "can_promote_members": False, "can_change_info": False,
        "can_pin_messages": False, "can_manage_topics": False,
    },
    RANK_SENIOR: {
        "can_delete_messages": True, "can_restrict_members": True,
        "can_invite_users": True, "can_manage_chat": True,
        "can_promote_members": False, "can_change_info": True,
        "can_pin_messages": True, "can_manage_topics": True,
    },
}


PERM_NAMES_RU = {
    "can_delete_messages": "Удаление сообщений",
    "can_restrict_members": "Блокировка участников",
    "can_invite_users": "Добавление участников",
    "can_manage_chat": "Управление чатом",
    "can_promote_members": "Назначение админов",
    "can_change_info": "Изменение профиля группы",
    "can_pin_messages": "Закрепление сообщений",
    "can_manage_topics": "Управление темами",
}


ROULETTE_TIMEOUT = 900
RL_NEXT_ROUND_TIMEOUT = 180  # сколько ждём «следующий раунд», прежде чем освободить слот


ITEM_NAMES = {
    "lupa": "Лупа", "pila": "Пила", "naruch": "Наручники", "sigara": "Сигарета",
    "tabletki": "Таблетки", "telefon": "Телефон", "bpatron": "Б. Патрон",
    "nasadka": "Насадка", "adrenalin": "Адреналин", "zerkalo": "Зеркало",
}


ROUND2_POOL = ["lupa", "pila", "naruch", "sigara"]
ROUND3_POOL = ["tabletki", "telefon", "bpatron", "nasadka", "adrenalin", "zerkalo"]


ROUND2_ITEM_LIMIT = 6
ROUND3_ITEM_LIMIT = 8
ROUND2_RELOAD_BONUS = 2
ROUND3_RELOAD_BONUS = 4
MAX_DUPLICATE_ITEMS = 2


DUEL_ACTIONS = ["ПИУ", "ПАУ", "ПАФ"]
DUEL_COUNTERS = {"ПИУ": "ПАУ", "ПАУ": "ПИУ", "ПАФ": "ПАФ"}


DUEL_TIMEOUT = 120


CREATOR_CACHE_TTL = 3600


INLINE_SEEN_LIMIT = 100
INLINE_SEEN_TTL = 300


INLINE_RESULT_CACHE_TTL = 300


PLAYLIST_LIMIT = 20
RECENT_PLAYLISTS_LIMIT = 20
RECENT_PLAYLISTS_TTL = 300


GREET_DEDUP_TTL = 5


DB_POOL_MAX = 10
DB_STALE_AFTER = 20  # если соединение простаивало дольше — проверяем его перед использованием


MAX_CHAT_ROULETTES = 3


SYS_CACHE_TTL = 60


GREET_PREFIXES = ["рар измени приветствие","рар, измени приветствие","rar измени приветствие","rar, измени приветствие","рар изменить приветствие","рар, изменить приветствие","rar изменить приветствие","rar, изменить приветствие"]
FAREWELL_PREFIXES = ["рар измени прощание","рар, измени прощание","rar измени прощание","rar, измени прощание","рар изменить прощание","рар, изменить прощание","rar изменить прощание","rar, изменить прощание"]
DELETE_COMMANDS = ["рар удали","рар, удали","rar удали","rar, удали","рар удалить","рар, удалить","rar удалить","rar, удалить"]
