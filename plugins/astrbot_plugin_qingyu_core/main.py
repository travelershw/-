r"""轻语 Agent 架构核心：感知链 + 世界状态 + 决策链 + 情节记忆。

设计见 ``PROJECT_ROOT\轻语Agent架构设计.md``。这个插件是「她自己」那一层：
所有模块只通过 ``qingyu.db`` 与 ``event`` 的 extra 通信，能力模块不再各自决定
要不要说话。

Decision enforcement is controlled by ``ENFORCE``:

- ``ENFORCE = False``: shadow mode records decisions without changing behavior.
- ``ENFORCE = True`` (current default): decisions and proactive replies are active.

阶段一的情景记忆已经接入：``remember`` 工具、概率注入、``/忘记我``、``/记忆抽取``。
"""

import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import (
    chance,
    decide,
    express,
    holiday,
    memory,
    nightly,
    petlog,
    report,
    store,
    style,
    tuning,
    world,
)

ENFORCE = True
# 兜底的人设名：读不到人设表时用它认"正文里是不是在叫她"（人设名存在 personas 表里）。
SELF_FALLBACK_NAME = "轻语"
# 名字后面跟着这些虚词时，多半是"别人在议论她"而不是在跟她说话（例：「轻语又没法进行开发」）。
GOSSIP_AFTER = ("又", "还", "也", "已经", "都", "就", "好像", "真的", "不是", "原来")
# 「她刚开口，群友接着跟她聊」的接力窗口：时间范围与"像在接话"的字数门槛。
# 为什么要有这个：她插嘴之后，群友想跟她聊两句，不该还得先 @ 她一下；
# 但也不能把群里随后的任何一条都算成找她——所以要求"紧接着她那一轮、中间没人插话"。
CONTINUATION_SECONDS = 90
CONTINUATION_MAX_CHARS = 25
# 免@ 窗口的**绝对截止时间**（存进 meta，单位是秒级时间戳）。
# 只有她"自己拿到话头"的那一轮才刷新它：被接话规则唤醒后的回复不再刷新。
# 否则每回一次就把锚点往后推一次，群友只要 90 秒内接一句她就能一直不用 @——
# 2026-09-22 实测出现过连续 3.5 分钟都在免@ 状态的情况，比设定的 90 秒长得多。
CONTINUATION_UNTIL_KEY = "continuation_until:{group}"
# 超过这么久才算"积压消息"：电脑休眠或进程卡住之后解冻时，几小时前的群消息
# 会在几秒内一次性涌进来。这种旧消息不该把她唤醒，否则她会连着回复一堆过期内容
# （2026-09-23 22:51 实测：事件循环卡了 4.5 小时后解冻，上百条旧消息一秒内到达）。
STALE_MESSAGE_SECONDS = 120
# 同一句话在这段时间内又要发一次，就当重复、拦掉不发。
# 为什么会重复：模型在同一轮里既能给正文又能调工具，AstrBot 先把正文发出去、执行完工具
# 再问一次，模型把同样的话又输出了一遍（2026-09-24 实测 update_affection 那轮，间隔 3 秒
# 发了两条一模一样的）。
DUPLICATE_REPLY_SECONDS = 20
# 表达层开关：回复前的停顿、闲聊时偶尔只回一句、插嘴时的"说短点"提示。
# 只改"怎么说话"，不改"说不说话"，一直开着。
EXPRESS_ENFORCE = True
# 插嘴写超了要不要让她重写一句（多花一次模型调用，但比"事后截断"自然）。
CHIME_REWRITE = True
PLUGIN_VERSION = "0.19.0"
# 决策只对真实聊天生效，这些平台不参与。
SKIP_PLATFORMS = ("webchat",)
# 好感度还由旧插件写 JSON，这里定期镜像进 relations 表（阶段 C 会搬过来）。
AFFECTION_SYNC_SECONDS = 300
# 每天过了这个点、当天还没抽过记忆，就在下一条群消息到达时补跑一次。
# 不依赖 AstrBot 的定时任务：机器人没开机也不会漏掉，开机后自然补上。
LAZY_EXTRACT_AFTER_HOUR = 4
# 观测期日志节流：同一种会话每这么多秒至少留一条评估记录（能看出在跑又不刷屏）。
OBSERVE_LOG_INTERVAL_SECONDS = 300
# 指令回复的长度上限（超过就截断，免得一条消息刷太长）。
MAX_CHARS = 800
# 周报要摊开十来项指标，单独给一个更宽的上限。
REPORT_MAX_CHARS = 1600
CHIME_STATE = Path(get_astrbot_plugin_data_path()) / "random_chime_state.json"
USAGE = (
    "用法：\n"
    "· /记忆　看轻语记住了什么（管理员）\n"
    "· /忘记我　删掉关于你的全部记忆（任何人）\n"
    "· /记忆抽取　立刻手动跑一次夜间抽取（管理员）\n"
    "· /轻语状态　看她今天的状态与决策分布（管理员）\n"
    "· /轻语周报　看最近一周的评估数据（管理员）\n"
    "· /插嘴阈值　看/临时改插嘴门槛与机会点门槛（管理员）\n"
    "· /祝福　看今天是什么节、本群开没开、发过没；/祝福 列表 看节日表（管理员）\n"
    "· /风格　看学到的群味（每群的插嘴上限、答话上限）；/风格 重建 立刻重建\n"
    "· /风格 老师　看学习对象（谁在贡献样本）；加：/风格 老师 加 10001（QQ 或昵称都行）"
)


TEACHER_USAGE = (
    "用法（QQ 号或群里昵称都行）：\n"
    "· /风格 老师　看当前学习对象\n"
    "· /风格 老师 加 10001　加一个（也可以写 /风格 老师 加10001）\n"
    "· /风格 老师 删 10001　移除一个"
)


HOLIDAY_USAGE = (
    "用法：\n"
    "· /祝福　看今天是什么节、本群开没开、今天发过没\n"
    "· /祝福 开　/祝福 关　开关本群的节日祝福\n"
    "· /祝福 试 [节日名]　只在本群演练一次，便于调语气\n"
    "· /祝福 列表　看节日表（含窗口、级别、主动发的节日）\n"
    "· /祝福 设 2027-02-06 春节　补一条农历日期"
)


def parse_teacher_command(text: str) -> tuple[str, str]:
    """Parse「老师 加/删 谁」这种说法，尽量宽一点。

    2026-09-19 的 bug：原来只认 ``加<空格><QQ>``，于是
    ``/风格 老师 加10001``（没空格）、``/风格 老师 +10001``、
    ``/风格 老师 添加 10001`` 里除最后一种外都直接回"用法…"，看着就像指令坏了。
    现在把这些写法都收下，并顺手去掉尖括号/引号（我自己写的用法文本里就带 ``<QQ>``，
    照抄的人会连着尖括号一起发过来）。

    Args:
        text: ``/风格`` 后面、去掉「老师」之后的那一段。

    Returns:
        ``(action, target)``；action 是 ``add`` / ``remove`` / 空串（没看懂）。
    """

    def _clean(value: str) -> str:
        """去掉包裹用的尖括号/引号/全角空格（用法文本里写了 <QQ>，有人会照抄）。"""
        return (
            value.replace("＜", "<")
            .replace("＞", ">")
            .strip()
            .strip("<>「」【】\"' \t　:：,，")
        )

    cleaned = _clean(text)
    if not cleaned:
        return "", ""
    head = cleaned[0]
    if head in "+＋":
        return "add", _clean(cleaned[1:])
    for word, action in (
        ("添加", "add"),
        ("增加", "add"),
        ("加", "add"),
        ("add", "add"),
        ("删除", "remove"),
        ("移除", "remove"),
        ("删", "remove"),
        ("remove", "remove"),
    ):
        if cleaned.startswith(word):
            rest = _clean(cleaned[len(word) :])
            return (action, rest) if rest else ("", "")
    # 只给了个名字/号码：当成"加"（比什么都不做更符合直觉）。
    if all(char.isdigit() for char in cleaned):
        return "add", cleaned
    return "", ""


