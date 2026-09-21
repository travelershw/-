"""决策层：该不该说话、以什么身份说、花多少预算。

这是架构里新增的核心。原来"要不要说话"由 random_chime 的计时器独家决定，
现在拆成两件事：计时器只管**时机**，这里的意愿分管**想不想**。

影子模式（``ENFORCE = False``）下只计算并落库，不改变任何行为——先跑一天看
action 分布是否合理，再打开。
"""

import random
from dataclasses import dataclass, field

from . import chance as chance_mod
from .world import Snapshot

# 意愿分权重（见架构文档 §三 决策层）。
# 在场基线：只要群里有人正常说话、不是刷屏也不是深夜，就先给一点分。
# 没有这个基线，新群里（好感 60、精力不到 70）永远只有"好久没开口"的 1.0，
# 评估结果会清一色是"不插嘴"，观测期看不出梯度。
W_BASE = 0.4
W_DIRECT = 3.0
# "像在问问题"只给很小的分（2026-09-16 从 2.0 降到 0.5）：群里别人的提问不该让
# 她跃跃欲试地去抢答，被点名才是强信号。
W_QUESTION = 0.5
W_AFFECTION = 1.0
W_ENERGY = 1.0
# "久没开口"按时间分档（2026-09-17 改）：原来 >15 分钟直接给满 1.0，把所有人的分数
# 都顶到"0.4+1.0=1.4"的天花板，观测期的评估没有梯度。现在 15 分钟 0.2、半小时 0.4、
# 一小时以上 0.6——光靠"憋久了"越不过门槛，必须叠加好感或精力。
W_SILENCE_TIERS = ((3600, 0.6), (1800, 0.4), (900, 0.2))
W_JUST_SPOKE = -2.0
W_FLOOD = -1.5
W_NIGHT = -1.0
W_IGNORED = -2.0
# 好感加分的基准点：60 分在她的档位表里已经是"轻快亲近"，不该当中性值。
AFFECTION_NEUTRAL = 50
# 阈值。
RESPOND_AT = 2.0
# 评估"想不想插嘴"的门槛。校准史：0.6 → 1.5 → 2.0 + 分档静默 → **1.2（2026-09-17 测试值）**。
# 2.0 太保守（实测一晚上 0 次想插嘴，没法测）；1.5 时早上实测 1.25，还是差 0.25 够不到。
# 1.2 的效果：有人说话、她憋了一小时以上（+0.6）、好感 60（+0.25）、在场（+0.4）= 1.25
# 就会想接一句；但"刚说过话"（−2.0）、刷屏（−1.5）、深夜（−1.0）都会压回去，
# 加上闹钟的 5~15 分钟间隔与每日 6 次上限，实际频率仍然很低。
# 测试结束想让她更安静：改回 1.5（偶尔）或 2.0（几乎不主动）。
# 实测（2026-09-17 20:50，近 24 小时 461 条没被叫到的消息）"想插嘴"占比：
# 0.6→58%、0.8→44%、0.9→37%、1.0→34%、1.1→22%、1.2→14%、1.4→9%、1.6→3%。
# 运行时想临时改：群里发 ``/插嘴阈值 1.0``（写在 qingyu_tuning.json，不用改这里的常量）。
CHIME_AT = 1.2
# 机会点（L2，2026-09-19 加）：由 ``chance.py`` 按"像你这样的真人此刻会不会接话"算出概率，
# 这里只取它的**对数提升**（-1~1，见 chance.bonus）乘上这个权重，作为意愿分的修正项。
# 为什么要乘一个小系数而不是换掉整套规则：那套规则是踩过坑调出来的（刷屏/深夜/刚说过话
# 都压得住），机会点只负责"这一条值不值得接"，不该有能力单独把谁顶到门槛以上。
W_OPPORTUNITY = 0.6
# 机会点概率超过这个值就算"时机到了"，不再等随机闹钟（闹钟本身仍然管着最小间隔）。
CHANCE_DUE_AT = 0.35
# **先不开**这个替代路径：2026-09-19 的离线拟合（`fit_chime_weights.py`，1332 条样本）显示
# 时机信号的泛化能力很弱——拟合权重在按时间切的验证段 AUC 只有 0.42（还不如随机），
# 手写权重 0.56。所以机会点现在只做"小修正 + 有没有钩子的加分"，
# 不让它单独决定"到点了"。等攒够两周数据重新拟合、AUC 站得住再打开。
CHANCE_DRIVES_DUE = False
# 什么叫"有钩子"：这一条消息本身给了可接的东西（有人被 @、像提问、像玩梗、带图、话题相关）。
HOOK_FEATURES = ("prev_at", "prev_question", "prev_joke", "prev_image", "topic")
# 有钩子的**加分**。
# 2026-09-19 晚的重要修正：钩子**原来是一票否决**，结果"好久不插嘴"——
# 12 小时里 429 条被它拦下、只插了 9 次（她"想插嘴"的轮次有 367 条）。
# 群里大多数消息就是一句普通的话，没有 @、不是提问、不带梗，按一票否决她几乎永远开不了口。
# 现在改成：有钩子加分、没钩子不加分，节奏仍由闹钟 + 意愿门槛 + 180 秒间隔 + 每日额度管。
W_HOOK = 0.25
# 是否允许插嘴由"闹钟"（random_chime 的计时器）说了算：它到点了就在事件上挂
# qingyu.chime_due，我们再按意愿分决定接不接。没有闹钟信号就不插——不然变成话痨。
CHIME_MIN_GAP_SECONDS = 180.0
# 每天最多插嘴几次（**每个群**各算各的）。
# 2026-09-19 晚从 6 提到 12：10002 那天 16:22 就用完了 6 次，
# 之后到晚上一次都没插嘴——用户反馈"好久没有插嘴了"就是这个额度卡住的。
# 运行时用 ``/插嘴阈值 上限 12`` 可以临时改。
CHIME_DAILY_CAP = 12
QUESTION_MARKERS = (
    "?", "？", "吗", "怎么", "为什么", "多少", "哪儿", "哪里", "是不是", "能不能",
    "什么", "如何", "哪一", "解释", "介绍一下", "讲讲",
)
NIGHT_EXTRA = "深夜"
# ---- 阶段三：边界与拒绝 ----
# 只对"被点名"的轮次生效（没人叫她的时候，不想理就是 observe，不需要"拒绝"）。
# 触发词 -> (基础概率, 触发名, 概率上限)
REFUSE_ABUSE = ("滚", "傻", "蠢", "笨", "闭嘴", "垃圾", "废物", "弱智", "神经病", "去死")
REFUSE_TASK = (
    "帮我写", "帮我做", "代写", "写一篇", "写个论文", "做一下作业", "帮我答",
    "帮我把作业", "写代码作业", "帮我交",
)
# 这些主题永远不拒绝：正经问题必须答，而且要查证。
PROTECTED_WORDS = (
    "检查单", "复飞", "进近", "起飞", "着陆", "A320", "A32N", "A380", "ILS",
    "学生票", "12306", "报名", "考试", "成绩", "选课", "绩点", "四级", "六级", "考研",
    "知识库", "好感度", "记忆", "插件", "表情包",
)
REFUSE_TRIGGER_CHANCE = {
    "被骂": (0.60, 0.60),
    "让她代做": (0.50, 0.50),
    "反复追问": (0.35, 0.35),
    "深夜+心情差": (0.25, 0.25),
}
# 好感高会更愿意搭理（每高 10 点减 3%），心情差更容易拒（每低 10 点加 2%）。
REFUSE_AFFECTION_RELIEF = 0.03
REFUSE_MOOD_PENALTY = 0.02


