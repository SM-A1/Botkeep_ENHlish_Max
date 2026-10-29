import asyncio
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

import effects
import ui
from config import Config
from logging_setup import log_event
from services.delivery_service import DeliveryService
from services.settings_service import SettingsService
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("user")
router = Router(name="user")
HTML = "HTML"
CATS = ("new", "old")

# Kept under the old names for anything that still imports them.
TYPE_KB = ui.collection_kb()
AGAIN_KB = ui.back_kb()


# ------------------------------------------------------------------ helpers
async def _say(bot: Bot, chat_id: int, text: str, kb=None,
               effect: str | None = None) -> Message | None:
    if effect:
        try:
            return await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML,
                                          message_effect_id=effect)
        except TelegramAPIError:
            pass   # effect refused: fall through to a plain message
    try:
        return await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        return None


async def _edit(msg: Message | None, text: str, kb=None) -> None:
    if msg is None:
        return
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=HTML)
    except TelegramAPIError:
        pass   # "message is not modified", message gone, ...


async def _alert(bot: Bot, chat_id: int, cb: CallbackQuery | None, text: str) -> None:
    """Tell the user something short: a popup for button taps, a message otherwise."""
    text = ui.alert_text(text)
    if cb is not None:
        try:
            await cb.answer(text, show_alert=True)
            return
        except TelegramAPIError:
            pass
    await _say(bot, chat_id, ui.html_escape(text))


# ------------------------------------------------------------------ the one delivery pipeline
_BG: set[asyncio.Task] = set()


def _background(coro) -> asyncio.Task:
    """Fire-and-forget for things the user is not waiting for (logs, cosmetic edits)."""
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def run_request(bot: Bot, users: UserService, delivery: DeliveryService,
                      videos: VideoService, cfg: Config, *, user_id: int, chat_id: int,
                      category: str, count: int, shuffle: bool = False,
                      cb: CallbackQuery | None = None, locked: bool = False,
                      settings: SettingsService | None = None) -> None:
    """Lock -> rate limit -> unseen check -> deliver -> completion message.

    `locked=True` means the caller already moved the user into the 'delivering' state.
    Everything before the first video runs in parallel (rate limit, unseen videos, button
    answer, status message): the first video leaves as soon as the slowest of them is done."""
    if not locked and not await users.acquire_delivery_lock(user_id):
        await _alert(bot, chat_id, cb, ui.busy_text())
        return
    status: Message | None = None
    cleared = False
    try:
        # The unseen videos are read while the rate limit is checked (read-only: harmless if
        # the request turns out to be rate limited).
        prepare_job = asyncio.create_task(delivery.prepare(user_id, category, count, shuffle))
        try:
            allowed = await users.hit_rate_limit(user_id, cfg.rate_limit_requests,
                                                 cfg.rate_limit_window)
        except BaseException:
            prepare_job.cancel()
            raise
        if not allowed:
            prepare_job.cancel()
            log_event(log, logging.INFO, "rate_limited", user_id=user_id)
            await _alert(bot, chat_id, cb, ui.rate_text())
            return

        async def answer_button() -> None:
            if cb is not None:
                try:
                    await cb.answer()
                except TelegramAPIError:
                    pass

        async def show_status() -> Message | None:
            # One status message: reuse the pressed menu message, or send a fresh one.
            if cb is not None and isinstance(cb.message, Message):
                await _edit(cb.message, ui.preparing_text())
                return cb.message
            return await _say(bot, chat_id, ui.preparing_text())

        _, status, prepared = await asyncio.gather(answer_button(), show_status(), prepare_job)
        batch = prepared.videos
        unseen = len(batch)   # at most `count`: exactly what this request needs to know
        if unseen == 0:
            empty = await videos.count_available() == 0
            other_left = 0 if empty else await videos.count_unseen_all(user_id, ui.other(category))
            log_event(log, logging.INFO, "library_empty" if empty else "category_exhausted",
                      user_id=user_id, category=category)
            text = ui.empty_text() if empty else ui.exhausted_text(category, bool(other_left))
            kb = ui.try_other_kb(category, count) if other_left else ui.finished_kb()
            await _edit(status, text, kb)
            if status is None:
                await _say(bot, chat_id, text, kb)
            return

        n = unseen
        short_of = count if unseen < count else None
        # Cosmetic status edit + the request log run next to the sending, not before it.
        side = [_background(_edit(status, ui.preparing_n_text(n, category, short_of)))]
        if delivery.events is not None:
            side.append(_background(delivery.events.record(
                "user_request", f"{count} {category} video(s) requested"
                + (" (fresh batch)" if shuffle else ""), user_id)))
        if (settings is None or settings.flag("delete_notice_enabled")) \
                and await users.claim_delete_notice(user_id):
            await _say(bot, chat_id, ui.delete_notice_text(cfg, settings))

        log_event(log, logging.INFO, "delivery_started", user_id=user_id, category=category,
                  requested=count, available=unseen, shuffle=shuffle)
        result = await delivery.deliver(user_id, chat_id, category, n, shuffle=shuffle,
                                        prepared=prepared)
        if side:
            await asyncio.gather(*side, return_exceptions=True)   # long done; keeps edit < delete

        # Release the user, drop the "preparing" line and send the summary at the same time.
        async def drop_status() -> None:
            if status is not None:
                try:
                    await status.delete()
                except TelegramAPIError:
                    pass

        async def summary() -> None:
            if result.blocked:
                return
            if result.delivered == 0:
                await _say(bot, chat_id, ui.failed_text(), ui.retry_kb(category, count))
                return
            kb = (ui.after_delivery_kb(category, count, settings) if not short_of
                  else ui.finished_kb())
            await _say(bot, chat_id,
                       ui.delivered_text(cfg, result.delivered, category, short_of,
                                         protected=delivery.protect_for(user_id)), kb,
                       effect=effects.PARTY if settings.flag("delivery_effect") else None)

        cleared = True
        await asyncio.gather(users.clear_state(user_id),
                             drop_status() if not result.blocked else asyncio.sleep(0),
                             summary())
    except Exception as exc:
        log.exception("delivery_crashed")
        if delivery.events is not None:
            await delivery.events.record("error", f"Delivery crashed: {type(exc).__name__}", user_id)
        await _say(bot, chat_id, ui.error_text(), ui.retry_kb(category, count))
    finally:
        if not cleared:
            await users.clear_state(user_id)


