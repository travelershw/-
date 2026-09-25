"""Affection system for the 轻语 persona.

Keeps a 0-100 affection score for every person the bot talks to, lets the model
nudge that score from the tone of the conversation through a function tool, and
feeds the current level back into the system prompt as a tone directive so the
persona answers differently at each level.

The store is a small JSON file under AstrBot's plugin data directory, so scores
survive restarts.
"""

import json
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

INITIAL_AFFECTION = 60
# 好感度涨得慢一点：单次最多 ±2，且只有明显的态度变化才调整。
MAX_STEP = 2
STORE_PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_affection.json"

# (lowest score of the band, band name, tone directive written into the prompt)
BANDS: tuple[tuple[int, str, str], ...] = (
    (80, "亲昵", "语气很亲昵，会主动关心对方、撒点小娇、开熟络的玩笑"),
    (60, "亲近", "语气轻快亲近，像关系不错的朋友，会主动打趣、愿意多聊两句"),
    (40, "普通", "语气自然友好，礼貌但不过分热络，回应简洁"),
    (20, "疏远", "语气客气而冷淡，回应简短，不太主动接话"),
    (0, "戒备", "语气明显冷淡、有距离感，能少说就少说"),
)


@register(
    "qingyu_affection",
    "migration",
    "轻语好感度系统：按说话语气在 0~100 间微调好感度，并按好感度切换回答语气",
    "1.0.0",
)
class QingyuAffectionPlugin(Star):
    """Tracks and applies the affection level of each person."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._scores: dict[str, int] = self._load()
        self._tool_checked = False

    @filter.on_llm_request()
    async def inject_affection(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Append the current affection level and its tone directive.

        Args:
            event: The message event that triggered this LLM request.
            req: The provider request whose system prompt will be extended.
        """
        sender = event.get_sender_id()
        score = self._score(sender)
        _, band, directive = self._band(score)
        who = event.get_sender_name() or sender
        # The affection level and its tone directive change every turn, so they go
        # to the tail of the user content; keeping the system prompt byte-identical
        # lets the provider reuse its prefix cache for the conversation history.
        req.extra_user_content_parts.append(
            TextPart(
                text=(
                    f"\n[好感度系统] 你和当前说话的人「{who}」的好感度是 {score}/100（{band}）。"
                    f"本次回复的语气要求：{directive}。"
                    "每轮对话都要判断对方这句话的语气，但**加分的门槛要高**："
                    "只有对方明确夸你、真诚道谢、实打实关心你、或者聊得特别投机时才加 1~2 分；"
                    "只是礼貌寒暄、随口接话、发个表情，不算，不要调用工具。"
                    "反过来，只有对方明显粗鲁、嘲讽、骂你、冒犯、命令你或无理取闹时才减 1~2 分；"
                    "稍微冷淡或语气平淡都不算。"
                    "也就是说：绝大多数普通对话都不应该调用这个工具，"
                    "只有态度确实明显时才调用，且每次最多 2 分。"
                    "文字回复和工具调用可以同时进行，语气要自然地融进轻语的人设，"
                    "不要说出好感度数字，也不要提到这个系统本身。"
                ),
            ),
        )

        # AstrBot drops every tool when the provider config lists modalities
        # without "tool_use", which silently disables this feature; report it once.
        if not self._tool_checked:
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
        对方明显粗鲁、嘲讽、冒犯、命令你、无理取闹时减 1~2 分；
        绝大多数普通对话都不应该调用本工具。

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
        score = self._apply(sender, step)
        _, band, _ = self._band(score)
        logger.info(
            f"qingyu_affection: {sender} {step:+d} -> {score} ({band}) reason={reason}",
        )
        return f"好感度现在是 {score}/100（{band}）。"

    @filter.command("好感度")
    async def show_affection(self, event: AstrMessageEvent):
        """查看自己当前的好感度。

        Args:
            event: The command message event.

        Yields:
            The reply describing the current affection level.
        """
        sender = event.get_sender_id()
        score = self._score(sender)
        _, band, directive = self._band(score)
        yield event.plain_result(
            f"轻语对你的好感度：{score}/100（{band}）\n当前语气：{directive}",
        )

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
        return self._scores.get(str(sender), INITIAL_AFFECTION)

    def _apply(self, sender: str, step: int) -> int:
        """Add a step to a person's score, clamp it, and persist the result.

        Args:
            sender: Sender id of the person.
            step: Already clamped change to apply.

        Returns:
            The new score.
        """
        score = max(0, min(100, self._score(sender) + step))
        self._scores[str(sender)] = score
        self._save()
        return score

    def _load(self) -> dict[str, int]:
        """Read the stored scores from disk.

        Returns:
            Mapping of sender id to score; empty when the file is missing or bad.
        """
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        scores: dict[str, int] = {}
        for key, value in raw.items():
            if isinstance(value, int | float):
                scores[str(key)] = max(0, min(100, int(value)))
        return scores

    def _save(self) -> None:
        """Write the scores to disk, replacing the file atomically."""
        try:
            STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = STORE_PATH.with_suffix(".json.tmp")
            temp_path.write_text(
                json.dumps(self._scores, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(STORE_PATH)
        except OSError as exc:
            logger.error(f"qingyu_affection: failed to save scores: {exc}")
