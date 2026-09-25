"""模型文件的存放与下载（本机识别、静音检测共用）。

只做两件事：给出模型根目录、把文件下下来并**核对完整性**。

完整性这条不是洁癖：下载被中断会留下**半个文件**，而"文件存在"这种检查会把它当成好模型，
等到推理时才炸——那时候报错会指向完全无关的地方（本机识别那边已经踩过一次）。

`fetch_first()` 负责"依次试多个地址"，因为这台机器上不同源的可用性差别很大：
GitHub release 常常拉到 0 MB 就不动、huggingface 直连超时、只有镜像能跑通，
所以每个模型都给出多条路，并把每条路失败的原因**都**带回给调用方。
"""

import urllib.error
import urllib.request
from pathlib import Path

import paths

MODELS = paths.BASE / "models"
TIMEOUT_SECONDS = 600


def download(url: str, target: Path, progress=None) -> int:  # noqa: ANN001 - 可选回调
    """Download one file, verify it is complete, and clean up on failure.

    Args:
        url: Source URL.
        target: Destination path.
        progress: Optional ``callable(done, total)``.

    Returns:
        Bytes written.

    Raises:
        OSError: On network failure, or when the download was cut short.
    """
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url), timeout=TIMEOUT_SECONDS
        ) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except (urllib.error.URLError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise OSError(f"{type(exc).__name__}: {exc}") from exc
    if total and done < total:
        target.unlink(missing_ok=True)
        raise OSError(f"下载中断（{done}/{total} 字节），已删除不完整的文件")
    return done


def fetch_first(sources, target: Path, progress=None) -> tuple[str, int]:  # noqa: ANN001
    """Try each ``(label, url)`` in order until one download succeeds.

    Args:
        sources: Sequence of ``(label, url)`` pairs, best first.
        target: Destination path.
        progress: Optional ``callable(done, total)``.

    Returns:
        ``(label, bytes_written)`` for the source that worked.

    Raises:
        OSError: When every source failed; the message lists every reason.
    """
    reasons: list[str] = []
    for label, url in sources:
        try:
            size = download(url, target, progress)
        except OSError as exc:
            reasons.append(f"{label}：{exc}")
            continue
        return label, size
    raise OSError("所有下载源都失败——" + "；".join(reasons))
