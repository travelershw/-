r"""风格层：从群历史里学「这个群怎么说话」，用来管住插嘴的措辞（L1）。

为什么需要它：插嘴不自然，主要不是内容错，而是**形状错**——群里的人中位 7 个字、
56% 的话不超过 8 个字，她却常常写 40~60 字、反问对方、还带个波浪号收尾。
现在那段提示词（``express.CHIME_HINTS``）只有"短一点、口语一点"，没有任何
"这个群平时怎么说话"的信息。

这里做三件事：

1. **群味卡片**：从 AstrBot 的历史库 ``data_v4.db`` 统计每个群的说话形状——
   字数分位、短句率、emoji 率、波浪号率、引用率、句尾习惯，以及**学习对象**
   （白名单里的 QQ，初期只有你本人）自己的那一套；
   结果落在 ``plugin_data/qingyu_style.json``，每晚重建一次（懒加载兜底）。
2. **长度上限按群单独算**：取学习对象的 p75（样本不够就用全群 p75），
   夹在 12~60 字之间——不再是一个写死的 40。
3. **示例检索**：按字符 2-gram 相似度从**真人**（优先学习对象）的历史发言里挑几条，
   当作"这时候这个群的人会怎么说"塞进提示词。

边界：只读历史库；统计只在本地，不外发；样本太少（<30 条）就退回保守的通用约束。
"""

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

# 卡片文件位置（默认在 plugin_data）。离线重放/测试时可以用环境变量指到别处，
# 免得把测试数据写进线上那份。
STATE_PATH = Path(
    os.environ.get("QINGYU_STYLE_STATE")
    or (Path(get_astrbot_plugin_data_path()) / "qingyu_style.json"),
)
# 历史库：和 nightly 一样按候选顺序找（数据根目录下，不在 plugin_data 里）。
_HISTORY_CANDIDATES = (
    Path(get_astrbot_data_path()) / "data_v4.db",
    Path.home() / ".astrbot" / "data" / "data_v4.db",
)
HISTORY_DB = next(
    (path for path in _HISTORY_CANDIDATES if path.exists()), _HISTORY_CANDIDATES[0]
)
# 学习对象白名单：初期只有你本人，之后用 /风格 老师 加 别人。
DEFAULT_TEACHERS = ()
# 统计窗口与重建周期。
WINDOW_DAYS = 14
REBUILD_SECONDS = 12 * 3600
# 卡片结构版本：加了新字段就必须重建，否则老文件里没有那些键，
# 读出来是"空值 + 兜底值"，线上行为和测试对不上。
# 2026-09-19 踩了两次：v1→v2（缺 answer/answer_cap，"被 @ 的答话上限"退回插嘴上限 13 字）、
# v2→v3（缺 teacher_counts，"学习对象"被误报成"学不到东西"）。
# v3→v4（2026-09-23）：改成"一张总体卡片 + 每群细微差别"，老文件里没有 base。
CARD_VERSION = 4
# 总体卡片与每群差别的关系（2026-09-23 用户要求）：先按所有群合起来算一张总体卡片，
# 每群再在它之下只做**细微**调整，而不是各算各的。
#   · GROUP_WEIGHT：本群自己的测量值能拉动总体值多少（0.4 = 拉四成）；
#   · CAP_DRIFT / ANSWER_CAP_DRIFT：再多也不能偏离总体超过这么多字（硬顶）；
#   · POOL_MIN：本群自己的关键词/示例少于这么多条时，用总体的池子补齐。
GROUP_WEIGHT = 0.4
CAP_DRIFT = 6
ANSWER_CAP_DRIFT = 12
POOL_MIN = 12
# 样本不够就不敢下结论：少于这么多条时退回通用约束。
MIN_TEACHER_SAMPLES = 30
MIN_GROUP_SAMPLES = 60
# 插嘴长度上限的夹取范围与兜底值（兜底 = 以前写死的 40）。
CAP_MIN = 12
CAP_MAX = 60
CAP_FALLBACK = 40
# 每个群存多少条示例候选（只存短的、像随口说的话）。
EXEMPLAR_LIMIT = 240
EXEMPLAR_MAX_CHARS = 40
EXEMPLAR_K = 4
# 太小/太废的消息不算话（表情、单字、指令、链接）。
MIN_TEXT_CHARS = 2
COMMAND_PREFIXES = ("/", "／", "[", "【")
SHORT_CHARS = 8
LONG_CHARS = 60
# 波浪号必须按**单个字符**判：写成 `TILDE = "~～"` 再 `TILDE in text`，是在找
# "~～" 这个两字符子串，永远匹配不到——"带波浪号 0%" 就是这么来的假数据
# （2026-09-19 抓到的 bug：她那 22 条被点名回复里其实 19 条都带 ASCII `~`）。
TILDES = ("\u007e", "\uff5e", "\u301c")
# 被 @ 之后"怎么答"是另一套形状：统计口径是"一条提问之后 60 秒内、另一个人说的第一条"。
ANSWER_WINDOW_SECONDS = 60
ANSWER_MIN_SAMPLES = 8
# 「答话」的下限给得比插嘴高：回答总得能说清一件事（实测真人回答中位 19、p75 29，
# 你本人中位 25），而插嘴是纯顺口一句（中位 7）。上限仍然按群算。
ANSWER_CAP_MIN = 24
ANSWER_CAP_MAX = 120
ANSWER_EXEMPLAR_LIMIT = 80
QUESTION = re.compile(r"[?？]|吗|怎么|为什么|多少|哪儿|哪里|是不是|能不能|有没有")
# 「嗯~」这类开场白：白名单对象与群里真人几乎不用（0%），而她被 @ 之后 82% 都是这么开的。
OPENER_MARKS = ("嗯~", "嗯，", "嗯！", "哦~", "啊~", "唔~")
# 助手腔：这些词一出现就不像群里的人。
ASSISTANT_MARKS = (
    "以下是",
    "希望可以帮到",
    "帮你整理",
    "总结一下",
    "首先",
    "其次",
    "总之",
    "建议你",
    "如果需要",
)
# ---- 关系语气（好感度怎么影响"她怎么说话"）----
# 2026-09-19 晚的返工：第一版把群里那套"短平快"直接压在她身上，结果**人设被淹没、
# 好感度看不出作用**（用户原话）。现在分工改成：
#   · 群味只管**长度**（别写小作文）；
#   · **语气与口癖按好感度来**——关系越近，她越像"轻语"（「嗯~」、句尾「~」、打趣、多聊两句），
#     关系越远越客气简短。这样好感度是能被她说话的方式看见的。
FLAVOR_TIERS = (
    (
        80,
        "很亲昵",
        2.0,
        "跟他/她很亲近：可以「嗯~」开场、句尾带「~」，可以打趣、撒娇、多说两句，语气暖一点",
    ),
    (
        60,
        "轻快亲近",
        1.6,
        "关系不错：口气轻快，可以用「嗯~」和句尾的「~」，愿意多聊两句、偶尔打趣",
    ),
    (40, "普通友好", 1.2, "普通朋友：可以有你自己的口气，但别太黏，两三句就够"),
    (20, "客气疏远", 1.0, "不太熟：客气、简短，少用口癖"),
    (0, "明显冷淡", 0.8, "关系冷淡：只答必要的，语气疏离，不用口癖和波浪号"),
)


