"""Small, dependency-free helpers for group-chat support.

Kept free of aiogram imports so the parsing rules can be unit-tested on their own and shared by
the handlers and both middlewares (access gate / force-subscribe gate).

Group callback data (always owner-bound, <= 64 bytes):
    g:q:<uid>:<n>            quantity chosen            -> ask for the collection
    g:t:<uid>:<cat>:<n>      collection chosen / more   -> deliver
    g:s:<uid>:<cat>:<n>      shuffled ("surprise") batch -> deliver
    g:x:<uid>                close the menu
"""
from __future__ import annotations

import re

GROUP_TYPES = ("group", "supergroup")
GROUP_PREFIX = "g:"

# Commands the bot answers in groups. Everything else in a group is ignored, so the bot never
# reacts to normal conversation (also when Telegram "privacy mode" is switched off).
GROUP_COMMANDS = frozenset({"start", "menu", "help", "video", "videos", "new", "old"})

_COUNT_RE = re.compile(r"[0-9]{1,6}")


def is_group_chat(chat) -> bool:
    return getattr(chat, "type", None) in GROUP_TYPES


def parse_command(text: str | None, bot_username: str | None) -> tuple[str, str] | None:
    """('video', '10') for '/video@MyBot 10'. None if it is not a command, or if it is addressed
    to a different bot ('/video@OtherBot'), which must never be answered."""
    if not text or not text.startswith("/"):
        return None
    head, _, rest = text.strip().partition(" ")
    cmd, _, target = head[1:].partition("@")
    if target and (not bot_username or target.lower() != bot_username.lower()):
        return None
    cmd = cmd.lower()
    if not cmd:
        return None
    return cmd, rest.strip()


def group_command(text: str | None, bot_username: str | None) -> tuple[str, str] | None:
    """Like parse_command, but only for commands the bot handles in groups."""
    parsed = parse_command(text, bot_username)
    if parsed is None or parsed[0] not in GROUP_COMMANDS:
        return None
    return parsed


def parse_count(raw: str | None, maximum: int) -> int | None:
    """A request size between 1 and `maximum`, or None if `raw` is not a valid size."""
    raw = (raw or "").strip().split(" ")[0]
    if not _COUNT_RE.fullmatch(raw):
        return None
    n = int(raw)
    return n if 1 <= n <= maximum else None


# ----------------------------------------------------------------- callback data
def cb_quantity(uid: int, n: int) -> str:
    return f"g:q:{uid}:{n}"


def cb_deliver(uid: int, category: str, n: int, shuffle: bool = False) -> str:
    return f"g:{'s' if shuffle else 't'}:{uid}:{category}:{n}"


def cb_close(uid: int) -> str:
    return f"g:x:{uid}"


def parse_callback(data: str | None) -> dict | None:
    """Decode a group callback. None for anything malformed (never raises)."""
    if not data or not data.startswith(GROUP_PREFIX):
        return None
    parts = data.split(":")
    try:
        action = parts[1]
        if action == "x" and len(parts) == 3:
            return {"action": "x", "uid": int(parts[2])}
        if action == "q" and len(parts) == 4:
            return {"action": "q", "uid": int(parts[2]), "count": int(parts[3])}
        if action in ("t", "s") and len(parts) == 5 and parts[3] in ("new", "old"):
            return {"action": action, "uid": int(parts[2]), "category": parts[3],
                    "count": int(parts[4])}
    except (ValueError, IndexError):
        return None
    return None


def deep_link(bot_username: str | None, payload: str = "") -> str | None:
    """https://t.me/<bot>?start=<payload> (payload: letters, digits, _ and - only)."""
    if not bot_username:
        return None
    clean = re.sub(r"[^A-Za-z0-9_-]", "", payload)[:60]
    return f"https://t.me/{bot_username}" + (f"?start={clean}" if clean else "")


def mention(user_id: int, name: str | None) -> str:
    """HTML mention that pings the member; `name` is escaped here."""
    import html
    label = html.escape((name or "").strip()) or "there"
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


# ----------------------------------------------------------------- self-cleaning notices
_TASKS: set = set()
NOTICE_SECONDS = 12


async def reply_notice(message, text: str, kb=None, seconds: int = NOTICE_SECONDS) -> None:
    """Reply to a group command with a short notice that deletes itself after a few seconds, so
    refusals (banned, not verified, busy ...) never clutter the group. Never raises."""
    import asyncio
    from aiogram.exceptions import TelegramAPIError

    try:
        sent = await message.reply(text, reply_markup=kb, parse_mode="HTML")
    except TelegramAPIError:
        return

    async def _later() -> None:
        await asyncio.sleep(seconds)
        try:
            await sent.delete()
        except TelegramAPIError:
            pass

    task = asyncio.create_task(_later())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
