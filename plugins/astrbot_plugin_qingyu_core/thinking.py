"""思考开关：让不让她"先想一想"（DeepSeek V4 的推理模式）。

**为什么要这个开关**（2026-09-27 实测）：DeepSeek V4.1 Flash 默认**带思考**，同一句普通问话：

| 请求 | 耗时 | 思考 token | 回答 |
| --- | --- | --- | --- |
| `deepseek-flash`（默认，带思考） | 1.7–33 s | 137–6000 | 有的问题思考会**吃光 max_tokens 导致回复为空** |
| `deepseek-chat`（同一模型的**不思考入口**） | **0.82 s** | 无 | 正常 |
| `deepseek-flash` + `reasoning_effort: none` | 0.75 s | 无 | 正常 |

桌宠是"聊天伙伴"，回一句话先想十几秒是纯浪费；但遇到真需要动脑的问题（算题、写东西），
让她想一想确实更好。所以做成**开关**：默认**关**（快），需要时打开。

**怎么实现的**：`ProviderRequest.model` 是插件能改的字段，而请求体里的 `model` 就是它
（`tool_loop_agent_runner` 里 `payload["model"] = self.req.model`），所以这里**只换模型名**：
不思考走 `deepseek-chat`，思考走 `deepseek-flash`——**不用改 AstrBot 的配置、不用重启**，
QQ 和桌宠两条通道都立刻生效。

**只对 DeepSeek 生效、且只认那几个别名**：如果这个会话用的是别的模型（比如智谱的 GLM），
或者管理员自己选了 `deepseek-v4-pro` 这种更强的模型，这里**一概不插手**——
免得把别人精心选的模型偷偷换掉。

状态落在 ``plugin_data/qingyu_thinking.json``：``{"thinking": true}`` 表示开；删掉文件就是默认（关）。
"""

import json
import time
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_thinking.json"
# 每条消息都摸磁盘没必要，缓存几秒（和 tuning.py 一个路子）
CACHE_SECONDS = 5.0
# 默认**关**：桌宠要的是"秒回"，思考留给需要的时候
DEFAULT_THINKING = False
# 带思考 / 不带思考的两个入口（实测都指向 V4.1 Flash）
THINK_MODEL = "deepseek-flash"
FAST_MODEL = "deepseek-chat"
# 只有这几个名字才允许被换掉（管理员选了别的模型就别动）
SWITCHABLE = ("", FAST_MODEL, THINK_MODEL, "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")

_cache: bool | None = None
_cache_at = 0.0


def _read() -> bool:
    """Read the switch from disk (missing/broken file means the default).

    Returns:
        True when thinking is enabled.
    """
    try:
        raw = PATH.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return DEFAULT_THINKING
    except OSError as exc:
        logger.warning(f"qingyu_core: 思考开关读不了（{type(exc).__name__}），用默认值")
        return DEFAULT_THINKING
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("qingyu_core: 思考开关不是合法 JSON，用默认值")
        return DEFAULT_THINKING
    if not isinstance(data, dict):
        return DEFAULT_THINKING
    value = data.get("thinking")
    return bool(value) if isinstance(value, bool) else DEFAULT_THINKING


def enabled() -> bool:
    """Is thinking currently on?

    Returns:
        The switch state (cached for a few seconds).
    """
    global _cache, _cache_at
    now = time.time()
    if _cache is None or now - _cache_at >= CACHE_SECONDS:
        _cache = _read()
        _cache_at = now
    return bool(_cache)


def set_enabled(value: bool) -> str:
    """Flip the switch and return a chat-ready message.

    Args:
        value: New state.

    Returns:
        Text describing the result.
    """
    global _cache, _cache_at
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["thinking"] = bool(value)
    data["updated_at"] = int(time.time())
    try:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return f"写开关文件失败：{exc}"
    _cache, _cache_at = bool(value), time.time()
    logger.info(f"qingyu_core: 思考开关 = {'开' if value else '关'}")
    if value:
        return f"好，我说话前会先想一想（慢一些，但更周到）。模型：{THINK_MODEL}。"
    return f"好，我不想了，直接答（快很多）。模型：{FAST_MODEL}。"


def clear() -> str:
    """Restore the default (off).

    Returns:
        Text describing the result.
    """
    global _cache, _cache_at
    try:
        PATH.unlink(missing_ok=True)
    except OSError as exc:
        return f"删开关文件失败：{exc}"
    _cache, _cache_at = DEFAULT_THINKING, time.time()
    logger.info("qingyu_core: 思考开关已还原成默认（关）")
    return f"好，恢复默认：不思考（{FAST_MODEL}），回得快。"


def describe() -> str:
    """One line for the status command.

    Returns:
        Chinese description.
    """
    return (
        f"思考模式：{'开（' + THINK_MODEL + '，慢一些但更周到）' if enabled() else '关（' + FAST_MODEL + '，秒回）'}"
    )


def pick_model(current: str | None, thinking: bool) -> str | None:
    """Which model name should this request use?

    Args:
        current: The model name the request would use as-is (``None`` = provider default).
        thinking: Whether thinking is enabled.

    Returns:
        The model name to set, or ``None`` to leave the request untouched
        （不认识的模型名＝管理员自己选的，别插手）。
    """
    name = (current or "").strip()
    if name not in SWITCHABLE:
        return None
    return THINK_MODEL if thinking else FAST_MODEL
