"""情节记忆层（阶段一）：记人不记事的东西，隔几天自己提起来。

写入有两条路：
- ``remember``：轻语自己觉得值得记的时候调用（低置信）；
- 夜间抽取：把当天记录交给 flash 模型抽几条（极低置信）。

读出只有一条路：按"相关度 × 新鲜度 × 未用次数"打分，**按概率注入**，每轮最多
1~2 条，并且明确标注这是旧事、可能不准——因为群友能间接喂她记忆。
"""

import random
import re
import time

from astrbot.core import logger

from . import store

KINDS = ("fact", "preference", "event", "relation")
KIND_LABELS = {
    "fact": "事实",
    "preference": "喜好",
    "event": "事情",
    "relation": "关系",
}
MAX_TEXT_CHARS = 80
# 注入概率：基础、相关度加成、久未提起加成、刚提过惩罚。
P_BASE = 0.25
P_RELEVANCE = 0.35
P_STALE = 0.20
P_RECENT_PENALTY = 0.40
RECENT_HOURS = 24
# 刚提过的旧事不再提（免得变复读机）。
REPEAT_COOLDOWN_HOURS = 6
MAX_PER_TURN = 2
# 权重衰减：每被使用一次掉多少，长期不用自然沉底。
WEIGHT_DECAY = 0.12
MIN_WEIGHT = 0.15
# 模型自动抽取的置信度上限（人工写的是 1.0）。
AUTO_CONFIDENCE = 0.4
TOOL_CONFIDENCE = 0.6
STOPWORDS = set("的了是我你他她它们在有和与及就都而也不很这那请问一下怎么什么吗呢吧啊呀哦嘛")
# 这些词太泛，命中它们不代表话题相关。
GENERIC_TERMS = {
    "今天", "明天", "昨天", "现在", "时候", "可以", "这个", "那个", "我们",
    "他们", "你们", "一下", "东西", "事情", "问题", "知道", "觉得", "应该",
    "还是", "就是", "有点", "真的", "然后", "如果", "因为", "所以",
}


def clean_text(text: str) -> str:
    """Normalize a memory line.

    Args:
        text: Raw text from the model or the nightly extractor.

    Returns:
        A single-line, length-capped string.
    """
    flat = re.sub(r"\s+", " ", text or "").strip()
    flat = flat.strip("　 \"'“”")
    return flat[:MAX_TEXT_CHARS]


def remember(
    connection,
    *,
    uid: str,
    group_id: str,
    text: str,
    kind: str = "fact",
    confidence: float = TOOL_CONFIDENCE,
    source: str = "tool",
    related_uid: str = "",
    ts: int | None = None,
) -> int | None:
    """Store one episodic memory, skipping duplicates.

    Args:
        connection: Open core database connection.
        uid: Owner of the memory (empty for group-wide facts).
        group_id: Group the memory came from.
        text: Memory text.
        kind: One of :data:`KINDS`.
        confidence: 0~1 trust level.
        source: Who wrote it (``tool``, ``nightly``, ``admin``).
        related_uid: For ``relation`` memories, the other person involved (阶段五).
        ts: Timestamp.

    Returns:
        The new memory id, or None when it was a duplicate or empty.
    """
    cleaned = clean_text(text)
    if len(cleaned) < 4:
        return None
    if kind not in KINDS:
        kind = "fact"
    existing = connection.execute(
        "SELECT id FROM memories WHERE uid = ? AND text = ?",
        (uid, cleaned),
    ).fetchone()
    if existing:
        return None
    stamp = ts or store.now()
    # 只有"关系"记忆才带第二个人，避免把普通记忆误标成两人之间的事。
    other = related_uid if (kind == "relation" and related_uid != uid) else ""
    cursor = connection.execute(
        "INSERT INTO memories (uid, group_id, kind, text, confidence, weight,"
        " created_at, last_used_at, use_count, source, related_uid)"
        " VALUES (?, ?, ?, ?, ?, 1.0, ?, 0, 0, ?, ?)",
        (uid, group_id, kind, cleaned, float(confidence), stamp, source, other),
    )
    connection.commit()
    logger.info(
        f"qingyu_core: 记住 {uid or '本群'}（{KIND_LABELS[kind]}，"
        f"置信 {confidence}）：{cleaned[:40]}",
    )
    return int(cursor.lastrowid)


