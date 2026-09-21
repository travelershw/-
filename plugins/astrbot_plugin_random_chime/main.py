"""Random interjection (主动插嘴) for group chats, driven by a persisted timer.

**按群聊白名单**：只有白名单里的会话才会插嘴，名单为空就是全部禁用（默认）。
白名单存在 ``plugin_data/random_chime_state.json`` 的 ``enabled_sessions`` 里，
在群里用 ``/插嘴开``、``/插嘴关`` 现场管理（仅管理员）。

Every whitelisted group session keeps its own timer. When the timer is due and a
group member posts something substantive, the plugin marks the event as a wake
command so the normal agent pipeline answers it (persona, tools and group context
all apply). The next timer is drawn from a range and scaled by how the bot feels
about the person who just spoke: someone she likes shortens it, someone she
dislikes lengthens it. Interjections stay rare on purpose, to save tokens.

The schedule lives in ``plugin_data/random_chime_state.json`` (wall-clock time),
so reloading plugins or restarting AstrBot does not push the timer back.
"""

import json
import random
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

AFFECTION_STORE = Path(get_astrbot_plugin_data_path()) / "qingyu_affection.json"
STATE_STORE = Path(get_astrbot_plugin_data_path()) / "random_chime_state.json"

# 白名单初始值：只在状态文件里还没有 enabled_sessions 时用它做种子。
# 留空 = 一个群都不插嘴。平时用 /插嘴开、/插嘴关 管理，改这里要重载插件。
# 会话说到底就是 unified_msg_origin，形如 "napcat:GroupMessage:10003"。
CHIME_WHITELIST: tuple[str, ...] = ()
# 总闸：2026-09-17 起打开，进入"真的会插话"的测试阶段。
# 白名单为空时即使总闸开着也不会插。
CHIME_ENABLED = True
# True = 闹钟只负责"到点了"，插不插由核心（qingyu_core）按意愿分决定。
# 核心插件被停用时改成 False，回到"闹钟一响就插"的老行为。
CORE_DECIDES = True
# 每天每群最多插嘴几次（核心也会各自限一次，这里是硬顶）。
DAILY_CHIME_CAP = 8
# Base waiting range before the bot may chime in on its own (minutes).
BASE_MIN_MINUTES = 5.0
BASE_MAX_MINUTES = 15.0
# Hard floor: never chime in twice within this window, and never right after any
# bot message (keeps traffic, latency and token usage low).
MIN_INTERVAL_SECONDS = 180.0
MIN_GAP_AFTER_BOT_SECONDS = 120.0
# Only react to messages with real text and a little substance.
MIN_MESSAGE_CHARS = 6
MAX_MESSAGE_CHARS = 300
# Affection band -> timer multiplier (higher affection = more willing to chime).
AFFECTION_FACTORS = (
    (80, 0.5),
    (60, 0.8),
    (40, 1.0),
    (20, 1.6),
    (0, 2.5),
)