# ------------------------------------------------------------------ typed number
@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_number(message: Message, bot: Bot, users: UserService, cfg: Config,
                    delivery: DeliveryService, videos: VideoService,
                    settings: SettingsService) -> None:
    text = message.text.strip()
    count = int(text) if re.fullmatch(r"[0-9]{1,6}", text) else 0
    if count < 1 or count > cfg.max_videos_per_request:
        await users.upsert(message.from_user)
        await message.answer(ui.invalid_text(cfg), parse_mode=HTML)
        return

    user_id = message.from_user.id
    _, (state, data) = await asyncio.gather(users.upsert(message.from_user),
                                            users.get_state(user_id))
    preset = data.get("category") if state == "waiting_for_video_count" else None
    if preset in CATS:   # came from the "New Videos" / "Old Videos" menu buttons
        await run_request(bot, users, delivery, videos, cfg, user_id=user_id,
                          chat_id=message.chat.id, category=preset, count=count,
                          settings=settings)
        return

    ok = await users.set_state(user_id, "waiting_for_category", {"count": count})
    if not ok:
        await message.answer(ui.html_escape(ui.alert_text(ui.busy_text())), parse_mode=HTML)
        return
    log_event(log, logging.INFO, "user_request", user_id=user_id, requested=count)
    await message.answer(ui.choose_collection_text(), reply_markup=ui.collection_kb(),
                         parse_mode=HTML)