def forget_person(connection, uid: str) -> int:
    """Delete every memory about one person.

    Args:
        connection: Open core database connection.
        uid: Sender id.

    Returns:
        How many memories were removed.
    """
    cursor = connection.execute("DELETE FROM memories WHERE uid = ?", (uid,))
    connection.commit()
    return int(cursor.rowcount)


def _terms(query: str) -> list[str]:
    """Split a query into search terms.

    Args:
        query: Raw query text.

    Returns:
        ASCII tokens plus 2~3 character Chinese n-grams, minus generic words.
    """
    terms: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9_.+-]+|[^A-Za-z0-9_.+-]+", query or ""):
        if re.fullmatch(r"[A-Za-z0-9_.+-]{2,}", chunk):
            terms.append(chunk.lower())
            continue
        chars = "".join(ch for ch in chunk if ch not in STOPWORDS)
        for size in (3, 2):
            for start in range(0, max(0, len(chars) - size + 1)):
                terms.append(chars[start : start + size])
    seen: list[str] = []
    for term in terms:
        if term and term not in seen and term not in GENERIC_TERMS:
            seen.append(term)
    return seen[:24]


def _relevance(memory_text: str, query_terms: list[str]) -> float:
    """Score how well a memory matches the current topic.

    A single 2-character hit is already meaningful in Chinese (四级、考研、学生票),
    so one match is enough to count as relevant; extra matches push it higher.

    Args:
        memory_text: Stored memory text.
        query_terms: Terms built from the current message.

    Returns:
        A 0~1 relevance score.
    """
    if not query_terms:
        return 0.0
    lowered = memory_text.lower()
    matched = [term for term in query_terms if term in lowered]
    if not matched:
        return 0.0
    longest = max(len(term) for term in matched)
    return min(1.0, 0.5 + 0.2 * (len(matched) - 1) + 0.15 * (longest - 2))


def recall(
    connection,
    *,
    uid: str,
    group_id: str,
    query: str,
    limit: int = 3,
    ts: int | None = None,
) -> list[dict]:
    """Pick memories worth surfacing for a person in this group.

    Scoring mixes topic relevance, staleness and how often the memory was used,
    then a memory is kept with a probability so the bot does not turn into a
    broken record.

    Relation memories (阶段五) are those where the person is the second party
    (``related_uid``): she remembers "who did what with whom", so they also
    surface for the other side. They get a small bonus for being interesting,
    but at most one per turn so she does not gossip about a third person.

    Args:
        connection: Open core database connection.
        uid: Speaker whose memories may be surfaced.
        group_id: Group the turn happens in.
        query: Current message plus recent topic words.
        limit: Maximum number of memories to return.
        ts: Current timestamp.

    Returns:
        A list of dicts with id/text/kind/confidence.
    """
    stamp = ts or store.now()
    rows = connection.execute(
        "SELECT id, uid, related_uid, text, kind, confidence, weight,"
        " last_used_at, use_count, created_at"
        " FROM memories WHERE (uid = ? OR related_uid = ? OR (uid = '' AND group_id = ?))"
        " AND weight >= ?"
        " ORDER BY weight DESC, created_at DESC LIMIT 150",
        (uid, uid, group_id, MIN_WEIGHT),
    ).fetchall()
    terms = _terms(query)
    scored: list[tuple[float, dict, float]] = []
    for row in rows:
        memory = dict(row)
        # 自己是第二当事人的"关系"记忆：相关度按内容判，另加一点兴趣分。
        is_relation = bool(memory["related_uid"]) and memory["uid"] != uid
        relevance = _relevance(str(row["text"]), terms)
        age_hours = max(0.0, (stamp - int(row["created_at"])) / 3600.0)
        staleness = min(1.0, age_hours / (24.0 * 7))
        used_recently = int(row["last_used_at"]) > 0 and (
            stamp - int(row["last_used_at"]) < RECENT_HOURS * 3600
        )
        score = (
            0.1
            + P_RELEVANCE * relevance
            + P_STALE * staleness
            + 0.1 * min(1.0, int(row["use_count"]) / 3.0)
            + (0.15 if is_relation else 0.0)
            - (0.25 if used_recently else 0.0)
        )
        scored.append((score, memory, relevance))
    scored.sort(key=lambda item: item[0], reverse=True)

    picked: list[dict] = []
    relation_used = 0
    for score, memory, relevance in scored:
        if len(picked) >= limit:
            break
        is_relation = bool(memory["related_uid"]) and memory["uid"] != uid
        # 没聊到相关话题时，别人的关系记忆不要主动抖出来。
        if is_relation and relevance < 0.5 and relation_used >= 1:
            continue
        last_used = int(memory["last_used_at"])
        if last_used and stamp - last_used < REPEAT_COOLDOWN_HOURS * 3600:
            continue  # 刚说过，这轮别再提
        # 相关度高的必给（比如他正好在聊这件事），其余按概率给。
        if relevance >= 0.5 or random.random() < max(0.0, min(1.0, score)):
            picked.append(memory)
            if is_relation:
                relation_used += 1
    return picked


