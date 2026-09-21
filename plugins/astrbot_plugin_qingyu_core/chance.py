r"""机会点层：这一条消息值不值得接（L2）。

原来的时机是"随机闹钟"——``random_chime`` 掷出 5~15 分钟的间隔，到点了再由意愿分决定接不接，
所以她常在话题翻篇之后开口。这里换成**可学**的机会点：对每条群消息算一组特征，
用离线拟合出来的权重算出"一个像你这样的真人此刻会接话的概率"。

特征与权重都来自**历史**（``migration_tools/fit_chime_weights.py`` 用白名单对象的发言做标注：
"别人说完这句之后 60 秒内他有没有说话"）。实测信号（2026-09-19，1388 条别人的消息）：

- 总体接话率 14.1%；
- 上一条 @ 了某人 1.73x、像提问 1.42x、带半括号（玩笑）1.26x、上一条极短 0.67x；
- 被 @ 他自己是 5.45x，但那条走的是 respond 路径，不算插嘴机会。

分数是**加法**（log-odds），落到 ``p = sigmoid``；``decide`` 只把 ``log(p / 基础率)``
（对数提升，夹在 ±1）当作意愿分的修正项，门槛与安全阀全部保持原样，所以学歪了也不会变成话痨。
"""

import json
import math
import re
import time
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_DIR = Path(__file__).resolve().parent
# 权重文件跟着插件走（离线脚本生成、随插件一起部署）；
# 运行时也可以用 plugin_data 里的同名文件覆盖——换一版权重不用改代码、不用重载。
WEIGHTS_CANDIDATES = (
    Path(get_astrbot_plugin_data_path()) / "qingyu_chance_weights.json",
    PLUGIN_DIR / "chance_weights.json",
)
# 手写的默认权重：来自上面那次实测的 lift（ln 之后约等于 log-odds），
# 没有拟合文件时就用它，保证功能可用。
DEFAULT_BIAS = -1.81  # ln(0.141 / 0.859)
DEFAULT_WEIGHTS = {
    "prev_at": 0.55,  # 上一条 @ 了某人：有人被点名，接一句很自然
    "prev_question": 0.35,
    "prev_joke": 0.23,  # 半括号/哈哈：玩梗的回合
    "prev_image": 0.10,
    "prev_short": -0.20,  # 上一条极短（≤8 字）：多半是接话的尾声（拟合里这条不稳，先给轻一点）
    "flood": 0.05,
    "topic": 0.32,  # 话题落在学习对象的关键词上
    "bot_recent": -0.85,  # 她刚说过话
    "late_night": -0.80,
}
BASE_RATE = 0.141
LAUGH = re.compile(r"(哈哈|hh|HH|嘿嘿|233|笑死|草)")
# 权重缓存：每条群消息都要算机会点，按文件 mtime 缓存一份就够。
_cache: dict | None = None
_cache_stamp = 0
OPEN_PAREN = re.compile(r"[（(]")
QUESTION = re.compile(r"[?？]|吗|怎么|为什么|多少|哪儿|哪里|是不是|能不能")
SHORT_CHARS = 8


def load_weights() -> dict:
    """Read the fitted weights (cached until the file changes on disk).

    每条群消息都会问一次机会点，不能每次都摸磁盘；权重文件几乎不变，
    所以按 mtime 缓存，换了文件立刻生效（不用重载插件）。

    Returns:
        ``{"bias", "weights", "base_rate", "meta"}``.
    """
    global _cache, _cache_stamp
    for path in WEIGHTS_CANDIDATES:
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"qingyu_core: 机会点权重读不了（{type(exc).__name__}），用默认权重")
            break
        weights = data.get("weights")
        if isinstance(weights, dict) and weights:
            merged = dict(DEFAULT_WEIGHTS)
            for key, value in weights.items():
                if key in merged and isinstance(value, (int, float)):
                    merged[key] = float(value)
            _cache = {
                "bias": float(data.get("bias", DEFAULT_BIAS)),
                "weights": merged,
                "base_rate": float(data.get("base_rate", BASE_RATE)),
                "meta": str(data.get("meta") or path.name),
            }
            _cache_stamp = stamp
            return _cache
        break
    return {
        "bias": DEFAULT_BIAS,
        "weights": dict(DEFAULT_WEIGHTS),
        "base_rate": BASE_RATE,
        "meta": "内置默认权重",
    }


