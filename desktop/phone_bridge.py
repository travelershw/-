r"""手机中转服务：手机 ⇄ 家里那套"她"。

给手机用的**唯一入口**（WebSocket）。它把手机那边的简单协议翻译成 ``glasses`` 通道的协议，
并顺手把"语音识别"放在电脑上做（手机不用带 78 MB 模型）：

    手机（按住说话，走蓝牙耳机麦）
        │  wss://<机器名>.<tailnet>.ts.net:8443   ← tailscale serve 提供证书与 tailnet 认证
        ▼
    本服务（默认 127.0.0.1:6201）
        ├─ 本机离线识别（sherpa-onnx，热了约 35 ms/句）→ 只把**文字**交给通道
        ├─ 照片：存到本地再把路径交给通道（AstrBot 的看图通路现成，不用改配置）
        └─ 她的声音（裸 PCM 24 kHz）与文字原样转发回手机
        │
        ▼
    AstrBot glasses 通道 ws://127.0.0.1:6200（已鉴权、已支持流式下行）

**手机侧协议**（JSON 文本帧 + 二进制正文帧；正文帧前面必须先发一个说明帧）

| 手机 → 服务 | 说明 |
| --- | --- |
| ``{"type":"hello"}`` | 打招呼 → 服务回 ``ready``（含录音参数） |
| ``{"type":"utterance","sample_rate":16000,"channels":1,"secs":1.2}`` + 二进制 wav/PCM | 一句话 |
| ``{"type":"text","text":"…"}`` | 打字发一句 |
| ``{"type":"image","mime":"image/jpeg","text":"看看这个"}`` + 二进制 JPEG | 拍一张给她看 |
| ``{"type":"ping"}`` | 保活 |

| 服务 → 手机 | 说明 |
| --- | --- |
| ``{"type":"ready",…}`` | 告诉手机按什么格式录（16 kHz / 单声道 / PCM16） |
| ``{"type":"you","text":"…"}`` | **她听到的**（识别结果，用来核对收音；带电平与识别耗时） |
| ``{"type":"her","text":"…"}`` | 她的回复文字（流式片段） |
| ``{"type":"audio_begin","sample_rate":24000,"channels":1}`` + 二进制 PCM + ``{"type":"audio_end"}`` | 她的声音 |
| ``{"type":"error","message":"…"}`` | 出错时说人话 |

跑法（桌宠 venv，含 sherpa-onnx）：

    .\.venv\Scripts\python.exe phone_bridge.py                  # 起服务（只听回环，安全）
    .\.venv\Scripts\python.exe phone_bridge.py --host 0.0.0.0    # 局域网直连测试用
    .\.venv\Scripts\python.exe phone_bridge.py --selftest --wav 某个16k.wav
"""

import argparse
import asyncio
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import glasses_sim as sim  # noqa: E402 - 复用通道客户端（鉴权/流式接收都在里面）
import listening  # noqa: E402

DEFAULT_PORT = 6201
UPLOAD_DIR = BASE / "phone_uploads"
LOG_PATH = BASE.parent / "migration_tools" / "phone_bridge.log"
PHONE_RATE = 16000  # 手机该按这个录：识别模型只吃 16 kHz 单声道
# 手机 App 的 APK（构建产物）；手机浏览器可以直接从这里下载安装，省得插线开 USB 调试
DEFAULT_APK = (
    BASE.parent / "phone-app" / "android" / "app" / "build" / "outputs"
    / "apk" / "debug" / "app-debug.apk"
)


