"""Group knowledge lookup tool and chat command.

The documents under ``knowledge_docs`` are compiled into
``data/plugin_data/group_knowledge.db`` (one row per section) by
``migration_tools/build_group_knowledge.py``. This plugin exposes a keyword
scoring search over those rows as an LLM tool (``knowledge_lookup``) plus a chat
command (``/知识库``) that can search the database, list it, and append new
knowledge written by an administrator.
"""

import datetime as dt
import math
import os
import re
import sqlite3
from pathlib import Path

import httpx

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DB_PATH = next(
    (
        candidate
        for candidate in (
            Path(get_astrbot_plugin_data_path()) / "group_knowledge.db",
            Path(get_astrbot_plugin_data_path()) / "group_knowledge.db",
        )
        if candidate.exists()
    ),
    Path(get_astrbot_plugin_data_path()) / "group_knowledge.db",
)
DOCS_DIR = Path(os.environ.get("QINGYU_KNOWLEDGE_DIR", str(Path(get_astrbot_plugin_data_path()) / "knowledge_docs")))
# Knowledge added from the group chat lands in this document, so a rebuild from
# markdown keeps it. The same heading split as build_group_knowledge.py applies.
EXTRA_DOC = DOCS_DIR / "群聊补充.md"
SECTION_RE = re.compile(r"^#{1,3} .*$", re.MULTILINE)
MAX_SECTIONS = 2
MAX_CHARS = 1200
# A single 2-character hit scores 4 points, so a weak coincidental overlap
# (two common bigrams inside a long unrelated section) tops out around 8.
# Requiring 16 forces at least one 4-character / ASCII-word hit, two 3-character
# hits or four 2-character hits before a section is considered relevant.
MIN_SCORE = 16
# The second section must stay in the same ballpark as the best one, otherwise
# it is filler that pollutes the answer.
SECOND_SECTION_RATIO = 0.4
# 新建小节的正文下限。重建脚本只丢掉过短的 "# " 级前言，所以这个数字只是
# 防止把"好的"这种没信息量的东西记进去，不会让已收下的内容在重建时消失。
MIN_BLOCK_CHARS = 20
MAX_ADD_CHARS = 800
# 「补充」用关键词检索时的相关度门槛：标题里没有这个关键词时，检索分数要够高
# 才认作"就是这一节"。实测贴题的关键词：学生票 280、四级考试 45、复飞 20~27；
# 噪声：显卡 14、波音737 16、考研数学 36。所以取 40，再靠"标题命中"兜住短关键词。
SUPPLEMENT_MIN_SCORE = 40
# 不带「|」时，多长以内算"关键词"（超过就当整句话是要记的内容）。
AUTO_KEYWORD_MAX_CHARS = 20
# 自动整理前先联网检索（DeepSeek 原生 web_search，只在 Responses API 上生效）。
SEARCH_MODEL = "deepseek-v4-pro"
RESPONSES_PATH = "/responses"
SEARCH_TIMEOUT = 120.0
MAX_MATERIAL_CHARS = 2000
MAX_SOURCES = 2
# 子命令别名。/知识库 后面先按空格切，切不开再按这些词做前缀匹配，
# 这样「/知识库 补充线性代数」也能认。
QUERY_WORDS = {"查询", "查", "搜索", "搜", "找"}
ADD_WORDS = {"添加", "写入", "加", "记", "记住", "学"}
LIST_WORDS = {"列表", "目录", "清单"}
SUPPLEMENT_WORDS = {"补充", "补", "追加", "并入", "搜补", "学一下"}
ACTION_WORDS = QUERY_WORDS | ADD_WORDS | LIST_WORDS | SUPPLEMENT_WORDS
# 自动整理条目用的提示词（只有管理员发「/知识库 补充 <关键词>」不带内容时才用）。
DRAFT_SYSTEM_PROMPT = (
    "你在给一个 QQ 群的知识库写条目。输出要求："
    "第一行是不超过 12 个字的条目标题；从第二行开始，每行写一条事实，3~6 行，总共 120~300 字。"
    "中文，只写你有把握的内容，平实直接，不要客套、不要称呼、不要解释你在做什么、"
    "不要用 Markdown 标题或代码块、不要编造具体数字和人名。"
    "如果用户消息里给了联网资料，就以资料为准，不要加入资料里没有的具体数字或日期；"
    "资料和你的常识冲突时以资料为准。"
    "不确定的地方后面加（不确定）；会随时间变的信息（报名时间、价格、政策）后面加（以最新公告为准）。"
)
USAGE = (
    "用法：\n"
    "· /知识库 查询 <关键词>　检索本群知识库\n"
    "· /知识库 列表　列出知识库里的小节\n"
    "· /知识库 <关键词>　等同于查询\n"
    "· /知识库 添加 <标题> | <内容>　（仅管理员）标题和已有小节同名就并进去，否则新建一节\n"
    "· /知识库 补充 <关键词> | <内容>　（仅管理员）用关键词检索出对应小节，把内容并进去\n"
    "· /知识库 补充 <关键词>　（仅管理员）不带内容时，轻语会自己整理一条并放进对应小节"
)
STOP_CHARS = "的了是我你他她它们在有和与及就都而也不很这那请问一下怎么什么吗呢吧啊呀哦嘛"


