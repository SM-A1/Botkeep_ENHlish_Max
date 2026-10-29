"""Group chat support.

The bot answers a small, fixed set of commands in groups and supergroups:

    /video [N]   choose a quantity (buttons) and a collection, or go straight to the collection
    /new N       N newest videos          /old N   N older videos
    /start /menu /help   short help with an "Open Bot" button

Design rules
* Everything else in a group is ignored, so normal conversation is never touched (this also holds
  when Telegram's privacy mode is switched off for the bot).
* Every button is bound to the member who opened the menu (group_utils.parse_callback): nobody
  else can press it. The flow is stateless, so a group never touches the private-chat state.
* Bans, bot OFF / maintenance, the 18+ confirmation and force-subscribe are enforced BEFORE these
  handlers run (middlewares.AccessGate / force_subscribe.ForceSubscribeGate), exactly as in
  private chats. Those gates answer with a short notice and an "Open Bot" button.
* Deliveries use the SAME pipeline and per-user history as private chats (handlers.user.run_request)
  but are smaller (GROUP_MAX_VIDEOS), paced for Telegram's ~20 messages/minute group limit,
  always content-protected, and only one delivery runs per group at a time.
* The admin can switch the whole feature off: Admin Panel -> Bot Settings -> Group mode.
"""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

import group_utils as gu
import ui
from config import Config
from handlers.user import run_request
from logging_setup import log_event
from services.delivery_service import DeliveryService
from services.event_service import EventService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("group")
router = Router(name="group")
router.message.filter(F.chat.type.in_(gu.GROUP_TYPES))
router.my_chat_member.filter(F.chat.type.in_(gu.GROUP_TYPES))
HTML = "HTML"

_CHAT_LOCKS: dict[int, asyncio.Lock] = {}   # one running delivery per group


# ------------------------------------------------------------------ helpers
def max_in_group(cfg: Config) -> int:
    return max(1, min(cfg.max_videos_per_request, cfg.group_max_videos))


def _chat_lock(chat_id: int) -> asyncio.Lock:
    if len(_CHAT_LOCKS) > 5000:             # bounded memory: forget idle groups
        for key in [k for k, v in _CHAT_LOCKS.items() if not v.locked()]:
            _CHAT_LOCKS.pop(key, None)
    return _CHAT_LOCKS.setdefault(chat_id, asyncio.Lock())


_notice = gu.reply_notice   # short self-deleting reply (shared with the gates)


def _real_member(message: Message):
    """The human who wrote the message; None for anonymous admins, channels and bots."""
    user = message.from_user
    if user is None or user.is_bot or message.sender_chat is not None:
        return None
    return user


async def _deliver(bot: Bot, users: UserService, delivery: DeliveryService,
                   videos: VideoService, cfg: Config, settings: SettingsService, *,
                   chat_id: int, user, category: str, count: int, shuffle: bool = False,
                   cb: CallbackQuery | None = None, message: Message | None = None) -> None:
    lock = _chat_lock(chat_id)
    if lock.locked():
        if cb is not None:
            await cb.answer(ui.alert_text(ui.group_busy_chat_text()), show_alert=True)
        elif message is not None:
            await _notice(message, ui.group_busy_chat_text())
        return
    me = await bot.me()
    async with lock:
        await run_request(bot, users, delivery, videos, cfg, user_id=user.id, chat_id=chat_id,
                          category=category, count=count, shuffle=shuffle, cb=cb,
                          settings=settings, group=True, bot_username=me.username)


