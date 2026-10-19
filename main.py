import asyncio
import os
import traceback
from aiohttp import web
from telegram.error import RetryAfter
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, ChosenInlineResultHandler, CommandHandler, InlineQueryHandler, MessageHandler, filters
from rar.config import DATABASE_URL, TOKEN
from rar.db import get_owner_id, init_db
from rar.dialogs import SETTINGS_DIALOGS
from rar.games.roulette import rl_callback, rl_stuck_watchdog, rl_timeout_loop
from rar.handlers.commands import handle_message, start_command
from rar.handlers.inline import chosen_inline_result_handler, inline_query_handler, playlist_nav_handler
from rar.handlers.members import handle_chat_member, handle_my_chat_member
from rar.jobs import daily_track_loop, keep_database_alive, sleuth_deadline_loop


async def error_handler(update, context):
    if isinstance(context.error, RetryAfter):
        wait = context.error.retry_after
        print(f"[FLOOD CONTROL] Retry after {wait}s")
        return
    err_text = str(context.error)
    tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))[:1500]
    print(f"[ERROR HANDLER] {err_text}\n{tb}")
    try:
        owner_id = get_owner_id()
        if owner_id:
            is_network = any(kw in err_text for kw in ["httpx", "httpcore", "ReadTimeout", "ConnectTimeout", "ConnectionError", "NetworkError", "TimedOut"])
            if is_network:
                print(f"[NETWORK ERROR] {err_text}"); return
            is_noise = ("bot was kicked from the group chat" in err_text
                        or "bot was blocked by the user" in err_text
                        or "chat not found" in err_text)
            prefix = "NOISE" if is_noise else "ERROR"
            await context.bot.send_message(chat_id=owner_id, text=f"{prefix}\n<code>{err_text[:600]}</code>\n\n<pre>{tb[:1500]}</pre>", parse_mode="HTML")
    except Exception as e: print(f"[ERROR HANDLER SEND FAIL] {e}")


async def handle_http(request):
    return web.Response(text=SETTINGS_DIALOGS["bot_alive"])


async def start_webhook():
    app = web.Application()
    app.router.add_get("/", handle_http)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def on_startup(application):
    asyncio.create_task(start_webhook())
    asyncio.create_task(keep_database_alive())
    asyncio.create_task(daily_track_loop(application))
    asyncio.create_task(rl_timeout_loop(application))
    asyncio.create_task(rl_stuck_watchdog(application))
    asyncio.create_task(sleuth_deadline_loop(application))


def main():
    if not TOKEN or not DATABASE_URL:
        print("Ошибка: Переменные окружения не заданы!"); return
    init_db()
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(on_startup).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))
    app.add_handler(CallbackQueryHandler(playlist_nav_handler, pattern=r"^pl_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(rl_callback, pattern=r"^rl_"))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("Запуск бота...")
    app.run_polling(allowed_updates=["message", "chat_member", "my_chat_member", "inline_query", "chosen_inline_result", "callback_query"])