# ------------------------------------------------------------------ one-tap quantity
@router.callback_query(F.data.startswith("qty:"))
async def on_quick_count(cb: CallbackQuery, bot: Bot, users: UserService, cfg: Config,
                         delivery: DeliveryService, videos: VideoService,
                         settings: SettingsService) -> None:
    raw = cb.data.split(":", 1)[1]
    count = int(raw) if raw.isdigit() else 0
    if count < 1 or count > cfg.max_videos_per_request:
        await cb.answer(ui.alert_text(ui.expired_text()), show_alert=True)
        return
    user_id = cb.from_user.id
    state, data = await users.get_state(user_id)
    preset = data.get("category") if state == "waiting_for_video_count" else None
    if preset in CATS:
        await run_request(bot, users, delivery, videos, cfg, user_id=user_id,
                          chat_id=cb.message.chat.id, category=preset, count=count,
                          cb=cb, settings=settings)
        return
    if not await users.set_state(user_id, "waiting_for_category", {"count": count}):
        await cb.answer(ui.alert_text(ui.busy_text()), show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_text(ui.choose_collection_text(), reply_markup=ui.collection_kb(),
                                   parse_mode=HTML)
    except TelegramAPIError:
        await _say(bot, cb.message.chat.id, ui.choose_collection_text(), ui.collection_kb())


# ------------------------------------------------------------------ collection chosen
@router.callback_query(F.data.in_({"type:new", "type:old"}))
async def on_type(cb: CallbackQuery, bot: Bot, users: UserService, delivery: DeliveryService,
                  videos: VideoService, cfg: Config, settings: SettingsService) -> None:
    user_id = cb.from_user.id
    category = cb.data.split(":", 1)[1]
    data = await users.claim_state(user_id, "waiting_for_category", "delivering")
    if data is None:
        await cb.answer(ui.alert_text(ui.expired_text()), show_alert=True)
        return
    count = int(data.get("count", 0))
    if count < 1:
        await users.clear_state(user_id)
        await cb.answer(ui.alert_text(ui.expired_text()), show_alert=True)
        return
    log_event(log, logging.INFO, "category_selected", user_id=user_id,
              category=category, requested=count)
    await run_request(bot, users, delivery, videos, cfg, user_id=user_id,
                      chat_id=cb.message.chat.id, category=category, count=count,
                      cb=cb, locked=True, settings=settings)


# ------------------------------------------------------------------ More / Fresh buttons
def _parse(data: str, cfg: Config) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[1] not in CATS or not parts[2].isdigit():
        return None
    return parts[1], max(1, min(int(parts[2]), cfg.max_videos_per_request))


@router.callback_query(F.data.startswith("more:") | F.data.startswith("fresh:"))
async def on_more(cb: CallbackQuery, bot: Bot, users: UserService, delivery: DeliveryService,
                  videos: VideoService, cfg: Config, settings: SettingsService) -> None:
    parsed = _parse(cb.data, cfg)
    if parsed is None:
        await cb.answer()
        return
    category, count = parsed
    fresh = cb.data.startswith("fresh:")
    # `more:` is also used by the retry / "try the other collection" buttons, so it stays
    # available; the 🔥 More buttons themselves are simply hidden when an admin turns them off.
    await run_request(bot, users, delivery, videos, cfg, user_id=cb.from_user.id,
                      chat_id=cb.message.chat.id, category=category, count=count,
                      shuffle=fresh, cb=cb, settings=settings)


@router.callback_query(F.data.in_({"mmore:new", "mmore:old"}))
async def on_menu_more(cb: CallbackQuery, bot: Bot, users: UserService,
                       delivery: DeliveryService, videos: VideoService, cfg: Config,
                       settings: SettingsService) -> None:
    """🔥 More New / ♻️ More Old from the main menu: same size as the user's last request."""
    if not settings.button_on("btn_more"):
        await cb.answer("This option is currently unavailable.", show_alert=True)
        return
    category = cb.data.split(":", 1)[1]
    last = await videos.last_request_size(cb.from_user.id)
    count = max(1, min(last or min(10, cfg.max_videos_per_request), cfg.max_videos_per_request))
    await run_request(bot, users, delivery, videos, cfg, user_id=cb.from_user.id,
                      chat_id=cb.message.chat.id, category=category, count=count, cb=cb,
                      settings=settings)