def features(
    *,
    text: str,
    has_image: bool = False,
    has_at: bool = False,
    flood: bool = False,
    seconds_since_bot: float = 99999.0,
    is_night: bool = False,
    keywords: list[str] | None = None,
) -> dict:
    """Compute the opportunity features of one incoming message.

    Args:
        text: Message text.
        has_image: Whether the message carries an image/sticker.
        has_at: Whether the message @-mentions someone (QQ 的 @ 是独立消息段，不在文本里，
            所以必须由调用方从消息链上取，光看文本会漏掉——离线拟合时就踩过这个坑）。
        flood: Whether the group posted several messages in the last minute.
        seconds_since_bot: Seconds since she last spoke in this session.
        is_night: Whether it is late night for her.
        keywords: Topic keywords learned from the style card.

    Returns:
        Feature name -> 1.0/0.0 (plus ``topic`` which may be 0/0.5/1).
    """
    hits = sum(1 for word in (keywords or []) if word and word in text)
    return {
        "prev_at": 1.0 if (has_at or "@" in text) else 0.0,
        "prev_question": 1.0 if QUESTION.search(text) else 0.0,
        "prev_joke": 1.0 if (LAUGH.search(text) or OPEN_PAREN.search(text)) else 0.0,
        "prev_image": 1.0 if has_image else 0.0,
        "prev_short": 1.0 if len(text.strip()) <= SHORT_CHARS else 0.0,
        "flood": 1.0 if flood else 0.0,
        "topic": 1.0 if hits >= 2 else (0.5 if hits == 1 else 0.0),
        "bot_recent": 1.0 if seconds_since_bot < 300 else 0.0,
        "late_night": 1.0 if is_night else 0.0,
    }


def score(values: dict) -> dict:
    """Turn features into a probability and a human-readable reason.

    Args:
        values: Feature dict from :func:`features`.

    Returns:
        ``{"p", "lift", "top"}`` — probability, lift over the base rate, and the
        strongest contributing feature.
    """
    loaded = load_weights()
    weights = loaded["weights"]
    total = float(loaded["bias"])
    contributions: list[tuple[float, str]] = []
    labels = {
        "prev_at": "上一条 @ 了人",
        "prev_question": "像在提问",
        "prev_joke": "像在玩梗",
        "prev_image": "发了图",
        "prev_short": "上一条很短",
        "flood": "群里在刷屏",
        "topic": "话题与你有关",
        "bot_recent": "她刚说过话",
        "late_night": "深夜",
    }
    for key, value in values.items():
        weight = float(weights.get(key, 0.0))
        if not value or not weight:
            continue
        part = weight * float(value)
        total += part
        contributions.append((part, labels.get(key, key)))
    probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, total))))
    base = max(1e-6, float(loaded["base_rate"]))
    lift = probability / base
    contributions.sort(key=lambda item: -abs(item[0]))
    top = "、".join(f"{name}{'+' if part > 0 else ''}{part:.2f}" for part, name in contributions[:3])
    return {"p": round(probability, 4), "lift": round(lift, 2), "top": top or "无信号"}


def bonus(probability: float, base_rate: float = BASE_RATE) -> float:
    """Convert a probability into a nudge for the willingness score.

    Args:
        probability: Opportunity probability from :func:`score`.
        base_rate: The historical base rate.

    Returns:
        A value in -1~1 (``BONUS_WEIGHT`` scales it inside ``decide``).
    """
    if probability <= 0:
        return -1.0
    log_lift = math.log(max(1e-6, probability) / max(1e-6, base_rate))
    return max(-1.0, min(1.0, log_lift))


def selftest() -> str:
    """Describe the loaded weights and a couple of example scores.

    Returns:
        A short report.
    """
    loaded = load_weights()
    lines = [
        f"权重来源：{loaded['meta']}｜基础率 {loaded['base_rate']:.3f}｜bias {loaded['bias']:.2f}",
        "权重：" + "、".join(f"{key}={value:+.2f}" for key, value in loaded["weights"].items()),
    ]
    cases = {
        "有人 @ 了别人 + 提问": {"text": "@小明 这个你怎么看？", "keywords": []},
        "他关心的话题": {"text": "军训完回来了，选课也搞定了", "keywords": ["军训", "选课"]},
        "刷屏的短句": {"text": "哈哈哈哈", "flood": True, "keywords": []},
        "深夜正经问": {"text": "检查单上这一项怎么写", "is_night": True, "keywords": []},
    }
    for name, payload in cases.items():
        payload.setdefault("text", "")
        values = features(
            text=payload["text"],
            has_image=bool(payload.get("has_image")),
            flood=bool(payload.get("flood")),
            seconds_since_bot=float(payload.get("seconds_since_bot", 99999.0)),
            is_night=bool(payload.get("is_night")),
            keywords=payload.get("keywords") or [],
        )
        result = score(values)
        lines.append(f"· {name}：p={result['p']:.3f}（{result['lift']}x）{result['top']}")
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)
