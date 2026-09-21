"""DeepSeek native web search tool for AstrBot.

DeepSeek's Responses API can run a hosted web search (``tools=[{"type":
"web_search"}]``); it only works with a search-capable model (verified with
``deepseek-v4-pro``). This plugin exposes that capability as an LLM tool so the
chat model can pull in current information without a third-party search service.
"""

import datetime as dt

import httpx

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger

RESPONSES_PATH = "/responses"
SEARCH_MODEL = "deepseek-v4-pro"
MAX_ANSWER_CHARS = 1500
MAX_SOURCES = 5
REQUEST_TIMEOUT = 180.0


@register(
    "deepseek_search",
    "migration",
    "用 DeepSeek 原生 web_search（Responses API）提供联网检索工具",
    "1.0.0",
)
class DeepSeekSearchPlugin(Star):
    """Exposes DeepSeek's hosted web search as an LLM tool."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)

    def _credentials(self) -> tuple[str, str]:
        """Read the DeepSeek api base and key from the AstrBot provider config.

        The dashboard stores the key as a one-element list, so both shapes are
        accepted.

        Returns:
            A tuple of the api base (without trailing slash) and the api key.
        """
        sources = self.context.get_config().get("provider_sources", [])
        for source in sources:
            if not isinstance(source, dict):
                continue
            api_base = str(source.get("api_base", ""))
            if "deepseek" not in api_base:
                continue
            secret = source.get("key") or source.get("api_key") or ""
            if isinstance(secret, list):
                secret = secret[0] if secret else ""
            return api_base.rstrip("/"), str(secret).strip()
        return "", ""

    @filter.llm_tool(name="deepseek_web_search")
    async def deepseek_web_search(self, event: AstrMessageEvent, query: str) -> str:
        """联网检索最新信息（新闻、天气、价格、版本、赛事、实时事件等）。

        当你需要超出知识范围的实时信息时调用它。一次提问只调用一次：
        如果返回的内容不够，就基于已有信息回答或说明没查到，不要反复检索。
        返回检索要点与来源链接，你要用自己的语气转述，不要原样照抄，
        也不要在回复里输出引用标记。

        Args:
            query (str): 搜索关键词或问题，尽量写清名称与时间（例如"上海天气 今天"）

        Returns:
            str: 检索要点与来源链接；失败时返回错误说明
        """
        api_base, api_key = self._credentials()
        if not api_base or not api_key:
            return "联网搜索不可用：AstrBot 配置里找不到 DeepSeek 的 api_base 或 key。"

        today = dt.date.today().isoformat()
        payload = {
            "model": SEARCH_MODEL,
            "input": (
                f"今天是 {today}。请联网检索（最多 2 次），只依据检索到的最新内容，"
                f"用中文简明回答，并列出你使用过的来源链接：{query}"
            ),
            "tools": [{"type": "web_search"}],
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{api_base}{RESPONSES_PATH}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - report any failure to the model
            logger.error(f"deepseek_search: request failed: {type(exc).__name__}: {exc}")
            return f"联网搜索失败（{type(exc).__name__}），可以稍后再试或用已有知识回答。"

        answer = ""
        sources: list[str] = []
        searched = 0
        for item in body.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        answer += str(part.get("text", ""))
            elif item.get("type") == "web_search_call":
                searched += 1
                action = item.get("action") or {}
                url = action.get("url")
                if isinstance(url, str) and url:
                    clean = url.split("#", 1)[0]
                    if clean not in sources:
                        sources.append(clean)

        answer = answer.strip()
        if not answer:
            return "联网搜索没有返回可用内容。"
        logger.info(
            f"deepseek_search: '{query[:40]}' -> {len(answer)} chars, "
            f"{searched} search calls, {len(sources)} sources",
        )

        result = answer[:MAX_ANSWER_CHARS]
        if sources:
            result += "\n\n来源：\n" + "\n".join(f"- {url}" for url in sources[:MAX_SOURCES])
        return result
