import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import ui
from config import Config
from database import Database
from logging_setup import log_event
from models.video import Video
from services.user_service import UserService
from services.video_service import VideoService

log = logging.getLogger("delivery")

_INVALID_MARKERS = ("file identifier", "file_id", "file reference", "wrong remote file")
_GONE_MARKERS = ("message to copy not found", "message_id_invalid", "message can't be copied")


# ------------------------------------------------------------------ speed helpers
class _Gate:
    """Bot-wide smoother. Telegram allows ~30 messages/second for a whole bot; this spaces the
    START of every send (all users together) so a busy moment never triggers a bot-wide flood
    wait. A single user is never slowed down by it: one send already takes longer than the gap."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            await asyncio.sleep(slot - now)


_FLOOD_MEMORY: dict[int, tuple[float, float]] = {}   # chat_id -> (delay, when it was learned)
_FLOOD_MEMORY_SECONDS = 90.0


class _Pacer:
    """Per-delivery adaptive pacing. Starts at the configured SEND_DELAY (default 0 = full
    speed). The first time Telegram answers 'flood wait' for this chat it obeys the wait and
    keeps a small gap between the remaining videos, so one flood wait never turns into many.
    The learned gap is remembered for this chat for a short time (the next request starts
    with it instead of hitting the same wait again)."""

    def __init__(self, base: float, chat_id: int | None = None) -> None:
        self.base = max(0.0, float(base or 0.0))
        self.chat_id = chat_id
        self.delay = self.base
        self._first = True
        learned = _FLOOD_MEMORY.get(chat_id) if chat_id is not None else None
        if learned and time.monotonic() - learned[1] < _FLOOD_MEMORY_SECONDS:
            self.delay = max(self.delay, learned[0])

    async def wait(self) -> None:
        if self._first:          # the very first video of a delivery never waits
            self._first = False
            return
        if self.delay > 0:
            await asyncio.sleep(self.delay)

    def flood(self) -> None:
        # cap: 1 s for normal chats; for group pacing (base 3.2 s) never go below the base
        self.delay = min(max(1.0, self.base * 2), max(self.delay * 2, 0.3, self.base))
        if self.chat_id is not None:
            if len(_FLOOD_MEMORY) > 2000:
                _FLOOD_MEMORY.clear()
            _FLOOD_MEMORY[self.chat_id] = (self.delay, time.monotonic())


@dataclass
class Prepared:
    """Result of `DeliveryService.prepare`: read-only, nothing has been sent yet."""
    videos: list = field(default_factory=list)
    prevented: int = 0


class _History:
    """Delivery bookkeeping OFF the sending path.

    Sending a video never waits for the database: each sent video is queued here and written
    in small groups (one statement per group, in the background) while the next videos are
    already on their way. `close()` flushes what is left and waits for it - also when the
    delivery is cancelled or crashes - so everything that was sent is always recorded and
    never sent again."""

    CHUNK = 5

    def __init__(self, svc: "DeliveryService", user_id: int, chat_id: int, mode: str) -> None:
        self.svc, self.user_id, self.chat_id, self.mode = svc, user_id, chat_id, mode
        self.rows: list[tuple] = []
        self.task: asyncio.Task | None = None

    def add(self, video: Video, message_id: int | None) -> None:
        delete_at = datetime.now(timezone.utc) + timedelta(seconds=float(self.svc.cfg.auto_delete_seconds))
        self.rows.append((video, message_id, delete_at))
        if len(self.rows) >= self.CHUNK:
            self._kick()

    def _kick(self) -> None:
        batch, self.rows = self.rows, []
        previous = self.task
        self.task = asyncio.create_task(self._write(batch, previous))

    async def _write(self, batch: list[tuple], previous: asyncio.Task | None) -> None:
        if previous is not None:
            await previous          # groups are written in order; _write never raises
        await self.svc._write_history(self.user_id, self.chat_id, self.mode, batch)

    async def close(self) -> None:
        if self.rows:
            self._kick()
        task = self.task
        if task is None:
            return
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True    # finish recording first, then let the cancellation go on
            except Exception:       # noqa: BLE001 - _write logs its own failures
                break
        if cancelled:
            raise asyncio.CancelledError


@dataclass
class DeliveryResult:
    delivered: int = 0
    failed: int = 0
    blocked: bool = False


class DeliveryService:
    def __init__(self, bot: Bot, db: Database, videos: VideoService,
                 users: UserService, cfg: Config, settings=None, events=None) -> None:
        self.settings = settings   # SettingsService (optional): caption options
        self.events = events       # EventService (optional): admin Logs panel
        self.bot = bot
        self.db = db
        self.videos = videos
        self.users = users
        self.cfg = cfg
        self._access_warned = False
        self._gate = _Gate(float(getattr(cfg, "global_send_rate", 25.0) or 0))
        self._bg: set[asyncio.Task] = set()

    async def prepare(self, user_id: int, category: str, count: int,
                       shuffle: bool = False) -> Prepared:
        """Read-only first step, run before anything is sent: the next unseen videos of this
        user's mode and (statistics only) how many of the list's first `count` the user
        already has. Both queries run at the same time."""
        async def prevented() -> int:
            if shuffle:   # statistics only; must never get in the way of a delivery
                return 0
            try:
                return int(await self.videos.count_seen_ahead_all(user_id, category, count) or 0)
            except Exception:
                log.exception("prevented_counter_failed")
                return 0

        batch, skipped = await asyncio.gather(
            self.videos.fetch_session_batch(user_id, category, count, [], shuffle), prevented())
        return Prepared(list(batch), skipped)

    def _spawn(self, coro) -> asyncio.Task:
        """Run a side job (session row, statistics) next to the delivery, never on its path."""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    @staticmethod
    async def _quiet(coro, what: str) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(what)

    async def deliver(self, user_id: int, chat_id: int, category: str, count: int,
                      shuffle: bool = False, prepared: Prepared | None = None,
                      group: bool = False) -> DeliveryResult:
        """Send up to `count` videos this user has never received, in the order of their mode
        (`category` is the mode: new = newest -> oldest, old = oldest -> newest).
        Videos go out back to back, in order. Every sent video is written to
        user_video_history and queued for auto-deletion in the background, and the user's own
        session row (mode / position / progress) is advanced. Nothing is shared between
        users. `prepared` = the result of `prepare()` when the caller already ran it.

        `group=True` (the videos go into a group chat): the videos are always content-protected
        (everybody in the group can see them) and are paced to stay under Telegram's limit of
        about 20 messages per minute for one group, so the delivery is not aborted by flood
        waits. A refusal to send is then the GROUP's problem, never the user's."""
        result = DeliveryResult()
        tried_ids: set[int] = set()
        sent_unique: set[str] = set()
        base_delay = float(getattr(self.cfg, "send_delay", 0.0) or 0.0)
        if group:
            base_delay = max(base_delay, float(getattr(self.cfg, "group_send_gap", 3.2) or 0.0))
        pacer = _Pacer(base_delay, chat_id)
        protect = self.protect_for(user_id, group=group)
        history = _History(self, user_id, chat_id, category)
        pause = float(getattr(self.cfg, "delivery_batch_pause", 0.0) or 0.0)
        every = max(1, int(getattr(self.cfg, "delivery_batch_size", 10) or 10))

        # the user's own session: remember the chosen mode (next to the sending, not before it)
        session_job = self._spawn(self._quiet(self.users.start_session(user_id, category),
                                              "session_start_failed"))
        if prepared is None:
            prepared = await self.prepare(user_id, category, count, shuffle)

        try:
            for round_no in range(5):  # extra rounds replace videos that failed
                need = count - result.delivered
                if need <= 0:
                    break
                if round_no == 0:
                    batch = prepared.videos[:need]
                else:
                    batch = await self.videos.fetch_session_batch(user_id, category, need,
                                                                  list(tried_ids), shuffle)
                if not batch:
                    break
                for video in batch:
                    tried_ids.add(video.id)
                    if (not self.videos.allow_duplicates
                            and video.telegram_file_unique_id in sent_unique):
                        continue
                    outcome, message_id = await self._send_one(
                        chat_id, video, result.delivered + 1, protect, pacer)
                    if outcome is None:  # user blocked the bot
                        result.blocked = True
                        break
                    if outcome:
                        sent_unique.add(video.telegram_file_unique_id)
                        result.delivered += 1
                        history.add(video, message_id)
                        if pause and result.delivered % every == 0:
                            await asyncio.sleep(pause)   # optional extra breather (default off)
                    else:
                        result.failed += 1
                if result.blocked:
                    break
        finally:
            await history.close()   # everything that was sent is recorded, even on cancel

        if result.blocked and not group and chat_id == user_id:
            await self.users.mark_blocked(user_id)   # only a private chat means "blocked the bot"
        await self._record(user_id, category, count, result, prepared.prevented, session_job)
        return result

    async def _write_history(self, user_id: int, chat_id: int, mode: str,
                             rows: list[tuple]) -> None:
        """History rows (global + this mode's) + auto-delete jobs for a group of sent videos in
        ONE statement, plus the progress bookkeeping and the lock heartbeat, all in parallel."""
        videos = [r[0].id for r in rows]
        cats = [r[0].category for r in rows]
        mids = [r[1] for r in rows]
        due = [r[2] for r in rows]

        async def history() -> None:
            for attempt in range(3):
                try:
                    await self.db.pool.execute(
                        """
                        WITH d AS (
                            SELECT * FROM unnest($3::bigint[], $4::text[], $5::bigint[],
                                                 $6::timestamptz[])
                                   AS t(video_id, category, message_id, delete_at)
                        ), h AS (
                            INSERT INTO user_video_history (user_id, video_id, category)
                            SELECT $1, video_id, category FROM d
                            ON CONFLICT (user_id, video_id) DO NOTHING
                        ), m AS (
                            INSERT INTO user_mode_history (user_id, mode, video_id)
                            SELECT $1, $2, video_id FROM d
                            ON CONFLICT DO NOTHING
                        )
                        INSERT INTO delivered_messages
                            (user_id, chat_id, video_id, telegram_message_id, delete_at)
                        SELECT $1, $7, video_id, message_id, delete_at FROM d
                        WHERE message_id IS NOT NULL
                        """,
                        user_id, mode, videos, cats, mids, due, chat_id)
                    return
                except Exception:
                    log.exception("record_delivery_failed")
                    await asyncio.sleep(0.5 * (attempt + 1))
            log_event(log, logging.ERROR, "record_delivery_gave_up", user_id=user_id,
                      video_id=videos[-1])

        last = rows[-1][0]
        # Position/progress bookkeeping and lock heartbeat are best effort: the delivery
        # history (above) is what guarantees no repeats.
        await asyncio.gather(
            history(),
            self._quiet(self.users.advance_session_bulk(
                user_id, mode, last.id, last.message_date or last.created_at, len(rows)),
                "session_advance_failed"),
            self._quiet(self.users.refresh_delivery_lock(user_id), "lock_refresh_failed"),
        )

    def protect_for(self, user_id: int, group: bool = False) -> bool:
        """Telegram content protection for the videos sent to ONE user: no forwarding, no saving
        or downloading from the chat. Every user is protected until an admin allows Save/Forward
        for that user (User Manager). The global switch in Bot Settings stays a master switch:
        if an admin turns it OFF, nobody is protected."""
        if self.settings is not None and not self.settings.flag("protect_content"):
            return False
        if group:   # a group is a shared screen: a personal Save/Forward permission cannot apply
            return True
        return self.users.protect_for(user_id)

    async def _record(self, user_id: int, category: str, count: int, r: DeliveryResult,
                      prevented: int = 0, session_job: asyncio.Task | None = None) -> None:
        jobs = []
        if session_job is not None:
            jobs.append(session_job)
        if prevented:
            jobs.append(self._quiet(self.videos.bump_counter("delivery_duplicates_prevented",
                                                             prevented),
                                    "prevented_counter_failed"))
        if r.delivered:
            jobs.append(self._quiet(self.users.finish_batch(user_id, category),
                                    "session_finish_failed"))
        jobs.append(self.db.pool.execute(
            "INSERT INTO requests (user_id, category, requested, delivered, failed) "
            "VALUES ($1, $2, $3, $4, $5)",
            user_id, category, count, r.delivered, r.failed))
        log_event(log, logging.INFO, "delivery_finished", user_id=user_id, category=category,
                  requested=count, delivered=r.delivered, failed=r.failed, blocked=r.blocked)
        if self.events is not None:
            jobs.append(self.events.record(
                "delivery", f"{r.delivered}/{count} {category} videos delivered"
                + (f", {r.failed} failed" if r.failed else "")
                + (", user blocked the bot" if r.blocked else ""), user_id))
            if r.failed:
                jobs.append(self.events.record("error", f"{r.failed} video(s) could not be sent",
                                               user_id))
        await asyncio.gather(*jobs)   # one round-trip of waiting instead of five

    async def _send_one(self, chat_id: int, video: Video, position: int,
                        protect: bool = True,
                        pacer: _Pacer | None = None) -> tuple[bool | None, int | None]:
        """(True, message_id) = sent, (False, None) = failed, (None, None) = user blocked the bot.

        Delivery modes, tried in order: the stored Bot API file_id, then copying the original
        post out of the source channel (no "forwarded from", so the channel is never exposed).
        Videos found by the history scanner have no file_id and always use the copy."""
        # Clean caption of our own. The channel caption is only added if the admin opted in
        # (Bot Settings): channel captions can carry channel names or links.
        keep = self.settings is not None and self.settings.flag("keep_source_caption")
        caption = ui.video_caption(self.cfg, video, position, video.caption if keep else None)
        modes = []
        if video.telegram_file_id:
            modes.append("file")
        if video.channel_id is not None and video.telegram_message_id is not None:
            modes.append("copy")
        invalid_reasons = []
        for mode in modes:
            outcome, reason, message_id = await self._attempt(mode, chat_id, video, caption, protect, pacer)
            if outcome == "ok":
                return True, message_id
            if outcome == "blocked":
                return None, None
            if outcome == "error":      # transient / access problem: keep the video active
                return False, None
            invalid_reasons.append(reason)
        if len(invalid_reasons) == len(modes):   # every way of sending it is dead
            await self.videos.mark_invalid(video.id, "; ".join(invalid_reasons) or "no way to send")
            log_event(log, logging.ERROR, "video_deactivated", video_id=video.id)
            if self.events is not None:
                await self.events.record("error", f"Video ID {video.id} can no longer be sent "
                                                  "and was deactivated")
            await self._notify_admins(
                f"⚠️ Video ID {video.id} (#{video.video_number}, {video.category}) "
                "can no longer be sent and was deactivated. Re-scan the channel with /scan "
                "or send the video to the bot again to restore it."
            )
        return False, None

    async def _attempt(self, mode: str, chat_id: int, video: Video,
                       caption: str, protect: bool = True,
                       pacer: _Pacer | None = None) -> tuple[str, str, int | None]:
        """One delivery mode. Returns ("ok"|"blocked"|"error"|"invalid", reason, message_id).

        No fixed sleeps: videos go out as fast as Telegram accepts them. A flood wait is obeyed
        exactly (and teaches the pacer to leave a small gap), it does not use up a retry."""
        attempts = floods = 0
        while attempts < 5:
            try:
                await self._gate.wait()
                if pacer is not None:
                    await pacer.wait()
                if mode == "file":
                    # Original dimensions/duration are passed through untouched so Telegram's own
                    # viewer shows portrait / landscape / square exactly as posted; no
                    # thumbnail, re-encode, crop or padding is ever added.
                    sent = await self.bot.send_video(
                        chat_id, video.telegram_file_id, caption=caption, parse_mode="HTML",
                        width=video.width, height=video.height, duration=video.duration,
                        supports_streaming=True, protect_content=protect,
                    )
                else:
                    # copy_message keeps the original media (size, ratio, streaming) as is.
                    sent = await self.bot.copy_message(
                        chat_id, video.channel_id, video.telegram_message_id,
                        caption=caption, parse_mode="HTML",
                        protect_content=protect,
                    )
                return "ok", "", sent.message_id
            except TelegramRetryAfter as e:
                floods += 1
                log_event(log, logging.WARNING, "flood_wait", seconds=e.retry_after)
                if pacer is not None:
                    pacer.flood()
                if floods > 8:
                    return "error", "flood wait", None
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramForbiddenError as e:
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    return "blocked", "", None
                if mode == "copy":  # the BOT has no access to the channel: not the user's fault
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                return "blocked", "", None
            except TelegramBadRequest as e:
                msg = str(e).lower()
                if mode == "file" and any(m in msg for m in _INVALID_MARKERS):
                    log_event(log, logging.WARNING, "invalid_file_id", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and any(m in msg for m in _GONE_MARKERS):
                    log_event(log, logging.WARNING, "source_message_gone", video_id=video.id)
                    return "invalid", str(e)[:100], None
                if mode == "copy" and "chat not found" in msg:
                    await self._warn_channel_access(str(e))
                    return "error", str(e), None
                log_event(log, logging.ERROR, "telegram_bad_request", video_id=video.id, error=str(e))
                return "error", str(e), None
            except TelegramAPIError as e:  # network / server hiccups
                attempts += 1
                log_event(log, logging.WARNING, "telegram_error", video_id=video.id,
                          attempt=attempts, error=str(e))
                await asyncio.sleep(min(2 ** (attempts - 1), 4) * 0.5)   # quick retry, not 1-16 s
            except Exception:
                log.exception("unexpected_send_error")
                return "error", "unexpected", None
        return "error", "retries exhausted", None

    async def _warn_channel_access(self, detail: str) -> None:
        if self._access_warned:
            return
        self._access_warned = True
        log_event(log, logging.ERROR, "channel_access_denied", error=detail[:200])
        await self._notify_admins(
            "⚠️ The bot cannot read the source channel, so scanned videos cannot be delivered. "
            "Add the bot to the channel as an administrator."
        )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass
