"""表达层：让她说话不那么"齐"——慢一点、偶尔偷个懒。

两件事，都很小但很显眼：

1. **延迟**：回复前停顿 0.4~2.5 秒（按心情和消息类型调）。秒回本身就是机器味；
2. **偷懒**：闲聊时有约一成的概率让她只用一句话回完。注意是**让她少写**，不是
   事后把长回答剪掉——剪出来的半句话比长回答更假。

专业问题（检查单、知识库、时效信息）走 `plan.max_chars = 600` 那一档，两个规则都不碰。
"""

import asyncio
import random
import re

from astrbot.core import logger

from . import style
from .decide import Plan

# 闲聊消息偷懒的概率。
LAZY_PROBABILITY = 0.10
# 闲聊消息"敷衍两句"的概率（比偷懒更极端：就回一个很短的词）。
MINIMAL_PROBABILITY = 0.08
# 什么算闲聊：决策层给的软性字数上限在 200 以内的，就是随口聊聊。
CASUAL_MAX_CHARS = 200
CASUAL_TEXT_CHARS = 20
LAZY_HINTS = (
    "（这次懒一点：一句话回完，别超过 15 个字，别加表情符号）",
    "（这次就回一句短的，像随口接话那样，不用展开）",
    "（这次简短：一句话，别提问，别客套）",
)
# 主动插嘴时用这一条：插话本该是"顺口接一句"，不是发一篇小作文。
CHIME_HINTS = (
    "（这次是你主动接话：像群里随口插一句，1~2 句、总共不超过 40 个字，"
    "别提问、别客套、别说自己在查什么）",
    "（这次是你自己插的话：短一点、口语一点，三四十字以内，不要长篇大论）",
)
# 拒绝时用这一条：要有边界，但别刻薄、别长篇解释。
REFUSE_HINTS = (
    "（这次不答应他：用一两句话拒绝，语气自然、可以有点小脾气但别刻薄；"
    "能给个替代办法就给一句。别道歉超过一句，别解释自己是 AI，别讲道理）",
    "（这次直接拒绝：短，一句到两句，可以吐槽一下，但不要说教）",
)
# 偶尔敷衍：人就有一搭没一搭的时候，回个"嗯"比长篇大论更像人。
MINIMAL_HINTS = (
    "（这次就回两三个字：像「嗯」「懂了」「好耶」这种，别的什么都别加）",
    "（这次敷衍一下：给个很短的反应就行，不用解释、不用接梗）",
)
# 延迟范围（毫秒）。
MIN_DELAY_MS = 400
MAX_DELAY_MS = 2500
# 桌面语音轮的开口延迟上限：那个 0.4~2.5 秒的停顿是为**群聊**设计的（"秒回就是机器味"），
# 但桌面上是面对面说话，语音一轮本来还要等识别和模型，再故意拖 1.4 秒就纯粹让人干等
# （2026-09-25 用户反馈"还是不像真实对话"）。只对桌面通道生效，QQ 那边手感不变。
DESKTOP_FAST_MAX_MS = 400
# 被 @ 时的长度预算：闲聊按群的答话上限 × 好感度倍数；中等问题再翻倍；
# 超过这个预算（检查单/知识库那类正经问题）就**不压长度**，答准更重要。
SERIOUS_MIN_CAP = 40
PROFESSIONAL_MAX_CHARS = 300
# 插嘴写超了怎么办：超过上限这么多倍就**重写一次**（不是事后截断——截出来的半句话更假）。
CHIME_OVER_CAP_RATIO = 1.6
CHIME_REWRITE_PROMPT = (
    "你刚才在群里随口接了一句，但写长了，不像这个群的人。\n"
    "群里那条消息：{context}\n"
    "你刚才写的：{text}\n"
    "请把这句话改短并重说一遍：**{cap} 个字以内**，只给一个反应或一句吐槽，"
    "不要提问、不要解释自己在做什么、不要点评或总结。"
    "只输出改好的那一句，不要引号、不要解释。"
)


def chime_length_cap(group_id: str) -> int:
    """How long an interjection may be in this group.

    Args:
        group_id: Platform group id.

    Returns:
        The per-group cap in characters.
    """
    return style.length_cap(group_id)


def reply_length_cap(group_id: str) -> int:
    """How long a casual answer may be in this group (被 @ 的那些轮次).

    Args:
        group_id: Platform group id.

    Returns:
        The per-group answer cap in characters.
    """
    return style.answer_length_cap(group_id)


def chime_too_long(text: str, cap: int) -> bool:
    """Whether an interjection overshot the per-group cap badly.

    Args:
        text: The reply text.
        cap: The cap in characters.

    Returns:
        True when a rewrite is worth one extra model call.
    """
    limit = max(cap + 4, int(cap * CHIME_OVER_CAP_RATIO))
    return len(text.strip()) > limit


def chime_rewrite_prompt(text: str, context: str, cap: int) -> str:
    """Build the self-contained prompt used to shorten an over-long interjection.

    Args:
        text: What she just wrote.
        context: The group message she was reacting to.
        cap: The per-group cap in characters.

    Returns:
        The user prompt for the rewrite call.
    """
    return CHIME_REWRITE_PROMPT.format(
        context=(context or "（没记下来）")[:200],
        text=text[:300],
        cap=cap,
    )


def is_casual(plan: Plan | None, text: str) -> bool:
    """Whether this turn reads like small talk.

    Args:
        plan: The decision for this turn.
        text: Incoming message text.

    Returns:
        True when the reply may be short and casual.
    """
    if plan is None or plan.action != "respond":
        return False
    if plan.max_chars > CASUAL_MAX_CHARS:
        return False
    return len(text.strip()) <= CASUAL_TEXT_CHARS


def lazy_hint(plan: Plan | None, text: str) -> str | None:
    """Sometimes ask the model to answer with one short line.

    Args:
        plan: The decision for this turn.
        text: Incoming message text.

    Returns:
        A hint to append to the system prompt, or None.
    """
    if not is_casual(plan, text):
        return None
    if random.random() >= LAZY_PROBABILITY:
        return None
    return random.choice(LAZY_HINTS)


def chime_hint(plan: Plan | None, group_id: str = "", context: str = "") -> str | None:
    """Hint that keeps an interjection short and casual.

    优先用风格层按**这个群**统计出来的「怎么说」（``style.hint``：字数上限按群算、
    禁用提问/波浪号/meta 解说、并把该群真人的原话当例子）；没有卡片时退回那两条固定提示。

    Args:
        plan: The decision for this turn.
        group_id: Platform group id (for the per-group style card).
        context: Recent message text, used to pick examples.

    Returns:
        The hint, or None when this turn is not an interjection.
    """
    if plan is None or plan.action != "chime":
        return None
    if group_id:
        shaped = style.hint(str(group_id), context)
        if shaped:
            return shaped
    return random.choice(CHIME_HINTS)


def minimal_hint(plan: Plan | None, text: str) -> str | None:
    """Occasionally answer with just a couple of characters (阶段四).

    闲聊里有约 8% 的概率"敷衍一下"——人本来就会有一搭没一搭的时候，
    每次都认真接梗反而不像人。

    Args:
        plan: The decision for this turn.
        text: Incoming message text.

    Returns:
        The hint, or None when this turn deserves a normal answer.
    """
    if not is_casual(plan, text):
        return None
    if random.random() >= MINIMAL_PROBABILITY:
        return None
    return random.choice(MINIMAL_HINTS)


def refuse_hint(plan: Plan | None) -> str | None:
    """Hint that makes a refusal short and in character.

    Args:
        plan: The decision for this turn.

    Returns:
        The hint, or None when this turn is not a refusal.
    """
    if plan is None or plan.action != "refuse":
        return None
    return random.choice(REFUSE_HINTS)


def reply_budget(plan: Plan | None, group_id: str, affection: int | None = None) -> int | None:
    """How long an answer may be on this「被 @」turn.

    群味给**基准长度**，好感度给**倍数**（关系越近越愿意多说两句）：

    - 闲聊/短消息（``max_chars ≤ 200``）：群的答话上限 × 好感度倍数；
    - 中等（``≤ 300``，一般像提问的话）：两倍答话上限，至少 40 字；
    - 正经问题（``> 300``，检查单/知识库那类）：**不压**，返回 None（答准比答短重要）。

    Args:
        plan: The decision for this turn.
        group_id: Platform group id.
        affection: 说话人的好感度（None 按 60 处理）。

    Returns:
        The cap in characters, or None when the length must not be limited.
    """
    if plan is None or not group_id:
        return None
    budget = int(getattr(plan, "max_chars", 0) or 0)
    if budget > PROFESSIONAL_MAX_CHARS:
        return None
    base = style.answer_length_cap(str(group_id))
    _label, multiplier, _flavor = style.flavor_for(affection)
    if budget <= CASUAL_MAX_CHARS:
        return max(8, int(round(base * multiplier)))
    return max(int(round(base * multiplier * 2)), SERIOUS_MIN_CAP)


def reply_hint(
    plan: Plan | None,
    group_id: str = "",
    text: str = "",
    context: str = "",
    is_command: bool = False,
    affection: int | None = None,
) -> str | None:
    """Hint for the「被 @ / 被点名」那一轮：群味管长度，好感度管语气。

    分工（2026-09-19 两次返工后的结论）：

    - **长度**按这个群"回答别人的中位长度"给上限——治的是"写小作文"；
    - **语气与口癖按好感度**（`style.flavor_for`）——关系近就保留「嗯~」、句尾「~」、打趣与多聊两句，
      关系远就客气简短。第一版把口癖也一起禁了，结果人设被淹没、好感度看不出作用，所以返工；
    - 反助手腔（别「首先/其次/总之」、别解说自己在做什么）对每一档都生效，这条跟人设无关。

    Args:
        plan: The decision for this turn.
        group_id: Platform group id (取这个群的风格卡片)。
        text: The incoming message.
        context: Recent messages, used to pick answer examples.
        is_command: 指令回复是结构化输出，不加这类风格提示。
        affection: 说话人的好感度。

    Returns:
        The hint, or None when it should not apply.
    """
    if plan is None or plan.action != "respond" or is_command or not group_id:
        return None
    return style.reply_hint(
        str(group_id),
        text=text,
        budget=reply_budget(plan, group_id, affection),
        affection=affection,
        context=context,
    )


def length_hint(plan: Plan | None) -> str | None:
    """Tell the model how long this reply should be (阶段四).

    ``max_chars`` 之前只是算出来落库，从没作用到回复上——这里把它变成一句提示，
    让模型自己写短，而不是事后截断（截出来的半句话比长回复更假）。

    Args:
        plan: The decision for this turn.

    Returns:
        The hint, or None when the budget is generous.
    """
    if plan is None:
        return None
    if plan.max_chars <= 60:
        return "（这次很简短：一句话，15 个字以内，别加表情和波浪号）"
    if plan.max_chars <= 120:
        return "（这次短一点：一两句、40 字以内就够）"
    if plan.max_chars <= 200:
        return "（这次别太长：控制在 100 字以内，说重点）"
    return None


def delay_seconds(plan: Plan | None, text: str, *, fast: bool = False) -> float:
    """How long to wait before the reply is sent.

    Args:
        plan: The decision for this turn.
        text: Incoming message text.
        fast: 桌面语音轮传 ``True``：停顿压到 ``DESKTOP_FAST_MAX_MS`` 以内
            （见该常量的注释；QQ 群聊仍走原来的 0.4~2.5 秒）。

    Returns:
        Delay in seconds, inside the configured range (``fast`` 时另受上限约束).
    """
    base_ms = plan.delay_ms if plan else 800
    if is_casual(plan, text):
        # 随口接话更随意：有时快有时慢。
        base_ms += random.randint(-300, 400)
    jitter = random.randint(-150, 450)
    millis = max(MIN_DELAY_MS, min(MAX_DELAY_MS, base_ms + jitter))
    if fast:
        millis = min(millis, DESKTOP_FAST_MAX_MS)
    return millis / 1000.0


async def wait_before_reply(plan: Plan | None, text: str, *, fast: bool = False) -> float:
    """Sleep for the computed delay so the reply does not look instant.

    Args:
        plan: The decision for this turn.
        text: Incoming message text.
        fast: 桌面语音轮（见 :func:`delay_seconds`）。

    Returns:
        The delay that was applied, in seconds.
    """
    seconds = delay_seconds(plan, text, fast=fast)
    if seconds <= 0:
        return 0.0
    await asyncio.sleep(seconds)
    logger.info(f"qingyu_core: 回复前停顿 {seconds:.2f} 秒{'（桌面快速档）' if fast else ''}")
    return seconds