def build_terms(query: str) -> list[str]:
    """Turn a query into search terms: whole ASCII tokens plus Chinese n-grams.

    ASCII runs (``A320``, ``taxi``, ``737``) are kept whole so that random digit
    fragments cannot match QQ numbers or dates elsewhere in the corpus. Chinese
    text is cut at stop characters first, then expanded into 2-4 character
    n-grams inside each remaining segment, so no n-gram straddles a filler word.

    Args:
        query: Raw user query.

    Returns:
        Deduplicated search terms.
    """
    terms: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9_.+-]+|[^A-Za-z0-9_.+-]+", query):
        if re.fullmatch(r"[A-Za-z0-9_.+-]{2,}", chunk):
            terms.append(chunk.lower())
            continue
        for segment in re.split(rf"[{STOP_CHARS}\W_]+", chunk):
            for size in (4, 3, 2):
                for start in range(0, max(0, len(segment) - size + 1)):
                    terms.append(segment[start : start + size])
    seen: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.append(term)
    return seen[:40]


@register(
    "group_knowledge",
    "migration",
    "群聊知识库：检索工具 knowledge_lookup + 群聊指令 /知识库（查询、列表、添加、补充、联网自动整理）",
    "1.5.0",
)
class GroupKnowledgePlugin(Star):
    """Keyword search over the compiled group knowledge database."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)

    def _search(
        self,
        query: str,
        min_score: float = MIN_SCORE,
        ratio: float = SECOND_SECTION_RATIO,
    ) -> list[tuple[float, str, str, str]]:
        """按相关度检索知识小节。

        Args:
            query: 查询语句。
            min_score: 相关度门槛，低于它的小节不算命中。
            ratio: 次要命中相对首选小节的最低比例。

        Returns:
            按相关度降序排列的 (score, topic, source, content)；无命中时为空列表。
        """
        terms = build_terms(query)
        if not terms or not DB_PATH.exists():
            return []

        connection = None
        try:
            connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            rows = connection.execute(
                "SELECT topic, source, content FROM knowledge",
            ).fetchall()
        except sqlite3.Error as exc:
            logger.error(f"group_knowledge: query failed: {exc}")
            return []
        finally:
            if connection is not None:
                connection.close()

        # Rare terms carry the topic; terms that appear everywhere are noise.
        lowered_rows = [
            (topic, source, content, content.lower())
            for topic, source, content in rows
        ]
        idf: dict[str, float] = {}
        for term in terms:
            df = sum(1 for _t, _s, _c, low in lowered_rows if term in low)
            idf[term] = math.log(1 + len(lowered_rows) / max(df, 1))

        scored: list[tuple[float, str, str, str]] = []
        for topic, source, content, lowered in lowered_rows:
            score = 0.0
            for term in terms:
                hits = lowered.count(term)
                if hits:
                    score += hits * (len(term) ** 2) * idf[term]
            if score >= min_score:
                scored.append((score, topic, source, content))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            cutoff = max(min_score, scored[0][0] * ratio)
            scored = [item for item in scored if item[0] >= cutoff]
        return scored

    @filter.llm_tool(name="knowledge_lookup")
    async def knowledge_lookup(self, event: AstrMessageEvent, query: str) -> str:
        """查询本群专属的知识库（群内成员与话题、轻语自己的功能与好感度规则、
        AI 与大模型常识、A320 正常检查单与复飞记忆项目、教材购买与校园事务、
        12306 学生票规则）。

        凡是涉及这些主题的问题，**先调用本工具查证再回答**，不要凭印象作答、
        也不要因为"不确定"就直接拒绝。查到内容就按里面的格式具体给出
        （例如被要求背检查单就给完整条目），最后用一句话说明适用范围。
        如果本工具回答"知识库里没有相关内容"，就照实告诉对方本群知识库不覆盖，
        并说明原因（例如该机型/该阶段检查单没有收录），不要拿别的资料顶上。

        Args:
            query (str): 要查的主题或问题关键词，例如"好感度怎么算""复飞记忆项目"

        Returns:
            str: 命中的知识小节原文；没有命中时返回提示
        """
        if not DB_PATH.exists():
            logger.warning(f"group_knowledge: database missing at {DB_PATH}")
            return "知识库暂时不可用（数据库文件不存在）。"

        picked = self._search(query)[:MAX_SECTIONS]
        if not picked:
            logger.info(
                f"group_knowledge: '{query[:40]}' -> no section above "
                f"MIN_SCORE={MIN_SCORE}",
            )
            return (
                f"知识库里没有与「{query}」相关的内容（相关度过低）。"
                "请直接说明本群知识库不覆盖这一点，不要凭印象编内容。"
            )

        logger.info(
            f"group_knowledge: '{query[:30]}' -> "
            f"top='{picked[0][1]}' score={round(picked[0][0], 1)}",
        )
        blocks = [
            f"【{topic}】（来自 {source}，相关度 {round(score)}）\n{content}"
            for score, topic, source, content in picked
        ]
        return "\n\n".join(blocks)[:MAX_CHARS]

    @filter.command("知识库")
    async def knowledge_command(self, event: AstrMessageEvent):
        """群聊指令 /知识库：检索知识库、列出小节、把新知识加进知识库。

        用法见 ``USAGE``；添加只对管理员开放，查询与列表所有人可用。

        Args:
            event: 指令消息事件。

        Yields:
            指令的回复文本。
        """
        body = (event.message_str or "").strip().lstrip("/／!！.。")
        if body.startswith("知识库"):
            body = body[len("知识库") :].strip()
        action, _, argument = body.partition(" ")
        action, argument = action.strip(), argument.strip()
        # 群里常见写法是「补充<线性代数>｜」这种没有空格的，按子命令前缀再切一次。
        if action not in ACTION_WORDS:
            for word in sorted(ACTION_WORDS, key=len, reverse=True):
                if body.startswith(word):
                    action, argument = word, body[len(word) :].strip()
                    break

        if action in LIST_WORDS:
            yield event.plain_result(self._list_text())
            return
        if action in ADD_WORDS:
            text = self._add_knowledge(event, argument)
            logger.info(
                f"group_knowledge: add by {event.get_sender_id()} -> {text[:80]}",
            )
            yield event.plain_result(text)
            return
        if action in SUPPLEMENT_WORDS:
            text = await self._supplement_knowledge(event, argument)
            logger.info(
                f"group_knowledge: supplement by {event.get_sender_id()} "
                f"-> {text[:80]}",
            )
            yield event.plain_result(text)
            return

        query = argument if action in QUERY_WORDS else body
        if not query:
            yield event.plain_result(USAGE)
            return
        picked = self._search(query)[:MAX_SECTIONS]
        if not picked:
            yield event.plain_result(f"知识库里没有关于「{query}」的内容。")
            return
        lines = [
            f"【{topic}】（来自 {source}，相关度 {round(score)}）\n{content}"
            for score, topic, source, content in picked
        ]
        yield event.plain_result("\n\n".join(lines)[:MAX_CHARS])

    def _list_text(self) -> str:
        """列出知识库里的所有小节，按来源文档分组。

        Returns:
            给群聊看的清单文本。
        """
        if not DB_PATH.exists():
            return "知识库数据库不存在，先运行一次 build_group_knowledge.py。"
        connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT source, topic FROM knowledge ORDER BY source, id",
            ).fetchall()
        finally:
            connection.close()

        grouped: dict[str, list[str]] = {}
        for source, topic in rows:
            grouped.setdefault(source, []).append(topic)
        lines = [f"知识库共 {len(rows)} 节："]
        for source, topics in grouped.items():
            lines.append(f"【{source}】{len(topics)} 节\n　" + "、".join(topics))
        lines.append("查某节内容：/知识库 <关键词>；补充内容：/知识库 添加 <标题> | <内容>")
        return "\n".join(lines)[:MAX_CHARS]

    def _add_knowledge(self, event: AstrMessageEvent, argument: str) -> str:
        """把管理员给的内容写进知识库（并入同名小节或新建一节）。

        Args:
            event: 指令消息事件，用来判断权限。
            argument: ``标题 | 内容``，只写内容时标题自动截取。

        Returns:
            给群聊看的执行结果。
        """
        if not event.is_admin():
            return "添加知识库是管理员指令哦~ 想查的话直接：/知识库 <关键词>"
        if not DB_PATH.exists():
            return "知识库数据库不存在，先运行一次 build_group_knowledge.py。"

        title, separator, content = argument.partition("|")
        if separator:
            title, content = title.strip(), content.strip()
        else:
            title, content = "", argument.strip()
        content = content[:MAX_ADD_CHARS]
        if not content:
            return "用法：/知识库 添加 <标题> | <内容>\n例如：/知识库 添加 学生票 | 新生凭录取通知书当年开学前可买 1 次单程"
        if not title:
            title = content[:15].strip()

        bullet = f"- {content}"
        target = self._match_section(title)
        if target is not None:
            topic, source, section = target
            if bullet in section:
                return f"《{topic}》里已经有这条内容了，不用重复记~"
            if not self._append_to_section(topic, source, bullet):
                return f"找到了小节《{topic}》，但写入 {source} 失败（文件缺失或被占用）。"
            return f"已并入《{topic}》（来自 {source}）。"

        block = f"## {title}\n{bullet}"
        if len(block) < MIN_BLOCK_CHARS:
            return f"内容太短了（建议至少 {MIN_BLOCK_CHARS} 字），写具体一点我才记得住。"
        if not self._create_section(title, block):
            return f"写入 {EXTRA_DOC.name} 失败（目录不存在或被占用）。"
        return (
            f"已新建小节《{title}》，写入 {EXTRA_DOC.name}。"
            "以后有人问相关内容我就会查到啦~"
        )

    async def _supplement_knowledge(self, event: AstrMessageEvent, argument: str) -> str:
        """用关键词检索出对应小节，把内容并进去；没有对应小节就新建一节。

        两种用法：
        - ``关键词 | 内容``：用关键词找到该去的小节，把给的内容并进去；
        - 只给 ``关键词``：先让模型整理一条关于它的条目，再按同样的规则放进去。

        选小节的顺序：标题里就有这个关键词 → 检索相关度够高 → 都没有就新建。

        Args:
            event: 指令消息事件，用来判断权限。
            argument: 关键词，或 ``关键词 | 内容``。

        Returns:
            给群聊看的执行结果。
        """
        if not event.is_admin():
            return "补充知识库是管理员指令哦~ 想查的话直接：/知识库 <关键词>"
        if not DB_PATH.exists():
            return "知识库数据库不存在，先运行一次 build_group_knowledge.py。"

        keyword, separator, content = argument.replace("｜", "|").partition("|")
        keyword = keyword.strip().strip("<>＜＞《》【】「」“”\"'").strip()
        content = content.strip()[:MAX_ADD_CHARS]
        if not keyword:
            return (
                "用法：/知识库 补充 <关键词> | <内容>\n"
                "只写关键词也行，我会自己整理一条：/知识库 补充 线性代数"
            )

        # 没写「|」、给的又是一整句话时，当成要记的内容收下；只有短词才去自动整理。
        drafted = False
        searched = False
        sources: list[str] = []
        if (
            not content
            and not separator
            and len(keyword) > AUTO_KEYWORD_MAX_CHARS
        ):
            content, keyword = keyword[:MAX_ADD_CHARS], keyword[:15]
        if not content:
            draft = await self._draft_entry(event, keyword)
            if draft is None:
                return (
                    f"想自己整理「{keyword}」这条，但没拿到可用的模型回复，"
                    f"可以先手动写：/知识库 补充 {keyword} | 你要记的内容"
                )
            title, content, sources, searched = draft
            drafted = True

        if drafted:
            lines = [line for line in content.splitlines() if line.strip()]
            marker = (
                "- （轻语联网检索后整理，未人工核对）"
                if searched
                else "- （本条目由轻语自动整理，未经人工核对）"
            )
            bullet = "\n".join(
                [
                    marker,
                    *(f"- {line}" for line in lines),
                    *(f"- 来源：{url}" for url in sources),
                ],
            )
        else:
            bullet = f"- {content}"

        # 1) 标题里直接带这个关键词的，最可信（同名优先）。
        target = self._match_section(keyword)
        reason = f"标题命中《{target[0]}》" if target else ""
        # 2) 标题对不上，就用关键词检索，相关度够高才认。
        if target is None:
            hits = self._search(keyword, min_score=SUPPLEMENT_MIN_SCORE, ratio=0.0)
            if hits:
                score, topic, source, section = hits[0]
                target = (topic, source, section)
                reason = f"检索到《{topic}》相关度 {round(score)}"

        prefix = (
            "轻语联网查了一下并整理出一条" if searched
            else "轻语自动整理了一条" if drafted
            else ""
        )
        if target is not None:
            topic, source, section = target
            if bullet in section:
                return f"《{topic}》里已经有这条内容了（{reason}），不用重复记~"
            if not self._append_to_section(topic, source, bullet):
                return f"找到了小节《{topic}》，但写入 {source} 失败（文件缺失或被占用）。"
            head = (
                f"{prefix}并补充到《{topic}》"
                if drafted
                else f"已补充到《{topic}》"
            )
            body = f"\n{content[:400]}" if drafted else ""
            return f"{head}（来自 {source}，{reason}）。{body}"

        title = keyword[:15].strip()
        block = f"## {title}\n{bullet}"
        if len(block) < MIN_BLOCK_CHARS:
            return (
                f"没检索到相关小节，而且内容太短（至少 {MIN_BLOCK_CHARS} 字）没法新建一节，"
                "写具体一点再试~"
            )
        if not self._create_section(title, block):
            return f"写入 {EXTRA_DOC.name} 失败（目录不存在或被占用）。"
        head = (
            f"{prefix}，但库里没有相关小节，已新建《{title}》"
            if drafted
            else f"没检索到相关小节，已按关键词新建《{title}》"
        )
        body = f"\n{content[:400]}" if drafted else ""
        return f"{head}，写入 {EXTRA_DOC.name}。{body}"

    async def _draft_entry(
        self,
        event: AstrMessageEvent,
        keyword: str,
    ) -> tuple[str, str, list[str], bool] | None:
        """联网查一遍，再让模型就某个关键词整理一条知识库条目。

        Args:
            event: 指令消息事件，用来取该会话正在用的模型。
            keyword: 管理员给的关键词。

        Returns:
            (标题, 正文, 来源链接, 是否联网检索过)；拿不到可用回复时返回 None。
        """
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            logger.warning("group_knowledge: 没有可用的模型，无法自动整理条目")
            return None

        material, sources = await self._web_search(keyword)
        prompt = f"请就「{keyword[:60]}」写一条知识库条目。"
        if material:
            prompt += f"\n\n联网检索到的资料（请以此为准）：\n{material}"
        try:
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=DRAFT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - 供应商异常类型不固定
            logger.error(f"group_knowledge: draft failed: {type(exc).__name__}: {exc}")
            return None
        text = (getattr(response, "completion_text", "") or "").strip()
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = lines[0].lstrip("# ・-—*").strip()[:15] or keyword[:15]
        body = "\n".join(lines[1:]).strip() or text
        logger.info(
            f"group_knowledge: drafted '{keyword[:20]}' -> 《{title}》"
            f"({len(body)} 字符, 联网资料 {len(material)} 字符, {len(sources)} 来源)",
        )
        return title, body[:MAX_ADD_CHARS], sources, bool(material)

    async def _web_search(self, keyword: str) -> tuple[str, list[str]]:
        """用 DeepSeek 原生 web_search 检索与关键词相关的资料。

        这一步只是为了给整理提供最新事实，失败不影响流程（退回模型自身知识）。

        Args:
            keyword: 管理员给的关键词。

        Returns:
            (检索要点, 来源链接)；没有可用结果时返回 ("", [])。
        """
        api_base, api_key = self._credentials()
        if not api_base or not api_key:
            logger.warning("group_knowledge: 配置里没有 DeepSeek api_base/key，跳过联网")
            return "", []

        payload = {
            "model": SEARCH_MODEL,
            "input": (
                f"今天是 {dt.date.today().isoformat()}。请联网检索（最多 2 次）关于"
                f"「{keyword[:60]}」的可核实信息，用中文每条一行列出要点，"
                "最后列出你用过的来源链接。检索不到就直接说没有找到。"
            ),
            "tools": [{"type": "web_search"}],
        }
        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
                response = await client.post(
                    f"{api_base}{RESPONSES_PATH}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - 检索失败就退回模型知识
            logger.error(f"group_knowledge: web search failed: {type(exc).__name__}: {exc}")
            return "", []

        answer = ""
        sources: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict):
                        answer += str(part.get("text", ""))
            elif item.get("type") == "web_search_call":
                url = (item.get("action") or {}).get("url")
                if isinstance(url, str) and url:
                    clean = url.split("#", 1)[0]
                    if clean not in sources:
                        sources.append(clean)

        answer = answer.strip()[:MAX_MATERIAL_CHARS]
        if not sources:
            # 有些检索只返回 search 动作（没有 open_page），来源就写在正文里。
            for url in re.findall(r"https?://[^\s，。、；：）)】\]]+", answer):
                clean = url.split("#", 1)[0].rstrip(".,;:")
                if clean and clean not in sources:
                    sources.append(clean)
        logger.info(
            f"group_knowledge: web search '{keyword[:30]}' -> {len(answer)} 字符, "
            f"{len(sources)} 来源",
        )
        return answer, sources[:MAX_SOURCES]

    def _credentials(self) -> tuple[str, str]:
        """从 AstrBot 的 provider 配置里读 DeepSeek 的 api_base 与 key。

        面板把 key 存成一元列表，两种形状都接受。

        Returns:
            (api_base 去掉末尾斜杠, api_key)；没找到时返回两个空串。
        """
        get_config = getattr(self.context, "get_config", None)
        if get_config is None:
            return "", ""
        try:
            sources = get_config().get("provider_sources", [])
        except Exception as exc:  # noqa: BLE001 - 读配置失败就当作没有联网能力
            logger.warning(f"group_knowledge: 读 provider_sources 失败: {exc}")
            return "", ""
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

    def _match_section(self, title: str) -> tuple[str, str, str] | None:
        """按标题找要并入的小节：同名或互相包含即视为同一节。

        多个小节都沾边时，先要完全同名，否则取内容相关度最高的那个
        （标题里带关键词的常常不止一节，例如"复飞"能对上三节）。

        Args:
            title: 管理员给的标题或关键词。

        Returns:
            (topic, source, content)；没有匹配的小节时返回 None。
        """
        wanted = re.sub(r"\s+", "", title)
        if not wanted:
            return None
        candidates = []
        for _score, topic, source, content in self._search(
            title,
            min_score=0.0,
            ratio=0.0,
        ):
            plain = re.sub(r"\s+", "", topic)
            if wanted == plain:
                return (topic, source, content)
            if wanted in plain or plain in wanted:
                candidates.append((topic, source, content))
        return candidates[0] if candidates else None

    def _append_to_section(self, topic: str, source: str, bullet: str) -> bool:
        """把一条内容追加到已有小节的末尾，并同步数据库。

        Args:
            topic: 小节标题（等于 Markdown 标题行去掉 # 后的文字）。
            source: 小节所在的 Markdown 文件名。
            bullet: 要追加的 Markdown 行。

        Returns:
            写入是否成功。
        """
        path = DOCS_DIR / source
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        matches = list(SECTION_RE.finditer(text))
        for index, match in enumerate(matches):
            if match.group(0).lstrip("# ").strip() != topic:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            updated = (
                text[:end].rstrip("\n") + f"\n{bullet}\n\n" + text[end:].lstrip("\n")
            )
            path.write_text(updated, encoding="utf-8")
            break
        else:
            return False

        connection = sqlite3.connect(DB_PATH)
        try:
            connection.execute(
                "UPDATE knowledge SET content = content || ? "
                "WHERE topic = ? AND source = ?",
                (f"\n{bullet}", topic, source),
            )
            connection.commit()
        finally:
            connection.close()
        return True

    def _create_section(self, title: str, block: str) -> bool:
        """在群聊补充文档末尾新建一节，并写入数据库。

        Args:
            title: 小节标题。
            block: 完整的 Markdown 小节文本。

        Returns:
            写入是否成功。
        """
        if not DOCS_DIR.is_dir():
            return False
        if not EXTRA_DOC.exists():
            # 这段说明短于 40 字符，重建数据库时会被当作前言跳过。
            EXTRA_DOC.write_text(
                "# 群聊补充\n\n群里用 /知识库 添加 或 /知识库 补充 记下来的内容。\n",
                encoding="utf-8",
            )
        with EXTRA_DOC.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{block}\n")

        connection = sqlite3.connect(DB_PATH)
        try:
            connection.execute(
                "INSERT INTO knowledge (topic, source, content) VALUES (?, ?, ?)",
                (title, EXTRA_DOC.name, block),
            )
            connection.commit()
        finally:
            connection.close()
        return True
