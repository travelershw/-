"""Keyword meme search: find a sticker image online and post it in the chat.

There is no free official API for this, so the plugin queries the two image search
endpoints that actually answered from this machine — Bing's image result endpoint
and 360's image JSON API — then verifies the candidate before sending it, because
QQ silently drops an image whose URL cannot be fetched.

Calling the command again with the same keyword must give a *different* picture.
Rotating to the next URL is not enough: Chinese meme sites repost the same image
under several file names (originals, thumbnails, mirrors), so the plugin also
remembers a fingerprint of every picture it already sent for that keyword — the
normalized file name plus a hash of the first 64 KB — and skips those.

Phase six: the same search is also exposed as the LLM tool ``send_meme`` so 轻语
can drop a sticker into a conversation on her own. A tool that yields
``event.image_result(...)`` makes the framework send that chain straight to the
chat (``tool_direct_result``), and the following string tells the model it went
out so she does not repeat herself.

Command: ``/表情包 <关键词>`` (aliases: 表情 / meme / bqb / 斗图).
"""

import hashlib
import html
import random
import re
import time
import urllib.parse

import httpx

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# Static assets that live next to real results in the search HTML.
BAD_URL_MARKERS = (
    "bingstatic",
    "bing.com/sa/",
    "favicon",
    "/logo",
    "spinner",
    "placeholder",
    "blank.",
)
# Very rough safety net: a class group should not receive these.
BLOCKED_URL_MARKERS = ("porn", "sex", "nude", "naked", "hentai", "r18", "18+", "gore", "xxx")
BLOCKED_KEYWORDS = ("色情", "裸", "黄图", "血腥", "自杀", "毒品", "枪", "赌博")
DEFAULT_KEYWORDS = ("猫猫", "摸鱼", "无语", "点赞", "笑死", "委屈")
COMMAND_NAMES = ("表情包", "表情", "meme", "bqb", "斗图")
MAX_KEYWORD_CHARS = 20
CACHE_SECONDS = 600.0
MAX_CANDIDATES = 8
MAX_TRIES = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 指纹只读前这么多字节：足够区分两张图，又不至于把整张图拉下来。
FINGERPRINT_BYTES = 64 * 1024
# 每个关键词记住多少张"已经发过的图"。
SEEN_LIMIT = 60
# 文件名里表示尺寸/缩略图的片段，去掉后才能认出同一张图的另一个链接。
SIZE_MARKERS = (
    "thumb",
    "thumbnail",
    "small",
    "mini",
    "preview",
    "middle",
    "origin",
    "large",
    "1000_0",
    "700d1q75cms",
    "_r.",
    "@",
)
SEARCH_TIMEOUT = 20.0
PROBE_TIMEOUT = 10.0
USER_COOLDOWN_SECONDS = 5.0
# 她自己（LLM 工具）发图的冷却：同一个会话里两分钟最多一张，免得变成刷图机器。
TOOL_COOLDOWN_SECONDS = 120.0


