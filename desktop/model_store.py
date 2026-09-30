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


def download(url: str, target: Path, progress=None, *, attempts: int = 6) -> int:  # noqa: ANN001
    """Download one file with resume + retries, verifying it is complete.

    为什么要断点续传（2026-09-25 实测）：Kokoro 的 `model.onnx` 有 **310 MB**，
    过这条链路一次拉不完——两个源分别在 92 MB / 122 MB 处被切断。
    以前每次重试都从 0 开始，等于永远在拉前 100 MB；现在带 ``Range`` 从断点继续。

    完整性仍然照旧核对：拿实际字节数和 ``Content-Length`` / ``Content-Range`` 对账，
    对不上就继续续传，彻底放弃时才删掉半成品——**绝不留一个坏模型**。

    Args:
        url: Source URL.
        target: Destination path.
        progress: Optional ``callable(done, total)``.
        attempts: How many resume attempts before giving up.

    Returns:
        Bytes written.

    Raises:
        OSError: When every attempt failed.
    """
    expected = 0
    last_error: OSError | None = None
    for _attempt in range(max(1, attempts)):
        done = target.stat().st_size if target.exists() else 0
        request = urllib.request.Request(url)
        mode = "wb"
        if done:
            request.add_header("Range", f"bytes={done}-")
            mode = "ab"
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                if status == 206:
                    content_range = response.headers.get("Content-Range") or ""
                    total = (
                        int(content_range.rsplit("/", 1)[-1])
                        if "/" in content_range
                        else expected
                    )
                else:
                    # 服务端不支持续传（或本来就是第一次）：从头来
                    total = int(response.headers.get("Content-Length") or 0)
                    if done:
                        target.unlink(missing_ok=True)
                        done, mode = 0, "wb"
                expected = total or expected
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open(mode) as out:
                    while chunk := response.read(1 << 20):
                        out.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total or expected)
        except (urllib.error.URLError, OSError) as exc:
            last_error = OSError(f"{type(exc).__name__}: {exc}")
            continue
        if expected and done >= expected:
            return done
        if not expected and done:  # 服务端没给长度：能下多少算多少
            return done
        last_error = OSError(f"下载中断（{done}/{expected} 字节）")
    target.unlink(missing_ok=True)
    raise OSError(f"下载失败（试了 {attempts} 次）：{last_error}")


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
