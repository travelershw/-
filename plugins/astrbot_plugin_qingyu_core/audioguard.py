r"""本机没有 ffmpeg 时，先把消息里的语音摘掉，别让它打断整轮回复。

AstrBot 处理语音（统一转 wav）靠 PATH 里的 ``ffmpeg``。它自己那条分支没有兜底：
消息里（或**被引用消息**的链里）只要有语音段，``Record.convert_to_file_path()``
就会抛 ``ffmpeg not found``，异常一路冒到 agent 层 —— 整轮不答，还会把
``Error occurred while processing agent request: ffmpeg not found`` 当成回复发进群里。

2026-09-27 21:36 实测：群里有人引用了她刚发出的语音，于是群里收到那句英文报错。

这里在插件侧兜一下：本机没有 ffmpeg 就把语音段换成占位文字，AstrBot 拿不到需要
转码的音频，也就不会炸。装了 ffmpeg（加进 PATH 并重启 AstrBot 后生效）本模块自动
不再改动任何东西。
"""

from __future__ import annotations

import shutil
import time

from astrbot.api.message_components import Plain

# 语音段换成的占位文字：跟 AstrBot 自己给"模型不支持的模态"用的 `[Image]` 风格一致。
PLACEHOLDER = "[语音]"
QUOTED_PLACEHOLDER = "[被引用消息里的语音]"
# 多久重新查一次 PATH。PATH 变了要重启 AstrBot 才会生效，这里只是别每条消息都查一次盘。
CHECK_SECONDS = 30.0

_checked_at = 0.0
_present = False


def ffmpeg_available() -> bool:
    """PATH 里有没有 ffmpeg（``CHECK_SECONDS`` 内复用上次结果）。

    Returns:
        有 ffmpeg 返回 True；没有返回 False。
    """
    global _checked_at, _present
    now = time.monotonic()
    if now - _checked_at >= CHECK_SECONDS:
        _present = shutil.which("ffmpeg") is not None
        _checked_at = now
    return _present


def strip_audio(message: list) -> tuple[int, int]:
    """把消息链里的语音段换成占位文字（就地改写）。

    Args:
        message: 事件的消息链（``event.message_obj.message``）。

    Returns:
        ``(消息本身的语音段数, 被引用消息链里的语音段数)``。
    """
    own = 0
    quoted = 0
    for index, part in enumerate(message):
        if type(part).__name__ == "Record":
            message[index] = Plain(text=PLACEHOLDER)
            own += 1
    # 引用链：AstrBot 只取一层（内部再取时 get_reply=False），所以这里也只走一层。
    for part in message:
        if type(part).__name__ != "Reply":
            continue
        chain = getattr(part, "chain", None)
        if not isinstance(chain, list):
            continue
        for index, sub in enumerate(chain):
            if type(sub).__name__ == "Record":
                chain[index] = Plain(text=QUOTED_PLACEHOLDER)
                quoted += 1
    return own, quoted