@dataclass
class Plan:
    """一轮对话的决策结果。

    ``action`` 是这一轮**实际**会做的事；``planned_action`` 是"如果允许她主动
    插嘴，她会怎么做"——现阶段只评估不插话，靠的就是后面这个字段。
    """

    action: str
    reason: str
    speak_score: float
    planned_action: str = ""
    max_chars: int = 400
    delay_ms: int = 800
    tool_budget: int = 2
    tone: str = ""
    recall_needed: bool = True
    details: dict = field(default_factory=dict)

    def describe(self) -> str:
        """Return a one-line description for logs.

        Returns:
            The description string.
        """
        planned = (
            f" 评估={self.planned_action}"
            if self.planned_action and self.planned_action != self.action
            else ""
        )
        return (
            f"{self.action}({round(self.speak_score, 2)}){planned} {self.reason} "
            f"[{self.tone or '默认'} {self.max_chars}字 {self.delay_ms}ms]"
        )


def silence_score(seconds_since_bot: float) -> float:
    """How much "I have not spoken in a while" is worth.

    Args:
        seconds_since_bot: Seconds since the bot last spoke.

    Returns:
        The bonus, from 0 (just spoke) up to 0.6 (over an hour).
    """
    for threshold, bonus in W_SILENCE_TIERS:
        if seconds_since_bot >= threshold:
            return bonus
    return 0.0


