"""周报（阶段六）：把一周的决策、记忆、工具与节奏摊开，对照架构里的指标看趋势。

数据全部来自本插件自己的表，不需要额外埋点：

- ``turns``      每一条群消息都有一行 → 决策分布、插嘴后有没有人接话、回复耗时、token；
- ``recall_log`` 哪一轮注入了哪条旧事 → 注入率；
- ``memories``   新增/被用过的记忆 → 记忆是否真的有用；
- ``tool_calls`` LLM 工具调用（阶段六新增）→ 知识库查证率、表情包次数。

三处口径说明（不假装精确）：

- **拒绝率** 的分母是"她开了口的轮次"（respond + chime + refuse），不是全部消息——
  群里绝大多数消息她本来就不说话，拿全部消息当分母会把这个数压到 1% 以下；
- **专业问题查证率** 没有逐条判题，先看"调用 knowledge_lookup 的轮次 / 答复轮次"
  这个覆盖率，趋势比绝对值更有意义；
- **首字延迟** 记的是 `express` 的人为停顿（0.4~2.5 秒），`turns.latency_ms` 是整轮
  耗时（含模型调用），只作参考、不参与达标判定。
"""

from datetime import datetime

from . import decide, express, memory, store

# 架构文档 §十一 的目标区间。
TARGET_OBSERVE = 0.70
TARGET_CHIME_RANGE = (0.10, 0.30)
TARGET_REFUSE_RANGE = (0.03, 0.10)
TARGET_REPLY_RATE = 0.40
# 插嘴之后多久之内有人说话算"插得是时候"。
CHIME_REPLY_WINDOW = 60


def week_key(ts: int | None = None) -> str:
    """本周的键（ISO 年-周），用来判断这周的周报出过没有。

    Args:
        ts: 时间戳，默认取当前时间。

    Returns:
        形如 ``2026-W38`` 的字符串。
    """
    moment = datetime.fromtimestamp(ts if ts is not None else store.now())
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def log_call(connection, *, turn_id: str, tool: str, ts: int | None = None) -> None:
    """记一次 LLM 工具调用（周报的数据来源）。

    Args:
        connection: 核心库连接。
        turn_id: 这一轮的编号；不在感知链里的调用可以留空。
        tool: 工具名，例如 ``knowledge_lookup``。
        ts: 时间戳。
    """
    connection.execute(
        "INSERT INTO tool_calls (turn_id, tool, ts) VALUES (?, ?, ?)",
        (turn_id, tool, ts if ts is not None else store.now()),
    )
    connection.commit()


def _percent(part: int, whole: int) -> str:
    """把比例写成百分比文本。

    Args:
        part: 分子。
        whole: 分母。

    Returns:
        形如 ``87%`` 的字符串；分母为 0 时返回 ``-``。
    """
    if whole <= 0:
        return "-"
    return f"{round(part * 100 / whole)}%"


def _tick(ok: bool) -> str:
    """达标标记。

    Args:
        ok: 是否落在目标区间里。

    Returns:
        ``✅`` 或 ``⚠️``。
    """
    return "✅" if ok else "⚠️"


