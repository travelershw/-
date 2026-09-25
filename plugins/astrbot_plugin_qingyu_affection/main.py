"""轻语的关系系统：好感（affection）+ 信任（trust），并把它喂回人格。

为什么从"一个好感度数字"改成两个量（2026-09-25）：

- **好感**回答"喜不喜欢你"——涨得慢、门槛高（只有明确夸奖/道谢/关心才动），这是原来的行为；
- **信任**回答"敢不敢对你放松"——**伤害更快（-3）、修复更慢（+1）**，道歉只回一半。
  真人关系就是不对称的：被冒犯一次要缓很久，而缓过来也回不到从前那么快。
  `qingyu_core` 的表里本来就有 `trust` 列，但一直恒为 60（没人填），这里把它填起来。

存储仍是 `plugin_data/qingyu_affection.json`，但结构从 `{"uid": 63}` 升级为
`{"uid": {"affection": 63, "trust": 60, "last_day": "...", "day_gain": 1, "history": [...]}}`，
**旧格式会自动迁移**（读到整数就当 affection，其余取默认值）。

写入方只有这里（`qingyu_core` 只读，并在每条消息时现读，避免"刚夸完语气还是旧的"）。
`familiarity`（熟悉）由 `qingyu_core` 自己维护（每条消息 +1），本插件不碰，免得两个写入方打架。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

INITIAL_AFFECTION = 60
INITIAL_TRUST = 60
# 好感涨得慢一点：单次最多 ±2，且只有明显的态度变化才调整。
MAX_STEP = 2
# 信任的不对称：伤得更快、修得更慢（这是"真实"的核心之一）。
MAX_TRUST_DOWN = 3
MAX_TRUST_UP = 1
# 自动加分（不需要模型判断的"陪伴"信号）：
DAILY_BONUS = 1  # 每天第一次正经聊天
DAILY_BONUS_CAP = 2  # 每天自动加分最多这么多（深夜陪伴也算在里面）
LATE_NIGHT_FROM = 0  # 深夜陪伴时段（小时）
LATE_NIGHT_TO = 5
# 变化理由只留最近几条，`/好感度` 里给用户看"为什么"。
HISTORY_LIMIT = 3
STORE_PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_affection.json"

# (lowest score of the band, band name, tone directive written into the prompt)
BANDS: tuple[tuple[int, str, str], ...] = (
    (80, "亲昵", "语气很亲昵，会主动关心对方、撒点小娇、开熟络的玩笑"),
    (60, "亲近", "语气轻快亲近，像关系不错的朋友，会主动打趣、愿意多聊两句"),
    (40, "普通", "语气自然友好，礼貌但不过分热络，回应简洁"),
    (20, "疏远", "语气客气而冷淡，回应简短，不太主动接话"),
    (0, "戒备", "语气明显冷淡、有距离感，能少说就少说"),
)
# 信任低的时候：不许调侃、不许开玩笑——这是"敢不敢放松"的直接体现。
TRUST_GUARD = 45
TRUST_DIRECTIVES = {
    True: "另外你的信任感偏低（刚有过不愉快或还没缓过来）：**不要调侃、不要开玩笑**，"
    "回应可以正常但收着一点，别提这件事本身。",
    False: "",
}


@register(
    "qingyu_affection",
    "migration",
    "轻语关系系统：好感 + 信任（伤害更快、修复更慢），并按两者切换回答语气",
    "1.1.0",
)
class QingyuAffectionPlugin(Star):
    """Tracks and applies the affection/trust level of each person."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._people: dict[str, dict] = self._load()
        self._tool_checked = False

    # ------------------------------------------------------------------ 读写

    def _blank(self) -> dict:
        """A fresh record for someone we have not met.

        Returns:
            The default record.
        """
        return {
            "affection": INITIAL_AFFECTION,
            "trust": INITIAL_TRUST,
            "last_day": "",
            "day_gain": 0,
            "history": [],
        }

    @staticmethod
    def _coerce(uid: str, raw) -> dict:  # noqa: ANN001, ARG004 - 旧格式迁移
        """Turn whatever is on disk into a record.

        Args:
            uid: Person id (unused except for readability).
            raw: Stored value: an int (old format) or a dict (new format).

        Returns:
            A complete record.
        """
        record = {
            "affection": INITIAL_AFFECTION,
            "trust": INITIAL_TRUST,
            "last_day": "",
            "day_gain": 0,
            "history": [],
        }
        if isinstance(raw, bool):
            return record
        if isinstance(raw, int | float):  # 旧格式：就是一个分数
            record["affection"] = int(raw)
            return record
        if not isinstance(raw, dict):
            return record
        for key in ("affection", "trust"):
            value = raw.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                record[key] = int(value)
        if isinstance(raw.get("last_day"), str):
            record["last_day"] = raw["last_day"]
        if isinstance(raw.get("day_gain"), int | float):
            record["day_gain"] = int(raw["day_gain"])
        history = raw.get("history")
        if isinstance(history, list):
            record["history"] = [item for item in history if isinstance(item, dict)][
                -HISTORY_LIMIT:
            ]
        return record

    def _person(self, sender: str) -> dict:
        """Fetch (or create) one person's record.

        Args:
            sender: Sender id.

        Returns:
            The mutable record.
        """
        key = str(sender)
        record = self._people.get(key)
        if record is None:
            record = self._blank()
            self._people[key] = record
        return record

    def _load(self) -> dict[str, dict]:
        """Read the store, migrating any old-format entries.

        Returns:
            Mapping of sender id to record; empty when the file is missing or bad.
        """
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        people: dict[str, dict] = {}
        for key, value in raw.items():
            record = self._coerce(str(key), value)
            record["affection"] = max(0, min(100, record["affection"]))
            record["trust"] = max(0, min(100, record["trust"]))
            people[str(key)] = record
        return people

    def _save(self) -> None:
        """Write the store to disk, replacing the file atomically."""
        try:
            STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = STORE_PATH.with_suffix(".json.tmp")
            temp_path.write_text(
                json.dumps(self._people, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(STORE_PATH)
        except OSError as exc:
            logger.error(f"qingyu_affection: failed to save scores: {exc}")

    def _note(self, record: dict, what: str, delta: int, reason: str) -> None:
        """Append one change to the record's history (kept short).

        Args:
            record: The person record.
            what: ``affection`` or ``trust``.
            delta: Applied change.
            reason: Why it changed.
        """
        history = record.setdefault("history", [])
        history.append(
            {
                "ts": int(time.time()),
                "what": what,
                "delta": int(delta),
                "reason": str(reason or "")[:60],
            }
        )
        del history[:-HISTORY_LIMIT]

    def _apply(self, sender: str, what: str, step: int, reason: str) -> int:
        """Apply one clamped change, record it, and persist.

        Args:
            sender: Sender id.
            what: ``affection`` or ``trust``.
            step: Already clamped change.
            reason: Why it changed.

        Returns:
            The new value.
        """
        record = self._person(sender)
        value = max(0, min(100, int(record.get(what, INITIAL_AFFECTION)) + step))
        record[what] = value
        self._note(record, what, step, reason)
        self._save()
        return value

    def _band(self, score: int) -> tuple[int, str, str]:
        """Return the band tuple that matches a score.

        Args:
            score: Current affection score.

        Returns:
            The matching (lowest score, band name, tone directive) tuple.
        """
        for entry in BANDS:
            if score >= entry[0]:
                return entry
        return BANDS[-1]

    def _score(self, sender: str) -> int:
        """Return a person's affection score.

        Args:
            sender: Sender id of the person.

        Returns:
            The stored score, or the initial value when unknown.
        """
        return int(self._person(sender).get("affection", INITIAL_AFFECTION))

    def _trust(self, sender: str) -> int:
        """Return a person's trust score.

        Args:
            sender: Sender id of the person.

        Returns:
            The stored trust, or the initial value when unknown.
        """
        return int(self._person(sender).get("trust", INITIAL_TRUST))

    # ------------------------------------------------------------------ 注入

    @filter.on_llm_request()
    async def inject_affection(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Append the current relationship and its tone directive.

        Args:
            event: The message event that triggered this LLM request.
            req: The provider request whose user content will be extended.
        """
        sender = event.get_sender_id()
        score = self._score(sender)
        trust = self._trust(sender)
        _, band, directive = self._band(score)
        who = event.get_sender_name() or sender
        # The values and directive change every turn, so they go to the tail of the
        # user content; keeping the system prompt byte-identical lets the provider
        # reuse its prefix cache for the conversation history.
        req.extra_user_content_parts.append(
            TextPart(
                text=(
                    f"\n[关系系统] 你和当前说话的人「{who}」：好感 {score}/100（{band}），"
                    f"信任 {trust}/100。本次回复的语气要求：{directive}。"
                    f"{TRUST_DIRECTIVES[trust < TRUST_GUARD]}"
                    "每轮对话都要判断对方这句话的语气，但**加分的门槛要高**："
                    "只有对方明确夸你、真诚道谢、实打实关心你、或者聊得特别投机时才加 1~2 分；"
                    "只是礼貌寒暄、随口接话、发个表情，不算，不要调用工具。"
                    "反过来，只有对方明显粗鲁、嘲讽、骂你、冒犯、命令你或无理取闹时才扣分——"
                    f"这种情况用 update_trust 扣（一次最多 -{MAX_TRUST_DOWN}），"
                    "只有确实让你不舒服时才扣，语气平淡不算。"
                    "道歉或补救用 update_trust 加回来，但**一次只能加 1**（信任回得慢）。"
                    "也就是说：绝大多数普通对话都不应该调用这两个工具。"
                    "文字回复和工具调用可以同时进行，语气要自然地融进轻语的人设，"
                    "不要说出分数，也不要提到这个系统本身；关系刚有变化时可以用一句自然的话体现出来。"
                ),
            ),
        )
        self._warn_once_if_tools_missing(req)

    def _warn_once_if_tools_missing(self, req: ProviderRequest) -> None:
        """Report once when the provider hides our tools.

        这个特性以前会**静默失效**：服务商的 modalities 里没有 ``tool_use`` 时，
        AstrBot 会把工具全丢掉，而插件只是"什么都不发生"。

        Args:
            req: The provider request to inspect.
        """
        if self._tool_checked:
            return
        self._tool_checked = True
        names = {
            str(getattr(tool, "name", ""))
            for tool in (req.func_tool.tools if req.func_tool else [])
        }
        if "update_affection" in names:
            logger.info("qingyu_affection: update_affection is available.")
        else:
            logger.warning(
                "qingyu_affection: update_affection is missing from the request, so "
                "affection cannot change. Check that the provider's modalities "
                'include "tool_use".',
            )

    # ------------------------------------------------------------------ 工具

    @filter.llm_tool(name="update_affection")
    async def update_affection(
        self,
        event: AstrMessageEvent,
        delta: int,
        reason: str,
    ) -> str:
        """根据对方这句话的语气，微调你对他的好感度。

        加分门槛较高：只有对方明确夸你、真诚道谢、实打实关心你或聊得特别投机时加 1~2 分；
        礼貌寒暄、普通接话、发表情都不算。
        好感度几乎不因"态度差"而下降——那种情况下请用 update_trust。

        Args:
            delta (int): 好感度变化量，-2 到 +2 之间的整数
            reason (str): 一句话说明为什么这样调整

        Returns:
            str: 调整结果说明，会作为工具结果返回给你
        """
        try:
            step = int(delta)
        except (TypeError, ValueError):
            return "好感度未变化：delta 必须是整数。"
        step = max(-MAX_STEP, min(MAX_STEP, step))
        if step == 0:
            return "好感度未变化。"

        sender = event.get_sender_id()
        score = self._apply(sender, "affection", step, reason)
        _, band, _ = self._band(score)
        logger.info(
            f"qingyu_affection: {sender} 好感 {step:+d} -> {score} ({band}) reason={reason}",
        )
        return f"好感度现在是 {score}/100（{band}）。"

    @filter.llm_tool(name="update_trust")
    async def update_trust(
        self,
        event: AstrMessageEvent,
        delta: int,
        reason: str,
    ) -> str:
        """根据对方是否冒犯了你，调整你对他的信任感。

        信任是**不对称**的：被冒犯时一次可扣到 -3，而道歉/补救一次最多只能加回 +1
        （信任比好感难修）。信任低的时候你会避免调侃和玩笑。

        Args:
            delta (int): 信任变化量，-3 到 +1 之间的整数
            reason (str): 一句话说明为什么这样调整

        Returns:
            str: 调整结果说明，会作为工具结果返回给你
        """
        try:
            step = int(delta)
        except (TypeError, ValueError):
            return "信任未变化：delta 必须是整数。"
        step = max(-MAX_TRUST_DOWN, min(MAX_TRUST_UP, step))
        if step == 0:
            return "信任未变化。"

        sender = event.get_sender_id()
        value = self._apply(sender, "trust", step, reason)
        logger.info(
            f"qingyu_affection: {sender} 信任 {step:+d} -> {value} reason={reason}",
        )
        return f"信任感现在是 {value}/100。"

    @filter.command("好感度")
    async def show_affection(self, event: AstrMessageEvent):
        """查看自己当前的好感度、信任感，以及最近几次变化的原因。

        Args:
            event: The command message event.

        Yields:
            The reply describing the current relationship.
        """
        sender = event.get_sender_id()
        record = self._person(sender)
        score, trust = self._score(sender), self._trust(sender)
        _, band, directive = self._band(score)
        lines = [
            f"轻语对你的好感度：{score}/100（{band}）",
            f"信任感：{trust}/100{'（偏低，我会收着点说话）' if trust < TRUST_GUARD else ''}",
            f"当前语气：{directive}",
        ]
        history = record.get("history") or []
        if history:
            lines.append("最近的变化：")
            for item in reversed(history[-HISTORY_LIMIT:]):
                when = time.strftime("%m-%d %H:%M", time.localtime(item.get("ts", 0)))
                what = "好感" if item.get("what") == "affection" else "信任"
                lines.append(
                    f"· {when} {what} {int(item.get('delta', 0)):+d}"
                    f"（{item.get('reason') or '没写原因'}）"
                )
        else:
            lines.append("（还没有变化记录——普通聊天本来就不会改分数）")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ 陪伴

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def companionship(self, event: AstrMessageEvent) -> None:
        """Award the small, automatic "you were here" points.

        这一类加分**不需要模型判断**，因为它是行为信号而不是态度评价：

        - 每天第一次正经聊天 +1（"你今天还想着我"）；
        - 深夜（0~5 点）陪着说话 +1，每天最多一次；
        - 两者合计每天不超过 ``DAILY_BONUS_CAP``，避免"多说话就涨分"的刷分感。

        Args:
            event: The incoming message event.
        """
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        sender = event.get_sender_id()
        record = self._person(sender)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if record.get("last_day") != today:
            record["last_day"] = today
            record["day_gain"] = 0
        gained = int(record.get("day_gain") or 0)
        if gained >= DAILY_BONUS_CAP:
            return
        if gained == 0:
            reason = "今天第一次正经聊天"
        elif LATE_NIGHT_FROM <= now.hour < LATE_NIGHT_TO:
            reason = "深夜还陪着我说话"
        else:
            return
        record["day_gain"] = gained + 1
        score = self._apply(sender, "affection", DAILY_BONUS, reason)
        logger.info(
            f"qingyu_affection: {sender} 好感 {DAILY_BONUS:+d} -> {score} reason={reason}（每日自动）",
        )