def make_http_hook(apk_path: Path, token: str, *, trust_tailnet: bool = True, ws_hint: str = ""):  # noqa: ANN201
    """给 WS 服务加一个 HTTP 钩子：手机浏览器能下载 APK、看状态、抄连接参数。

    为什么需要：装 App 时插线开 USB 调试太麻烦，而手机本来就能通过 ``tailscale serve``
    访问这个端口——那就顺手把 APK 与连接参数发出去（真证书、只在 tailnet 内可达）。

    鉴权两选一：① URL 带 ``?token=``；② 请求带 ``Tailscale-User-Login``
    （只有在服务**仅监听回环**、也就是"只可能是 tailscale serve 转发进来"时才信任它——
    与 dsh-mobile 同一条规矩）。

    Args:
        apk_path: 要提供的 APK 路径。
        token: 需要的令牌（留空则不校验）。
        trust_tailnet: 是否信任 tailnet 身份头。
        ws_hint: 给页面显示的 WebSocket 地址（手机 App 里要填的）。

    Returns:
        ``process_request`` 回调；websockets 版本不支持时返回 None（该功能自动关闭）。
    """
    try:
        from websockets.datastructures import Headers
        from websockets.http11 import Response
    except Exception:  # noqa: BLE001 - 老版本没有这个钩子
        log("这个 websockets 版本不支持 HTTP 钩子，/apk 与 /health 用不了")
        return None

    def _ok(request) -> bool:  # noqa: ANN001
        """这次 HTTP 请求过没过鉴权。

        Args:
            request: HTTP 请求对象。

        Returns:
            通过返回 True。
        """
        if not token:
            return True
        path = str(getattr(request, "path", "") or "")
        if f"token={token}" in path:
            return True
        user = str(request.headers.get("Tailscale-User-Login", "") or "")
        return bool(trust_tailnet and user)

    def _page() -> bytes:
        """自服务首页：手机上打开就能装 App、抄连接参数。

        装 App 的主路径是**分块下载 + 页面拼装 Blob**（见下面的 JS）：手机的浏览器/下载器
        对"直接下载 APK"各有各的脾气（2026-09-29 实测：HEAD 探测、拒收 .apk 都遇到过），
        而"小分块 fetch + 本地拼装"这条路是页面自己完成的，出错也能把原因显示出来。

        Returns:
            HTML 字节。
        """
        size = apk_path.stat().st_size if apk_path.is_file() else 0
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>轻语 · 手机端</title>
<style>
 body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;margin:0;padding:18px;
      background:#15161a;color:#e8eaed;line-height:1.75}}
 h1{{font-size:20px;margin:0 0 6px}}
 .card{{background:#1e2026;border:1px solid #2f333b;border-radius:12px;padding:14px;margin:12px 0}}
 .big{{display:block;width:100%;border:0;background:#2f6fd0;color:#fff;text-align:center;
      padding:14px;border-radius:10px;font-size:17px;font-weight:600}}
 .alt{{display:block;text-align:center;color:#8ab4f8;font-size:14px;margin-top:10px}}
 code{{background:#0e1013;padding:2px 6px;border-radius:6px;word-break:break-all;font-size:13px}}
 .muted{{color:#9aa0a6;font-size:13px}}
 .lbl{{color:#9aa0a6;font-size:13px;margin-top:10px}}
 #log{{background:#0e1013;border-radius:8px;padding:10px;margin-top:10px;font-size:13px;
      white-space:pre-wrap;word-break:break-all;min-height:2.6em}}
</style></head><body>
<h1>轻语 · 手机端</h1>
<div class="card">
  <button class="big" onclick="installViaChunks()">⬇︎ 安装「轻语」App（{size:,} 字节）</button>
  <div id="log">点上面的按钮开始。装好后打开 App，地址与令牌已预填，点「连接」即可。</div>
  <a class="alt" href="/apk">直接下载（老办法；上面那个不行时再试）</a>
  <p class="muted">提示：第一次装要在系统设置里允许一次「安装未知来源应用」
     （华为：设置 → 安全 → 更多安全设置 → 安装外部来源应用）。</p>
</div>
<div class="card">
  <div class="lbl">服务器地址（App 里填这个）</div>
  <p><code>{ws_hint or "wss://<机器名>.<tailnet>.ts.net:8443/"}</code></p>
  <div class="lbl">令牌</div>
  <p><code>{token}</code></p>
  <p class="muted">令牌等于这台机器的访问权，别截图外传。</p>
</div>
<div class="card muted">状态接口：<code>/health</code>　·　APK：<code>/apk</code></div>
<script>
const TOTAL = {size};
const CHUNK = 65536;           // 每块 64 KB：小块过中继也稳，出错能定位到具体哪一块
const out = document.getElementById('log');
function say(text) {{ out.textContent = text; }}

async function installViaChunks() {{
  if (!TOTAL) {{ say('服务器上还没有构建好的 APK'); return; }}
  const parts = [];
  let got = 0;
  const count = Math.ceil(TOTAL / CHUNK);
  const started = Date.now();
  try {{
    for (let i = 0; i < count; i++) {{
      const start = i * CHUNK;
      const end = Math.min(start + CHUNK, TOTAL) - 1;
      say(`下载中… ${{i + 1}}/${{count}} 块（${{(got / 1048576).toFixed(2)}} / ${{(TOTAL / 1048576).toFixed(2)}} MB）`);
      const res = await fetch('/apk', {{
        headers: {{ Range: `bytes=${{start}}-${{end}}` }},
        cache: 'no-store',
      }});
      if (res.status !== 206 && res.status !== 200) {{
        throw new Error(`第 ${{i + 1}} 块失败：HTTP ${{res.status}}`);
      }}
      const buf = await res.arrayBuffer();
      parts.push(buf);
      got += buf.byteLength;
    }}
    if (got !== TOTAL) throw new Error(`字节数不对：拿到 ${{got}}，应该是 ${{TOTAL}}`);
    const blob = new Blob(parts, {{ type: 'application/vnd.android.package-archive' }});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'qingyu-phone.apk';
    document.body.appendChild(a);
    a.click();
    a.remove();
    say(`✓ 拼装完成：${{got}} 字节，用时 ${{((Date.now() - started) / 1000).toFixed(1)}} 秒。\\n`
        + '已触发安装；如果没弹安装界面，就去「文件管理 / 下载」里找 qingyu-phone.apk。');
  }} catch (err) {{
    say('✗ ' + (err && err.message ? err.message : String(err))
        + `\\n（已下 ${{got}} 字节，共 ${{count}} 块。把这句话发我就行）`);
  }}
}}
</script>
</body></html>""".encode()

    def hook(connection, request):  # noqa: ANN001, ANN202
        """处理非 WebSocket 的 HTTP 请求。

        Args:
            connection: 连接对象（不用）。
            request: HTTP 请求对象。

        Returns:
            HTTP 响应；若是 WebSocket 握手则返回 None 让它继续。
        """
        path = str(getattr(request, "path", "") or "")
        upgrade = str(request.headers.get("Upgrade", "") or "").lower()
        if upgrade == "websocket":
            return None

        def reply(status: int, reason: str, body: bytes,
                  ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):  # noqa: ANN202
            """造一个响应。

            Args:
                status: 状态码。
                reason: 原因短语。
                body: 正文。
                ctype: Content-Type。
                extra: 额外响应头。

            Returns:
                Response。
            """
            headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
            headers.update(extra or {})
            return Response(status, reason, Headers(headers), body)

        if path.startswith("/health"):
            size = apk_path.stat().st_size if apk_path.is_file() else 0
            body = json.dumps(
                {"ok": True, "service": "qingyu-phone-bridge", "apk_bytes": size,
                 "time": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False,
            ).encode()
            return reply(200, "OK", body, "application/json; charset=utf-8")

        if not _ok(request):
            return reply(403, "Forbidden", "缺少令牌，也不是 tailnet 身份".encode())

        if path.startswith("/apk"):
            if not apk_path.is_file():
                return reply(404, "Not Found", f"还没构建 APK：{apk_path}".encode())
            blob = apk_path.read_bytes()
            total = len(blob)
            common = {
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Disposition": 'attachment; filename="qingyu-phone.apk"',
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Connection": "close",
            }
            method = str(getattr(request, "method", "") or "GET").upper()
            if method == "HEAD":
                # 手机的下载器常常先发一个 HEAD 探测，而 **HEAD 绝不能带正文**。
                # 之前这里把正文也发了出去，客户端会判定响应损坏 —— 这就是
                # 2026-09-29 手机报"下载资源出错"的原因（curl 不报，手机报）。
                headers = dict(common, **{"Content-Length": str(total)})
                log(f"手机来探测 APK（HEAD）：{total} 字节")
                return Response(200, "OK", Headers(headers), b"")
            header_range = str(request.headers.get("Range", "") or "")
            if header_range.startswith("bytes="):
                spec = header_range[len("bytes=") :].split(",")[0].strip()
                start_s, _, end_s = spec.partition("-")
                try:
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else total - 1
                except ValueError:
                    start, end = 0, total - 1
                start = max(0, min(start, total - 1))
                end = max(start, min(end, total - 1))
                chunk = blob[start : end + 1]
                headers = dict(common, **{
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                })
                log(f"手机分段下载 APK：{start}-{end}/{total}")
                return Response(206, "Partial Content", Headers(headers), chunk)
            headers = dict(common, **{"Content-Length": str(total)})
            log(f"有人来下载 APK：{total} 字节")
            return Response(200, "OK", Headers(headers), blob)

        log("有人打开了手机端首页")
        return reply(200, "OK", _page(), "text/html; charset=utf-8")

    return hook


def log(line: str) -> None:
    """写一行日志（控制台 + 文件）。

    Args:
        line: 日志内容。
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[bridge {stamp}] {line}", flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:  # noqa: BLE001 - 写不了日志不影响服务
        pass


def wav_frames(blob: bytes) -> tuple[bytes, int, int]:
    """从 wav 字节里取出 PCM 与格式。

    Args:
        blob: wav 字节。

    Returns:
        ``(pcm, 采样率, 声道数)``。
    """
    import io

    with wave.open(io.BytesIO(blob), "rb") as handle:
        return handle.readframes(handle.getnframes()), handle.getframerate(), handle.getnchannels()


def is_wav(blob: bytes) -> bool:
    """这段字节是不是 wav。

    Args:
        blob: 字节。

    Returns:
        是 RIFF/WAVE 返回 True。
    """
    return len(blob) > 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"


def _transcribe(clip: Path) -> str:
    """后台线程里跑本机识别（别卡住事件循环）。

    Args:
        clip: 16 kHz 单声道 wav。

    Returns:
        识别文字。
    """
    import local_asr

    return local_asr.transcribe_file(str(clip))


class PhoneSession:
    """一台手机（一条 WS 连接）对应一个会话。"""

    def __init__(self, peer, channel_url: str, channel_secret: str) -> None:
        """记住连接信息。

        Args:
            peer: 对端地址（日志用）。
            channel_url: 家里的通道地址。
            channel_secret: 通道密钥。
        """
        self.peer = peer
        self.device = sim.DeviceSim(channel_url, channel_secret, audio=None, no_play=True)
        self.ws = None
        self.channel = None
        self.rounds = 0
        self.ready_sent = False

    async def send_ready(self, channel: str = "") -> None:
        """告诉手机按什么格式录（同一条连接只发一次，重复 hello 不会再发）。

        Args:
            channel: 通道地址（第一发带上，给手机看）。
        """
        if self.ready_sent:
            return
        payload = {
            "type": "ready", "format": "pcm16", "sample_rate": PHONE_RATE,
            "channels": 1,
        }
        if channel:
            payload["channel"] = channel
        await self.send_json(payload)
        self.ready_sent = True

    async def start(self) -> None:
        """连上家里的通道。"""
        self.channel = await self.device.connect()

    async def close(self) -> None:
        """断开通道连接。"""
        if self.channel is not None:
            try:
                await self.channel.close()
            except Exception:  # noqa: BLE001
                pass
            self.channel = None

    async def send_json(self, payload: dict) -> None:
        """给手机发一个 JSON 帧。

        Args:
            payload: 内容。
        """
        if self.ws is not None:
            await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def turn(self, text: str, *, kind: str, images: list[str] | None = None) -> dict:
        """把一句话（可带图）交给她，并把她的回答**流式**转发给手机。

        转发是边收边发的：她的音频一到位就转给手机，手机就能"边说边播"，
        不必等她整段合成完（实测差 2~3 秒，见迁移说明）。

        Args:
            text: 要发给她的文字。
            kind: 来源（voice/text/image），日志用。
            images: 一起发过去的本地图片路径。

        Returns:
            本轮实测数据。
        """
        if self.channel is None:
            raise RuntimeError("通道没连上")
        queue: asyncio.Queue = asyncio.Queue()
        state = {"rate": 24000, "began": False}

        async def forward() -> None:
            """把队列里的东西按顺序发给手机（事件与音频都在这一条线上，顺序不会乱）。"""
            while True:
                item = await queue.get()
                if item is None:
                    break
                tag, payload = item
                if tag == "reply":
                    await self.send_json({"type": "her", "text": payload})
                elif tag == "begin":
                    await self.send_json({
                        "type": "audio_begin", "format": "pcm16",
                        "sample_rate": state["rate"], "channels": 1,
                    })
                    state["began"] = True
                elif tag == "audio" and self.ws is not None:
                    await self.ws.send(payload)

        def on_event(name: str, payload) -> None:  # noqa: ANN001
            """通道事件 → 队列。

            Args:
                name: 事件名。
                payload: 内容。
            """
            if name == "reply" and payload:
                queue.put_nowait(("reply", str(payload)))
            elif name == "audio_begin":
                queue.put_nowait(("begin", None))

        def on_audio(chunk: bytes) -> None:
            """通道来的每一段音频 → 队列（顺序与上面的事件保持一致）。

            Args:
                chunk: 裸 PCM。
            """
            queue.put_nowait(("audio", chunk))

        forwarder = asyncio.create_task(forward())
        row = await self.device.one_turn(
            self.channel,
            text,
            kind=kind,
            images=images,
            on_event=on_event,
            on_audio=on_audio,
        )
        queue.put_nowait(None)
        await forwarder
        self.rounds += 1
        log(f"第 {self.rounds} 轮（{kind}）：她开口 {row['first_audio_ms']} ms，"
            f"音频 {row['audio_secs']} s（{row['audio_bytes']} 字节）")
        await self.send_json({
            "type": "audio_end", "reason": "finished",
            "row": {"first_audio_ms": row["first_audio_ms"], "audio_secs": row["audio_secs"]},
        })
        return row

    async def handle_audio(self, blob: bytes, meta: dict) -> None:
        """手机传来的一句话：识别 → 交给她 → 回音频。

        Args:
            blob: wav 或裸 PCM16。
            meta: 帧里的元信息。
        """
        if is_wav(blob):
            pcm, rate, channels = wav_frames(blob)
            wav_bytes = blob
        else:
            rate = int(meta.get("sample_rate") or PHONE_RATE)
            channels = int(meta.get("channels") or 1)
            pcm = blob
            wav_bytes = _wrap_wav(pcm, rate, channels)
        seconds = len(pcm) / max(1, rate * channels * 2)
        level = sim.pcm_dbfs(pcm if channels == 1 else pcm)
        clip = UPLOAD_DIR / f"phone_{int(time.time())}.wav"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(wav_bytes)
        # 识别只吃 16 kHz 单声道：不是就让 ffmpeg 转一下（手机端正常不会走到这里）
        try:
            _pcm, _rate, usable = sim.read_wav_pcm(clip)
            started = time.monotonic()
            text = await asyncio.to_thread(_transcribe, usable)
        except Exception as exc:  # noqa: BLE001 - 失败要说清楚
            log(f"识别失败：{type(exc).__name__}: {exc}")
            await self.send_json({"type": "error", "message": f"识别失败：{exc}"})
            return
        asr_ms = round((time.monotonic() - started) * 1000)
        clip.unlink(missing_ok=True)
        text = (text or "").strip()
        log(f"手机说了 {seconds:.2f} 秒（{level:.1f} dBFS）→ 「{text}」（识别 {asr_ms} ms）")
        if not text:
            await self.send_json({"type": "error", "message": "没听清，再说一次？"})
            return
        await self.send_json({
            "type": "you", "text": text, "secs": round(seconds, 2),
            "level_dbfs": round(level, 1), "asr_ms": asr_ms,
        })
        await self.turn(text, kind="voice")

    async def handle_image(self, blob: bytes, meta: dict) -> None:
        """手机传来一张照片：存本地 → 连文字一起交给她看。

        Args:
            blob: 图片字节。
            meta: 帧里的元信息（mime/text）。
        """
        suffix = ".jpg" if "jpeg" in str(meta.get("mime", "image/jpeg")) else ".png"
        target = UPLOAD_DIR / f"photo_{int(time.time())}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        text = str(meta.get("text") or "").strip() or "看看这个"
        log(f"收到照片 {len(blob)} 字节 → {target.name}（附带话：{text}）")
        await self.send_json({"type": "you", "text": f"[照片] {text}"})
        await self.turn(text, kind="image", images=[str(target)])


def _wrap_wav(pcm: bytes, rate: int, channels: int) -> bytes:
    """把裸 PCM 封成 wav。

    Args:
        pcm: 裸 PCM（16 bit）。
        rate: 采样率。
        channels: 声道数。

    Returns:
        wav 字节。
    """
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(max(1, channels))
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


async def serve(args) -> int:  # noqa: ANN001 - argparse
    """起 WS 服务，等手机来连。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码。
    """
    import websockets

    loopback = args.host in {"127.0.0.1", "localhost", "::1"}
    if not loopback:
        log("注意：绑到了非回环地址，此时**不信任** tailnet 身份头（只有回环才安全）")

    async def handler(ws) -> None:  # noqa: ANN001 - websockets connection
        """一条手机连接的生命周期。

        Args:
            ws: websockets 连接。
        """
        peer = getattr(ws, "remote_address", None)
        request = getattr(ws, "request", None)
        path = str(getattr(request, "path", "") or "")
        headers = getattr(request, "headers", {}) or {}
        token = ""
        if "token=" in path:
            token = path.split("token=", 1)[1].split("&", 1)[0]
        token = token or str(headers.get("x-qingyu-token", "") or "")
        tailnet_user = str(headers.get("Tailscale-User-Login", "") or "") if loopback else ""
        if args.token and token != args.token and not (
            args.trust_tailnet and tailnet_user
        ):
            log(f"{peer} 令牌不对，已拒绝")
            await ws.close(1008, "bad token")
            return
        who = f"tailnet:{tailnet_user}" if tailnet_user else "令牌"
        log(f"{peer} 已连接（{who}）")
        session = PhoneSession(peer, args.channel, args.channel_secret)
        session.ws = ws
        try:
            try:
                await session.start()
            except Exception as exc:  # noqa: BLE001 - 通道连不上要告诉手机
                log(f"连不上家里的通道：{exc}")
                await ws.send(json.dumps({"type": "error",
                                          "message": f"连不上家里的通道：{exc}"}))
                return
            await session.send_ready(args.channel)
            pending: dict = {}
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    meta, pending = pending, {}
                    kind = str(meta.get("type") or "")
                    if kind == "image":
                        await session.handle_image(bytes(raw), meta)
                    else:
                        await session.handle_audio(bytes(raw), meta)
                    continue
                data = json.loads(raw)
                kind = str(data.get("type") or "")
                if kind in {"utterance", "image"}:
                    pending = data  # 正文在下一个二进制帧里
                elif kind == "text":
                    text = str(data.get("text") or "").strip()
                    if text:
                        await session.send_json({"type": "you", "text": text})
                        await session.turn(text, kind="text")
                elif kind == "ping":
                    await session.send_json({"type": "pong", "ts": data.get("ts")})
                elif kind == "hello":
                    await session.send_ready()
        except Exception as exc:  # noqa: BLE001 - 断线是常态
            log(f"{peer} 连接结束（{type(exc).__name__}: {exc}）")
        finally:
            await session.close()

    apk_path = Path(args.apk)
    hook = make_http_hook(
        apk_path, args.token, trust_tailnet=(args.trust_tailnet and loopback),
        ws_hint=args.ws_hint,
    )
    async with websockets.serve(
        handler, args.host, args.port, max_size=None, ping_interval=None,
        process_request=hook,
    ) if hook else websockets.serve(
        handler, args.host, args.port, max_size=None, ping_interval=None,
    ):
        log(f"手机中转已监听 ws://{args.host}:{args.port}（通道 {args.channel}）")
        if loopback:
            log("手机装 App：https://<机器名>.<tailnet>.ts.net:8443/apk?token=<令牌>")
            log("在外请用：wss://<机器名>.<tailnet>.ts.net:8443/?token=<令牌>"
                "（8443 → 127.0.0.1:%d 的 tailscale serve 映射）" % args.port)
        await asyncio.Future()
    return 0


async def selftest(args) -> int:  # noqa: ANN001 - argparse
    """自己走一遍全链路（不开麦）：拿现成 wav 当"手机说的话"。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码。
    """
    wav_path = Path(args.wav) if args.wav else None
    if wav_path is None or not wav_path.is_file():
        print("[selftest] 需要一个 16k wav，用 --wav 指定")
        return 2

    class _Collector:
        """把发给手机的帧收下来（代替真手机）。"""

        def __init__(self) -> None:
            """建一个空收集器。"""
            self.frames: list[dict] = []

        async def send(self, payload) -> None:  # noqa: ANN001
            """收一帧文本（JSON）或一段二进制。

            Args:
                payload: JSON 字符串或 bytes。
            """
            if isinstance(payload, (bytes, bytearray)):
                self.frames.append({"type": "_pcm", "bytes": len(payload)})
            else:
                self.frames.append(json.loads(payload))

    session = PhoneSession("selftest", args.channel, args.channel_secret)
    session.ws = _Collector()
    await session.start()
    await session.handle_audio(wav_path.read_bytes(), {"format": "wav"})
    await session.close()
    kinds = [f.get("type") for f in session.ws.frames]
    heard = next((f.get("text") for f in session.ws.frames if f.get("type") == "you"), "")
    reply = "".join(str(f.get("text")) for f in session.ws.frames if f.get("type") == "her")
    audio_end = next((f for f in session.ws.frames if f.get("type") == "audio_end"), {})
    print(f"[selftest] 手机侧收到：{kinds}")
    print(f"[selftest] 她听到：「{heard}」")
    print(f"[selftest] 她回答（文字）：「{reply}」")
    print(f"[selftest] 她开口：{audio_end.get('row', {}).get('first_audio_ms')} ms，"
          f"音频 {audio_end.get('row', {}).get('audio_secs')} s")
    return 0 if heard and kinds and "audio_begin" in kinds else 1


def main() -> int:
    """解析参数并开跑。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="轻语手机中转服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只回环）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--channel", default=sim.URL, help="家里的通道地址")
    parser.add_argument("--channel-secret", default="auto", help="通道密钥（auto = 从配置读）")
    parser.add_argument("--token", default="auto", help="手机要带的令牌（auto = 用通道密钥）")
    parser.add_argument("--trust-tailnet", dest="trust_tailnet", action="store_true",
                        default=True, help="回环绑定时信任 tailnet 身份头（默认开）")
    parser.add_argument("--no-trust-tailnet", dest="trust_tailnet", action="store_false",
                        help="不信任 tailnet 身份头（只认令牌）")
    parser.add_argument("--apk", default=str(DEFAULT_APK), help="手机 App 的 APK 路径（手机可下载）")
    parser.add_argument("--ws-hint", default="", help="首页显示的 WebSocket 地址（手机 App 里要填的）")
    parser.add_argument("--selftest", action="store_true", help="不开麦自检")
    parser.add_argument("--wav", default="", help="自检用的 16k wav")
    args = parser.parse_args()
    if args.channel_secret == "auto":
        args.channel_secret = sim.read_platform_secret("glasses")
    if args.token == "auto":
        args.token = args.channel_secret
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if args.selftest:
        return asyncio.run(selftest(args))
    return asyncio.run(serve(args))


if __name__ == "__main__":
    raise SystemExit(main())