# ------------------------------------------------------------------ commands
@router.message(F.text.startswith("/"))
async def on_group_command(message: Message, bot: Bot, cfg: Config, users: UserService,
                           settings: SettingsService, delivery: DeliveryService,
                           videos: VideoService) -> None:
    me = await bot.me()
    parsed = gu.group_command(message.text, me.username)
    user = _real_member(message)
    if parsed is None or user is None:
        return                                  # not for us / nobody to answer
    cmd, arg = parsed
    open_bot = ui.group_open_bot_kb(me.username)

    if not settings.flag("group_mode"):
        await _notice(message, ui.group_disabled_text(), open_bot)
        return

    limit = max_in_group(cfg)
    await users.upsert(user)

    if cmd in ("start", "menu", "help"):
        await message.reply(ui.group_help_text(cfg, limit), reply_markup=open_bot,
                            parse_mode=HTML)
        return

    count = gu.parse_count(arg, limit)
    if arg and count is None:
        await _notice(message, f"⚠️ Please send a number between <code>1</code> and "
                               f"<code>{limit}</code>, e.g. <code>/{cmd} 5</code>.")
        return

    if cmd in ("new", "old"):
        if count is None:
            await _notice(message, f"Usage: <code>/{cmd} 5</code> (1–{limit})")
            return
        log_event(log, logging.INFO, "group_request", user_id=user.id, chat_id=message.chat.id,
                  category=cmd, requested=count)
        await _deliver(bot, users, delivery, videos, cfg, settings, chat_id=message.chat.id,
                       user=user, category=cmd, count=count, message=message)
        return

    # /video, /videos
    if count is None:
        await message.reply(ui.group_ask_count_text(user.id, user.first_name, limit),
                            reply_markup=ui.group_quantity_kb(cfg, user.id, limit),
                            parse_mode=HTML)
    else:
        await message.reply(ui.group_choose_text(user.id, user.first_name, count),
                            reply_markup=ui.group_collection_kb(user.id, count),
                            parse_mode=HTML)


# ------------------------------------------------------------------ buttons
@router.callback_query(F.data.startswith(gu.GROUP_PREFIX))
async def on_group_button(cb: CallbackQuery, bot: Bot, cfg: Config, users: UserService,
                          settings: SettingsService, delivery: DeliveryService,
                          videos: VideoService) -> None:
    msg = cb.message
    if not isinstance(msg, Message) or not gu.is_group_chat(msg.chat):
        await cb.answer()
        return
    parsed = gu.parse_callback(cb.data)
    if parsed is None:
        await cb.answer(ui.alert_text(ui.expired_text()), show_alert=True)
        return
    if cb.from_user.id != parsed["uid"]:        # not your menu
        await cb.answer(ui.alert_text(ui.group_not_owner_text()), show_alert=True)
        return
    if not settings.flag("group_mode"):
        await cb.answer(ui.alert_text(ui.group_disabled_text()), show_alert=True)
        return

    action = parsed["action"]
    if action == "x":
        await cb.answer()
        try:
            await msg.delete()
        except TelegramAPIError:
            pass
        return

    limit = max_in_group(cfg)
    count = parsed["count"]
    if not 1 <= count <= limit:
        await cb.answer(ui.alert_text(ui.expired_text()), show_alert=True)
        return

    if action == "q":                           # quantity chosen -> collection
        await cb.answer()
        try:
            await msg.edit_text(ui.group_choose_text(cb.from_user.id, cb.from_user.first_name, count),
                                reply_markup=ui.group_collection_kb(cb.from_user.id, count),
                                parse_mode=HTML)
        except TelegramAPIError:
            pass
        return

    # "t" (deliver) / "s" (shuffled batch)
    await users.upsert(cb.from_user)
    log_event(log, logging.INFO, "group_request", user_id=cb.from_user.id, chat_id=msg.chat.id,
              category=parsed["category"], requested=count)
    await _deliver(bot, users, delivery, videos, cfg, settings, chat_id=msg.chat.id,
                   user=cb.from_user, category=parsed["category"], count=count,
                   shuffle=action == "s", cb=cb)


# ------------------------------------------------------------------ added to / removed from a group
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added(update: ChatMemberUpdated, bot: Bot, cfg: Config, settings: SettingsService,
                   events: EventService) -> None:
    title = update.chat.title or str(update.chat.id)
    log_event(log, logging.INFO, "group_joined", chat_id=update.chat.id)
    await events.record("admin", f"Bot added to group “{title}” ({update.chat.id})",
                        update.from_user.id if update.from_user else None)
    if not settings.flag("group_mode"):
        return
    try:
        await bot.send_message(update.chat.id, ui.group_welcome_text(max_in_group(cfg)),
                               reply_markup=ui.group_open_bot_kb((await bot.me()).username),
                               parse_mode=HTML)
    except TelegramAPIError:
        pass                                    # no right to write yet: nothing to do


@router.my_chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_removed(update: ChatMemberUpdated, events: EventService) -> None:
    title = update.chat.title or str(update.chat.id)
    log_event(log, logging.INFO, "group_left", chat_id=update.chat.id)
    _CHAT_LOCKS.pop(update.chat.id, None)
    await events.record("admin", f"Bot removed from group “{title}” ({update.chat.id})",
                        update.from_user.id if update.from_user else None)