def mark_used(connection, memory_ids: list[int], ts: int | None = None) -> None:
    """Age the weight of memories that were just injected.

    Args:
        connection: Open core database connection.
        memory_ids: Memory ids that went into the prompt.
        ts: Current timestamp.
    """
    if not memory_ids:
        return
    stamp = ts or store.now()
    for memory_id in memory_ids:
        connection.execute(
            "UPDATE memories SET last_used_at = ?, use_count = use_count + 1,"
            " weight = MAX(?, weight - ?) WHERE id = ?",
            (stamp, MIN_WEIGHT, WEIGHT_DECAY, memory_id),
        )
    connection.commit()


def log_recall(connection, turn_id: str, memory_ids: list[int], ts: int | None = None) -> None:
    """Record which memories were injected, so effectiveness can be measured.

    Args:
        connection: Open core database connection.
        turn_id: Identifier of the turn.
        memory_ids: Memory ids injected.
        ts: Current timestamp.
    """
    if not memory_ids:
        return
    stamp = ts or store.now()
    connection.executemany(
        "INSERT INTO recall_log (turn_id, memory_id, ts) VALUES (?, ?, ?)",
        [(turn_id, memory_id, stamp) for memory_id in memory_ids],
    )
    connection.commit()


def format_for_prompt(
    memories: list[dict],
    nickname: str,
    names: dict[str, str] | None = None,
    self_uid: str = "",
) -> str:
    """Render recalled memories as a prompt block.

    The wording matters: it tells the model these are old, possibly wrong notes
    that it may bring up naturally, not facts to recite.

    Args:
        memories: Rows returned by :func:`recall`.
        nickname: Display name of the person they belong to.
        names: Optional uid -> nickname map, used to name the owner of a
            relation memory that the speaker is only the second party of.
        self_uid: Speaker id, so a relation memory owned by someone else can be
            attributed instead of being read as the speaker's own note.

    Returns:
        The prompt text, or an empty string when there is nothing to add.
    """
    if not memories:
        return ""
    who = nickname or "对方"
    labels = names or {}
    lines = []
    for memory in memories:
        label = KIND_LABELS.get(str(memory["kind"]), "事实")
        doubt = "（不太确定）" if float(memory["confidence"]) < 0.5 else ""
        owner_uid = str(memory.get("uid") or "")
        # 关系记忆里自己是第二当事人时，点明这条是谁的事，免得她把主角认错。
        owner = labels.get(owner_uid, "") if owner_uid and owner_uid != self_uid else ""
        extra = f"（{owner}的事，你也在场）" if str(memory.get("related_uid") or "") and owner else ""
        lines.append(f"- {label}：{memory['text']}{extra}{doubt}")
    return (
        f"\n【你记得关于{who}的一些旧事（可能过时或不准，别当成事实背出来，"
        "合适的时候可以自然提一句）】\n" + "\n".join(lines)
    )


def stats(connection) -> dict:
    """Summarize the memory store for the status command.

    Args:
        connection: Open core database connection.

    Returns:
        Counts and the newest entries.
    """
    total = connection.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
    auto = connection.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE confidence < 0.5",
    ).fetchone()["n"]
    used = connection.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE use_count > 0",
    ).fetchone()["n"]
    relations = connection.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE kind = 'relation' AND related_uid != ''",
    ).fetchone()["n"]
    recent = connection.execute(
        "SELECT uid, kind, text, created_at FROM memories ORDER BY created_at DESC LIMIT 5",
    ).fetchall()
    return {
        "total": int(total),
        "auto": int(auto),
        "used": int(used),
        "relations": int(relations),
        "recent": [dict(row) for row in recent],
    }