def refusal_chance(
    snapshot: Snapshot,
    *,
    text: str,
    is_command: bool,
    is_wake: bool,
    repeat_asker: bool,
) -> tuple[float, str]:
    """算出这一轮拒绝的概率与原因（阶段三）。

    只在被点名、且不属于"永不该拒"的主题时才可能拒绝。

    Args:
        snapshot: World state snapshot.
        text: Message text.
        is_command: Whether this is a plugin command.
        is_wake: Whether the bot was addressed.
        repeat_asker: Whether this person has been pestering her just now.

    Returns:
        ``(概率, 原因)``；不拒绝时概率为 0。
    """
    if is_command or not is_wake or not text:
        return 0.0, ""
    if any(word in text for word in PROTECTED_WORDS):
        return 0.0, ""
    if any(word in text for word in REFUSE_ABUSE):
        trigger = "被骂"
    elif any(word in text for word in REFUSE_TASK):
        trigger = "让她代做"
    elif repeat_asker:
        trigger = "反复追问"
    elif snapshot.clock.is_night and snapshot.mood.mood <= 45:
        trigger = "深夜+心情差"
    else:
        return 0.0, ""

    base, ceiling = REFUSE_TRIGGER_CHANCE[trigger]
    chance = base
    chance -= max(0, snapshot.person.affection - 60) / 10 * REFUSE_AFFECTION_RELIEF
    chance += max(0, 65 - snapshot.mood.mood) / 10 * REFUSE_MOOD_PENALTY
    return max(0.0, min(ceiling, chance)), trigger