def flavor_for(affection: int | None) -> tuple[str, float, str]:
    """Pick the relationship flavour (label, length multiplier, instruction).

    Args:
        affection: Affection value of the person talking (None = treat as neutral).

    Returns:
        ``(label, multiplier, instruction)``。
    """
    value = 60 if affection is None else int(affection)
    for threshold, label, multiplier, instruction in FLAVOR_TIERS:
        if value >= threshold:
            return label, multiplier, instruction
    return FLAVOR_TIERS[-1][1], FLAVOR_TIERS[-1][2], FLAVOR_TIERS[-1][3]


LAUGH = re.compile(r"(哈哈|hh|HH|嘿嘿|233|笑死|草)")
OPEN_PAREN = re.compile(r"[（(]")
EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")
TRAILING_TAILS = ("。。。", "……", "（", "hh", "哈哈", "！", "?")
# 状态文件缓存：每条群消息都要问一次"这个群的风格是什么"，不能每次都摸磁盘。
# (mtime_ns, state) is one variable so a single reference read/write is atomic and a
# reader can never observe a mismatched stamp/data pair.
_cache_entry: tuple[int, dict] | None = None
# Serialize only the write/rebuild compound operations (ensure_cards / set_teachers /
# save_state) so two writers cannot overwrite each other's teacher list. The read path
# (load_state / card) takes no lock and instead relies on save_state's atomic os.replace
# for a complete snapshot, so reads never block a long rebuild. Reentrant because
# set_teachers calls ensure_cards / save_state internally.
_lock = threading.RLock()


def _parse(content: str) -> dict | None:
    """Decode one stored message row.

    Args:
        content: The ``content`` column (JSON, or plain text for old rows).

    Returns:
        ``{"text", "images", "quoted", "at"}`` or None for bot rows / unparsable.
    """
    raw = str(content or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        return {"text": raw, "images": 0, "quoted": False, "at": False}
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") == "bot":
        return None
    parts: list[str] = []
    images = 0
    quoted = False
    at = False
    for segment in data.get("message") or []:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "plain")
        if kind == "plain":
            parts.append(str(segment.get("text") or ""))
        elif kind in {"image", "emoji", "sticker"}:
            images += 1
        elif kind == "reply":
            quoted = True
        elif kind == "at":
            at = True
    return {
        "text": "".join(parts).strip(),
        "images": images,
        "quoted": quoted,
        "at": at,
    }


def _stamp_to_epoch(value: str) -> int:
    """Convert a history timestamp to a local epoch second.

    **这一列是 UTC**（实测：本机 16:47 时它写的是 08:47），而 `plugin_data` 里
    我们自己写的 `ts` 是本地 epoch。第一版按本地时间去 parse，于是所有跨表的时间比较
    都差 8 小时——"她说完 180 秒内谁接话"这类判据直接永远不成立（2026-09-19 抓到）。

    Args:
        value: ``created_at`` text from ``platform_message_history``.

    Returns:
        Local epoch seconds (now when unparsable).
    """
    text = str(value)[:19]
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return int(time.time())
    return int(naive.replace(tzinfo=timezone.utc).timestamp())


