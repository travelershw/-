"""本机语音识别（不上传）：sherpa-onnx + 中文 Paraformer 小模型（P3-b 的本机路线）。

用户选了"声音不出本机"，所以这条路线**完全不联网识别**：模型下到本机、推理在 CPU 上跑，
录到的 wav 只在本进程里过一遍，不发给任何服务商。

几个刻意的选择：

- **懒加载**：模型约 74 MB，启动就载会明显拖慢桌宠，所以第一次用到才载（并加锁，只载一次）；
- **线程数按 2 给**（`NUM_THREADS`）：桌宠要一直活着，不该为了识别把 CPU 吃满；
- **只吃 16 kHz 单声道 16 位 wav**：正是 `microphone.record()` 写出来的格式；
  别的格式**如实报错**，不偷偷重采样（悄悄改采样率是最容易"听起来能跑、结果全错"的坑）；
- **模型可以自己下**：`fetch()` 从 sherpa-onnx 官方 release 拉模型包（带体积校验、下完删压缩包），
  这样别人拿到这份代码也能一条命令跑起来，不必依赖我手工放文件；
- 没装引擎/没有模型时，**如实说清楚缺什么**，不影响桌宠其它功能。

模型来源：k2-fsa/sherpa-onnx 的 `asr-models` release。选 `paraformer-zh-small`（实测 74.3 MB）
而不是 `sense-voice`（999 MB）或大号 paraformer（223 MB）——点一次录 5 秒的场景，小的够用。
"""

import shutil
import tarfile
import threading
import wave
from array import array
from pathlib import Path

import model_store
import paths

from asr import ASRError

MODELS = paths.BASE / "models" / "asr"
MODEL_NAME = "sherpa-onnx-paraformer-zh-small-2024-03-09"
# 下载路线，按顺序试：
#   1. hf-mirror 分文件拿（实测这台机器唯一能跑通的：GitHub release 与 huggingface.co 都超时）
#   2. huggingface 直连（能用它的机器会走这条）
#   3. GitHub release 的官方 tar.bz2（作者原来的路线，直连拉不动时最后试）
FILE_SOURCES = (
    ("hf-mirror", "https://hf-mirror.com/csukuangfj/" + MODEL_NAME + "/resolve/main/"),
    ("huggingface", "https://huggingface.co/csukuangfj/" + MODEL_NAME + "/resolve/main/"),
)
TARBALL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_NAME}.tar.bz2"
)
TARBALL_BYTES = 77_920_048  # 实测 Content-Length（74.31 MB）
# 只认这两个文件名：小号 paraformer 只有 int8，没有 fp32
MODEL_FILE = "model.int8.onnx"
TOKENS_FILE = "tokens.txt"
# 比这还小就肯定是坏文件（真模型 78 MB；测试里会把这个数改小）
MIN_MODEL_BYTES = 10 * 1024 * 1024
NUM_THREADS = 2
SAMPLE_RATE = 16000
TIMEOUT_SECONDS = 600

_lock = threading.Lock()
_recognizer = None


def model_dir() -> Path:
    """Where the model lives.

    Returns:
        The model directory (``models/asr/<MODEL_NAME>`` by default).
    """
    return MODELS / MODEL_NAME


def model_files(root: Path | None = None) -> tuple[Path, Path] | None:
    """Find the ONNX model and the token table.

    Args:
        root: Directory to search (defaults to :func:`model_dir`).

    Returns:
        ``(onnx, tokens)``, or ``None`` when the model is not on disk yet.
    """
    where = root or model_dir()
    if not where.is_dir():
        return None
    tokens = where / "tokens.txt"
    if not tokens.is_file():
        return None
    candidates = sorted(where.glob("*.onnx"))
    if not candidates:
        return None
    # 优先 int8：小模型在 CPU 上明显更快，而短句识别效果差不了多少
    int8 = [path for path in candidates if "int8" in path.name]
    return ((int8 or candidates)[0], tokens)


def is_ready() -> bool:
    """Is a usable model on disk?

    Returns:
        True when both the ONNX file and the token table exist.
    """
    return model_files() is not None


def describe() -> str:
    """One line about the local engine, for the bubble and the log.

    Returns:
        Chinese description of what is (not) available.
    """
    files = model_files()
    if files is None:
        return f"本机识别还没装模型（{model_dir()}）"
    onnx, _tokens = files
    size = onnx.stat().st_size / 1024 / 1024
    return f"本机识别：{onnx.name}（{size:.0f} MB，{NUM_THREADS} 线程）"


def _download(url: str, target: Path, progress=None) -> int:  # noqa: ANN001 - 可选回调
    """Download one file through the shared store (kept for the module's own tests).

    完整性核对（实际字节数 vs ``Content-Length``）统一在 `model_store.download` 里做：
    中断会留下半个文件，而"文件在不在"的检查会把它当好模型，报错时指向完全无关的地方。

    Args:
        url: Source URL.
        target: Destination path.
        progress: Optional ``callable(done, total)``.

    Returns:
        Bytes written.

    Raises:
        ASRError: On network failure, or when the download was cut short.
    """
    try:
        return model_store.download(url, target, progress)
    except OSError as exc:
        raise ASRError(str(exc)) from exc