@register(
    "random_chime",
    "migration",
    "群聊随机插嘴（总闸默认关闭＝只评估；打开后按群白名单插嘴）",
    "1.3.0",
)
class RandomChimePlugin(Star):
    """Occasionally joins the conversation without being mentioned."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._due_at: dict[str, float] = {}
        self._last_bot_at: dict[str, float] = {}
        self._chime_count: dict[str, int] = {}
        self._skip_logged: set[str] = set()
        self._enabled: set[str] = set(CHIME_WHITELIST)
        self._disabled_logged: set[str] = set()
        self._load_state()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=95)
    async def maybe_chime(self, event: AstrMessageEvent) -> None:
        """Arm the timer, and tell the core when the moment has come.

        优先级 95 比核心的感知链（90）高：先在这里把"闹钟到点了"挂到事件上，
        核心读完再决定这一句到底接不接（意愿分、间隔、每日额度都在核心那边）。

        Args:
            event: The incoming message event.
        """
        if not event.get_group_id() or event.is_at_or_wake_command:
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        umo = event.unified_msg_origin
        if not CHIME_ENABLED:
            # 只评估阶段：核心插件会把"本来想不想插嘴"记进 turns 表，这里什么都不做。
            if not self._disabled_logged:
                self._disabled_logged.add("*")
                logger.info(
                    "random_chime: 插嘴总闸是关的（只评估阶段），所有群都不会插嘴",
                )
            return
        if umo not in self._enabled:
            # 白名单模式：这个群没被放行，什么都不做（不计时、不插嘴）。
            if umo not in self._disabled_logged:
                self._disabled_logged.add(umo)
                logger.info(
                    f"random_chime: {umo} 不在白名单里，本群插嘴已禁用"
                    "（管理员可用 /插嘴开 放行）",
                )
            return

        text = (event.message_str or "").strip()
        now = time.time()

        due = self._due_at.get(umo)
        if due is None:
            self._arm(umo, event, reason="首次计时")
            return
        if now < due:
            return

        if not MIN_MESSAGE_CHARS <= len(text) <= MAX_MESSAGE_CHARS:
            self._log_skip_once(
                umo,
                f"消息长度 {len(text)} 不在 {MIN_MESSAGE_CHARS}~{MAX_MESSAGE_CHARS} 之间",
            )
            return
        gap = now - self._last_bot_at.get(umo, 0.0)
        if gap < MIN_GAP_AFTER_BOT_SECONDS:
            self._log_skip_once(umo, f"机器人 {gap:.0f} 秒前刚说过话，静默期未过")
            return

        self._skip_logged.discard(umo)
        # 闹钟响了就重新上弦——不管核心最后接不接。节奏由这里管，接不接由核心管。
        # （每日额度也在核心那边判，这边只负责"什么时候允许考虑"。）
        self._chime_count[umo] = self._chime_count.get(umo, 0) + 1
        self._arm(umo, event, reason=f"闹钟响第 {self._chime_count[umo]} 次")
        event.set_extra("qingyu.chime_due", True)
        logger.info(
            f"random_chime: 闹钟到点 {umo}（第 {self._chime_count[umo]} 次），"
            f"交给核心判断 trigger='{text[:24]}'",
        )
        if not CORE_DECIDES:
            # 没有核心时的退路：老行为，闹钟一响就直接插。
            event.is_at_or_wake_command = True

    @filter.after_message_sent()
    async def remember_bot_message(self, event: AstrMessageEvent) -> None:
        """Remember when the bot last spoke, so interjections keep their distance.

        Args:
            event: The message event whose reply was just sent.
        """
        self._last_bot_at[event.unified_msg_origin] = time.time()
        self._save_state()

    @filter.command("插嘴状态")
    async def chime_status(self, event: AstrMessageEvent):
        """查看插嘴白名单与本次会话的计时状态。

        Args:
            event: The command message event.

        Yields:
            The current whitelist and timer state.
        """
        umo = event.unified_msg_origin
        if not CHIME_ENABLED:
            yield event.plain_result(
                "现在插嘴总闸是关的（只评估阶段），所有群都不会插嘴。",
            )
            return
        allowed = "已开启" if umo in self._enabled else "未开启"
        lines = [f"本群插嘴：{allowed}（白名单模式）"]
        if umo in self._enabled:
            due = self._due_at.get(umo)
            if due is None:
                lines.append("这个会话还没开始计时（下一条群消息会启动计时）。")
            else:
                left = max(0.0, due - time.time()) / 60
                lines.append(
                    f"距离下一次闹钟约 {left:.1f} 分钟；"
                    f"今天闹钟响过 {self._chime_count.get(umo, 0)} 次"
                    "（真正插不插由核心按意愿分决定）。",
                )
        lines.append(self._whitelist_text())
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("插嘴开")
    async def enable_chime(self, event: AstrMessageEvent):
        """把本群加入插嘴白名单。

        Args:
            event: The command message event.

        Yields:
            The result of enabling.
        """
        if not event.get_group_id():
            yield event.plain_result("这个指令要在群里用哦~")
            return
        umo = event.unified_msg_origin
        if umo in self._enabled:
            yield event.plain_result(f"本群本来就在白名单里：{self._whitelist_text()}")
            return
        self._enabled.add(umo)
        self._save_state()
        logger.info(f"random_chime: {umo} 已加入白名单 by {event.get_sender_id()}")
        if not CHIME_ENABLED:
            yield event.plain_result(
                f"本群已记进白名单（{self._short(umo)}），但插嘴总闸还关着——"
                "现在只评估不插话，改 random_chime 的 CHIME_ENABLED 再重载才会真的插嘴。",
            )
            return
        yield event.plain_result(
            f"好~本群插嘴已开启（{self._short(umo)}）。闹钟到点后由核心看她想不想接："
            "意愿分够、间隔够、当天额度没超，才会真的插一句。",
        )
        if umo in self._enabled:
            yield event.plain_result(f"本群本来就在白名单里：{self._whitelist_text()}")
            return
        self._enabled.add(umo)
        self._save_state()
        logger.info(f"random_chime: {umo} 已加入白名单 by {event.get_sender_id()}")
        yield event.plain_result(
            f"好~本群插嘴已开启（{self._short(umo)}）。下一条群消息开始计时，"
            "5~15 分钟随机一次，好感度高的人来了会更快。",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("插嘴关")
    async def disable_chime(self, event: AstrMessageEvent):
        """把本群移出插嘴白名单，并清掉计时。

        Args:
            event: The command message event.

        Yields:
            The result of disabling.
        """
        umo = event.unified_msg_origin
        self._enabled.discard(umo)
        self._due_at.pop(umo, None)
        self._skip_logged.discard(umo)
        self._save_state()
        logger.info(f"random_chime: {umo} 已移出白名单 by {event.get_sender_id()}")
        yield event.plain_result(
            f"本群插嘴已关闭（{self._short(umo)}），计时也清掉了。"
            "想再打开：/插嘴开",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("插嘴")
    async def force_chime(self, event: AstrMessageEvent) -> None:
        """立即插一次嘴（管理员用来测试，不受白名单限制）。

        Args:
            event: The command message event.
        """
        logger.info(f"random_chime: manual trigger on {event.unified_msg_origin}")
        event.is_at_or_wake_command = True

    def _short(self, umo: str) -> str:
        """取会话标识的最后一段，方便在群里显示。

        Args:
            umo: Session identifier.

        Returns:
            The trailing group id, or the identifier itself.
        """
        return umo.rsplit(":", 1)[-1] or umo

    def _whitelist_text(self) -> str:
        """Describe the current whitelist for the chat.

        Returns:
            A one-line summary of the enabled sessions.
        """
        if not self._enabled:
            return "白名单为空：所有群都不插嘴（管理员在群里发 /插嘴开 可放行本群）。"
        ids = "、".join(sorted(self._short(umo) for umo in self._enabled))
        return f"白名单（{len(self._enabled)} 个）：{ids}"

    def _arm(self, umo: str, event: AstrMessageEvent, *, reason: str) -> None:
        """Draw and store the next waiting time for a session.

        Args:
            umo: Session identifier.
            event: The message event that just arrived.
            reason: Why the timer was re-armed, used for the log line.
        """
        seconds = self._roll(event)
        self._due_at[umo] = time.time() + seconds
        self._save_state()
        logger.info(
            f"random_chime: armed {umo} -> next chance in {seconds / 60:.1f} min ({reason})",
        )

    def _log_skip_once(self, umo: str, reason: str) -> None:
        """Log a skipped interjection once per due window.

        Args:
            umo: Session identifier.
            reason: Why the interjection was skipped.
        """
        if umo in self._skip_logged:
            return
        self._skip_logged.add(umo)
        logger.info(f"random_chime: 计时已到但跳过 {umo}：{reason}")

    def _roll(self, event: AstrMessageEvent) -> float:
        """Draw the next waiting time for a session.

        Args:
            event: The message event that just arrived.

        Returns:
            Seconds to wait before the bot may chime in again.
        """
        base = random.uniform(BASE_MIN_MINUTES, BASE_MAX_MINUTES) * 60
        factor = 1.0
        for threshold, multiplier in AFFECTION_FACTORS:
            if self._affection(event.get_sender_id()) >= threshold:
                factor = multiplier
                break
        return max(MIN_INTERVAL_SECONDS, base * factor)

    def _affection(self, sender: str) -> int:
        """Read a person's affection score from the affection plugin's store.

        Args:
            sender: Sender id of the person.

        Returns:
            The stored score, or 60 when unknown.
        """
        try:
            data = json.loads(AFFECTION_STORE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 60
        if not isinstance(data, dict):
            return 60
        value = data.get(str(sender), 60)
        return int(value) if isinstance(value, int | float) else 60

    def _load_state(self) -> None:
        """Restore the timer state written by an earlier run."""
        try:
            data = json.loads(STATE_STORE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            stored = data.get("enabled_sessions")
            if isinstance(stored, list):
                self._enabled = {str(item) for item in stored}
            for key, value in (data.get("due_at") or {}).items():
                if isinstance(value, int | float):
                    self._due_at[str(key)] = float(value)
            for key, value in (data.get("last_bot_at") or {}).items():
                if isinstance(value, int | float):
                    self._last_bot_at[str(key)] = float(value)
            for key, value in (data.get("chime_count") or {}).items():
                if isinstance(value, int | float):
                    self._chime_count[str(key)] = int(value)
        # 只留下白名单里的计时，避免放行时接着用很久以前的时间；
        # 同时把升级后的状态（含 enabled_sessions）写回文件。
        self._due_at = {k: v for k, v in self._due_at.items() if k in self._enabled}
        self._save_state()
        logger.info(
            f"random_chime: restored state for {len(self._due_at)} session(s), "
            f"白名单 {len(self._enabled)} 个",
        )

    def _save_state(self) -> None:
        """Persist the timer state so reloads and restarts keep the schedule."""
        try:
            STATE_STORE.parent.mkdir(parents=True, exist_ok=True)
            STATE_STORE.write_text(
                json.dumps(
                    {
                        "enabled_sessions": sorted(self._enabled),
                        "due_at": self._due_at,
                        "last_bot_at": self._last_bot_at,
                        "chime_count": self._chime_count,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"random_chime: failed to save state: {exc}")
