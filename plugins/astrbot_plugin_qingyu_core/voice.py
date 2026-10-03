"""什么时候让她用语音说话（阶段 2a：只私聊、只被动回复）。

**为什么要有这个模块**：语音好不好听是一回事，"什么时候该出声"是另一回事——
信息类的话（数据、链接、解释）用文字更清楚，而问候/关心/哄人这类"有温度没信息量"的话
用语音才加分；群里发语音还可能打扰别人。所以把"该不该出声"做成**可解释的打分 + 配额**，
而不是随机数：每次不发都会在日志里写清理由。

规则（按顺序判定，任一不过就退回文字）：

1. **会话必须显式开过**（``/语音 自动 开``）——默认谁都不发，免得对不认识的人突然出声；
2. **只私聊**：群聊一律文字；
3. **长度 ≤ 60 字**：再长就该看，不该听（设备通道例外，上限 600 字：那边是切段合成的，
   见 :data:`DEVICE_MAX_CHARS`）；
4. **夜里（23:00–08:00）只有你刚发的是语音才回语音**——对等回应，不主动吵人；
5. **冷却 10 分钟**、**每会话每天 20 条**、**全局每天 300 条**（钱的天花板）；
6. **内容打分 ≥ 0.5**：问候/关心/哄人加分，带数字/链接/问句减分（见 :func:`score`）。

状态落在 ``plugin_data/qingyu_voice.json``：会话开关、每日计数、每人最后一条的时间。
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

STATE_PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_voice.json"
CACHE_SECONDS = 3.0
# 只留最近几天：文件别长成日志
KEEP_DAYS = 3

DEFAULTS: dict = {
    "max_chars": 60,
    "cooldown_seconds": 600,
    "daily_per_session": 20,
    "daily_total": 300,
    "threshold": 0.5,
    "night_from": 23,
    "night_to": 8,
    # True = 文字和语音都发；默认只发语音（私聊里更自然）
    "dual": False,
    # 只在这些平台上生效：QQ（napcat/aiocqhttp）。桌宠通道、面板通道都不该收到语音段。
    "platforms": ["aiocqhttp", "napcat"],
    # 「语音优先」的平台：设备通道（眼镜）。在这里语音是**常态**而不是特例，所以
    # 群聊那几条限制不适用（长度上限、冷却、每会话条数、内容打分、夜间对等），
    # 但**全局每日条数**仍然守着——钱的天花板不能拆。
    "voice_first_platforms": ["glasses"],
    # ---- 测试模式（`/语音 测试 开`）：群聊也出声，但**有边界**，到点/到量自己关 ----
    "test_minutes": 30,
    "test_count": 10,
    # 测试时长度上限放到服务的单次上限（200 字），而不是平时的 60
    "test_max_chars": 200,
}
# 设备通道（眼镜 / 手机）单条最多念多少字。**必须**和 `tts.SPEECH_TOTAL_CHARS`（600）对齐：
# 那边是"按标点切段、总长封顶"的实现，这边是"长到不该念"的判定，两边不对齐就会出现
# **判定为不念 → 文字有了、声音一个字都没有**（2026-10-03 实测：306 字的回答 → 0 字节音频，
# 用户连着两轮报"回答还是没有声音"，就是被这里的 200 字门槛挡掉的）。
DEVICE_MAX_CHARS = 600
# 有温度的话：问候、关心、哄人、祝福
WARM_WORDS = (
    "晚安",
    "早安",
    "早上好",
    "晚上好",
    "午安",
    "节日快乐",
    "生日快乐",
    "新年",
    "辛苦",
    "加油",
    "抱抱",
    "想你",
    "别累",
    "记得吃",
    "记得喝",
    "注意身体",
    "好好休息",
    "早点睡",
    "别熬夜",
    "我在呢",
    "陪着你",
    "开心",
    "别难过",
    "没事的",
    "乖",
)
# 语气词：短句里带这些更像"随口一句话"，不是资料
TONE_WORDS = ("呀", "啦", "呢", "哦", "嘛", "吧", "噢", "诶", "嘿", "呜")
# 信息特征：出现就不再适合语音（听起来费劲）
INFO_MARKS = ("http", "```", "\\", "：", "=", "**", "|")

# ---- 动态语气（"怎么念"）：语速/音调都是**相对默认的偏移** ----
# 让她按心情/精力/时段/关系换腔调，而不是永远一个调门。
# 安全范围：别越界到听不出人话（服务端允许 rate −50~100、pitch −12~12）。
SAFE_RATE = (-25, 25)
SAFE_PITCH = (-6, 6)
# 哄人/安慰类：慢下来、压低一点
GENTLE_WORDS = ("晚安", "早点睡", "别熬夜", "休息", "抱抱", "没事", "别难过", "乖", "陪着你")
# 高兴类：快一点、亮一点
CHEER_WORDS = ("哈哈", "开心", "太好了", "好耶", "嘿嘿", "真棒", "耶")
# 郑重类（祝福）：稳一点
SOLEMN_WORDS = ("快乐", "祝福", "生日", "新年", "节日", "恭喜")

_state: dict | None = None
_state_at = 0.0


@dataclass
class Decision:
    """One "should she speak this out loud" answer."""

    allowed: bool
    reason: str
    score: float = 0.0
    detail: str = ""
    speech_rate: int = 0
    pitch: int = 0
    prosody_why: str = ""

    def params(self) -> dict:
        """Request parameters for the speech service.

        Returns:
            ``{}`` when nothing needs overriding, otherwise the overrides
            （语速与音调；音调走 ``post_process.pitch``）。
        """
        values: dict = {}
        if self.speech_rate:
            values["speech_rate"] = int(self.speech_rate)
        if self.pitch:
            values["post_process"] = {"pitch": int(self.pitch)}
        return values

    def describe_voice(self) -> str:
        """One line about how she would sound.

        Returns:
            e.g. ``语速 -10、音调 -3（夜里轻一点）``.
        """
        if not self.speech_rate and not self.pitch:
            return "原声"
        parts = []
        if self.speech_rate:
            parts.append(f"语速 {self.speech_rate:+d}")
        if self.pitch:
            parts.append(f"音调 {self.pitch:+d}")
        detail = f"（{self.prosody_why}）" if self.prosody_why else ""
        return "、".join(parts) + detail


def prosody_for(
    *,
    text: str,
    night: bool,
    mood: int | None = None,
    energy: int | None = None,
    trust: int | None = None,
    familiarity: int | None = None,
) -> tuple[int, int, str]:
    """How should this line sound? (语速偏移, 音调偏移, 为什么）

    全是一眼能看懂的加减法，最后夹到安全范围里：

    - **夜里**轻一点：语速 −10、音调 −3（不吵人）；
    - **心情好**（≥70）活泼：+8 / +2；**心情低**（≤35）柔一点：−8 / −2；
    - **精力低**（≤30）慢慢说：语速 −6；
    - **信任低**（<45）别飘：音调 −2；
    - **老熟人**（照面 ≥200）随意点：语速 +4；
    - **话本身**：哄人休息/安慰 → −8 / −2；高兴 → +6 / +2；祝福 → −6。

    Args:
        text: Her reply.
        night: Whether it is the quiet window.
        mood: Her mood (0–100), when known.
        energy: Her energy (0..100), when known.
        trust: Trust level with this person, when known.
        familiarity: How many times they have met, when known.

    Returns:
        ``(speech_rate, pitch, why)``.
    """
    rate = 0
    pitch = 0
    why: list[str] = []
    if night:
        rate -= 10
        pitch -= 3
        why.append("夜里轻一点")
    if mood is not None:
        if mood >= 70:
            rate += 8
            pitch += 2
            why.append("心情好")
        elif mood <= 35:
            rate -= 8
            pitch -= 2
            why.append("心情低")
    if energy is not None and energy <= 30:
        rate -= 6
        why.append("累了")
    if trust is not None and trust < 45:
        pitch -= 2
        why.append("收着点")
    if familiarity is not None and familiarity >= 200:
        rate += 4
        why.append("老熟人")
    if any(word in text for word in GENTLE_WORDS):
        rate -= 8
        pitch -= 2
        why.append("哄你")
    elif any(word in text for word in CHEER_WORDS):
        rate += 6
        pitch += 2
        why.append("高兴")
    elif any(word in text for word in SOLEMN_WORDS):
        rate -= 6
        why.append("郑重")
    rate = max(SAFE_RATE[0], min(SAFE_RATE[1], rate))
    pitch = max(SAFE_PITCH[0], min(SAFE_PITCH[1], pitch))
    return rate, pitch, "、".join(why) or "没什么特别的"


def _empty() -> dict:
    """A fresh state document.

    Returns:
        The default state.
    """
    return {"auto": {}, "days": {}, "last": {}, "test": {}}


def state() -> dict:
    """Read the state file (cached for a few seconds).

    Returns:
        The state document with ``auto`` / ``days`` / ``last``.
    """
    global _state, _state_at
    now = time.time()
    if _state is None or now - _state_at >= CACHE_SECONDS:
        values = _empty()
        try:
            stored = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(stored, dict):
                for key in values:
                    if isinstance(stored.get(key), dict):
                        values[key] = stored[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning(f"qingyu_core: 语音状态读不了（{type(exc).__name__}），用默认值")
        _state, _state_at = values, now
    return _state


def _write(values: dict) -> None:
    """Persist the state and refresh the cache.

    Args:
        values: The state document to store.
    """
    global _state, _state_at
    # 只留最近几天
    days = values.get("days") or {}
    for key in sorted(days)[:-KEEP_DAYS]:
        days.pop(key, None)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"qingyu_core: 语音状态写不了（{exc}）")
    _state, _state_at = values, time.time()


def settings() -> dict:
    """Effective settings.

    Returns:
        Defaults (no separate settings file for now — 阶段 2a 先固定这套规则）。
    """
    return dict(DEFAULTS)


def enabled_for(umo: str) -> bool:
    """Has this session turned automatic voice on?

    Args:
        umo: Session origin.

    Returns:
        True when ``/语音 自动 开`` was used here.
    """
    return bool((state().get("auto") or {}).get(str(umo)))


def set_auto(umo: str, enabled: bool) -> str:
    """Turn automatic voice on/off for one session.

    Args:
        umo: Session origin.
        enabled: New state.

    Returns:
        A chat-ready line.
    """
    values = state()
    auto = dict(values.get("auto") or {})
    if enabled:
        auto[str(umo)] = True
    else:
        auto.pop(str(umo), None)
    values["auto"] = auto
    _write(values)
    logger.info(f"qingyu_core: 自动语音 {'开' if enabled else '关'}（{umo}）")
    if enabled:
        return (
            "好，这个会话我会在合适的时候用语音回你——"
            "短句、问候和哄人的话才出声，长话和资料还是文字；"
            "发「/语音 自动 关」随时关掉，发「/语音 统计」看今天发了多少。"
        )
    return "好，这个会话我改用文字了。"


def _today() -> str:
    """Today's local date key.

    Returns:
        ``YYYY-MM-DD``.
    """
    return datetime.now().strftime("%Y-%m-%d")


def test_active(umo: str) -> dict | None:
    """Is test mode on for this session (and still within its bounds)?

    测试模式会**自动过期**（默认 30 分钟 / 最多 10 条）：忘了关也不会一直吵群。

    Args:
        umo: Session origin.

    Returns:
        ``{"until": ts, "left": n}`` while active, otherwise ``None``.
    """
    values = state()
    entry = (values.get("test") or {}).get(str(umo))
    if not isinstance(entry, dict):
        return None
    if float(entry.get("until", 0.0)) <= time.time() or int(entry.get("left", 0)) <= 0:
        _clear_test(umo)
        return None
    return {"until": float(entry["until"]), "left": int(entry["left"])}


def _clear_test(umo: str) -> None:
    """Drop this session's test entry.

    Args:
        umo: Session origin.
    """
    values = state()
    tests = dict(values.get("test") or {})
    tests.pop(str(umo), None)
    values["test"] = tests
    _write(values)


def set_test(
    umo: str,
    enabled: bool,
    minutes: int | None = None,
    count: int | None = None,
) -> str:
    """Turn test mode on/off for one session.

    开会话的自动语音也一起打开（省得两条命令），并给出明确的边界说明。

    Args:
        umo: Session origin.
        enabled: New state.
        minutes: How long it may run (default from settings).
        count: How many voice replies it may send.

    Returns:
        A chat-ready line.
    """
    values = settings()
    if not enabled:
        _clear_test(umo)
        logger.info(f"qingyu_core: 语音测试模式 关（{umo}）")
        return "好，测试模式关了。"
    minutes = int(minutes or values["test_minutes"])
    count = int(count or values["test_count"])
    minutes = max(1, min(240, minutes))
    count = max(1, min(100, count))
    state_values = state()
    tests = dict(state_values.get("test") or {})
    tests[str(umo)] = {"until": time.time() + minutes * 60, "left": count}
    state_values["test"] = tests
    _write(state_values)
    set_auto(umo, True)
    logger.info(f"qingyu_core: 语音测试模式 开（{umo}，{minutes} 分钟 / {count} 条）")
    return (
        f"⚠ 测试模式开了：**这个会话（包括群）接下来的回答会用语音**，"
        f"{minutes} 分钟或发满 {count} 条后**自动关**。"
        "想立刻停就发「/语音 测试 关」。"
    )


def consume_test(umo: str) -> str:
    """Count one test-mode voice reply and report what is left.

    Args:
        umo: Session origin.

    Returns:
        A short description (empty when test mode is not active).
    """
    entry = test_active(umo)
    if entry is None:
        return ""
    left = entry["left"] - 1
    if left <= 0:
        _clear_test(umo)
        logger.info(f"qingyu_core: 语音测试模式用完了（{umo}）")
        return "测试模式已用完（自动关）"
    state_values = state()
    tests = dict(state_values.get("test") or {})
    tests[str(umo)] = {"until": entry["until"], "left": left}
    state_values["test"] = tests
    _write(state_values)
    return f"测试模式还剩 {left} 条"


def is_night(now: datetime | None = None) -> bool:
    """Is it the quiet window?

    Args:
        now: Local time (defaults to now).

    Returns:
        True between ``night_from`` and ``night_to``.
    """
    values = settings()
    hour = (now or datetime.now()).hour
    start, end = int(values["night_from"]), int(values["night_to"])
    return hour >= start or hour < end


def score(text: str) -> tuple[float, str]:
    """How suitable is this line for being spoken?

    Args:
        text: Her reply.

    Returns:
        ``(score 0..1, why)``.
    """
    values = settings()
    text = (text or "").strip()
    if not text:
        return 0.0, "空"
    points: list[str] = []
    value = 0.0
    if any(word in text for word in WARM_WORDS):
        value += 0.5
        points.append("有问候/关心的话")
    length = len(text)
    if length <= 20:
        value += 0.25
        points.append("很短")
    elif length <= 40:
        value += 0.15
        points.append("偏短")
    if not any(mark in text for mark in INFO_MARKS) and not any(
        ch.isdigit() for ch in text
    ):
        value += 0.2
        points.append("没有数字/资料")
    else:
        value -= 0.3
        points.append("带数字或资料（文字更清楚）")
    if any(word in text for word in TONE_WORDS):
        value += 0.1
        points.append("有语气词")
    if text.endswith(("？", "?")):
        value -= 0.2
        points.append("是问句")
    value = max(0.0, min(1.0, value))
    return value, "、".join(points) or "没什么特别的"


def _counts(umo: str, today: str) -> tuple[int, int]:
    """Today's counters.

    Args:
        umo: Session origin.
        today: Date key.

    Returns:
        ``(this session's count, global count)``.
    """
    day = (state().get("days") or {}).get(today) or {}
    sessions = day.get("sessions") or {}
    return int(sessions.get(str(umo), 0)), int(day.get("total", 0))


def decide(
    *,
    text: str,
    umo: str,
    platform: str,
    is_private: bool,
    user_was_voice: bool = False,
    mood: int | None = None,
    energy: int | None = None,
    trust: int | None = None,
    familiarity: int | None = None,
    now: datetime | None = None,
) -> Decision:
    """Should this reply be sent as voice, and how should it sound?

    Args:
        text: Her reply.
        umo: Session origin.
        platform: Platform name (``napcat`` / ``desktop_pet`` …).
        is_private: True for one-to-one chats.
        user_was_voice: The message being answered was itself a voice message.
        mood: Her mood (0–100) when known.
        energy: Her energy when known.
        trust: Trust with this person when known.
        familiarity: How many times they have met when known.
        now: Local time (for tests).

    Returns:
        The decision, with a human-readable reason either way
        （允许时还带上语速/音调）。
    """
    values = settings()
    stamp_dt = now or datetime.now()
    quiet = is_night(stamp_dt)
    rate, pitch, why = prosody_for(
        text=text,
        night=quiet,
        mood=mood,
        energy=energy,
        trust=trust,
        familiarity=familiarity,
    )
    test = test_active(umo)
    # 设备通道（眼镜）：语音是常态，不吃群聊那几条限制；测试模式同理。
    device = str(platform) in (values.get("voice_first_platforms") or [])
    relaxed = device or test is not None
    if str(platform) not in values["platforms"] and not device:
        return Decision(False, f"这个平台不发语音（{platform}）")
    if not relaxed and not enabled_for(umo):
        return Decision(False, "这个会话没开自动语音（发「/语音 自动 开」）")
    if not is_private and not relaxed:
        return Decision(False, "群聊不发语音（要测就发「/语音 测试 开」）")
    length = len(text.strip())
    limit = int(values["test_max_chars"] if relaxed else values["max_chars"])
    if device:
        # 设备通道的上限跟"能念出来的总长"对齐（见 DEVICE_MAX_CHARS 的说明）
        limit = max(limit, DEVICE_MAX_CHARS)
    if length > limit:
        return Decision(False, f"太长了（{length} 字 > {limit}）")
    if not relaxed and quiet and not user_was_voice:
        return Decision(False, "夜里只在你发语音时才回语音")
    seconds = float(values["cooldown_seconds"])
    last = float((state().get("last") or {}).get(str(umo), 0.0))
    stamp = stamp_dt.timestamp()
    if not relaxed and last and stamp - last < seconds:
        return Decision(False, f"刚发过（{int(seconds - (stamp - last))} 秒冷却）")
    today = stamp_dt.strftime("%Y-%m-%d")
    mine, total = _counts(umo, today)
    if not relaxed and mine >= int(values["daily_per_session"]):
        return Decision(False, f"这个会话今天已经发了 {mine} 条（上限 {values['daily_per_session']}）")
    if total >= int(values["daily_total"]):
        return Decision(False, f"今天总共发了 {total} 条（全局上限 {values['daily_total']}）")
    value, why_score = score(text)
    if not relaxed and value < float(values["threshold"]):
        return Decision(False, f"内容不适合念（{value:.2f} < {values['threshold']}）：{why_score}")
    if test:
        reason = f"测试模式（剩 {test['left']} 条）"
    elif device:
        reason = f"眼镜通道（{value:.2f}）：{why_score}"
    else:
        reason = f"合适（{value:.2f}）：{why_score}"
    return Decision(
        True,
        reason,
        score=value,
        detail=why_score,
        speech_rate=rate,
        pitch=pitch,
        prosody_why=why,
    )


def record(umo: str, chars: int, billed: int = 0) -> None:
    """Count one spoken reply.

    Args:
        umo: Session origin.
        chars: Characters spoken.
        billed: Characters the service reported (0 when unknown).
    """
    values = state()
    today = _today()
    days = dict(values.get("days") or {})
    day = dict(days.get(today) or {})
    sessions = dict(day.get("sessions") or {})
    sessions[str(umo)] = int(sessions.get(str(umo), 0)) + 1
    day["sessions"] = sessions
    day["total"] = int(day.get("total", 0)) + 1
    day["chars"] = int(day.get("chars", 0)) + max(0, int(chars))
    day["billed"] = int(day.get("billed", 0)) + max(0, int(billed))
    days[today] = day
    values["days"] = days
    last = dict(values.get("last") or {})
    last[str(umo)] = time.time()
    values["last"] = last
    _write(values)


def stats_text(umo: str) -> str:
    """Human-readable counters for the command.

    Args:
        umo: Session origin.

    Returns:
        A few lines for chat.
    """
    values = settings()
    today = _today()
    day = (state().get("days") or {}).get(today) or {}
    mine, total = _counts(umo, today)
    state_line = "开" if enabled_for(umo) else "关"
    test = test_active(umo)
    if test:
        left_minutes = max(1, int((test["until"] - time.time()) / 60))
        test_line = f"⚠ 测试模式：开（约 {left_minutes} 分钟后过期，还剩 {test['left']} 条）"
    else:
        test_line = "测试模式：关（发「/语音 测试 开」＝群聊也出声，30 分钟/10 条后自动关）"
    return (
        f"· 这个会话的自动语音：{state_line}（发「/语音 自动 开」或「关」）\n"
        f"· {test_line}\n"
        f"· 今天这个会话：{mine} 条（上限 {values['daily_per_session']}）\n"
        f"· 今天总共：{total} 条（全局上限 {values['daily_total']}）\n"
        f"· 今天念了 {day.get('chars', 0)} 字（服务端计费 {day.get('billed', 0)} 字）\n"
        f"· 规则：只私聊、≤{values['max_chars']} 字、冷却 {int(values['cooldown_seconds']) // 60} 分钟、"
        f"夜里（{values['night_from']}:00–{values['night_to']}:00）只在你发语音时回语音"
    )