def build_report(connection, *, days: int = 7, ts: int | None = None) -> str:
    """生成最近若干天的评估周报。

    Args:
        connection: 核心库连接。
        days: 统计窗口，默认一周。
        ts: 当前时间戳。

    Returns:
        多行纯文本报表（可直接发到群里或写进日志）。
    """
    now = ts if ts is not None else store.now()
    since = now - days * 86400
    actions = {
        str(row["action"]): int(row["n"])
        for row in connection.execute(
            "SELECT action, COUNT(*) AS n FROM turns WHERE ts >= ? GROUP BY action",
            (since,),
        )
    }
    planned = {
        str(row["planned_action"] or "（升级前）"): int(row["n"])
        for row in connection.execute(
            "SELECT planned_action, COUNT(*) AS n FROM turns WHERE ts >= ?"
            " GROUP BY planned_action",
            (since,),
        )
    }
    total = sum(actions.values())
    spoke = sum(actions.get(name, 0) for name in ("respond", "chime", "refuse"))
    chimes = actions.get("chime", 0)
    refusals = actions.get("refuse", 0)

    # 插嘴之后 60 秒内群里有没有人继续说话（每条群消息都有一行 turns，所以能这么查）。
    chime_rows = connection.execute(
        "SELECT ts, umo FROM turns WHERE ts >= ? AND action = 'chime'",
        (since,),
    ).fetchall()
    answered = 0
    for row in chime_rows:
        follow = connection.execute(
            "SELECT 1 FROM turns WHERE umo = ? AND ts > ? AND ts <= ? LIMIT 1",
            (row["umo"], row["ts"], int(row["ts"]) + CHIME_REPLY_WINDOW),
        ).fetchone()
        if follow:
            answered += 1

    latency = [
        int(row["latency_ms"])
        for row in connection.execute(
            "SELECT latency_ms FROM turns WHERE ts >= ? AND latency_ms > 0",
            (since,),
        )
    ]
    tokens = connection.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0) AS total,"
        " COALESCE(SUM(reply_chars), 0) AS chars FROM turns WHERE ts >= ?",
        (since,),
    ).fetchone()

    injected_rows = connection.execute(
        "SELECT COALESCE(SUM(injected), 0) AS n FROM turns WHERE ts >= ?",
        (since,),
    ).fetchone()
    memory_stats = memory.stats(connection)
    new_memories = connection.execute(
        "SELECT source, COUNT(*) AS n FROM memories WHERE created_at >= ? GROUP BY source",
        (since,),
    ).fetchall()
    new_total = sum(int(row["n"]) for row in new_memories)
    new_auto = sum(int(row["n"]) for row in new_memories if str(row["source"]) == "nightly")

    tools = {
        str(row["tool"]): int(row["n"])
        for row in connection.execute(
            "SELECT tool, COUNT(*) AS n FROM tool_calls WHERE ts >= ? GROUP BY tool"
            " ORDER BY n DESC",
            (since,),
        )
    }
    groups = connection.execute(
        "SELECT umo, COUNT(*) AS n FROM turns WHERE ts >= ? GROUP BY umo"
        " ORDER BY n DESC LIMIT 3",
        (since,),
    ).fetchall()

    observe_share = (actions.get("observe", 0) / total) if total else 0.0
    chime_share = (planned.get("chime", 0) / total) if total else 0.0
    refuse_share = (refusals / spoke) if spoke else 0.0
    avg_latency = (sum(latency) / len(latency) / 1000.0) if latency else 0.0
    max_latency = (max(latency) / 1000.0) if latency else 0.0
    reply_rate = (answered / chimes) if chimes else 0.0
    lookup = tools.get("knowledge_lookup", 0)
    memes = tools.get("send_meme", 0)

    start = datetime.fromtimestamp(since).strftime("%m-%d %H:%M")
    end = datetime.fromtimestamp(now).strftime("%m-%d %H:%M")
    lines = [
        f"轻语周报（{start} ~ {end}，{days} 天）",
        f"决策：{total} 轮｜observe {actions.get('observe', 0)}"
        f"（{_percent(actions.get('observe', 0), total)} {_tick(observe_share > TARGET_OBSERVE)}"
        f" 目标 >70%）｜respond {actions.get('respond', 0)}｜chime {chimes}"
        f"｜refuse {refusals}",
        f"评估想插嘴：{planned.get('chime', 0)} 轮（{_percent(planned.get('chime', 0), total)}"
        f" {_tick(TARGET_CHIME_RANGE[0] <= chime_share <= TARGET_CHIME_RANGE[1])}"
        f" 目标 10~30%）",
        f"拒绝率：{_percent(refusals, spoke)}（{refusals}/{spoke} 次开口"
        f" {_tick(TARGET_REFUSE_RANGE[0] <= refuse_share <= TARGET_REFUSE_RANGE[1])}"
        f" 目标 3~10%）",
        f"插嘴后 60 秒有人接话：{_percent(answered, chimes)}（{answered}/{chimes}"
        f" {_tick(reply_rate > TARGET_REPLY_RATE)} 目标 >40%）",
        f"回复节奏：人为开口停顿 {express.MIN_DELAY_MS}~{express.MAX_DELAY_MS} 毫秒"
        f"（目标 0.4~2.5 秒）；整轮平均 {avg_latency:.1f} 秒，最慢 {max_latency:.1f} 秒"
        f"（含模型调用，仅供参考）｜回复 {int(tokens['chars'])} 字",
        f"记忆：共 {memory_stats['total']} 条（关系 {memory_stats['relations']}），"
        f"本周新增 {new_total}（自动 {new_auto}），"
        f"注入 {int(injected_rows['n'])} 轮（{_percent(int(injected_rows['n']), total)}），"
        f"被用过 {memory_stats['used']} 条",
        f"工具：knowledge_lookup {lookup} 次（答复轮次覆盖 "
        f"{_percent(lookup, spoke)}）｜send_meme {memes} 次｜"
        f"remember {tools.get('remember', 0)} 次",
        f"用量：{int(tokens['total'])} token；最活跃："
        + "、".join(f"{str(row['umo']).split(':')[-1]}（{row['n']} 轮）" for row in groups),
        f"判定阈值：插嘴门槛 {decide.CHIME_AT}｜每群每日插嘴上限 {decide.CHIME_DAILY_CAP}",
    ]
    return "\n".join(lines)