def _read_history(days: int = WINDOW_DAYS, limit: int = 6000) -> list[dict]:
    """Read recent human messages from AstrBot's history table.

    Args:
        days: How far back to read.
        limit: Hard cap on rows read.

    Returns:
        Rows as ``{"group", "uid", "name", "text", "images", "quoted", "at", "ts"}``.
    """
    if not HISTORY_DB.exists():
        logger.warning(f"qingyu_core: 找不到历史库 {HISTORY_DB}，风格卡片这次不建")
        return []
    cutoff = time.time() - days * 86400
    try:
        connection = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT created_at, user_id, sender_id, sender_name, content"
            " FROM platform_message_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(f"qingyu_core: 读历史建卡片失败: {exc}")
        return []
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass

    out: list[dict] = []
    for created_at, umo, sender_id, sender_name, content in rows:
        parsed = _parse(content)
        if not parsed or not parsed["text"]:
            continue
        stamp = _stamp_to_epoch(created_at)
        if stamp < cutoff:
            continue
        group = str(umo).split(":")[-1]
        if not group.isdigit():
            # 面板/桌宠那些会话不是群，学出来的"群味"没意义。
            continue
        out.append(
            {
                "group": group,
                "uid": str(sender_id or ""),
                "name": str(sender_name or ""),
                "text": parsed["text"],
                "images": parsed["images"],
                "quoted": parsed["quoted"],
                "at": parsed["at"],
                "ts": int(stamp),
            },
        )
    return out


def _percentile(values: list[int], fraction: float) -> int:
    """Pick a percentile from a sorted list.

    Args:
        values: Sorted integers.
        fraction: 0~1 position.

    Returns:
        The value at that position (0 for an empty list).
    """
    if not values:
        return 0
    index = min(len(values) - 1, max(0, int(len(values) * fraction)))
    return int(values[index])


def _shape(rows: list[dict]) -> dict:
    """Describe how a set of messages "looks".

    Args:
        rows: Message rows (already filtered to real conversation).

    Returns:
        The shape metrics used by the card.
    """
    total = len(rows)
    if not total:
        return {}
    lengths = sorted(len(row["text"]) for row in rows)

    def share(predicate) -> float:
        return round(sum(1 for row in rows if predicate(row)) / total, 3)

    tails: dict[str, int] = {}
    for tail in TRAILING_TAILS:
        hit = sum(1 for row in rows if row["text"].endswith(tail))
        if hit:
            tails[tail] = hit
    return {
        "samples": total,
        "p25": _percentile(lengths, 0.25),
        "p50": _percentile(lengths, 0.50),
        "p75": _percentile(lengths, 0.75),
        "p95": _percentile(lengths, 0.95),
        "short_share": share(lambda row: len(row["text"]) <= SHORT_CHARS),
        "long_share": share(lambda row: len(row["text"]) >= LONG_CHARS),
        "emoji_share": share(lambda row: bool(EMOJI.search(row["text"]))),
        "tilde_share": share(lambda row: any(char in row["text"] for char in TILDES)),
        "laugh_share": share(lambda row: bool(LAUGH.search(row["text"]))),
        "paren_share": share(lambda row: bool(OPEN_PAREN.search(row["text"]))),
        "quote_share": share(lambda row: row["quoted"]),
        "at_share": share(lambda row: row["at"]),
        "question_share": share(lambda row: bool(QUESTION.search(row["text"]))),
        "tail": [
            tail for tail, _count in sorted(tails.items(), key=lambda kv: -kv[1])[:3]
        ],
    }


# 太常见的两字词（功能词/口头语）当不了"话题关键词"，第一版把「怎么/可以/没有」这些
# 也学进去了，拿它们算"话题相关"等于随机命中。
STOPWORDS = frozenset(
    {
        "怎么",
        "可以",
        "没有",
        "现在",
        "你们",
        "不要",
        "这么",
        "我要",
        "一个",
        "但是",
        "你是",
        "还有",
        "我是",
        "我的",
        "还是",
        "什么",
        "知道",
        "不是",
        "就是",
        "这个",
        "那个",
        "因为",
        "所以",
        "如果",
        "然后",
        "真的",
        "有点",
        "应该",
        "感觉",
        "觉得",
        "时候",
        "东西",
        "事情",
        "一起",
        "已经",
        "可能",
        "一下",
        "咱们",
        "我们",
        "他们",
        "自己",
        "这里",
        "那里",
        "多少",
        "这样",
        "那样",
        "不过",
        "而且",
        "反正",
        "肯定",
        "哈哈",
        "呜呜",
        "然后",
        "怎么",
        "为啥",
        "有没有",
        "应该是",
        "怎么办",
    },
)


def _keywords(rows: list[dict], limit: int = 24) -> list[str]:
    """Extract frequent 2-character tokens from the learning source.

    Args:
        rows: Message rows of the learning source.
        limit: How many keywords to keep.

    Returns:
        Keywords ordered by frequency.
    """
    counter: dict[str, int] = {}
    for row in rows:
        text = re.sub(r"[^\u4e00-\u9fff]+", " ", row["text"])
        for chunk in text.split():
            for index in range(len(chunk) - 1):
                token = chunk[index : index + 2]
                if token in STOPWORDS:
                    continue
                counter[token] = counter.get(token, 0) + 1
    picked = [
        token
        for token, count in sorted(counter.items(), key=lambda kv: -kv[1])
        if count >= 3
    ]
    return picked[:limit]


