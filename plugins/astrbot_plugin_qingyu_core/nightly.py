"""夜间批处理：把当天群聊里值得记住的东西抽出来（阶段一的补充写入路径）。

逐条消息调模型太贵也太吵，所以只在一天结束时跑一次：读 AstrBot 存的群历史，
交给 flash 模型抽 3~5 条，进低置信库；用户随时可以用 ``/忘记我`` 删掉。

提示词要求模型按 ``昵称 | 类型 | 内容`` 一行一条输出，解析失败就当天不记。
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# AstrBot 的历史库在数据根目录下，不在 plugin_data 里——第一版写成了 plugin_data，
# 结果每天抽取都在"记录太少"里空转（可读记录 0 条）。这里按候选顺序找，命中即用。
_PLUGIN_DATA = Path(get_astrbot_plugin_data_path())
DB_CANDIDATES = (
    _PLUGIN_DATA.parent / "data_v4.db",
    Path.home() / ".astrbot" / "data" / "data_v4.db",
    _PLUGIN_DATA / "data_v4.db",
)
DB_PATH = next((path for path in DB_CANDIDATES if path.exists()), DB_CANDIDATES[0])
MAX_TRANSCRIPT_LINES = 400
MAX_TRANSCRIPT_CHARS = 6000
# 一次抽取最多入库几条（模型可能一口气给很多行）。
MAX_EXTRACT_PER_DAY = 5

EXTRACT_SYSTEM_PROMPT = (
    "你在帮一个 QQ 群机器人整理「值得记住的旧事」。"
    "输入是群里一天的聊天记录，输出 0~5 行，每行格式固定："
    "昵称 | 类型 | 内容。类型只能是 事实/喜好/事情/关系 之一。"
    "类型写成 关系 时，后面再加一列对方的昵称：昵称 | 关系 | 内容 | 对方昵称，"
    "两边的昵称都必须用记录里出现过的名字。"
    "判断标准：**过一个月再提起来还有意义**的才记。"
    "可以记：长期身份与状况（专业、年级、在准备什么考试/比赛/项目）、"
    "稳定喜好（常玩什么、爱吃什么、讨厌什么）、约定与承诺、人际（谁和谁一起做什么）。"
    "不要记：今天/今晚/刚才的临时安排、一次性的吐槽与玩笑、"
    "机器人自己说过的话、没头没尾的半句话、以及只是转述别人说了什么的流水账。"
    "内容控制在 30 字内，用第三人称转述（例如：下个月要考四级）。"
    "没有值得记的就输出「无」。不要解释、不要编号、不要 Markdown。"
    "宁少勿滥：一天最多 5 条，凑不满就少写几条。"
)


def load_transcript(group_id: str, since_hours: int = 24) -> list[tuple[str, str]]:
    """Read the group's recent messages from AstrBot's own history table.

    Args:
        group_id: Platform group id.
        since_hours: How far back to read.

    Returns:
        ``(nickname, text)`` lines in chronological order.
    """
    if not DB_PATH.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=since_hours)
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT sender_name, content, created_at FROM platform_message_history"
            " WHERE user_id LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{group_id}%", MAX_TRANSCRIPT_LINES),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(f"qingyu_core: 读群历史失败: {exc}")
        return []
    finally:
        connection.close()

    lines: list[tuple[str, str]] = []
    total = 0
    for sender, content, created_at in reversed(rows):
        # 这一列是 **UTC**（实测：本机 16:47 时它写 08:47），而 cutoff 是本地时间——
        # 不换算的话"最近 24 小时"整体错 8 小时，抽取会把最新的聊天记录全漏掉。
        try:
            stamp = datetime.fromisoformat(str(created_at))
        except ValueError:
            stamp = None
        if stamp is not None:
            local = stamp.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
            if local < cutoff:
                continue
        text = _flatten(content)
        if not text or len(text) < 4:
            continue
        lines.append((str(sender or "某人"), text))
        total += len(text)
        if total >= MAX_TRANSCRIPT_CHARS:
            break
    return lines


def _flatten(content: str) -> str:
    """Turn one stored message row into plain text.

    Args:
        content: JSON encoded message content.

    Returns:
        The text of the message, or an empty string for non-text rows.
    """
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return str(content or "").strip()
    if not isinstance(parsed, dict):
        return ""
    if parsed.get("type") == "bot":
        return ""
    parts = []
    for segment in parsed.get("message", []):
        if isinstance(segment, dict) and segment.get("type") == "plain":
            parts.append(str(segment.get("text") or ""))
    return " ".join(part for part in parts if part).strip()


def build_prompt(lines: list[tuple[str, str]]) -> str:
    """Render the transcript for the extractor model.

    Args:
        lines: ``(nickname, text)`` lines.

    Returns:
        The user prompt.
    """
    body = "\n".join(f"{nickname}: {text}" for nickname, text in lines)
    return f"今天的群聊记录：\n{body}\n\n请按格式输出值得记住的旧事。"


def name_map(people: list[tuple[str, str]]) -> dict[str, str]:
    """Build a nickname -> uid map for attaching memories to owners.

    Args:
        people: ``(uid, nickname)`` pairs known from the database.

    Returns:
        The nickname map.
    """
    return {nickname: uid for uid, nickname in people if nickname}


def name_map_from_history(group_id: str, since_hours: int = 72) -> dict[str, str]:
    """Collect nickname -> uid pairs from the stored group messages.

    The extractor only sees nicknames, so they must be resolved back to ids.
    AstrBot's history table is the most complete source of that mapping.

    Args:
        group_id: Platform group id.
        since_hours: How far back to look.

    Returns:
        The nickname map.
    """
    if not DB_PATH.exists():
        return {}
    cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat()
    connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT sender_name, sender_id, MAX(created_at) FROM platform_message_history"
            " WHERE user_id LIKE ? AND created_at >= ? AND sender_name != ''"
            " GROUP BY sender_name",
            (f"%{group_id}%", cutoff),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {str(name): str(uid) for name, uid, _stamp in rows if name and uid}