@register(
    "meme_search",
    "migration",
    "按关键词联网搜表情包并发送：/表情包 <关键词>（同一词换不同图，按内容指纹去重）"
    "；并给轻语提供 send_meme 工具，让她自己在对话里发表情包",
    "1.2.0",
)
class MemeSearchPlugin(Star):
    """Finds and posts a sticker matching a keyword."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._cursor: dict[str, int] = {}
        self._last_used: dict[str, float] = {}
        # 关键词 -> 已经发过的图片指纹（文件名 + 内容哈希）。
        self._seen: dict[str, list[str]] = {}
        # 会话 -> 她上次自己发表情包的时间（工具冷却用）。
        self._tool_last: dict[str, float] = {}

    @filter.command("表情包", alias={"表情", "meme", "bqb", "斗图"})
    async def meme_command(self, event: AstrMessageEvent):
        """按关键词联网搜一张表情包发出来。

        用法：``/表情包 <关键词>``；不写关键词就随机挑一个常见的。
        同一个关键词连续调用会换下一张（结果缓存 10 分钟）。

        Args:
            event: 指令消息事件。

        Yields:
            图片结果，或者失败时的说明文本。
        """
        keyword = self._keyword_from(event)
        if any(word in keyword for word in BLOCKED_KEYWORDS):
            yield event.plain_result("这个关键词我不搜哦~")
            return

        key = f"{event.unified_msg_origin}:{event.get_sender_id()}"
        now = time.time()
        if now - self._last_used.get(key, 0.0) < USER_COOLDOWN_SECONDS:
            yield event.plain_result("刚发过一张，等我缓一下下~")
            return
        self._last_used[key] = now

        urls = await self._candidates(keyword)
        if not urls:
            yield event.plain_result(f"没搜到「{keyword}」的表情包，换个词试试~")
            return
        image = await self._first_usable(keyword, urls)
        if image is None:
            yield event.plain_result(
                f"「{keyword}」这次的图都不太合适，等会儿再试或者换个词~",
            )
            return
        yield event.image_result(image)

    @filter.llm_tool(name="send_meme")
    async def meme_tool(self, event: AstrMessageEvent, keyword: str = ""):
        """想用一张表情包回应群友时调用（接梗、调侃、表示无语或开心）。

        只在**真的合适**的时候用：对方刚开了个玩笑、气氛轻松、或者一张图比一句话
        更贴切。一次对话最多发一张，不要连着发，也不要每条消息都配图；正经提问、
        有人在求助、气氛紧张时不要用。发出去之后不用再用文字描述这张图。

        Args:
            keyword (str): 表情包的主题词，2~6 个字，例如"摸鱼""笑死""无语""点赞""委屈"；留空就随机挑一个

        Returns:
            str: 发送结果说明
        """
        word = (keyword or "").strip().strip("「」\"'")[:MAX_KEYWORD_CHARS]
        if not word:
            word = random.choice(DEFAULT_KEYWORDS)
        if any(bad in word for bad in BLOCKED_KEYWORDS):
            yield "这个词不适合发，换个轻松点的词，或者干脆用文字回。"
            return

        umo = event.unified_msg_origin
        now = time.time()
        if now - self._tool_last.get(umo, 0.0) < TOOL_COOLDOWN_SECONDS:
            yield "这个会话刚刚发过一张了，这次用文字回。"
            return

        urls = await self._candidates(word)
        image = await self._first_usable(word, urls) if urls else None
        if image is None:
            yield f"「{word}」这次没找到合适的图，用文字回吧。"
            return

        self._tool_last[umo] = now
        logger.info(f"meme_search: 她自己发了一张「{word}」的表情包 -> {image[:90]}")
        # 先让框架把图片直接发给群，再用一句文本告诉模型结果（免得她复述图片内容）。
        yield event.image_result(image)
        yield f"已经发出一张「{word}」的表情包。"

    def _keyword_from(self, event: AstrMessageEvent) -> str:
        """从指令文本里取出关键词。

        Args:
            event: 指令消息事件。

        Returns:
            关键词；没写就随机取一个默认词。
        """
        raw = (event.message_str or "").strip().lstrip("/／!！.。")
        lowered = raw.lower()
        for name in sorted(COMMAND_NAMES, key=len, reverse=True):
            if lowered.startswith(name):
                raw = raw[len(name) :].strip()
                break
        raw = raw.strip("<>《》【】「」“”\"'").strip()
        if not raw:
            return random.choice(DEFAULT_KEYWORDS)
        return raw[:MAX_KEYWORD_CHARS]

    async def _candidates(self, keyword: str) -> list[str]:
        """取关键词对应的候选图片链接（带缓存）。

        Args:
            keyword: 搜索关键词。

        Returns:
            可用的候选链接，动图排在前面；没有结果时为空列表。
        """
        cached = self._cache.get(keyword)
        if cached and time.time() - cached[0] < CACHE_SECONDS:
            return cached[1]
        urls = await self._search(keyword)
        if urls:
            urls.sort(key=lambda url: 0 if url.lower().split("?")[0].endswith(".gif") else 1)
            self._cache[keyword] = (time.time(), urls)
        return urls

    async def _search(self, keyword: str) -> list[str]:
        """依次问两个图片搜索接口。

        Args:
            keyword: 搜索关键词。

        Returns:
            去重后的候选链接。
        """
        for name, search in (("bing", self._search_bing), ("360", self._search_360)):
            try:
                raw = await search(keyword)
            except Exception as exc:  # noqa: BLE001 - 换下一个源就是了
                logger.warning(f"meme_search: {name} 搜索失败: {type(exc).__name__}: {exc}")
                continue
            urls: list[str] = []
            for url in raw:
                if self._looks_like_image(url) and url not in urls:
                    urls.append(url)
            logger.info(f"meme_search: '{keyword[:20]}' via {name} -> {len(urls)} 个候选")
            if urls:
                return urls[:MAX_CANDIDATES]
        return []

    async def _search_bing(self, keyword: str) -> list[str]:
        """Bing 图片搜索结果页（异步片段，体积小、好解析）。

        Args:
            keyword: 搜索关键词。

        Returns:
            原始候选链接。
        """
        query = urllib.parse.quote(f"{keyword} 表情包")
        url = (
            "https://www.bing.com/images/async?"
            f"q={query}&first=0&count=35&mmasync=1&adlt=strict"
        )
        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            text = html.unescape(response.text)
        found = re.findall(r'"murl":"(.*?)"', text)
        found += re.findall(r'mediaurl=(https?%3A%2F%2F[^&"]+)', text)
        return [urllib.parse.unquote(item) for item in found]

    async def _search_360(self, keyword: str) -> list[str]:
        """360 图片搜索 JSON 接口（备用源）。

        Args:
            keyword: 搜索关键词。

        Returns:
            原始候选链接。
        """
        query = urllib.parse.quote(f"{keyword} 表情包")
        url = f"https://image.so.com/j?q={query}&src=srp&sn=0&pn=35"
        async with httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        results: list[str] = []
        for item in data.get("list") or []:
            if isinstance(item, dict):
                results.append(str(item.get("img") or item.get("thumb") or ""))
        return results

    def _looks_like_image(self, url: str) -> bool:
        """粗筛候选链接，挡掉静态资源和明显不合适的图。

        Args:
            url: 候选链接。

        Returns:
            是否值得再去做一次下载校验。
        """
        if not url.startswith("http"):
            return False
        lowered = url.lower()
        if any(marker in lowered for marker in BAD_URL_MARKERS):
            return False
        if any(marker in lowered for marker in BLOCKED_URL_MARKERS):
            return False
        return lowered.split("?", 1)[0].endswith(IMAGE_SUFFIXES)

    async def _first_usable(self, keyword: str, urls: list[str]) -> str | None:
        """按轮换顺序挑一张**没发过的**、真的能下载的图。

        QQ 收到链接会自己去下，下载失败就什么都不发，所以这里先试一次；同时用
        "文件名 + 前 64KB 哈希"认一遍，避免同一个关键词老给同一张（很多站点把
        同一张图挂在好几个链接下，缩略图又是另一个名字）。都没发过时才重来一轮。

        Args:
            keyword: 搜索关键词，用来记录轮换位置与已发指纹。
            urls: 候选链接。

        Returns:
            可用的图片链接；都不行时返回 None。
        """
        seen = self._seen.setdefault(keyword, [])
        start = self._cursor.get(keyword, 0)
        picked = await self._scan(urls, start, seen, skip_seen=True)
        if picked is None and seen:
            # 这个词的图都发过一轮了：清空记录，允许从头再来一轮。
            logger.info(f"meme_search: '{keyword[:20]}' 的图都发过一轮，重新开始")
            self._seen[keyword] = []
            seen = self._seen[keyword]
            picked = await self._scan(urls, start, seen, skip_seen=False)
        if picked is None:
            logger.warning(f"meme_search: '{keyword[:20]}' 候选都下不动或者都试过了")
            return None

        index, url, digest = picked
        if digest:
            seen.append(digest)
        seen.append(self._fingerprint_name(url))
        del seen[:-SEEN_LIMIT]
        self._cursor[keyword] = (index + 1) % len(urls)
        logger.info(
            f"meme_search: '{keyword[:20]}' -> 第 {index + 1} 张 {url[:90]}"
            f"（已记 {len(seen)} 个指纹）",
        )
        return url

    async def _scan(
        self,
        urls: list[str],
        start: int,
        seen: list[str],
        *,
        skip_seen: bool,
    ) -> tuple[int, str, str] | None:
        """从候选里扫出一张可用的图。

        Args:
            urls: 候选链接。
            start: 轮换起点。
            seen: 已经发过的指纹。
            skip_seen: 是否跳过已发过的图。

        Returns:
            (下标, 链接, 内容指纹)；没扫到时返回 None。
        """
        for offset in range(min(len(urls), MAX_TRIES)):
            index = (start + offset) % len(urls)
            url = urls[index]
            if skip_seen and self._fingerprint_name(url) in seen:
                continue  # 同一张图的另一个链接（缩略图/别称），不用再试
            digest = await self._probe(url)
            if digest is None:
                continue
            if skip_seen and digest and digest in seen:
                continue  # 换了链接还是同一张图
            return index, url, digest
        return None

    def _fingerprint_name(self, url: str) -> str:
        """把链接归一成指纹：去掉尺寸/缩略图标记，只留文件名主干。

        Args:
            url: 图片链接。

        Returns:
            归一的指纹字符串。
        """
        path = urllib.parse.urlsplit(url).path
        name = path.rsplit("/", 1)[-1].lower()
        for marker in SIZE_MARKERS:
            name = name.replace(marker, "")
        return re.sub(r"[\W_]+", "", name)

    async def _probe(self, url: str) -> str | None:
        """确认这张图能下载，并返回内容指纹。

        Args:
            url: 候选图片链接。

        Returns:
            前 64KB 的 md5；不可用时返回 None。
        """
        origin = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlsplit(url))
        try:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Referer": origin},
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        return None
                    if not response.headers.get("content-type", "").startswith("image/"):
                        return None
                    length = response.headers.get("content-length")
                    if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                        return None
                    digest = hashlib.md5()
                    read = 0
                    async for chunk in response.aiter_bytes(FINGERPRINT_BYTES):
                        digest.update(chunk)
                        read += len(chunk)
                        if read >= FINGERPRINT_BYTES:
                            break
                    return digest.hexdigest() if read else ""
        except Exception:  # noqa: BLE001 - 不可用就换下一张
            return None