def _exemplars(rows: list[dict]) -> list[str]:
    """Pick short, human-sounding lines to show the model as examples.

    Args:
        rows: Message rows of the learning source.

    Returns:
        Up to :data:`EXEMPLAR_LIMIT` lines, newest first, de-duplicated.
    """
    seen: set[str] = set()
    picked: list[str] = []
    for row in sorted(rows, key=lambda item: -item["ts"]):
        text = row["text"].strip()
        if not (MIN_TEXT_CHARS <= len(text) <= EXEMPLAR_MAX_CHARS):
            continue
        if text.startswith(COMMAND_PREFIXES) or "@" in text:
            continue
        if not EMOJI.search(text) and not re.search(r"[\u4e00-\u9fff]", text):
            # 纯符号/纯表情的，学不到措辞。
            continue
        if text in seen:
            continue
        seen.add(text)
        picked.append(text)
        if len(picked) >= EXEMPLAR_LIMIT:
            break
    return picked


def _answers_to_questions(
    items: list[dict], teacher_ids: list[str]
) -> tuple[list[dict], list[dict]]:
    """Collect "how people in this group answer a question".

    做法：找一条像提问的消息，再看 **60 秒内另一个人** 说的第一条——那就是一次"回答"。
    分两份：全群真人的回答、学习对象本人的回答（后者更贴你的语感）。

    Args:
        items: All message rows of one group, any order.
        teacher_ids: Uids treated as the learning source.

    Returns:
        ``(all_answers, teacher_answers)``。
    """
    ordered = sorted(items, key=lambda row: row["ts"])
    everyone: list[dict] = []
    mine: list[dict] = []
    for index, item in enumerate(ordered):
        if not QUESTION.search(item["text"]):
            continue
        for later in ordered[index + 1 :]:
            if later["ts"] - item["ts"] > ANSWER_WINDOW_SECONDS:
                break
            if later["uid"] == item["uid"]:
                continue
            everyone.append(later)
            if later["uid"] in teacher_ids:
                mine.append(later)
            break
    return everyone, mine


def _answer_cap(shape: dict, fallback: int) -> int:
    """Turn the answer register into a casual-answer cap.

    Args:
        shape: Shape metrics of the answers.
        fallback: Value to use when there are too few samples.

    Returns:
        The cap in characters.
    """
    if shape.get("samples", 0) < ANSWER_MIN_SAMPLES:
        return fallback
    cap = int(shape.get("p75") or 0)
    return max(ANSWER_CAP_MIN, min(ANSWER_CAP_MAX, cap)) if cap else fallback


def _card_for(items: list[dict], teacher_ids: list[str], days: int) -> dict:
    """Measure one set of messages into a card.

    总体卡片和每个群的卡片都走这里，保证两者口径完全一致、可以互相比较。

    Args:
        items: Message rows belonging to this set.
        teacher_ids: Uids treated as the learning source.
        days: How far back the rows were read (stored on the card).

    Returns:
        The card dict, without any总体 adjustments applied yet.
    """
    mine = [row for row in items if row["uid"] in teacher_ids]
    group_shape = _shape(items)
    teacher_shape = _shape(mine) if len(mine) >= MIN_TEACHER_SAMPLES else {}
    if teacher_shape:
        source, source_name = mine, "学习对象"
    elif group_shape.get("samples", 0) >= MIN_GROUP_SAMPLES:
        source, source_name = items, "全群"
    else:
        source, source_name = [], "样本不足"
    # 长度上限：学习对象的 p75 更贴你的语感；不够样本就看全群。
    basis = teacher_shape or group_shape
    cap = int(basis.get("p75") or 0)
    cap = max(CAP_MIN, min(CAP_MAX, cap)) if cap else CAP_FALLBACK
    # 被 @ 之后怎么答：先看学习对象自己的回答，再看全群。
    answers, teacher_answers = _answers_to_questions(items, teacher_ids)
    answer_shape = (
        _shape(teacher_answers)
        if len(teacher_answers) >= ANSWER_MIN_SAMPLES
        else _shape(answers)
    )
    # 每个学习对象在**这个集合**里有多少条发言：加完人要能立刻看出"学不学得到东西"。
    counts = {uid: sum(1 for row in items if row["uid"] == uid) for uid in teacher_ids}
    return {
        "built_at": int(time.time()),
        "window_days": days,
        "cap": cap,
        "cap_basis": source_name,
        "group": group_shape,
        "teacher": teacher_shape,
        "teacher_counts": counts,
        "keywords": _keywords(source),
        "exemplars": _exemplars(source),
        "answer": answer_shape,
        "answer_cap": _answer_cap(answer_shape, cap),
        "answer_examples": _exemplars(teacher_answers or answers)[
            :ANSWER_EXEMPLAR_LIMIT
        ],
    }


def _drift_towards(base_value: int, own_value: int, drift: int) -> int:
    """Pull one length cap from the overall value towards the group's own value.

    Args:
        base_value: The value measured across all groups.
        own_value: The value measured in this group alone.
        drift: Hard limit on how far the result may differ from ``base_value``.

    Returns:
        The blended cap, never further than ``drift`` from the overall value.
    """
    if not base_value:
        return own_value
    pulled = base_value + round((own_value - base_value) * GROUP_WEIGHT)
    return max(1, max(base_value - drift, min(base_value + drift, pulled)))