def prune(connection, *, min_weight: float = 0.0, keep_days: int = 365) -> int:
    """Drop memories that have faded away or are ancient.

    Args:
        connection: Open core database connection.
        min_weight: Delete memories whose weight fell below this.
        keep_days: Keep at least this many days of history.

    Returns:
        How many rows were deleted.
    """
    cutoff = store.now() - keep_days * 86400
    cursor = connection.execute(
        "DELETE FROM memories WHERE (weight <= ? AND use_count = 0 AND created_at < ?)"
        " OR created_at < ?",
        (min_weight, store.now() - 30 * 86400, cutoff),
    )
    connection.commit()
    return int(cursor.rowcount)


def age_weight(connection, *, days: int = 1) -> int:
    """Decay the weight of memories that were never reused.

    Args:
        connection: Open core database connection.
        days: How many days of decay to apply.

    Returns:
        How many rows changed.
    """
    cursor = connection.execute(
        "UPDATE memories SET weight = MAX(?, weight - ?)"
        " WHERE last_used_at > 0 AND last_used_at < ?",
        (MIN_WEIGHT, 0.05 * days, store.now() - days * 86400),
    )
    connection.commit()
    return int(cursor.rowcount)


def parse_extraction(reply: str, known_names: dict[str, str]) -> list[dict]:
    """Parse the nightly extractor's reply into memory drafts.

    The model is asked for one memory per line in the form
    ``昵称 | 类型 | 内容``, where 类型 is one of 事实/喜好/事情/关系. A 关系 line
    carries a fourth field naming the other person (阶段五), which is resolved
    back to a uid so the memory surfaces for both sides.

    Args:
        reply: Raw model output.
        known_names: Nickname -> uid map used to attach owners.

    Returns:
        Draft dicts with uid/nickname/text/kind/confidence/related_uid.
    """
    label_to_kind = {label: kind for kind, label in KIND_LABELS.items()}
    drafts: list[dict] = []
    for line in (reply or "").splitlines():
        parts = [part.strip() for part in line.strip().lstrip("-*　 ").split("|")]
        if len(parts) < 3:
            continue
        nickname, label, rest = parts[0], parts[1], parts[2:]
        kind = label_to_kind.get(label, "fact")
        related_uid = ""
        related_name = ""
        # 关系行：最后一列是对方昵称，内容是不含那一列的中间部分。
        if kind == "relation" and len(rest) >= 2:
            other = known_names.get(rest[-1], "")
            if other and other != known_names.get(nickname, ""):
                related_uid = other
                related_name = rest[-1]
                rest = rest[:-1]
        cleaned = clean_text("|".join(rest))
        if len(cleaned) < 6:
            continue
        drafts.append(
            {
                "uid": known_names.get(nickname, ""),
                "nickname": nickname,
                "text": cleaned,
                "kind": kind,
                "confidence": AUTO_CONFIDENCE,
                "related_uid": related_uid,
                "related_name": related_name,
            },
        )
    return drafts


def delete_recent(connection, *, hours: int = 24, source: str = "nightly") -> int:
    """Delete freshly written memories from one source.

    Used while tuning extraction quality: a bad batch can be purged without
    touching what the bot remembered herself.

    Args:
        connection: Open core database connection.
        hours: Only delete memories created within this many hours.
        source: Which writer they came from (``nightly`` / ``tool`` / ``admin``).

    Returns:
        How many memories were removed.
    """
    cutoff = store.now() - hours * 3600
    cursor = connection.execute(
        "DELETE FROM memories WHERE source = ? AND created_at >= ?",
        (source, cutoff),
    )
    connection.commit()
    logger.info(
        f"qingyu_core: 清理最近 {hours} 小时的 {source} 记忆 {cursor.rowcount} 条",
    )
    return int(cursor.rowcount)


def humanize_age(created_at: int, now_ts: int | None = None) -> str:
    """Describe how old a memory is.

    Args:
        created_at: Creation timestamp.
        now_ts: Current timestamp.

    Returns:
        A Chinese age label.
    """
    stamp = now_ts or int(time.time())
    hours = max(0.0, (stamp - created_at) / 3600.0)
    if hours < 1:
        return "刚刚"
    if hours < 24:
        return f"{int(hours)} 小时前"
    days = int(hours / 24)
    if days < 30:
        return f"{days} 天前"
    return f"{int(days / 30)} 个月前"
