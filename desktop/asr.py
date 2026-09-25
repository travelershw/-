"""语音转文字：把 wav 交给 OpenAI 兼容的 `/audio/transcriptions`（P3-b）。

**为什么是"OpenAI 兼容"这条路**：AstrBot 里已经配好了智谱（`glm-4v-flash` 一直在用），
而实测 `https://open.bigmodel.cn/api/paas/v4/audio/transcriptions` + `model=glm-asr`
**鉴权是通的**（用同一个 key，返回的是业务错误而不是 401），所以桌宠端只要按同一个
形状发请求，就能"复用 AstrBot 已经配好的那家"，不必新引入一个服务商。

> 实测记录：同一把 key 打 `chat/completions` 是 200；打 `audio/transcriptions` 返回
> **429 / code 1113「余额不足或无可用资源包，请充值」**——也就是说**接口可用、但这个账号
> 还没有语音识别的额度**。所以本模块失败时会把这句话原样翻出来，而不是含糊地说"识别失败"。
> 换任何别的兼容端点（SiliconFlow 的 SenseVoice、自建 whisper.cpp 等）只要改配置即可。

Key 的取法（按顺序，都不打印、不外传）：
1. 桌宠自己的 `config.json` → `asr.api_key`；
2. 空的话，**只在这台机器上**读 AstrBot 的 `data/cmd_config.json` 里那家已经配好的 key
   （AstrBot 把 key 存成**列表**，取第一个——踩过这个坑，见 `pick_key`）。
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-asr"
TIMEOUT_SECONDS = 60
ASTRBOT_CONFIG = Path.home() / ".astrbot" / "data" / "cmd_config.json"


class ASRError(Exception):
    """Recognition failed, with a message meant to be shown to the user."""


# 识别结果后处理：把**反复错的那几个词**直接改对。
# 为什么不用 sherpa 自带的 `hr_dict_dir`/`hr_lexicon`（同音替换）：那套要按它的词典格式摆文件、
# 出错了很难判断是哪一层的问题；而"人名/群名/简称"这类错误是**固定映射**，
# 一张表就够，还能离线测、你也能自己往里加词（config.json 的 asr.replacements）。
DEFAULT_REPLACEMENTS = {
    # 她自己的名字最常被听错——这几个是拼音相近的典型误识
    "青鱼": "轻语",
    "轻宇": "轻语",
    "青玉": "轻语",
    "轻雨": "轻语",
    "青羽": "轻语",
}
# 明显不像人话的判定（不是"置信度"——paraformer 贪心解码不给这个词，
# 所以这里用**可解释的规则**，宁可说"这是启发式"，也不假装有置信度）：
NOISE_MIN_CHARS = 2  # 短于这个字数当噪声（"嗯""呃"这类单字）
NOISE_REPEAT_RUN = 5  # 同一个字连续出现这么多次当噪声（实测"喂喂喂喂喂喂"就是没对着麦说话）


def apply_replacements(text: str, replacements: dict | None = None) -> str:
    """Rewrite known mis-heard words.

    Args:
        text: Raw transcript.
        replacements: ``{wrong: right}`` map; falls back to :data:`DEFAULT_REPLACEMENTS`.

    Returns:
        The corrected text.
    """
    if not text:
        return ""
    table = DEFAULT_REPLACEMENTS if replacements is None else replacements
    out = text
    for wrong, right in (table or {}).items():
        if wrong and isinstance(wrong, str) and isinstance(right, str):
            out = out.replace(wrong, right)
    return out


def looks_like_noise(text: str) -> str:
    """Is this transcript probably not something worth answering?

    Args:
        text: Transcript (after replacements).

    Returns:
        A Chinese reason when it looks like noise/filler, else an empty string.
    """
    clean = (text or "").strip()
    if not clean:
        return "什么都没听到"
    stripped = "".join(char for char in clean if char.strip("，。！？、~… \u3000"))
    if len(stripped) < NOISE_MIN_CHARS:
        return f"只听到「{clean}」（太短，像语气词）"
    run = 1
    longest = 1
    for prev, char in zip(clean, clean[1:]):
        run = run + 1 if char == prev else 1
        longest = max(longest, run)
    if longest >= NOISE_REPEAT_RUN:
        return f"听到的是重复音（「{clean}」），像是没对着麦克风说话"
    return ""


def postprocess(text: str, options: dict | None = None) -> tuple[str, str]:
    """Clean one transcript: fix known words, then check for noise.

    Args:
        text: Raw transcript.
        options: The ``asr`` config block (may carry ``replacements``).

    Returns:
        ``(text, reason)`` — ``reason`` is empty when the text looks usable.
    """
    fixed = apply_replacements(text, (options or {}).get("replacements"))
    return fixed, looks_like_noise(fixed)


def endpoint(base_url: str | None) -> str:
    """Build the transcription URL from a base URL.

    Args:
        base_url: e.g. ``https://open.bigmodel.cn/api/paas/v4`` (trailing slash ok).

    Returns:
        The full ``/audio/transcriptions`` URL.
    """
    base = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/audio/transcriptions"):
        return base
    return f"{base}/audio/transcriptions"


def pick_key(raw) -> str:  # noqa: ANN001 - 可能是字符串或列表
    """Pull one usable key out of whatever the config holds.

    AstrBot stores keys as a list (``["id.secret"]``); passing the list straight into an
    ``Authorization`` header produce a 401 that looks like a bad key but is actually a
    formatting bug — hence this function.

    Args:
        raw: A string, a list of strings, or something else.

    Returns:
        The first non-empty key, or an empty string.
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def astrbot_key(source_id: str = "zhipu") -> str:
    """Read the key AstrBot already uses for one provider source.

    Args:
        source_id: Provider source id in ``provider_sources`` (``"zhipu"`` by default).

    Returns:
        The key, or an empty string when there is nothing usable.
    """
    try:
        data = json.loads(ASTRBOT_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for source in data.get("provider_sources") or []:
        if isinstance(source, dict) and str(source.get("id")) == source_id:
            return pick_key(source.get("key") or source.get("api_key"))
    return ""


def parse_response(payload) -> str:  # noqa: ANN001 - 服务商返回什么都有可能
    """Pull the transcript out of a transcription response.

    Args:
        payload: Decoded JSON (dict) or raw text.

    Returns:
        The transcript, or an empty string when the response carries none.
    """
    if isinstance(payload, dict):
        for key in ("text", "result", "transcription", "transcript"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"].strip()
        return ""
    if isinstance(payload, str):
        return payload.strip()
    return ""


def describe_error(status: int, body: str) -> str:
    """Turn a failed response into one honest Chinese sentence.

    Args:
        status: HTTP status code.
        body: Response body text.

    Returns:
        A message suitable for the bubble and the log.
    """
    message = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        if not message and isinstance(payload, dict):
            message = str(payload.get("message") or "")
    except (ValueError, TypeError):
        message = ""
    message = message or (body or "").strip()[:120]
    if status == 401:
        return f"识别服务的 Key 不对（401）{('：' + message) if message else ''}"
    if status == 429 or "余额" in message or "资源包" in message:
        return (
            f"识别服务说额度不够（{status}）：{message}"
            "——智谱的语音识别要单独开资源包/充值，或者把 asr 配置换成别的兼容端点"
        )
    if status == 404:
        return f"这个地址没有识别接口（404）{('：' + message) if message else ''}"
    return f"识别服务返回 {status}{('：' + message) if message else ''}"


def transcribe(
    path: str,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str = "",
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Send one wav file and return its transcript.

    Args:
        path: Local wav path.
        base_url: OpenAI-compatible base URL.
        model: Model name (``glm-asr`` by default).
        api_key: Key to use; falls back to AstrBot's configured key when empty.
        timeout: Socket timeout in seconds.

    Returns:
        The transcript text.

    Raises:
        ASRError: When there is no key, the file is unreadable, the request fails, or
            the response carries no text.
    """
    audio = Path(path)
    key = pick_key(api_key) or astrbot_key()
    if not key:
        raise ASRError("还没配语音识别的 Key（config.json 里的 asr.api_key）")
    try:
        data = audio.read_bytes()
    except OSError as exc:
        raise ASRError(f"读不到录音文件：{exc}") from exc
    boundary = "----qingyupetformboundary"
    parts: list[bytes] = []
    for name, value in (("model", str(model or DEFAULT_MODEL)), ("response_format", "json")):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{audio.name}\"\r\n"
        "Content-Type: audio/wav\r\n\r\n".encode()
    )
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        endpoint(base_url),
        data=b"".join(parts),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ASRError(describe_error(exc.code, detail)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ASRError(f"连不上识别服务：{exc}") from exc
    try:
        payload = json.loads(body)
    except ValueError:
        payload = body
    text = parse_response(payload)
    if not text:
        raise ASRError("识别服务没返回文字（可能是静音或格式不对）")
    return text


def transcribe_with(path: str, options: dict) -> str:
    """Turn a recording into text using the engine the user picked.

    ``asr.engine`` 选 ``local``（默认，声音不出本机：sherpa-onnx + 本地模型）或
    ``openai``（OpenAI 兼容的云端接口，音频会上传）。

    Args:
        path: Local wav recording.
        options: The ``asr`` config block.

    Returns:
        The transcript.

    Raises:
        ASRError: On any failure, with a message meant for the user.
    """
    engine = str((options or {}).get("engine") or "local").strip().lower()
    if engine == "local":
        # 绝对导入：pet_desktop 不是包（模块都是顶层导入的），相对导入会直接报错
        import local_asr  # noqa: PLC0415 - 延迟导入，缺引擎也不影响云端路线
        return local_asr.transcribe_file(path)
    return transcribe(
        path,
        base_url=(options or {}).get("base_url"),
        model=(options or {}).get("model"),
        api_key=str((options or {}).get("api_key") or ""),
    )