def _fit_to_base(own: dict, base: dict) -> dict:
    """Keep a group card close to the overall card, adding only subtle differences.

    Args:
        own: The card measured from this group alone.
        base: The card measured across all groups.

    Returns:
        The group card with blended caps, base fallbacks for thin samples, and
        the original measurements kept alongside for `/风格` to display.
    """
    card = dict(own)
    # 本群样本不够时，长度直接用总体值（样本不足时本群测出来的数字是噪声，不该拿它去偏离），
    # 语气参照也换成总体；但**本群自己的统计数字一并保留**，`/风格` 里要显示真实的样本条数，
    # 不能把总体的 2090 条算到这个群头上（2026-09-23 自己发现并修掉）。
    thin = own["cap_basis"] == "样本不足"
    if thin:
        card["cap"] = int(base.get("cap") or own["cap"])
        card["answer_cap"] = int(base.get("answer_cap") or own["answer_cap"])
        card["cap_basis"] = "总体"
        card["borrowed"] = True
        card["basis"] = dict(base.get("teacher") or base.get("group") or {})
    else:
        card["cap"] = _drift_towards(
            int(base.get("cap") or 0),
            int(own["cap"]),
            CAP_DRIFT,
        )
        card["answer_cap"] = _drift_towards(
            int(base.get("answer_cap") or 0),
            int(own["answer_cap"]),
            ANSWER_CAP_DRIFT,
        )
        card["basis"] = dict(own.get("teacher") or own.get("group") or {})
    card["cap_own"] = int(own["cap"])
    card["answer_cap_own"] = int(own["answer_cap"])
    for key in ("keywords", "exemplars", "answer_examples"):
        pool = list(own.get(key) or [])
        if len(pool) < POOL_MIN:
            extra = [
                item for item in (base.get(key) or []) if item not in pool
            ]
            card[key] = pool + extra
            card["borrowed"] = True
    return card


def build_cards(days: int = WINDOW_DAYS, teachers: list[str] | None = None) -> dict:
    """Rebuild the overall style card and every group's card beneath it.

    统计分两套：**学习对象**（白名单里那些人）与**全群真人**。措辞约束优先用学习对象的，
    因为你要的是"像你们群里的人"，而白名单初期就是你本人。

    2026-09-19 补：被 @ 之后"怎么答"是另一套形状（群里人回答的中位 19 字、没人用「嗯~」开场），
    所以卡片里单独存一份 **answer**（问句之后 60 秒内别人的第一条）与它的长度上限。

    2026-09-23 改：先按所有群合起来算一张**总体卡片**（``state["base"]``），每个群再在它之下
    只做细微调整（见 :func:`_fit_to_base`）——她要先是"同一个人"，然后才是"在哪个群"。

    Args:
        days: How far back to read.
        teachers: Uids to learn from (defaults to :data:`DEFAULT_TEACHERS`).

    Returns:
        The new state dict (``teachers`` + ``base`` + ``cards`` + ``built_at``).
    """
    teacher_ids = [str(item) for item in (teachers or DEFAULT_TEACHERS)]
    rows = _read_history(days=days)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)

    base = _card_for(rows, teacher_ids, days)
    cards: dict[str, dict] = {
        group: _fit_to_base(_card_for(items, teacher_ids, days), base)
        for group, items in groups.items()
    }
    described = "、".join(
        "{}: 插嘴 {}字/答 {}字".format(group, item.get("cap"), item.get("answer_cap"))
        for group, item in cards.items()
    )
    logger.info(
        f"qingyu_core: 群味卡片重建完成——总体 插嘴 {base.get('cap')}字/答 "
        f"{base.get('answer_cap')}字，{len(cards)} 个群（{described or '无样本'}）",
    )
    return {
        "version": CARD_VERSION,
        "teachers": teacher_ids,
        "built_at": int(time.time()),
        "window_days": days,
        "base": base,
        "cards": cards,
    }


def load_state() -> dict:
    """Read the style state file (cached until the file changes on disk).

    Returns:
        The state dict; an empty-but-valid skeleton when the file is missing or broken.
    """
    global _cache_entry
    # The read path takes no lock: save_state publishes via os.replace, so a reader
    # always sees a complete file. The cache is a single (stamp, data) tuple, so one
    # reference read/write is atomic and stamp/data can never mismatch.
    try:
        stamp = STATE_PATH.stat().st_mtime_ns
    except OSError:
        stamp = 0
    entry = _cache_entry
    if entry is None or entry[0] != stamp:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            logger.warning(
                f"qingyu_core: 风格文件读不了（{type(exc).__name__}），先用默认值"
            )
            data = {}
        if not isinstance(data, dict):
            data = {}
        teachers = data.get("teachers")
        if not isinstance(teachers, list) or not teachers:
            teachers = list(DEFAULT_TEACHERS)
        data["teachers"] = [str(item) for item in teachers]
        if not isinstance(data.get("cards"), dict):
            data["cards"] = {}
        entry = (stamp, data)
        _cache_entry = entry
    # Return a copy whose "teachers" list is independent of the shared cache, so a
    # caller (set_teachers) can mutate it without corrupting the cached state.
    _, data = entry
    snapshot = dict(data)
    snapshot["teachers"] = list(snapshot["teachers"])
    return snapshot