@register(
    "qingyu_core",
    "migration",
    "轻语 Agent 核心：世界状态 + 决策链 + 情节记忆 + 关系记忆 + 周报",
    PLUGIN_VERSION,
)
class QingyuCorePlugin(Star):
    """Owns the bot's state, decisions and memories."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._db = store.connect()
        self._background_tasks: set[asyncio.Task] = set()
        self._closing = False
        # 每个会话最近一次发出去的正文：用来识别"同一句话短时间内又发一遍"。
        self._last_reply: dict[str, tuple[str, float]] = {}
        self._last_sync = 0.0
        self._extracting: set[str] = set()
        self._last_observe_log: dict[str, float] = {}
        # 「算不算在叫她」用的名字集合（懒加载 + 兜底名）。
        self._self_names_cache: set[str] = set()
        self._self_names_loaded = False
        synced = world.sync_affection(self._db)
        # 群味卡片（L1）：启动时后台建一次，缺了/过期了运行时会自己补，不阻塞消息处理。
        self._style_task = asyncio.create_task(self._warm_style())
        # Holiday greetings (mode B): 除夕/元旦/春节 do not wait for anyone to
        # speak, so a background task re-checks every few minutes. It rides the
        # same task set as the other background work, so terminate() cancels it.
        task = asyncio.create_task(self._holiday_loop())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info(
            f"qingyu_core: 启动（决策{'生效' if ENFORCE else '只评估，不插话不静默'}／"
            f"表达{'生效' if EXPRESS_ENFORCE else '关闭'}），"
            f"库 {store.DB_PATH}，同步好感 {synced} 条，"
            f"风格学习对象 {style.load_state()['teachers']}",
        )

    async def _warm_style(self) -> None:
        """Build the per-group style cards off the event loop."""
        try:
            state = await asyncio.to_thread(style.ensure_cards)
            logger.info(
                f"qingyu_core: 群味卡片就绪（{len(state.get('cards') or {})} 个群）"
            )
        except Exception as exc:  # noqa: BLE001 - 建不起来也只是退回固定提示
            logger.warning(
                f"qingyu_core: 群味卡片构建失败（{type(exc).__name__}: {exc}）"
            )

    # ---------------------------------------------------------------- 感知链

    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def perceive(self, event: AstrMessageEvent, *args, **kwargs) -> None:
        """更新世界状态：谁在说、群在什么状态、这一轮该不该说话。

        Args:
            event: 收到的消息事件。
            *args: 框架传入的多余参数。
            **kwargs: 框架传入的多余参数。
        """
        if self._closing:
            return
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        text = (event.message_str or "").strip()
        group_id = str(event.get_group_id() or "")
        ts = store.now()

        if not uid or uid == self_id or not text:
            return
        if event.get_platform_name() in SKIP_PLATFORMS:
            return

        world.touch_group(self._db, group_id, umo, ts)
        world.remember_person(self._db, uid, event.get_sender_name(), ts=ts)
        if group_id:
            # 精力按消息量掉，但每 20 条才扣 1 点：水群里不至于十分钟就见底。
            recent = world.recent_message_count(
                self._db,
                umo,
                ts,
                world.ENERGY_WINDOW_SECONDS,
            )
            if recent % world.ENERGY_DRAIN_EVERY == 0:
                world.bump_mood(self._db, group_id, energy=-1, mood=0, ts=ts)
            # 阶段二：心情由事件驱动（被夸/被怼/被点名/被无视/被接话）。
            mood_events = world.react_to_message(
                self._db,
                group_id=group_id,
                umo=umo,
                text=text,
                is_wake=bool(event.is_at_or_wake_command),
                ts=ts,
            )
            if mood_events:
                event.set_extra("qingyu.mood_events", mood_events)
                # 桌宠要按"她被夸了/被怼了"换表情，所以心情事件也记一份带时间戳的。
                last = world.last_mood_reason(self._db, group_id)
                petlog.log(
                    self._db,
                    kind="mood",
                    text=str(last or mood_events[-1]),
                    umo=umo,
                    group_id=group_id,
                    extra={"events": [str(item) for item in mood_events]},
                    ts=ts,
                )
        if ts - self._last_sync >= AFFECTION_SYNC_SECONDS:
            self._last_sync = ts
            world.sync_affection(self._db)
        if group_id:
            self._maybe_extract(group_id, umo, ts)
            self._maybe_weekly_report(ts)
            # 群味卡片过期（或格式升级）就在后台重建，不阻塞这条消息的处理。
            if self._style_task.done() and style.needs_rebuild():
                self._style_task = asyncio.create_task(self._warm_style())

        snapshot = world.snapshot(self._db, umo, uid, group_id, ts)
        is_command = self._is_command(event)

        # 「别人在跟她互动」也算在叫她（2026-09-19 用户要求：她插嘴之后群友想跟她聊两句，
        # 不该还得先 @ 一下）。三层判定，都不依赖适配器：
        #   1. 引用了她刚说过的话 / 引用的昵称是她 / 正文里叫了她的名字（见 _addressing_reason）；
        #   2. **她刚开口、紧接着这一条**（见 _continuation_reason）——QQ 里接着聊的人往往
        #      既不 @ 也不引用，只能按"接力的位置"认：紧接她那一轮、中间没人插话、90 秒内、像在接话；
        #   3. 平台自己能认出来的（@ 到她、Reply.sender_id 是她）本来就已经置位了，这里不重复处理。
        # 积压消息（休眠解冻后一次性涌进来的旧消息）不参与唤醒与插嘴判定：
        # 她的规则都建立在"刚刚在聊什么"上，对几小时前的消息一律不成立。
        age = self._message_age(event)
        stale = age is not None and age > STALE_MESSAGE_SECONDS
        if stale:
            logger.info(
                f"qingyu_core: 这条消息已经 {int(age)} 秒了（积压/解冻），不把她唤醒"
            )

        addressing = "" if stale else self._addressing_reason(event)
        # 这一次唤醒是不是"接话规则"给的——决定要不要续期免@ 窗口（见下方 note_bot_spoke）。
        from_continuation = False
        if not addressing and not stale:
            # 她刚开口、紧接着这一条 —— 群友不用 @ 也能接着跟她聊（见 _continuation_reason）。
            addressing = self._continuation_reason(
                umo=umo,
                group_id=group_id,
                text=text,
                ts=ts,
                messages=list(event.get_messages() or []),
                self_id=self_id,
            )
            from_continuation = bool(addressing)
        if addressing and not event.is_at_or_wake_command:
            event.is_at_or_wake_command = True
            event.is_wake = True
            event.set_extra("qingyu.wake_reason", addressing)
            logger.info(
                f"qingyu_core: 这条算在叫她（{addressing}），核心补了唤醒（不用等 @）"
            )

        # Holiday greeting (mode A): the first message inside the window wakes her
        # so she says it herself. The window, the level switch and the "already
        # greeted" check all live in holiday.pending (any moment inside the window
        # counts as a same-day catch-up). The bookkeeping happens on the wake side:
        # without it every later message in the window would wake her again.
        if (
            group_id
            and not stale
            and not is_command
            and not event.is_at_or_wake_command
            and self._holiday_group_wanted(group_id, umo)
        ):
            greeting = holiday.pending(self._db, group_id)
            gap = world.seconds_since_bot(self._db, umo, ts) if greeting else 0.0
            if greeting and gap >= holiday.min_gap_seconds():
                holiday.mark_greeted(
                    self._db,
                    group_id,
                    greeting["name"],
                    greeting["date"],
                    ts,
                )
                event.is_at_or_wake_command = True
                event.is_wake = True
                event.set_extra("qingyu.holiday", greeting["name"])
                logger.info(
                    f"qingyu_core: holiday {greeting['name']} wakeup in {group_id}"
                    f" (last spoke {int(gap)}s ago, window {greeting['window']})"
                )

        # 机会点（L2）：这一条值不值得接——按本群学到的"群里的人此刻会不会说话"来算。
        # 只对"没被点名、也不是指令"的消息算：被点名走 respond，不需要机会点。
        opportunity = None
        if group_id and not is_command and not event.is_at_or_wake_command:
            opportunity = self._opportunity(event, snapshot, umo, group_id, text, ts)
        plan = decide.decide(
            snapshot,
            text=text,
            is_wake=bool(event.is_at_or_wake_command),
            is_command=is_command,
            chime_allowed=self._chime_wanted(umo),
            chime_due=bool(event.get_extra("qingyu.chime_due")) and not stale,
            repeat_asker=world.repeat_asker(self._db, umo, uid, ts),
            enforcing=ENFORCE,
            chime_at=tuning.chime_at(),
            daily_cap=tuning.daily_cap(),
            opportunity=opportunity,
            chance_due_at=tuning.chance_due(),
        )
        turn_id = f"{group_id or uid}#{ts}#{uuid.uuid4().hex[:6]}"
        # Event-local timing cannot retain silent or failed turns in the plugin.
        event.set_extra("qingyu.started", time.monotonic())
        event.set_extra("qingyu.plan", plan)
        event.set_extra("qingyu.turn_id", turn_id)
        event.set_extra("qingyu.snapshot", snapshot)

        world.log_turn(
            self._db,
            turn_id=turn_id,
            umo=umo,
            uid=uid,
            action=plan.action,
            reason=plan.reason,
            speak_score=plan.speak_score,
            mode=plan.details.get("shadow") and "shadow" or "live",
            planned_action=plan.planned_action,
            ts=ts,
        )
        message = (
            f"qingyu_core: 决策 {plan.describe()} | {snapshot.describe()}"
            f"{'（只评估）' if not ENFORCE else ''}"
        )
        # 观测期要能"看日志确认在跑"：想插嘴的轮次一定打 INFO；其余每种会话
        # 每 OBSERVE_LOG_INTERVAL 秒也漏一条，避免把日志刷爆。
        loud = plan.action != "observe" or plan.planned_action == "chime"
        if not loud:
            last = self._last_observe_log.get(umo, 0.0)
            if ts - last >= OBSERVE_LOG_INTERVAL_SECONDS:
                self._last_observe_log[umo] = ts
                loud = True
        if loud:
            logger.info(message)
        else:
            logger.debug(message)

        # 决策生效时：插嘴轮次把事件标成"要回"，让正常管线接这一句。
        # 注意**不**对 observe 调 stop_event——群里没被点名的消息本来就不会触发模型
        # （pipeline 只在 is_at_or_wake_command 时才调 LLM），停掉它只会连带跳过
        # 群聊历史的记录，得不偿失。
        if ENFORCE and plan.action == "chime":
            event.is_at_or_wake_command = True
        # 她开口了：记时间（之后判断有没有人接）并消耗一点精力。
        # 指令回复不算"主动搭话"，不参与"被无视"的判断。
        if (
            group_id
            and plan.action in {"respond", "chime", "refuse"}
            and not (is_command and plan.action == "respond")
        ):
            world.note_bot_spoke(self._db, group_id, ts)
            # 只有她自己拿到话头的那一轮才重新开一个 90 秒免@ 窗口；
            # 靠接话被唤醒后的回复不再续期，否则窗口会无限往后滑。
            if not from_continuation:
                store.set_meta(
                    self._db,
                    CONTINUATION_UNTIL_KEY.format(group=group_id),
                    str(ts + CONTINUATION_SECONDS),
                )

    @filter.on_using_llm_tool()
    async def note_tool_use(
        self, event: AstrMessageEvent, tool, tool_args=None
    ) -> None:
        """记下这一轮调了哪个工具（周报要看查证率与表情包次数）。

        Args:
            event: 触发本次工具调用的消息事件。
            tool: 即将执行的工具对象（只读它的名字）。
            tool_args: 工具参数，这里不用，只是框架会传。
        """
        name = str(getattr(tool, "name", "") or "")
        if not name:
            return
        report.log_call(
            self._db,
            turn_id=str(event.get_extra("qingyu.turn_id") or ""),
            tool=name,
        )
        logger.info(f"qingyu_core: 这轮调了工具 {name}")

    @filter.on_decorating_result(priority=100)
    async def record_outgoing(self, event: AstrMessageEvent) -> None:
        """把她正要发出去的话记进 ``pet_events``（桌宠要冒泡显示）。

        AstrBot 的历史表拿不到"她刚说的那句"（实测会落后几个小时），所以在这里记一份。
        优先级给高一点：先记账，再做后面那些会改内容/加延迟的处理。

        Args:
            event: 即将发出回复的消息事件。
        """
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        text = "".join(
            str(getattr(part, "text", ""))
            for part in chain
            if type(part).__name__ == "Plain"
        ).strip()
        if not text:
            return
        plan = event.get_extra("qingyu.plan")
        action = getattr(plan, "action", "")
        umo = event.unified_msg_origin
        # 重复正文拦掉：工具循环会让模型把同一句再说一次，看着像她结巴了。
        # 清空消息链后 respond 阶段会因"消息为空"直接跳过发送（见 stage.py 的空链判断）。
        now = time.monotonic()
        previous = self._last_reply.get(umo)
        if (
            previous
            and previous[0] == text
            and 0 <= now - previous[1] <= DUPLICATE_REPLY_SECONDS
        ):
            logger.info(
                f"qingyu_core: 同一句话 {now - previous[1]:.1f} 秒内重复，这次不发：{text[:24]}"
            )
            result.chain = []
            return
        self._last_reply[umo] = (text, now)
        group_id = str(event.get_group_id() or "")
        if not group_id:
            # 私聊/面板的回复不进事件流：桌宠的气泡只讲"她在群里干了什么"。
            return
        petlog.log(
            self._db,
            kind="chime" if action == "chime" else "speak",
            text=text,
            umo=umo,
            group_id=group_id,
            extra={"action": action} if action else None,
        )

    @filter.after_message_sent()
    async def finish_turn(self, event: AstrMessageEvent) -> None:
        """一轮结束：把耗时写回 ``turns``，并清理本次决策的临时记录。

        Args:
            event: 回复已发出的消息事件。
        """
        turn_id = event.get_extra("qingyu.turn_id")
        started = event.get_extra("qingyu.started")
        if self._closing or not turn_id or started is None:
            return
        event.set_extra("qingyu.started", None)
        world.finish_turn(
            self._db,
            turn_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    @filter.on_llm_response()
    async def record_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """把这一轮回复的长度与 token 记进 ``turns``（可观测性）。

        Args:
            event: 触发本次模型请求的事件。
            response: 模型返回的结果。
        """
        turn_id = event.get_extra("qingyu.turn_id")
        if not turn_id:
            return
        text = getattr(response, "completion_text", "") or ""
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total", 0)) if usage is not None else 0
        self._db.execute(
            "UPDATE turns SET reply_chars = ?, prompt_tokens = ? WHERE turn_id = ?",
            (len(text), tokens, turn_id),
        )
        self._db.commit()

    # ---------------------------------------------------------------- 记忆注入

    @filter.on_llm_request()
    async def inject_memories(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """把"这一轮是谁在说话"和"关于这个人的旧事"掺进提示词。

        Args:
            event: 触发本次模型请求的事件。
            req: 即将发出的模型请求。
        """
        turn_id = event.get_extra("qingyu.turn_id")
        snapshot = event.get_extra("qingyu.snapshot")
        plan = event.get_extra("qingyu.plan")
        uid = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        nickname = snapshot.person.nickname if snapshot else event.get_sender_name()
        prompt = req.system_prompt or ""

        # 群里一条会话是好几个人共用的、消息本身又不带署名，不先钉死"这轮谁在说"，
        # 她就会把提问的人认成上一条消息的主角——2026-09-17 群里连着两个人问
        # "我是谁"，她两次都答"你是示例用户"，就是这么翻车的。
        # Per-turn text goes to the tail of the user content instead of the system
        # prompt: the system prompt must stay byte-identical so the provider can
        # reuse its prefix cache for the conversation history.
        if group_id and uid:
            about = ""
            if snapshot:
                about = f"，好感 {snapshot.person.affection}、照过 {snapshot.person.familiarity} 次面"
            req.extra_user_content_parts.append(
                TextPart(
                    text=(
                        f"【这一轮跟你说话的是「{nickname or uid}」"
                        f"（QQ {uid}{about}），回答时认准这个人，别认错。】"
                    ),
                ),
            )
            if "User ID:" not in prompt:
                logger.info(
                    f"qingyu_core: 平台没给发言人标注（identifier 关着？），"
                    f"核心自己补了「{nickname or uid}」",
                )
        # 关系语气（2026-09-19 加）：好感度不只是在决策层加分，也要**看得见**——
        # 把"你跟他什么关系 → 该怎么说话"直接写进提示词。之前这段只印了个数字，
        # 模型不知道 85 和 45 在语气上该有什么差别（用户反馈"好感度的作用被拉低"）。
        if snapshot:
            label, _multiplier, flavor = style.flavor_for(snapshot.person.affection)
            req.extra_user_content_parts.append(
                TextPart(
                    text=(
                        f"【你跟「{snapshot.person.nickname or uid or '对方'}」的关系："
                        f"好感 {snapshot.person.affection}/100（{label}）→ {flavor}】"
                    ),
                ),
            )
        # 不依赖感知链：面板会话等被感知链跳过的场合，也照样能把旧事带上。
        if plan is not None and not plan.recall_needed:
            return

        query = event.message_str or ""
        picked = memory.recall(self._db, uid=uid, group_id=group_id, query=query)
        if not picked:
            return

        # 关系记忆（阶段五）可能是"别人的事、自己在场"，注入时要点明是谁的事，
        # 所以只在真挑到关系记忆时才去查昵称表，省掉每轮一次查询。
        names: dict[str, str] = {}
        if any(str(item.get("related_uid") or "") for item in picked):
            names = {
                str(row["uid"]): str(row["nickname"])
                for row in self._db.execute(
                    "SELECT uid, nickname FROM persons WHERE nickname != ''",
                )
            }
        block = memory.format_for_prompt(picked, nickname, names, uid)
        req.extra_user_content_parts.append(TextPart(text="\n" + block))
        memory.mark_used(self._db, [int(item["id"]) for item in picked])
        if turn_id:
            memory.log_recall(self._db, turn_id, [int(item["id"]) for item in picked])
            self._db.execute(
                "UPDATE turns SET injected = 1, recall_ids = ? WHERE turn_id = ?",
                (",".join(str(item["id"]) for item in picked), turn_id),
            )
            self._db.commit()
        relations = sum(1 for item in picked if str(item.get("related_uid") or ""))
        logger.info(
            f"qingyu_core: 注入 {len(picked)} 条旧事给 {nickname or uid}"
            f"（关系 {relations} 条）："
            + "；".join(str(item["text"])[:20] for item in picked),
        )

    @filter.on_llm_request()
    async def maybe_style_hint(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """按这一轮的决策给模型加一句"怎么说"的提示。

        几种提示互斥，按优先级取第一个命中的：

        - **拒绝**（阶段三）：短、有边界、给替代方案；
        - **插嘴**：像随口接一句，按群味给字数与语气（禁提问/禁波浪号）；
        - **敷衍**（阶段四）：约 8% 的闲聊只回两三个字；
        - **Direct replies**: group style controls length; affection controls
          tone and persona mannerisms, including affectionate sentence endings;
        - **长度**（阶段四）：把 ``plan.max_chars`` 变成"写短点"的提示，而不是事后截断；
        - **偷懒**：闲聊时偶尔只回一句。

        Args:
            event: 触发本次模型请求的事件。
            req: 即将发出的模型请求。
        """
        plan = event.get_extra("qingyu.plan")
        text = event.message_str or ""
        group_id = str(event.get_group_id() or "")
        if not EXPRESS_ENFORCE:
            return
        context = self._chime_context(event, text)
        snapshot = event.get_extra("qingyu.snapshot")
        hint = (
            express.refuse_hint(plan)
            or express.chime_hint(plan, group_id=group_id, context=context)
            or express.minimal_hint(plan, text)
            or express.reply_hint(
                plan,
                group_id=group_id,
                text=text,
                context=context,
                is_command=self._is_command(event),
                affection=(snapshot.person.affection if snapshot else None),
            )
            or express.length_hint(plan)
            or express.lazy_hint(plan, text)
        )
        if not hint:
            return
        req.extra_user_content_parts.append(TextPart(text="\n" + hint))
        event.set_extra("qingyu.style_hint", hint[:24])
        if plan is not None and plan.action == "refuse":
            logger.info(
                f"qingyu_core: 这轮是拒绝（{plan.details.get('refusal')}），要求说短"
            )
        elif plan is not None and plan.action == "chime":
            logger.info("qingyu_core: 插嘴这轮要求说短（1~2 句）")
        elif (
            plan is not None
            and plan.action == "respond"
            and group_id
            and not self._is_command(event)
        ):
            logger.info(
                f"qingyu_core: 这轮按关系与群味答（{plan.tone}，上限 "
                f"{express.reply_budget(plan, group_id, snapshot.person.affection if snapshot else None)} 字）",
            )
        elif hint in express.MINIMAL_HINTS:
            logger.info("qingyu_core: 这轮敷衍一下，只回两三个字")
        elif plan is not None and plan.max_chars <= 200:
            logger.info(f"qingyu_core: 这轮加了长度提示（上限 {plan.max_chars} 字）")
        else:
            logger.info("qingyu_core: 这轮让她偷懒，只回一句")

    @filter.on_llm_request()
    async def inject_holiday(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Tell the model which holiday it is when the greeting was triggered.

        The wording is shared with the proactive path (see
        :func:`holiday.greet_instruction`) so both greetings sound the same.

        Args:
            event: Message event of this model request.
            req: The request that is about to be sent.
        """
        name = str(event.get_extra("qingyu.holiday") or "")
        if not name:
            return
        group_id = str(event.get_group_id() or "")
        cap = express.chime_length_cap(group_id)
        req.extra_user_content_parts.append(
            TextPart(text="\n" + holiday.greet_instruction(name, cap)),
        )
        logger.info(
            f"qingyu_core: holiday greeting hint injected ({name}, {cap} chars)"
        )

    @filter.on_decorating_result()
    async def delay_before_send(self, event: AstrMessageEvent) -> None:
        """发送前停顿一下，别秒回。

        放在装饰阶段（真正发出之前），所以只影响这一条回复的节奏，不占别的会话。

        Args:
            event: 即将发出回复的消息事件。
        """
        if not EXPRESS_ENFORCE:
            return
        if event.get_extra("qingyu.delay_done"):
            return
        event.set_extra("qingyu.delay_done", True)
        plan = event.get_extra("qingyu.plan")
        # 桌面通道＝面对面说话：走"快速档"（停顿 ≤0.4 秒），QQ 群聊那边保持原样。
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        await express.wait_before_reply(
            plan,
            event.message_str or "",
            fast="desktop_pet" in umo,
        )

    # ---------------------------------------------------------------- 行动层工具

    @filter.llm_tool(name="remember")
    async def remember_tool(
        self, event: AstrMessageEvent, text: str, kind: str = "fact"
    ):
        """把某个人说过的、以后可能用得上的事记下来（旧事记忆）。

        只在真的值得记的时候调用：考试/比赛/课程/习惯/约定/人际这类以后能提起来的
        具体信息。不要记寒暄、不要记你自己说过的话、不要记没头没尾的句子。
        同一个人的同一件事只记一次，记过就别重复调用了。
        kind 用 relation 时，text 里要带上对方的群昵称（例如"和小刚约了周末打球"），
        这样以后小刚那一侧也能被提起来。

        Args:
            text (str): 用第三人称转述的简短内容，30 字内，例如"下个月要考四级"
            kind (str): 类型，可选 fact（事实）/ preference（喜好）/ event（事情）/ relation（关系）

        Returns:
            str: 记录结果说明
        """
        uid = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        owner_uid, cleaned = self._resolve_person(text, uid, group_id)
        related_uid = ""
        if kind == "relation":
            # 关系记忆要靠昵称找出"另一个人"，长昵称先匹配，免得短的抢了长的。
            rows = self._db.execute(
                "SELECT uid, nickname FROM persons WHERE nickname != ''"
                " ORDER BY LENGTH(nickname) DESC",
            ).fetchall()
            for row in rows:
                name = str(row["nickname"])
                if len(name) >= 2 and name in cleaned and str(row["uid"]) != owner_uid:
                    related_uid = str(row["uid"])
                    break
        memory_id = memory.remember(
            self._db,
            uid=owner_uid,
            group_id=group_id,
            text=cleaned,
            kind=kind,
            confidence=memory.TOOL_CONFIDENCE,
            source="tool",
            related_uid=related_uid,
        )
        if memory_id is None:
            return "这条要么太短、要么已经记过了，不用重复记。"
        return (
            f"记下了（编号 {memory_id}）。以后合适的时候可以自然提一句，不用刻意复述。"
        )

    # ---------------------------------------------------------------- 指令

    @filter.command("记忆")
    async def memory_status(self, event: AstrMessageEvent):
        """看看轻语记住了什么（管理员）。

        Args:
            event: 指令消息事件。

        Yields:
            记忆统计文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~ 想删自己的记忆发 /忘记我")
            return
        data = memory.stats(self._db)
        lines = [
            f"记忆共 {data['total']} 条（自动抽取 {data['auto']} 条，被用过 {data['used']} 条，"
            f"关系 {data['relations']} 条）",
        ]
        for row in data["recent"]:
            age = memory.humanize_age(int(row["created_at"]))
            lines.append(f"· [{row['kind']}] {row['text']}（{age}）")
        if not data["recent"]:
            lines.append("还没记住什么。")
        yield event.plain_result("\n".join(lines))

    @filter.command("忘记我")
    async def forget_me(self, event: AstrMessageEvent):
        """删掉关于自己的全部记忆。

        Args:
            event: 指令消息事件。

        Yields:
            删除结果文本。
        """
        uid = str(event.get_sender_id() or "")
        removed = memory.forget_person(self._db, uid)
        logger.info(f"qingyu_core: {uid} 要求忘记，删了 {removed} 条")
        yield event.plain_result(f"好，关于你的 {removed} 条记忆都删掉了。")

    @filter.command("记忆抽取")
    async def extract_now(self, event: AstrMessageEvent):
        """立刻跑一次每日抽取（管理员，用来验证抽取质量）。

        在群里用只抽那个群；在私聊/面板里用就把已知的群都抽一遍。

        Args:
            event: 指令消息事件。

        Yields:
            抽取结果文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用哦~")
            return
        targets = self._known_groups()
        group_id = str(event.get_group_id() or "")
        if group_id:
            targets = [(group_id, event.unified_msg_origin)]
        if not targets:
            yield event.plain_result("还没有任何群记录，先让群里聊两句再抽。")
            return
        lines = [f"抽取 {len(targets)} 个群："]
        for target_group, umo in targets:
            saved, detail = await self._run_extraction(target_group, umo)
            lines.append(f"· {target_group}：新增 {saved} 条 {detail}")
        yield event.plain_result("\n".join(lines)[:MAX_CHARS])

    def _known_groups(self) -> list[tuple[str, str]]:
        """列出已知的群（群号 + 会话标识）。

        Returns:
            (群号, umo) 列表。
        """
        rows = self._db.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'umo:%' ORDER BY key",
        ).fetchall()
        return [
            (str(row["key"]).split(":", 1)[1], str(row["value"]))
            for row in rows
            if str(row["value"])
        ]

    @filter.command("记忆清理")
    async def purge_recent_memories(self, event: AstrMessageEvent):
        """清掉刚抽进来的记忆（管理员，抽取质量不满意时用）。

        用法：``/记忆清理``（默认清最近 24 小时的自动抽取结果）
        或 ``/记忆清理 全部 24`` 连她自己记的一起清。

        Args:
            event: 指令消息事件。

        Yields:
            清理结果文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用哦~")
            return
        argument = (event.message_str or "").replace("记忆清理", "", 1).strip()
        parts = argument.split()
        source = "nightly" if not parts or parts[0] != "全部" else ""
        hours = 24
        for part in parts:
            if part.isdigit():
                hours = int(part)
        if source:
            removed = memory.delete_recent(self._db, hours=hours, source=source)
            yield event.plain_result(
                f"清掉最近 {hours} 小时自动抽取的 {removed} 条记忆"
                "（她自己记的没动）。想连她的一起清：/记忆清理 全部 " + str(hours),
            )
            return
        connection = self._db
        cursor = connection.execute(
            "DELETE FROM memories WHERE created_at >= ?",
            (store.now() - hours * 3600,),
        )
        connection.commit()
        yield event.plain_result(
            f"清掉最近 {hours} 小时的全部记忆 {cursor.rowcount} 条。"
        )

    @filter.command("轻语状态")
    async def core_status(self, event: AstrMessageEvent):
        """看她今天的状态、决策分布与记忆情况（管理员）。

        Args:
            event: 指令消息事件。

        Yields:
            状态文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        umo = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        snapshot = world.snapshot(self._db, umo, uid, group_id, store.now())
        rows = self._db.execute(
            "SELECT action, COUNT(*) AS n FROM turns WHERE ts >= ? GROUP BY action"
            " ORDER BY n DESC",
            (store.now() - 86400,),
        ).fetchall()
        planned_rows = self._db.execute(
            "SELECT planned_action, COUNT(*) AS n FROM turns WHERE ts >= ?"
            " GROUP BY planned_action ORDER BY n DESC",
            (store.now() - 86400,),
        ).fetchall()
        distribution = "、".join(f"{row['action']} {row['n']}" for row in rows) or "无"
        planned = (
            "、".join(
                f"{row['planned_action'] or '升级前的旧记录'} {row['n']}"
                for row in planned_rows
            )
            or "无"
        )
        refusals = next(
            (int(row["n"]) for row in rows if row["action"] == "refuse"),
            0,
        )
        data = memory.stats(self._db)
        lines = [
            f"模式：决策{'生效' if ENFORCE else '只评估（不插话、不静默）'}"
            f"／表达{'生效' if EXPRESS_ENFORCE else '关闭'}",
            f"状态：{snapshot.describe()}",
            f"最近一次心情变化：{world.last_mood_reason(self._db, group_id) or '还没有'}",
            f"近 24 小时实际动作：{distribution}",
            f"近 24 小时评估结果：{planned}"
            f"（插嘴门槛 {tuning.fmt('chime_at', tuning.chime_at())}"
            f"{'（临时值）' if 'chime_at' in tuning.load() else ''}）",
            f"近 24 小时拒绝：{refusals} 次",
            f"记忆：{data['total']} 条（自动 {data['auto']}，用过 {data['used']}）",
            f"本群今天插嘴：{snapshot.chimes_today} 次（额度 {tuning.daily_cap()}）",
            petlog.describe(self._db),
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("轻语周报")
    async def weekly_report(self, event: AstrMessageEvent):
        """看最近一周的评估数据（管理员，阶段六）。

        用法：``/轻语周报``（默认 7 天）或 ``/轻语周报 14``（最多 30 天）。

        Args:
            event: 指令消息事件。

        Yields:
            周报文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        argument = (event.message_str or "").replace("轻语周报", "", 1).strip()
        days = int(argument) if argument.isdigit() else 7
        days = max(1, min(30, days))
        text = report.build_report(self._db, days=days)
        yield event.plain_result(text[:REPORT_MAX_CHARS])

    @filter.command("插嘴阈值")
    async def chime_threshold(self, event: AstrMessageEvent):
        """查看或临时改插嘴门槛（管理员，不用改代码也不用重载）。

        用法：``/插嘴阈值`` 看当前值；``/插嘴阈值 1.0`` 改门槛；
        ``/插嘴阈值 上限 12`` 改每日上限；``/插嘴阈值 默认`` 还原成代码默认值。

        Args:
            event: 指令消息事件。

        Yields:
            结果文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用哦~")
            return
        argument = (event.message_str or "").replace("插嘴阈值", "", 1).strip()
        if not argument:
            yield event.plain_result(
                "现在生效的值：\n"
                f"{tuning.describe()}\n"
                "改法：/插嘴阈值 1.0　/插嘴阈值 上限 12　/插嘴阈值 机会点 0.35　/插嘴阈值 默认",
            )
            return
        if argument in {"默认", "还原", "reset", "default"}:
            yield event.plain_result(tuning.clear())
            return
        key, _, raw = argument.partition(" ")
        if key in {"上限", "cap", "每日上限"}:
            target, text = "chime_daily_cap", raw.strip()
        elif key in {"机会点", "机会", "chance"}:
            target, text = "chance_due", raw.strip()
        else:
            target, text = "chime_at", key
        try:
            value = float(text)
        except ValueError:
            yield event.plain_result(
                "要填数字哦，比如 /插嘴阈值 1.0 或 /插嘴阈值 上限 12"
            )
            return
        yield event.plain_result(tuning.save(target, value))

    @filter.command("风格")
    async def style_command(self, event: AstrMessageEvent):
        """看/改「学谁说话」和群味卡片（管理员）。

        用法：``/风格`` 看卡片（本群高亮）；``/风格 重建`` 立刻按最新历史重建；
        ``/风格 老师`` 看学习对象（含每人可用发言条数）；
        ``/风格 老师 加 <QQ或昵称>``、``/风格 老师 删 <QQ或昵称>``。

        Args:
            event: 指令消息事件。

        Yields:
            卡片摘要或操作结果。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        argument = (event.message_str or "").replace("风格", "", 1).strip()
        group_id = str(event.get_group_id() or "")
        if argument in {"重建", "重来", "rebuild"}:
            state = await asyncio.to_thread(style.ensure_cards, True)
            yield event.plain_result(
                f"重建好了：{len(state.get('cards') or {})} 个群。\n{style.summary(group_id)[:REPORT_MAX_CHARS]}",
            )
            return
        if argument.startswith("老师"):
            rest = argument.replace("老师", "", 1).strip()
            if not rest:
                yield event.plain_result(style.teacher_report())
                return
            action, target = parse_teacher_command(rest)
            if not action:
                yield event.plain_result(TEACHER_USAGE)
                return
            uid = self._resolve_uid(target)
            if not uid:
                yield event.plain_result(
                    f"库里找不到「{target}」这个人。可以直接填 QQ 号，例如：/风格 老师 加 10001",
                )
                return
            reply = await asyncio.to_thread(style.set_teachers, action, uid)
            nickname = self._nickname_of(uid)
            yield event.plain_result(f"{reply}{f'（{nickname}）' if nickname else ''}")
            return
        yield event.plain_result(style.summary(group_id)[:REPORT_MAX_CHARS])

    @filter.command("祝福")
    async def holiday_command(self, event: AstrMessageEvent):
        """Show or change the holiday greetings (admin only).

        Usage: ``/祝福`` reports today's holiday, this group's switch and whether
        it was greeted; ``/祝福 开`` / ``/祝福 关`` flip this group;
        ``/祝福 试 [节日名]`` rehearses once here; ``/祝福 列表`` shows the
        holiday table; ``/祝福 设 2027-02-06 春节`` adds one lunar date.

        Args:
            event: Command message event.

        Yields:
            Status text or the result of the change.
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能用哦~")
            return
        argument = (event.message_str or "").replace("祝福", "", 1).strip()
        group_id = str(event.get_group_id() or "")
        if argument.startswith("设"):
            parts = argument.replace("设", "", 1).split()
            if len(parts) < 2:
                yield event.plain_result("用法：/祝福 设 2027-02-06 春节")
                return
            entry, message = holiday.save_date(parts[0], parts[1])
            if entry:
                logger.info(
                    f"qingyu_core: holiday date saved {entry['date']} "
                    f"{entry['name']} level={entry['level']}",
                )
            yield event.plain_result(message)
            return
        if argument.startswith("列表"):
            yield event.plain_result(holiday.table_text()[:REPORT_MAX_CHARS])
            return
        if argument.startswith("试"):
            if not group_id:
                yield event.plain_result("演练要在群里用，私聊里没有群味可参照。")
                return
            name = argument.replace("试", "", 1).strip()
            if not name:
                today = holiday.today()
                name = today[0]["name"] if today else ""
            if not name:
                yield event.plain_result(
                    "今天不是节日。想演练就带上名字：/祝福 试 中秋",
                )
                return
            cap = express.chime_length_cap(group_id)
            text = await self._holiday_greeting_text(
                name, event.unified_msg_origin, group_id
            )
            if not text:
                yield event.plain_result(
                    "没生成出来（没有可用模型或调用失败），日志里有原因。",
                )
                return
            yield event.plain_result(
                f"演练一次（{name}，{len(text)} 字／上限 {cap}）：\n{text}\n"
                "这次只在这里回，不记账、不进群。",
            )
            return
        if argument in {"开", "打开", "on"} or argument in {"关", "关闭", "off"}:
            if not group_id:
                yield event.plain_result("这个要在群里用：在哪个群发就在哪个群开关。")
                return
            yield event.plain_result(
                holiday.set_group(group_id, argument in {"开", "打开", "on"}),
            )
            return
        if argument:
            yield event.plain_result(HOLIDAY_USAGE)
            return
        now = datetime.now()
        entries = holiday.today(now)
        if entries:
            lines = [
                f"今天是{entry['name']}（level {entry['level']}，窗口 {entry['window']}，"
                "当天补发：窗口内随时可发）"
                for entry in entries
            ]
        else:
            lines = [
                f"今天（{now.date().isoformat()}）不是节日：公历表里没有，"
                "配置的农历日期里也没有今天。"
            ]
        offset = holiday.group_override(group_id)
        wanted = self._holiday_group_wanted(group_id, event.unified_msg_origin)
        source = "配置里显式指定" if offset is not None else "跟随插嘴白名单"
        lines.append(f"本群祝福：{'开' if wanted else '关'}（{source}）")
        if group_id and entries:
            for entry in entries:
                done = holiday.already_greeted(
                    self._db,
                    group_id,
                    entry["name"],
                    entry["date"],
                )
                lines.append(
                    f"· {entry['name']}：{'今天已经发过' if done else '今天还没发'}"
                )
        lines.append(f"插嘴白名单里的会话：{len(self._chime_sessions())} 个")
        lines.append(
            f"农历日期共 {len(holiday.load()['dates'])} 条（/祝福 列表 看节日表）"
        )
        lines.append(
            "改法：/祝福 开　/祝福 关　/祝福 试　/祝福 列表　/祝福 设 2027-02-06 春节"
        )
        yield event.plain_result("\n".join(lines)[:MAX_CHARS])

    @filter.on_decorating_result(priority=110)
    async def shorten_over_long(self, event: AstrMessageEvent) -> None:
        """写太长了就让她重写一句（**不是**事后截断）。

        管的只有**长度**这一类毛病（写小作文），**不管她的口癖**：

        - **插嘴**：群里的人中位 7 个字，一句 60 字的"随口接话"一眼假；
        - **被 @ 的轮次**：闲聊按这个群的答话上限 × 好感度倍数，中等问题翻倍，
          正经问题（检查单/知识库那类）不压。

        2026-09-19 返工记录：第一版还顺带做了"去掉开头语气词与波浪号"的确定性清理，
        结果**人设被淹没、好感度看不出作用**（用户反馈）。口癖是她的标志，归关系管，
        不归长度管——所以现在只在她写太长时才让她重写，且重写提示词里也不再提波浪号。

        Args:
            event: 即将发出回复的消息事件。
        """
        if not (EXPRESS_ENFORCE and CHIME_REWRITE):
            return
        plan = event.get_extra("qingyu.plan")
        action = getattr(plan, "action", "")
        group_id = str(event.get_group_id() or "")
        if not group_id or action not in {"chime", "respond"}:
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        text = "".join(
            str(getattr(part, "text", ""))
            for part in chain
            if type(part).__name__ == "Plain"
        ).strip()
        if not text:
            return

        snapshot = event.get_extra("qingyu.snapshot")
        affection = snapshot.person.affection if snapshot else None
        if action == "respond":
            if self._is_command(event):
                return
            cap = express.reply_budget(plan, group_id, affection)
        else:
            cap = express.chime_length_cap(group_id)
        if not cap or not express.chime_too_long(text, cap):
            return
        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if provider is None:
            logger.warning(
                f"qingyu_core: 超长（{len(text)} 字 > {cap}）但没有可用模型，原样发"
            )
            return
        try:
            response = await provider.text_chat(
                prompt=express.chime_rewrite_prompt(text, event.message_str or "", cap),
                session_id=None,
                system_prompt=(
                    "你在帮一个 QQ 群里的十六岁少女把一句话改短。"
                    "只输出改好的那一句，不要引号、不要解释、不要加任何前后缀。"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 重写失败就原样发，不影响这一轮
            logger.warning(
                f"qingyu_core: 重写失败（{type(exc).__name__}: {exc}），原样发"
            )
            return
        shortened = (
            str(getattr(response, "completion_text", "") or "").strip().strip("「」\"'")
        )
        if not shortened or len(shortened) >= len(text):
            logger.info(
                f"qingyu_core: 重写没变短（{len(text)} → {len(shortened)}），原样发"
            )
            return
        for part in chain:
            if type(part).__name__ == "Plain":
                part.text = shortened
        logger.info(
            f"qingyu_core: {action} 超长已重写 {len(text)} → {len(shortened)} 字（上限 {cap}）",
        )
        event.set_extra("qingyu.style_rewritten", True)

    @filter.command("轻语帮助")
    async def help_command(self, event: AstrMessageEvent):
        """列出核心插件提供的指令。

        Args:
            event: 指令消息事件。

        Yields:
            用法文本。
        """
        yield event.plain_result(USAGE)

    # ---------------------------------------------------------------- 内部

    def _maybe_extract(self, group_id: str, umo: str, ts: int) -> None:
        """每天补跑一次记忆抽取（过了凌晨 4 点、当天还没抽过）。

        用"下一条消息触发"代替定时任务：机器人关机时不会漏，开机后自然补上，
        也不会往 AstrBot 的定时任务表里堆重复作业。

        Args:
            group_id: 群号。
            umo: 会话标识，用来取模型。
            ts: 当前时间戳。
        """
        key = f"extract:{group_id}"
        last = store.get_meta(self._db, key, "0")
        now_dt = datetime.fromtimestamp(ts)
        if last.isdigit() and int(last) > 0:
            if datetime.fromtimestamp(int(last)).date() == now_dt.date():
                return
        if now_dt.hour < LAZY_EXTRACT_AFTER_HOUR or group_id in self._extracting:
            return
        self._extracting.add(group_id)
        store.set_meta(self._db, key, str(ts))
        task = asyncio.create_task(self._extract_in_background(group_id, umo))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _extract_in_background(self, group_id: str, umo: str) -> None:
        """后台跑抽取，失败只记日志，不影响聊天。

        Args:
            group_id: 群号。
            umo: 会话标识。
        """
        try:
            saved, detail = await self._run_extraction(group_id, umo)
            # 无论抽到几条都把原因写进日志，避免"新增 0 条"看不出是哪种情况。
            logger.info(
                f"qingyu_core: 每日抽取 {group_id} 完成：新增 {saved} 条｜{detail}"
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛出去
            logger.error(f"qingyu_core: 抽取任务失败: {type(exc).__name__}: {exc}")
        finally:
            self._extracting.discard(group_id)

    async def _holiday_loop(self) -> None:
        """Proactively greet mode-B holidays while the plugin is loaded.

        Mode B (除夕/元旦/春节) does not wait for anybody to speak: this loop
        re-checks every few minutes. It never returns and dies only when
        ``terminate`` cancels it through ``_background_tasks``.
        """
        while not self._closing:
            try:
                await self._greet_mode_b()
            except Exception as exc:  # noqa: BLE001 - the loop must survive failures
                logger.warning(
                    f"qingyu_core: holiday task failed: {type(exc).__name__}: {exc}"
                )
            await asyncio.sleep(holiday.MODE_B_INTERVAL_SECONDS)

    async def _greet_mode_b(self) -> None:
        """Greet every enabled group that still owes a mode-B holiday greeting.

        A group only counts when it has been active recently: waking up a silent
        group with a greeting out of nowhere is worse than skipping the holiday.
        """
        ts = store.now()
        for group_id, umo in self._known_groups():
            if self._closing:
                return
            if not self._holiday_group_wanted(group_id, umo):
                continue
            active = world.recent_message_count(
                self._db,
                umo,
                ts,
                holiday.ACTIVE_WINDOW_SECONDS,
            )
            if not active:
                continue
            entry = holiday.pending(self._db, group_id, mode_b=True)
            if not entry:
                continue
            text = await self._holiday_greeting_text(entry["name"], umo, group_id)
            if not text:
                continue
            try:
                sent = await self.context.send_message(
                    umo,
                    MessageChain([Plain(text)]),
                )
            except Exception as exc:  # noqa: BLE001 - one group must not stop the others
                logger.warning(
                    f"qingyu_core: holiday greeting send failed for {group_id} "
                    f"({type(exc).__name__}: {exc})"
                )
                continue
            if not sent:
                logger.warning(
                    f"qingyu_core: holiday greeting not sent, no platform for {umo}"
                )
                continue
            holiday.mark_greeted(self._db, group_id, entry["name"], entry["date"], ts)
            # A proactive send does not travel through the event pipeline, so the
            # desktop pet's "what she just said" stream needs this row by hand.
            petlog.log(
                self._db,
                kind="speak",
                text=text,
                umo=umo,
                group_id=group_id,
                extra={"holiday": entry["name"]},
                ts=ts,
            )
            logger.info(
                f"qingyu_core: holiday {entry['name']} greeting sent to {group_id}"
            )

    async def _holiday_greeting_text(
        self,
        name: str,
        umo: str,
        group_id: str,
    ) -> str:
        """Ask the model for one holiday line (used by mode B and ``/祝福 试``).

        This is a standalone call, not a turn of the group conversation, so the
        persona plus the greeting instruction go into ``system_prompt``; the
        group style caps the length.

        Args:
            name: Holiday name.
            umo: Session used to pick the provider and the persona.
            group_id: Group whose style caps the length.

        Returns:
            The greeting text, or an empty string when it cannot be generated.
        """
        provider = self.context.get_using_provider(umo=umo)
        if provider is None:
            logger.warning("qingyu_core: no provider, skip the holiday greeting")
            return ""
        persona = ""
        try:
            result = await self.context.persona_manager.get_default_persona_v3(umo=umo)
            # get_default_persona_v3 returns a dict in this version (Personality
            # is a TypedDict), but keep attribute access working as a fallback.
            if isinstance(result, dict):
                persona = str(result.get("prompt") or "")
            else:
                persona = str(getattr(result, "prompt", "") or "")
        except Exception as exc:  # noqa: BLE001 - 读不到人设也还能发一句
            logger.warning(
                f"qingyu_core: holiday greeting persona unreadable "
                f"({type(exc).__name__}), sending without it"
            )
        cap = express.chime_length_cap(group_id)
        system_prompt = f"{persona}\n\n{holiday.greet_instruction(name, cap)}".strip()
        try:
            response = await provider.text_chat(
                prompt=holiday.greet_prompt(name),
                system_prompt=system_prompt,
            )
        except Exception as exc:  # noqa: BLE001 - 生成失败就静默跳过这一轮
            logger.warning(
                f"qingyu_core: holiday greeting call failed "
                f"({type(exc).__name__}: {exc})"
            )
            return ""
        return (
            str(getattr(response, "completion_text", "") or "").strip().strip("「」\"'")
        )

    def _holiday_group_wanted(self, group_id: str, umo: str) -> bool:
        """Whether holiday greetings are on for this group.

        Args:
            group_id: Platform group id.
            umo: Unified message origin; it decides the switch when the config
                does not list this group explicitly.

        Returns:
            True when the config lists the group as on, False when it lists it as
            off, otherwise the interjection whitelist decides.
        """
        override = holiday.group_override(group_id)
        if override is not None:
            return override
        return self._chime_wanted(umo)

    def _maybe_weekly_report(self, ts: int) -> None:
        """每周自动出一份周报（同一周只出一次，写进 meta 并落日志）。

        和每日抽取一样用"下一条消息触发"代替定时任务：机器人关机时不会漏，
        开机后自然补上，也不会往 AstrBot 的定时任务表里堆重复作业。

        Args:
            ts: 当前时间戳。
        """
        key = f"report:{report.week_key(ts)}"
        if store.get_meta(self._db, key, ""):
            return
        text = report.build_report(self._db, days=7, ts=ts)
        store.set_meta(self._db, key, str(ts))
        logger.info(f"qingyu_core: 本周周报（{key}）\n{text}")

    def _is_command(self, event: AstrMessageEvent) -> bool:
        """是否是指令消息。

        Args:
            event: 消息事件。

        Returns:
            True 表示这条消息命中了指令处理器。
        """
        handlers = event.get_extra("activated_handlers") or []
        for handler in handlers:
            for event_filter in getattr(handler, "event_filters", []):
                if type(event_filter).__name__ == "CommandFilter":
                    return True
        return False

    def _chime_sessions(self) -> set[str]:
        """Read the interjection whitelist written by random_chime.

        Returns:
            The enabled session ids; an empty set when the file is missing or
            broken.
        """
        try:
            data = json.loads(CHIME_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(item) for item in data.get("enabled_sessions", [])}

    def _chime_wanted(self, umo: str) -> bool:
        """Whether this session is allowed to interject.

        Args:
            umo: Unified message origin.

        Returns:
            True when the session is whitelisted (holiday greetings follow it by
            default as well).
        """
        return umo in self._chime_sessions()

    def _self_names(self) -> set[str]:
        """她在这个机器人里可能被叫到的名字（人设名 + 从引用里学到的昵称）。

        为什么要自己维护一份：QQ 的"引用"段里只有 `sender_nickname`，没有可靠的 `sender_id`，
        所以只能按名字/原话去认"这条是不是在回她"。

        Returns:
            名字集合。
        """
        if not self._self_names_loaded:
            self._self_names_loaded = True
            names = {SELF_FALLBACK_NAME}
            try:
                with closing(
                    sqlite3.connect(f"file:{nightly.DB_PATH}?mode=ro", uri=True)
                ) as connection:
                    rows = connection.execute(
                        "SELECT persona_id FROM personas"
                    ).fetchall()
                names.update(str(row[0]) for row in rows if row and row[0])
            except sqlite3.Error as exc:
                logger.warning(
                    f"qingyu_core: 读人设名失败（{type(exc).__name__}），只用兜底名"
                )
            self._self_names_cache = {name for name in names if name}
        return self._self_names_cache

    def _quoted_is_mine(self, quoted: str) -> bool:
        """被引用的那段文字，是不是她刚说过的话。

        拿 `pet_events` 里她最近发出去的记录比（那张表就是为"她自己说过什么"建的），
        完全相等、或者一方包含另一方且够长，就算她的。

        Args:
            quoted: 被引用消息的纯文本。

        Returns:
            True 表示这段话是她说过的。
        """
        target = " ".join(str(quoted or "").split())
        if len(target) < 4:
            return False
        for row in petlog.recent(self._db, limit=30):
            mine = " ".join(str(row.get("text") or "").split())
            if not mine:
                continue
            if target == mine or (
                len(target) >= 6 and (target in mine or mine in target)
            ):
                return True
        return False

    def _addressing_reason(self, event: AstrMessageEvent) -> str:
        """这条消息算不算"在叫她"（不依赖 @）。

        判定顺序（都要求不是指令）：

        1. 引用了她（`Reply.sender_id` 是她 → 引用的昵称是她 → 引用的原话是她说的）；
        2. 正文里叫了她的名字（开头就是名字，或名字 + 问号）。

        Args:
            event: 消息事件。

        Returns:
            命中原因（空串表示没在叫她）。
        """
        if self._is_command(event):
            return ""
        self_id = str(event.get_self_id() or "")
        names = self._self_names()
        for part in event.get_messages() or []:
            kind = type(part).__name__
            if kind != "Reply":
                continue
            if self_id and str(getattr(part, "sender_id", "") or "") == self_id:
                return "引用了你的话"
            nickname = str(
                getattr(part, "sender_nickname", "")
                or getattr(part, "sender_name", "")
                or "",
            ).strip()
            if nickname and nickname in names:
                return f"引用了你的话（昵称「{nickname}」）"
            quoted = str(
                getattr(part, "message_str", "") or getattr(part, "text", "") or ""
            )
            if self._quoted_is_mine(quoted):
                return "引用了你刚说过的那句"
        text = (event.message_str or "").strip()
        if any(name and name in text for name in names):
            asked = any(char in text for char in "?？")
            for name in names:
                index = text.find(name) if name else -1
                if index < 0:
                    continue
                rest = text[index + len(name) :].lstrip(" \u3000")
                if asked:
                    return f"叫了你的名字（{name}）"
                if rest[:1] in {"，", ",", "、", "：", ":", "！", "!", "~", "～"}:
                    return f"叫了你的名字（{name}）"
                # 「轻语又没法进行开发」这种是**别人在议论她**，不算在叫她；
                # 名字后面直接跟这些虚词时跳过。
                if index == 0 and not rest.startswith(GOSSIP_AFTER):
                    return f"叫了你的名字（{name}）"
        return ""

    def _continuation_reason(
        self,
        *,
        umo: str,
        group_id: str,
        text: str,
        ts: int,
        messages: list,
        self_id: str,
    ) -> str:
        """她刚说完话、紧接着的这一条 —— 当作在跟她说话（这样群友不用 @ 就能接着聊）。

        用户要的场景（2026-09-19）：**她插嘴之后，群友想跟她聊两句**，
        不应该还得先 @ 她一下。QQ 里接着说话的人往往既不 @ 也不引用，所以只能按"接力的位置"认：

        1. 上一条**轮次**是她开口（chime / respond / refuse）——她刚拿到话头；
        2. 时间上挨得近（默认 90 秒内）；
        3. **这一条紧接在她后面**：上一条轮次就是她那轮，中间没有别人插话
           （所以群里刷屏时不会误判成"在跟她说话"）；
        4. 不是在叫别人：没有 @ 其他人；
        5. 像在接话：带问号，或者很短（≤25 字）。

        Args:
            umo: 会话标识。
            group_id: 群号。
            text: 这条消息的文本。
            ts: 当前时间戳。
            messages: 消息链（用来判断有没有 @ 别人）。
            self_id: 她自己的 QQ。

        Returns:
            命中原因（空串表示不算）。
        """
        if not group_id:
            return ""
        # 窗口到期时间是绝对时间戳，由她"自己拿到话头"的那一轮写入；
        # 不能按"离她上一轮多久"来判，否则她的每一次接话回复都会把窗口续期。
        deadline = store.get_meta(
            self._db,
            CONTINUATION_UNTIL_KEY.format(group=group_id),
            "",
        )
        if not deadline.isdigit() or ts > int(deadline):
            return ""
        recent = world.recent_messages(self._db, group_id, limit=2)
        if not recent:
            return ""
        previous = recent[-1]
        if str(previous.get("action") or "") not in {"respond", "chime", "refuse"}:
            return ""
        gap = ts - int(previous.get("ts") or 0)
        if gap <= 0:
            return ""
        for part in messages:
            if (
                type(part).__name__ == "At"
                and str(getattr(part, "qq", "") or "") != self_id
            ):
                return ""
        if any(char in text for char in "?？") or len(text) <= CONTINUATION_MAX_CHARS:
            return f"她 {gap} 秒前刚开口，这是紧接着的一条"
        return ""

    def _message_age(self, event: AstrMessageEvent) -> float | None:
        """How long ago the platform says this message was actually sent.

        解冻积压靠这个区分：事件到达时间都一样，只有原始发送时间能看出它是旧消息。

        Args:
            event: 消息事件。

        Returns:
            秒数；平台没给原始时间时返回 None。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            sent_at = raw.get("time")
        elif raw is not None:
            sent_at = getattr(raw, "time", None)
        else:
            return None
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
            return None
        if sent_at <= 0:
            return None
        return max(0.0, time.time() - float(sent_at))

    def _resolve_uid(self, target: str) -> str:
        """把用户填的 QQ 号或昵称解析成 uid。

        Args:
            target: 用户填的内容（QQ 号或昵称）。

        Returns:
            uid；解析不出来时返回空串。
        """
        text = str(target or "").strip().strip("<>「」\"'")
        if text.isdigit():
            return text
        if not text:
            return ""
        row = self._db.execute(
            "SELECT uid FROM persons WHERE nickname = ? OR aliases LIKE ? LIMIT 1",
            (text, f"%{text}%"),
        ).fetchone()
        return str(row["uid"]) if row else ""

    def _nickname_of(self, uid: str) -> str:
        """Look up a nickname for display.

        Args:
            uid: Person id.

        Returns:
            The nickname, or an empty string.
        """
        row = self._db.execute(
            "SELECT nickname FROM persons WHERE uid = ?", (str(uid),)
        ).fetchone()
        return str(row["nickname"]) if row and row["nickname"] else ""

    def _opportunity(
        self,
        event: AstrMessageEvent,
        snapshot,
        umo: str,
        group_id: str,
        text: str,
        ts: int,
    ) -> dict:
        """算这一条消息的「机会点」（L2）：像你这样的真人此刻会不会接话。

        特征全部是能当场算出来的东西：上一条有没有 @ 人、像不像提问、是不是玩梗、
        带没带图、是不是很短；群里是不是在刷屏；话题有没有落在学习对象的关键词上；
        她是不是刚说过话；是不是深夜。

        Args:
            event: 消息事件（要它的消息链来判断有没有图）。
            snapshot: 世界状态快照（取"她上次说话多久之前"）。
            umo: 会话标识（统计刷屏用）。
            group_id: 群号（取这个群的风格卡片）。
            text: 这条消息的文本。
            ts: 当前时间戳。

        Returns:
            ``{"p", "lift", "top", "base_rate", "features"}``。
        """
        card = style.card(group_id)
        recent = world.recent_message_count(self._db, umo, ts, 60)
        kinds = {type(part).__name__ for part in (event.get_messages() or [])}
        values = chance.features(
            text=text,
            has_image=bool(kinds & {"Image", "Face", "Emoji"}),
            has_at="At" in kinds,
            flood=recent >= 3,
            seconds_since_bot=snapshot.seconds_since_bot,
            is_night=bool(snapshot.clock.is_night),
            keywords=list(card.get("keywords") or []),
        )
        result = chance.score(values)
        result["base_rate"] = chance.load_weights()["base_rate"]
        result["features"] = values
        return result

    def _chime_context(self, event: AstrMessageEvent, text: str) -> str:
        """拼出一小段"刚才群里在说什么"，用来挑风格示例。

        插嘴时她知道的不只是眼前这条消息，还有前几条——挑示例时把这几条一起拿去比，
        挑出来的"本群真人原话"才更贴题。数据来自 `turns` 里这个群最近的决策行
        （只有时间与 uid，没有原文），所以原文部分只能拼眼前这一条加上发言人昵称。

        Args:
            event: 消息事件。
            text: 本条消息文本。

        Returns:
            用于相似度比较的上下文文本。
        """
        group_id = str(event.get_group_id() or "")
        recent = world.recent_messages(self._db, group_id, limit=4) if group_id else []
        names = []
        for row in recent:
            person = world.load_person(self._db, str(row.get("uid") or ""))
            if person and person.nickname:
                names.append(person.nickname)
        speaker = str(event.get_sender_name() or "")
        parts = [*names[-3:], speaker, text]
        return " ".join(part for part in parts if part)

    def _resolve_person(self, text: str, uid: str, group_id: str) -> tuple[str, str]:
        """把"某人：内容"里的某人解析成 uid，并把内容剥出来。

        Args:
            text: 模型给的文本。
            uid: 当前发言人。
            group_id: 当前群号。

        Returns:
            (目标 uid, 内容)。
        """
        cleaned = text.strip()
        name = ""
        if "：" in cleaned:
            head, _, tail = cleaned.partition("：")
            if 0 < len(head.strip()) <= 12:
                name, cleaned = head.strip(), tail.strip()
        if not name:
            return uid, cleaned
        row = self._db.execute(
            "SELECT uid FROM persons WHERE nickname = ? OR aliases LIKE ? LIMIT 1",
            (name, f"%{name}%"),
        ).fetchone()
        if row:
            return str(row["uid"]), cleaned
        logger.info(f"qingyu_core: 记忆里的「{name}」在库里对不上，记到发言人身上")
        return uid, cleaned

    async def _run_extraction(self, group_id: str, umo: str) -> tuple[int, str]:
        """读群历史、让模型抽旧事、写进低置信库。

        Args:
            group_id: 群号。
            umo: 会话标识，用来取当前模型。

        Returns:
            (新增条数, 说明文本)。
        """
        lines = nightly.load_transcript(group_id)
        if len(lines) < 20:
            return 0, f"记录太少（{len(lines)} 条），先攒攒再抽。"
        provider = self.context.get_using_provider(umo)
        if provider is None:
            return 0, "没有可用的模型。"
        people = self._db.execute("SELECT uid, nickname FROM persons").fetchall()
        names = nightly.name_map([(row["uid"], row["nickname"]) for row in people])
        # 抽取结果里只有昵称，用群历史里的 昵称->ID 映射补齐。
        for nickname, uid in nightly.name_map_from_history(group_id).items():
            names.setdefault(nickname, uid)
        try:
            response = await provider.text_chat(
                prompt=nightly.build_prompt(lines),
                system_prompt=nightly.EXTRACT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - 抽取失败不影响使用
            logger.error(f"qingyu_core: 抽取失败: {type(exc).__name__}: {exc}")
            return 0, f"模型调用失败（{type(exc).__name__}）。"
        reply = (getattr(response, "completion_text", "") or "").strip()
        if not reply or reply.startswith("无"):
            return 0, "今天没有值得记的东西。"
        drafts = memory.parse_extraction(reply, names)
        if len(drafts) > nightly.MAX_EXTRACT_PER_DAY:
            logger.info(
                f"qingyu_core: 模型给了 {len(drafts)} 条，只取前 "
                f"{nightly.MAX_EXTRACT_PER_DAY} 条",
            )
            drafts = drafts[: nightly.MAX_EXTRACT_PER_DAY]
        saved = 0
        stored: list[str] = []
        for draft in drafts:
            if draft["uid"]:
                memory_id = memory.remember(
                    self._db,
                    uid=draft["uid"],
                    group_id=group_id,
                    text=draft["text"],
                    kind=draft["kind"],
                    confidence=draft["confidence"],
                    source="nightly",
                    related_uid=draft.get("related_uid", ""),
                )
                if memory_id:
                    saved += 1
                    other = draft.get("related_name", "")
                    who = str(draft["nickname"]) + (f" × {other}" if other else "")
                    stored.append(f"· {who}：{draft['text']}")
        # 顺手做遗忘：用过但很久没再提起的记忆自然沉底，沉到 0 就删掉。
        aged = memory.age_weight(self._db, days=1)
        pruned = memory.prune(self._db)
        logger.info(
            f"qingyu_core: 抽取 {len(lines)} 条记录 -> 新增 {saved} 条记忆"
            f"（衰减 {aged}，清理 {pruned}）",
        )
        return saved, "\n".join(stored) or "（对不上人，没记）"

    async def terminate(self) -> None:
        """Stop background work before closing the database on plugin unload."""
        self._closing = True
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._extracting.clear()
        # Cancelling to_thread does not stop its worker; wait for its file writes
        # before a replacement plugin instance starts rebuilding the same cards.
        await asyncio.shield(self._style_task)
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 - 关闭失败无所谓
            pass