def fetch(progress=None) -> str:  # noqa: ANN001 - 可选回调
    """Download and unpack the model (no pip, no cloud, try mirrors in order).

    Args:
        progress: Optional ``callable(done_bytes, total_bytes)`` for a bubble hint.

    Returns:
        A Chinese result line naming the source that worked.

    Raises:
        ASRError: When every source failed, with the reasons collected.
    """
    if is_ready():
        return f"模型已经在本机了（{model_dir()}）"
    where = model_dir()
    where.mkdir(parents=True, exist_ok=True)
    reasons: list[str] = []

    # 路线 1/2：直接从镜像分文件拿模型与词表（小模型只有 int8 这一个权重文件）
    for label, base in FILE_SOURCES:
        try:
            _download(base + MODEL_FILE, where / MODEL_FILE, progress)
            _download(base + TOKENS_FILE, where / TOKENS_FILE, progress)
        except ASRError as exc:
            reasons.append(f"{label}：{exc}")
            continue
        size = (where / MODEL_FILE).stat().st_size
        if size < MIN_MODEL_BYTES:
            reasons.append(f"{label}：模型文件太小（{size} 字节），已删除")
            (where / MODEL_FILE).unlink(missing_ok=True)
            continue
        if is_ready():
            return f"模型装好了（来自 {label}）：{describe()}"
        reasons.append(f"{label}：下载完但文件不齐")

    # 路线 3：官方 tar.bz2（作者这边的直连拉不动，但别人可能可以）
    archive = MODELS / f"{MODEL_NAME}.tar.bz2"
    try:
        _download(TARBALL_URL, archive, progress)
        if archive.stat().st_size < TARBALL_BYTES * 0.9:
            size = archive.stat().st_size
            archive.unlink(missing_ok=True)
            raise ASRError(f"没下完（{size} 字节）")
        with tarfile.open(archive, "r:bz2") as bundle:
            bundle.extractall(MODELS)  # noqa: S202 - 官方 release，只解出模型文件
        archive.unlink(missing_ok=True)
        if is_ready():
            return f"模型装好了（来自官方 release）：{describe()}"
        reasons.append("官方 release：解包后没找到模型文件")
    except (ASRError, tarfile.TarError, OSError) as exc:
        archive.unlink(missing_ok=True)
        reasons.append(f"官方 release：{exc}")

    raise ASRError("下载识别模型失败——两条路都没成：" + "；".join(reasons))


def wav_samples(path: str) -> array:
    """Read a 16 kHz mono 16-bit wav into float samples.

    Args:
        path: Wav file written by :func:`microphone.record`.

    Returns:
        Float samples scaled to -1.0 … 1.0.

    Raises:
        ASRError: When the file is missing, unreadable, or not the expected format.
    """
    try:
        with wave.open(path, "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as exc:
        raise ASRError(f"读不到录音文件：{exc}") from exc
    if (rate, channels, width) != (SAMPLE_RATE, 1, 2):
        # 不做重采样、不做混音：宁可报错，也不要"能跑但结果不对"
        raise ASRError(f"录音格式不对（{rate}Hz/{channels}声道/{width * 8}位），需要 16kHz 单声道 16 位")
    raw = array("h")
    raw.frombytes(frames[: len(frames) - (len(frames) % 2)])
    return array("f", (value / 32768.0 for value in raw))


def parse_text(text: str) -> str:
    """Strip model tags like ``<|zh|><|NEUTRAL|>`` and tidy whitespace.

    Args:
        text: Raw text from the recognizer.

    Returns:
        Clean text.
    """
    if not text:
        return ""
    out = []
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
            continue
        if char == ">":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(char)
    return "".join(out).replace("\u3000", " ").strip()


def load():  # noqa: ANN202 - sherpa_onnx.OfflineRecognizer
    """Load the recognizer once (thread-safe).

    Returns:
        The recognizer.

    Raises:
        ASRError: When the engine or the model is missing.
    """
    global _recognizer
    with _lock:
        if _recognizer is not None:
            return _recognizer
        try:
            import sherpa_onnx  # noqa: PLC0415 - 可选依赖，缺了也不该影响桌宠
        except Exception as exc:  # noqa: BLE001 - 没装引擎
            raise ASRError(
                "没装本机识别引擎（在 pet_desktop 里执行 "
                "`.venv\\Scripts\\python.exe -m pip install sherpa-onnx`）"
            ) from exc
        files = model_files()
        if files is None:
            raise ASRError(
                f"本机还没有识别模型（右键「下载识别模型…」或放好 {model_dir()}）"
            )
        onnx, tokens = files
        try:
            _recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(onnx),
                tokens=str(tokens),
                num_threads=NUM_THREADS,
                sample_rate=SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
            )
        except Exception as exc:  # noqa: BLE001 - 模型坏/版本不匹配
            raise ASRError(f"加载本机识别模型失败：{type(exc).__name__}: {exc}") from exc
        return _recognizer


def transcribe_file(path: str) -> str:
    """Recognize one wav file locally.

    Args:
        path: Wav file (16 kHz mono 16-bit).

    Returns:
        The transcript.

    Raises:
        ASRError: On any failure, with a message meant for the user.
    """
    samples = wav_samples(path)
    head = samples[: SAMPLE_RATE // 10]
    loudest = max((abs(value) for value in head), default=0.0)
    recognizer = load()
    try:
        stream = recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        recognizer.decode_stream(stream)
        text = parse_text(stream.result.text)
    except Exception as exc:  # noqa: BLE001 - 推理失败不该把桌宠带崩
        raise ASRError(f"本机识别出错：{type(exc).__name__}: {exc}") from exc
    if not text:
        raise ASRError(f"本机没听出文字（起始音量 {loudest:.3f}）")
    return text


def drop_model() -> bool:
    """Delete the downloaded model (frees ~74 MB).

    Returns:
        True when the directory is gone.
    """
    global _recognizer
    _recognizer = None
    try:
        shutil.rmtree(MODELS)
        return True
    except OSError:
        return False