def save_state(state: dict) -> None:
    """Persist the style state.

    Args:
        state: State dict to write.
    """
    global _cache_entry
    with _lock:
        temp_path = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            # Write a temp file first, then atomically replace, so no thread reads a
            # half-written JSON file.
            os.replace(temp_path, STATE_PATH)
            _cache_entry = None
        except OSError as exc:
            logger.warning(f"qingyu_core: 写风格文件失败: {exc}")
            # Do not leave the temp file behind when the replace fails.
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def needs_rebuild() -> bool:
    """Whether the stored cards are missing, stale, or written by an older version.

    Returns:
        True when a rebuild is due.
    """
    state = load_state()
    age = time.time() - float(state.get("built_at") or 0)
    return (
        int(state.get("version") or 0) != CARD_VERSION
        or not state["cards"]
        or age > REBUILD_SECONDS
    )


def ensure_cards(force: bool = False) -> dict:
    """Load the cards, rebuilding them when they are missing, stale, or from an older format.

    Args:
        force: Rebuild regardless of age.

    Returns:
        The current state dict.
    """
    with _lock:
        state = load_state()
        age = time.time() - float(state.get("built_at") or 0)
        outdated = int(state.get("version") or 0) != CARD_VERSION
        if force or outdated or not state["cards"] or age > REBUILD_SECONDS:
            if outdated and state["cards"]:
                logger.info(
                    f"qingyu_core: 卡片是旧格式（v{int(state.get('version') or 0)} → v{CARD_VERSION}），重建一次",
                )
            state = build_cards(days=WINDOW_DAYS, teachers=state["teachers"])
            save_state(state)
        return state


def card(group_id: str) -> dict:
    """Return one group's card, falling back to the overall card.

    Args:
        group_id: Platform group id.

    Returns:
        The card dict (the总体 card for a group with no samples yet, else ``{}``).
    """
    state = load_state()
    return state["cards"].get(str(group_id)) or state.get("base") or {}


def pick_answer_examples(
    group_id: str, context: str, k: int = 3, min_chars: int = 0
) -> list[str]:
    """Pick stored "how people answered" lines that fit the current question.

    Args:
        group_id: Platform group id.
        context: The question (plus a little context).
        k: How many examples to return.
        min_chars: 只挑至少这么长的例子（正经问题用，免得拿"真的""加一"当范例）。

    Returns:
        Example lines, most similar first.
    """
    pool = [
        line
        for line in (card(group_id).get("answer_examples") or [])
        if len(line) >= min_chars
    ]
    if not pool:
        return []
    scored = sorted(
        ((_similarity(context, line), line) for line in pool), key=lambda item: -item[0]
    )
    picked = [line for score, line in scored[:k] if score > 0]
    if len(picked) < k:
        for line in sorted(pool, key=len):
            if line not in picked:
                picked.append(line)
            if len(picked) >= k:
                break
    return picked[:k]


def reply_hint(
    group_id: str,
    text: str = "",
    budget: int | None = None,
    affection: int | None = None,
    context: str = "",
) -> str | None:
    """Build the「怎么答」段落 for an **被 @ / 被点名**那一轮。

    2026-09-19 的两次返工，这里是最终的分工：

    - **群味只管长度**：这个群的人说话短，所以别写成小作文（`budget` 个字以内、一句话给结论）；
    - **语气按好感度**（`flavor_for`）：关系越近，越保留她自己的口癖与温度（「嗯~」、句尾「~」、
      打趣、多聊两句）；关系越远越客气简短。第一版把口癖一并禁掉，结果"人设被淹没、
      好感度看不出作用"，所以**口癖归关系管，不归群味管**；
    - **反助手腔**：别用「首先/其次/总之」、别解说自己在做什么、别复述问题——这条跟人设无关，
      是"别像个助手"。

    Args:
        group_id: Platform group id.
        text: The incoming message.
        budget: 允许的字数上限；None 表示不压长度（正经问题）。
        affection: 说话人对她的好感度（决定口癖与温度）。
        context: Recent messages, used to pick answer examples.

    Returns:
        The hint paragraph, or None when this group has no card yet.
    """
    data = card(group_id)
    if not data:
        return None
    answer = data.get("answer") or {}
    label, _multiplier, flavor = flavor_for(affection)
    parts = [f"（你跟他/她的关系：**{label}** → {flavor}。"]
    if budget is None:
        parts.append(
            "这次是正经问题：**先给结论**，再补必需的细节；别客套、别复述问题、"
            "别用「首先/其次/总之」这种书面结构，也别写成一整篇说明。",
        )
        examples = pick_answer_examples(group_id, context or text, min_chars=12)
    else:
        if answer:
            parts.append(
                f"这个群的人说话都短（他们回答别人时中位 {answer.get('p50')} 个字），"
                "所以别写成小作文、别铺垫、别复述问题。",
            )
        parts.append(
            f"这次照群里的长度来：**{budget} 个字以内**、一句话给结论；**但你自己的口气留着**。"
        )
        parts.append(
            "**先把话答上再带口气**——哪怕关系很近，也别光顾着撒娇打趣而不回答。"
        )
        parts.append("别解说自己在做什么（别说「我看看」「让我想想」「稍等」）。")
        if float(answer.get("question_share") or 0) >= 0.4:
            parts.append("群里人回答时也常反问一句，你觉得不对就先问回去。")
        examples = pick_answer_examples(group_id, context or text)
    if examples:
        parts.append("群里人是这么答的：" + "".join(f"「{line}」" for line in examples))
    parts.append("）")
    return "".join(parts)


