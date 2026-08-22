# ruff: noqa: F403, F405
"""
Optional extra "leech" bots.

BOT_TOKEN (config.py) is always the main bot and always answers the normal,
un-suffixed commands (/leech, /mirror, /status, /cancel, ...).

LEECH2_TOKEN / LEECH3_TOKEN / LEECH4_TOKEN / LEECH5_TOKEN are each fully
optional. If a token is set, that number's bot is started as a completely
separate Telegram bot account and gets its OWN copy of the task-related
commands, suffixed with its number - e.g. LEECH2_TOKEN gives you /leech2,
/mirror2, /status2, /cancel2, /select2, etc. If a token is left blank, that
bot simply never starts and its N-suffixed commands don't exist anywhere -
nothing else about the bot changes.

This lets a group spread task commands across several bot accounts (each
Telegram bot has its own flood/rate limits) instead of a single bot getting
hammered by everyone in a busy group.

Only user-facing task commands are duplicated here (mirror/leech family,
clone, count, del, list, cancel, status, select, forcestart, mediainfo,
help). Owner/admin-only commands (broadcast, restart, exec, shell, rss,
bot settings, sudo/blacklist management, etc.) are intentionally NOT
duplicated - those stay on the main bot only, so there is exactly one place
to administer the bot regardless of how many leech bots are running.

Everything downstream (task_listener, telegram_uploader, message_utils)
already threads the *client that received the command* through the whole
task lifecycle (self.client / message.reply / message.edit), so no changes
were needed there - a task started on leech bot #2 is uploaded, has its
status edited, and can be cancelled entirely through leech bot #2.
"""

from pyrogram.filters import command, regex
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

from .. import LOGGER
from ..helper.telegram_helper.filters import CustomFilters
from ..modules import *  # noqa
from .tg_client import TgClient

# base (un-suffixed) command names -> handler function
# tuple values are (aliases_without_suffix, handler)
_TASK_COMMANDS = {
    "mirror": (["mirror", "m"], mirror),
    "qbmirror": (["qbmirror", "qm"], qb_mirror),
    "jdmirror": (["jdmirror", "jm"], jd_mirror),
    "nzbmirror": (["nzbmirror", "nm"], nzb_mirror),
    "ytdl": (["ytdl", "y"], ytdl),
    "uphoster": (["uphoster", "up"], uphoster),
    "leech": (["leech", "l"], leech),
    "qbleech": (["qbleech", "ql"], qb_leech),
    "jdleech": (["jdleech", "jl"], jd_leech),
    "ytdlleech": (["ytdlleech", "yl"], ytdl_leech),
    "nzbleech": (["nzbleech", "nl"], nzb_leech),
    "clone": (["clone", "cl"], clone_node),
    "count": (["count"], count_node),
    "del": (["del"], delete_file),
    "list": (["list"], gdrive_search),
    "cancelall": (["cancelall", "call"], cancel_all_buttons),
    "forcestart": (["forcestart", "fs"], remove_from_queue),
    "status": (["status", "s"], task_status),
    "mediainfo": (["mediainfo", "mi"], mediainfo),
    "help": (["help", "h"], bot_help),
}

# short alias used for the "/cancel_<gid>" / "/select_<gid>" style deep-link
# regex patterns (mirrors the pattern used in core/handlers.py for the main bot)
_CANCEL_SHORT = "c"
_SELECT_SHORT = "sel"


def _suffixed(names, suffix):
    return [f"{n}{suffix}" for n in names]


def add_task_handlers(client, suffix):
    """Register the task-command subset on `client`, with every command
    name suffixed by `suffix` (e.g. suffix="2" -> /leech2, /mirror2, ...).
    """
    # must be registered first so blacklisted users are blocked before any
    # of the task-command handlers below get a chance to run
    client.add_handler(
        MessageHandler(
            black_listed,
            filters=regex(r"^/") & CustomFilters.authorized & CustomFilters.blacklisted,
        )
    )

    for aliases, handler in _TASK_COMMANDS.values():
        client.add_handler(
            MessageHandler(
                handler,
                filters=command(_suffixed(aliases, suffix), case_sensitive=True)
                & CustomFilters.authorized,
            )
        )

    # /c<suffix>_<gid> (button deep-link only - same as main bot, which has no
    # bare /cancel handler either; use /cancelall<suffix> or the inline
    # buttons under /status<suffix> to cancel tasks). Suffix is mandatory
    # here (unlike the main bot's pattern) so leech-bot #2..#5 don't all
    # try to process a plain "/c_<gid>" meant for the main bot.
    client.add_handler(
        MessageHandler(
            cancel,
            filters=regex(rf"^/{_CANCEL_SHORT}{suffix}(?:_\w+).*$")
            & CustomFilters.authorized,
        )
    )

    # /sel<suffix>_<gid> (button deep-link only, same reasoning as above)
    client.add_handler(
        MessageHandler(
            select,
            filters=regex(rf"^/{_SELECT_SHORT}{suffix}(?:_\w+).*$")
            & CustomFilters.authorized,
        )
    )

    # Callback query buttons attached to messages this client sends only get
    # delivered to this same client by Telegram, so these are safe to
    # register verbatim (no collision with the main bot or other leech bots).
    client.add_handler(CallbackQueryHandler(confirm_selection, filters=regex("^sel")))
    client.add_handler(CallbackQueryHandler(cancel_all_update, filters=regex("^canall")))
    client.add_handler(CallbackQueryHandler(cancel_multi, filters=regex("^stopm")))
    client.add_handler(CallbackQueryHandler(status_pages, filters=regex("^status")))
    client.add_handler(CallbackQueryHandler(select_type, filters=regex("^list_types")))
    client.add_handler(CallbackQueryHandler(arg_usage, filters=regex("^help")))


async def add_all_leech_bot_handlers():
    for no, client in TgClient.leech_bots.items():
        add_task_handlers(client, str(no))
        LOGGER.info(
            f"Registered leech-bot #{no} command handlers ([@{client.me.username}])"
        )