def is_question(text: str) -> bool:
    """Whether a message reads like a question directed at the bot.

    Args:
        text: Message text.

    Returns:
        True when it looks like a question.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    return any(marker in stripped for marker in QUESTION_MARKERS)


def score(
    snapshot: Snapshot,
    *,
    text: str,
    is_wake: bool,
    is_command: bool,
) -> tuple[float, list[str]]:
    """Compute how willing the bot is to speak this turn.

    Args:
        snapshot: World state snapshot.
        text: Incoming message text.
        is_wake: Whether the bot was addressed (at/wake prefix/private chat).
        is_command: Whether the message is a plugin command.

    Returns:
        The score and the reasons that moved it.
    """
    total = 0.0
    reasons: list[str] = []
    if not snapshot.flood and not snapshot.clock.is_night:
        total += W_BASE
        reasons.append("在场")
    if is_wake:
        total += W_DIRECT
        reasons.append("被点名")
    if is_question(text):
        total += W_QUESTION
        reasons.append("像在问问题")
    # 只有真的加到分才写进理由，否则"好感度 60"这种中性值会让人误以为加了分。
    affection_bonus = W_AFFECTION * min(
        1.0,
        max(0.0, (snapshot.person.affection - AFFECTION_NEUTRAL) / 40.0),
    )
    if affection_bonus > 0.05:
        total += affection_bonus
        reasons.append(f"好感度高 +{affection_bonus:.2f}")
    energy_bonus = W_ENERGY * min(1.0, max(0.0, (snapshot.mood.energy - 70) / 30.0))
    if energy_bonus > 0.05:
        total += energy_bonus
        reasons.append(f"还有精神 +{energy_bonus:.2f}")
    silence_bonus = silence_score(snapshot.seconds_since_bot)
    if silence_bonus > 0:
        total += silence_bonus
        reasons.append(f"好久没开口 +{silence_bonus:.1f}")
    if snapshot.seconds_since_bot < 60:
        total += W_JUST_SPOKE
        reasons.append("刚说过话")
    if snapshot.flood:
        total += W_FLOOD
        reasons.append("群内在刷屏")
    if snapshot.clock.is_night:
        total += W_NIGHT
        reasons.append("深夜")
    if is_command:
        # 指令永远要回应，不受意愿分影响。
        total = max(total, RESPOND_AT + 1)
        reasons.append("指令")
    return total, reasons


def decide(
    snapshot: Snapshot,
    *,
    text: str,
    is_wake: bool,
    is_command: bool,
    chime_allowed: bool,
    chime_due: bool = False,
    repeat_asker: bool = False,
    enforcing: bool = False,
    chime_at: float | None = None,
    daily_cap: int | None = None,
    opportunity: dict | None = None,
    chance_due_at: float | None = None,
) -> Plan:
    """Turn the willingness score into a concrete plan.

    三种可能的结果：

    - ``respond``：正常回应；
    - ``refuse``：被点名了但这一轮不想答（阶段三，概率化，专业问题永不拒）；
    - ``chime`` / ``observe``：没被点名时该主动接还是安静（见 ``planned_action``）。

    Args:
        snapshot: World state snapshot.
        text: Incoming message text.
        is_wake: Whether the bot was addressed.
        is_command: Whether the message is a command.
        chime_allowed: Whether this session is on the chime whitelist.
        chime_due: Whether the chime alarm rang for this message.
        repeat_asker: Whether this person has been pestering her just now.
        enforcing: Whether decisions are actually applied.
        chime_at: Runtime chime threshold (``None`` uses :data:`CHIME_AT`).
        daily_cap: Runtime daily chime cap (``None`` uses :data:`CHIME_DAILY_CAP`).
        opportunity: Result of ``chance.score`` for this message (None = 不参与).
        chance_due_at: Probability above which the opportunity itself counts as "到点".

    Returns:
        The plan for this turn.
    """
    gate = CHIME_AT if chime_at is None else float(chime_at)
    cap = CHIME_DAILY_CAP if daily_cap is None else int(daily_cap)
    due_at = CHANCE_DUE_AT if chance_due_at is None else float(chance_due_at)
    total, reasons = score(snapshot, text=text, is_wake=is_wake, is_command=is_command)
    chance_note = ""
    hook = False
    if opportunity:
        _probability = float(opportunity.get("p") or 0.0)
        nudge = W_OPPORTUNITY * chance_mod.bonus(
            _probability,
            float(opportunity.get("base_rate") or chance_mod.BASE_RATE),
        )
        total += nudge
        features = opportunity.get("features") or {}
        hook = any(float(features.get(name) or 0.0) > 0 for name in HOOK_FEATURES)
        if hook:
            total += W_HOOK
        chance_note = (
            f"机会点 {_probability:.2f}（{opportunity.get('lift')}x：{opportunity.get('top')}）"
            f"{'，有钩子 +%.2f' % W_HOOK if hook else '，没有钩子（不加分）'}"
        )
        reasons.append(chance_note)
        if CHANCE_DRIVES_DUE and _probability >= due_at:
            # 机会点够高：这一条本身就是"到点"，不必等闹钟掷出那个随机间隔。
            chime_due = True
            chance_carries_due = True
        else:
            chance_carries_due = False
    else:
        chance_carries_due = False
    tone = _tone(snapshot)
    max_chars = _max_chars(snapshot, text)

    if is_command or is_wake:
        planned = "respond"
    elif total >= gate:
        planned = "chime"
    else:
        planned = "observe"

    action = "respond" if (is_command or is_wake) else "observe"
    refusal_trigger = ""
    if is_wake and not is_command:
        chance, trigger = refusal_chance(
            snapshot,
            text=text,
            is_command=is_command,
            is_wake=is_wake,
            repeat_asker=repeat_asker,
        )
        if chance > 0 and random.random() < chance:
            action = "refuse"
            refusal_trigger = trigger
            planned = "refuse"
            reasons.append(f"不想答（{trigger}，{chance:.0%}）")
            max_chars = min(max_chars, 120)

    if action != "refuse" and planned == "chime":
        if not chime_allowed:
            reasons.append("评估想插嘴，本群未放行插嘴")
        elif not chime_due:
            reasons.append("评估想插嘴，但时机没到（闹钟没到点）")
        elif snapshot.seconds_since_bot < CHIME_MIN_GAP_SECONDS:
            reasons.append("评估想插嘴，但离上次开口太近")
        elif snapshot.chimes_today >= cap:
            reasons.append(f"评估想插嘴，但今天额度用完了（{snapshot.chimes_today}/{cap}）")
        elif not enforcing:
            reasons.append("评估想插嘴（只评估，不插话）")
        else:
            action = "chime"
            reasons.append(
                "机会点够高 + 意愿够，接一句" if chance_carries_due else "闹钟到点 + 意愿够，插一句",
            )
    elif not (is_command or is_wake):
        # 评估为"不插嘴"时把差距写出来，观测期一眼能看出离门槛还差多少。
        gap = gate - total
        reasons.append(
            f"评估不插嘴（差 {gap:.2f} 分）" if gap <= 1.5 else "评估不插嘴（意愿太低）",
        )
    return Plan(
        action=action,
        planned_action=planned,
        reason="、".join(reasons) or "无信号",
        speak_score=total,
        max_chars=max_chars,
        delay_ms=_delay(snapshot),
        tool_budget=2 if action == "respond" else 1,
        tone=tone,
        details={
            "shadow": not enforcing,
            "refusal": refusal_trigger,
            "chime_at": gate,
            "daily_cap": cap,
            "opportunity": opportunity or {},
            "hook": hook,
            "chance_carries_due": chance_carries_due,
        },
    )


def _tone(snapshot: Snapshot) -> str:
    """Pick a tone label from affection, mood and time.

    Args:
        snapshot: World state snapshot.

    Returns:
        A short Chinese tone label.
    """
    affection = snapshot.person.affection
    if affection >= 80:
        base = "很亲昵"
    elif affection >= 60:
        base = "轻快亲近"
    elif affection >= 40:
        base = "普通友好"
    elif affection >= 20:
        base = "客气疏远"
    else:
        base = "明显冷淡"
    if snapshot.mood.mood <= 35:
        base += "、有点蔫"
    elif snapshot.mood.mood >= 80:
        base += "、情绪很好"
    if snapshot.clock.is_night:
        base += "、深夜话短"
    return base


def _max_chars(snapshot: Snapshot, text: str) -> int:
    """Decide how long the reply may be.

    Args:
        snapshot: World state snapshot.
        text: Incoming message text.

    Returns:
        A soft character budget for the reply.
    """
    # 正经问题（含专业主题词、或本身就是长问句）不限制长度：检查单、参数、流程都要给全。
    # 顺序很重要：先判专业/长文本，再让深夜和坏心情把闲聊压短。
    if any(word in text for word in PROTECTED_WORDS) or len(text) > 30:
        return 600
    if snapshot.clock.is_night:
        return 60
    if snapshot.mood.mood <= 35:
        return 80
    if is_question(text):
        return 300
    return 200


def _delay(snapshot: Snapshot) -> int:
    """Decide how long to wait before answering.

    Args:
        snapshot: World state snapshot.

    Returns:
        Delay in milliseconds; humans do not answer instantly.
    """
    base = 800
    if snapshot.mood.energy < 50:
        base += 700
    if snapshot.clock.is_night:
        base += 500
    return base