def length_cap(group_id: str) -> int:
    """How many characters an interjection may use in this group.

    Args:
        group_id: Platform group id.

    Returns:
        The cap in characters (fallback when there is no card yet).
    """
    return int(card(group_id).get("cap") or CAP_FALLBACK)


def answer_length_cap(group_id: str) -> int:
    """How many characters a casual answer may use in this group.

    Args:
        group_id: Platform group id.

    Returns:
        The answer cap in characters (falls back to the interjection cap).
    """
    data = card(group_id)
    return int(data.get("answer_cap") or data.get("cap") or CAP_FALLBACK)


def _similarity(left: str, right: str) -> float:
    """Character-bigram Jaccard similarity.

    Args:
        left: First text.
        right: Second text.

    Returns:
        Similarity in 0~1.
    """

    def grams(text: str) -> set[str]:
        clean = re.sub(r"\s+", "", text)
        return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}

    first, second = grams(left), grams(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def pick_exemplars(group_id: str, context: str, k: int = EXEMPLAR_K) -> list[str]:
    """Pick the stored examples that look most like the current moment.

    Args:
        group_id: Platform group id.
        context: Text of the recent messages (the current situation).
        k: How many examples to return.

    Returns:
        Example lines, most similar first (empty when there is no card).
    """
    pool = card(group_id).get("exemplars") or []
    if not pool:
        return []
    scored = sorted(
        ((_similarity(context, line), line) for line in pool),
        key=lambda item: -item[0],
    )
    picked = [line for score, line in scored[:k] if score > 0]
    if len(picked) < k:
        # 没相似的（新话题）：补几条最短的日常发言，至少让语气有个参照。
        for line in sorted(pool, key=len):
            if line not in picked:
                picked.append(line)
            if len(picked) >= k:
                break
    return picked[:k]


def hint(group_id: str, context: str = "") -> str | None:
    """Build the「怎么说」段落 for an interjection prompt.

    Args:
        group_id: Platform group id.
        context: Text of the recent messages, used to pick examples.

    Returns:
        The hint paragraph, or None when there is nothing to say.
    """
    data = card(group_id)
    if not data:
        return None
    # basis：本群自己的形状；样本不足时是总体的形状（见 _fit_to_base）。
    basis = data.get("basis") or data.get("teacher") or data.get("group") or {}
    if not basis:
        return None
    cap = int(data.get("cap") or CAP_FALLBACK)
    parts = [
        f"（这个群的人说话很短：中位 {basis.get('p50')} 个字，"
        f"{round(float(basis.get('short_share') or 0) * 100)}% 的话不超过 {SHORT_CHARS} 个字",
    ]
    if float(basis.get("tilde_share") or 0) < 0.02:
        parts.append("、没人用波浪号收尾")
    if float(basis.get("emoji_share") or 0) >= 0.05:
        parts.append(f"、约 {round(float(basis['emoji_share']) * 100)}% 的话带 emoji")
    parts.append("。")
    parts.append(
        f"你现在是**随口接一句**：只给一个反应或一句吐槽，**{cap} 个字以内**；"
        "不要提问、不要解释自己在做什么、不要点评或总结别人、不要说教。",
    )
    if float(basis.get("tilde_share") or 0) < 0.02:
        parts.append("整句话里**不要出现波浪号**，也别用「嗯~」这种开场。")
    if (
        float(basis.get("quote_share") or 0) >= 0.03
        or float(basis.get("at_share") or 0) >= 0.03
    ):
        # 2026-09-19 用户要求：插嘴时如果别人本来就在跟她互动，不用 @；群里人也很少 @（该群 22% 才用）。
        parts.append("接话就直接说事，**不用 @ 谁**（别人在跟你说话时更不用）。")
    examples = pick_exemplars(group_id, context)
    if examples:
        parts.append(
            "这个群的人平时这么说话：" + "".join(f"「{line}」" for line in examples)
        )
    parts.append("）")
    return "".join(parts)


def summary(group_id: str = "") -> str:
    """Describe the current cards for the `/风格` command.

    Args:
        group_id: When given, only this group is described in detail.

    Returns:
        A chat-ready block of text.
    """
    state = load_state()
    lines = [f"学习对象（白名单）：{'、'.join(state['teachers'])}"]
    built = int(state.get("built_at") or 0)
    if built:
        age_hours = (time.time() - built) / 3600
        lines.append(
            f"卡片构建于 {time.strftime('%m-%d %H:%M', time.localtime(built))}（{age_hours:.1f} 小时前）"
        )
    else:
        lines.append("卡片还没建过（发「/风格 重建」立刻建一次）")
    cards = state["cards"]
    base = state.get("base") or {}
    if base:
        lines.append(
            f"总体（所有群合起来）：样本 {base.get('group', {}).get('samples', 0)} 条"
            f"（学习对象 {base.get('teacher', {}).get('samples', 0)} 条）｜"
            f"中位 {(base.get('teacher') or base.get('group') or {}).get('p50')} 字｜"
            f"插嘴上限 {base.get('cap')} 字（按{base.get('cap_basis')}）｜"
            f"答话上限 {base.get('answer_cap')} 字",
        )
    if not cards:
        lines.append("没有任何群的样本。")
        return "\n".join(lines)
    for group, data in sorted(
        cards.items(), key=lambda kv: -(kv[1].get("group") or {}).get("samples", 0)
    ):
        if group_id and group != str(group_id):
            continue
        basis = data.get("teacher") or data.get("group") or {}
        drift = ""
        if base:
            drift = f"（总体 {base.get('cap')}→{data.get('cap')}）"
        lines.append(
            f"· 群 {group}：样本 {data.get('group', {}).get('samples', 0)} 条"
            f"（学习对象 {data.get('teacher', {}).get('samples', 0)} 条）｜"
            f"中位 {basis.get('p50')} 字 / p75 {basis.get('p75')} 字｜"
            f"插嘴上限 {data.get('cap')} 字{drift}（按{data.get('cap_basis')}）｜"
            f"答话上限 {data.get('answer_cap')} 字（{data.get('answer', {}).get('samples', 0)} 条回答样本）｜"
            f"波浪号 {round(float(basis.get('tilde_share') or 0) * 100)}%｜"
            f"示例 {len(data.get('exemplars') or [])} 条"
            + ("　（借了总体样本）" if data.get("borrowed") else "")
            + ("　← 当前群" if str(group_id) == group else ""),        )
    return "\n".join(lines)


def teacher_report() -> str:
    """Describe the learning whitelist with each person's usable sample count.

    为什么要把条数写出来：加进去一个"这个库里没说过话"的人，卡片其实什么也学不到，
    但原来只回一句"也拿来学了"，看着像成功、其实是空的（2026-09-19 的反馈）。

    Returns:
        A chat-ready block of text.
    """
    state = load_state()
    cards = state["cards"]
    totals: dict[str, int] = dict.fromkeys(state["teachers"], 0)
    groups_with: dict[str, int] = dict.fromkeys(state["teachers"], 0)
    has_counts = False
    for card_data in cards.values():
        counts = card_data.get("teacher_counts")
        if isinstance(counts, dict):
            has_counts = True
        for uid, count in (counts or {}).items():
            totals[uid] = totals.get(uid, 0) + int(count)
            if int(count):
                groups_with[uid] = groups_with.get(uid, 0) + 1
    if cards and not has_counts:
        # 老版本建的卡片里没有这个字段——别把它误报成"这个人学不到东西"。
        return (
            "卡片是旧格式，先发一次「/风格 重建」，再看这份名单。\n"
            f"当前学习对象：{'、'.join(state['teachers'])}"
        )
    lines = [f"学习对象（白名单）共 {len(state['teachers'])} 个："]
    for uid in state["teachers"]:
        count = totals.get(uid, 0)
        if count:
            lines.append(
                f"· {uid}：可用发言 {count} 条（分布在 {groups_with.get(uid, 0)} 个群）"
            )
        else:
            lines.append(f"· {uid}：⚠ 这个库里没有他/她的发言，暂时学不到东西")
    lines.append(
        "加/删：/风格 老师 加 10001｜/风格 老师 删 10001（QQ 号或昵称都行）"
    )
    return "\n".join(lines)


def set_teachers(action: str, uid: str = "") -> str:
    """Add or remove a learning source (the whitelist behind「学谁」).

    Args:
        action: ``list``, ``add`` or ``remove``.
        uid: QQ number for add/remove.

    Returns:
        A chat-ready result message (含这个人有多少条可用发言)。
    """
    with _lock:
        state = load_state()
        teachers = list(state["teachers"])
        if action == "list":
            return teacher_report()
        uid = str(uid).strip()
        if not uid.isdigit():
            return "要填 QQ 号哦，例如：/风格 老师 加 10001"
        if action == "add":
            if uid in teachers:
                return f"{uid} 已经在学习名单里了。"
            teachers.append(uid)
            message = f"好，{uid} 的发言也拿来学了。"
        elif action == "remove":
            if uid not in teachers:
                return f"{uid} 不在学习名单里。"
            if len(teachers) <= 1:
                return "至少要留一个学习对象，不然没得学。要换人的话先加新的再删旧的。"
            teachers.remove(uid)
            message = f"好，{uid} 不再作为学习对象了。"
        else:
            return (
                "用法：/风格 老师｜/风格 老师 加 10001｜/风格 老师 删 10001"
            )
        state["teachers"] = teachers
        save_state(state)
        fresh = ensure_cards(force=True)
        samples = sum(
            int((card_data.get("teacher_counts") or {}).get(uid, 0))
            for card_data in (fresh.get("cards") or {}).values()
        )
        if action == "add":
            message += (
                f"这个库里他/她有 {samples} 条发言，卡片已按最新的重建。"
                if samples
                else "不过这个库里查不到他/她的发言（可能不在这些群里），暂时学不到东西。"
            )
        logger.info(
            f"qingyu_core: 风格学习对象改为 {teachers}（{uid} 可用发言 {samples} 条）"
        )
        return message


def selftest() -> str:
    """Build the cards once and describe them (used by the offline checker).

    Returns:
        A summary block.
    """
    state = ensure_cards(force=True)
    return summary() if state else "建卡片失败"
