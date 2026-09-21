""" NapCat / AstrBot connection watchdog: detect the bot going offline and tell you.

被 QQ 强制下线或 NapCat 挂掉时，AstrBot 这边只是"收不到消息"——日志里一片安静，
2026-09-17 就这样闷了 10 个小时。这个看门狗每 2 分钟检查一次 6199 端口上有没有
NapCat 的反连：

1. **判定离线**：连续 ``FAILS_TO_ACT`` 次（默认 2 次 ≈ 4 分钟）没有连接才算掉线，避免误报；
2. **只提醒，不动你的 QQ**（``AUTO_RESTART = False``，2026-09-18 起）：
   Windows 弹窗 + 写日志 + 给桌面桌宠留一条提醒（QQ 断了她只能在桌面上说话）；
3. **要重启由你决定**：发 ``/看门狗 重启``（插件里），或手动把 ``AUTO_RESTART`` 改成 True
   恢复原来的自动抢救（结束 QQ → 用加载器拉起，一般不用重新扫码）。

Usage:
    python napcat_watchdog.py             # 正常跑一次（插件/计划任务用这个）
    python napcat_watchdog.py --dry-run   # 只报告会做什么，不动进程
    python napcat_watchdog.py --status    # 只看当前连接状态
    python napcat_watchdog.py --restart   # 立刻重启 NapCat（手动路径）
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

NAPCAT_DIR = Path(os.environ.get("QINGYU_NAPCAT_DIR", str(Path.home() / "NapCat.Framework")))
QQ_EXE = Path(os.environ.get("QINGYU_QQ_EXE", "C:/Program Files/Tencent/QQNT/QQ.exe"))
PORT = 6199
LOG = Path.home() / ".astrbot" / "logs" / "napcat_watchdog.log"
STATE = Path.home() / ".astrbot" / "logs" / "napcat_watchdog_state.json"
FAILS_TO_ACT = 2
ALERT_COOLDOWN_MINUTES = 30
START_WAIT_SECONDS = 75
MAX_ATTEMPTS = 2
POPUP_TIMEOUT_SECONDS = 60
# 掉线之后**只提醒、不自动重启**（2026-09-18 改）。
# 以前是自动拉起 NapCat：好处是能自己恢复，坏处是——你正在用 QQ 聊天/切号时它突然把 QQ
# 杀掉重开，而且"AstrBot 悄悄把 QQ 重启了"这件事没有任何提示，出事时反而更难查。
# 现在改成：判定掉线 → 弹窗 + 写日志 + 给桌面桌宠留一条提醒；要重启由你决定：
#   · 群里/面板发 ``/看门狗 重启``（手动）
#   · 或把这里改成 True（保留原逻辑，想恢复自动抢救再打开）
AUTO_RESTART = False
# 桌面桌宠会读这个文件，把提醒冒泡到桌面上（QQ 断了的时候，桌面是她唯一还能说话的地方）
ALERT_FILE = Path.home() / ".astrbot" / "logs" / "napcat_alert.json"


def log(message: str) -> None:
    """Append one line to the watchdog log (and echo it).

    Args:
        message: Text to record.
    """
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        print(f"（写日志失败: {exc}）")


def load_state() -> dict:
    """Read the persisted counters.

    Returns:
        The state dict (empty when missing or broken).
    """
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    """Persist the counters.

    Args:
        state: State dict to write.
    """
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"（写状态失败: {exc}）")


def netstat_lines() -> list[str]:
    """Run netstat and return its output lines.

    Returns:
        Raw netstat output lines (empty when the call failed or gave no output).
    """
    try:
        completed = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"netstat 执行失败: {exc}")
        return []
    output = completed.stdout
    if not output:
        log("netstat 没有返回内容，改用 psutil 之外的手段都不行")
        return []
    return output.splitlines()


def connections_on_port(port: int) -> list[tuple[str, int]]:
    """List established connections on a local port.

    优先用 psutil（AstrBot 自带的运行环境里有），拿不到再退回 netstat——
    有些环境（比如被限制的桌面进程）跑子进程拿不到输出。

    Args:
        port: Local port to look for.

    Returns:
        ``(state, pid)`` tuples.
    """
    try:
        import psutil  # noqa: PLC0415 - 可选依赖，按需导入
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return [
                (str(conn.status), int(conn.pid or 0))
                for conn in psutil.net_connections(kind="tcp")
                if conn.status == "ESTABLISHED"
                and conn.laddr
                and getattr(conn.laddr, "port", None) == port
            ]
        except Exception as exc:  # noqa: BLE001 - 退回 netstat
            log(f"psutil 查连接失败（{type(exc).__name__}），改用 netstat")

    found: list[tuple[str, int]] = []
    needle = f":{port}"
    for line in netstat_lines():
        parts = line.split()
        if len(parts) < 5 or needle not in parts[1]:
            continue
        if parts[3] != "ESTABLISHED":
            continue
        try:
            found.append((parts[3], int(parts[4])))
        except ValueError:
            continue
    return found


def process_running(name: str) -> bool | None:
    """Whether a process with this image name is running.

    优先 psutil（不依赖子进程输出——桌面端跑的 AstrBot 进程里子进程捕获会是 None），
    取不到信息时返回 None（未知），调用方按"可能在跑"处理，避免误杀。

    Args:
        name: Image name, for example ``QQ.exe``.

    Returns:
        True / False，无法判断时返回 None。
    """
    try:
        import psutil  # noqa: PLC0415 - 可选依赖，按需导入
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["name"]):
                proc_name = proc.info.get("name") or ""
                if proc_name.lower() == name.lower():
                    return True
            return False
        except Exception:  # noqa: BLE001 - 退回 tasklist
            pass

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout
    if not output:
        return None
    return name.lower() in output.lower()


def kill_qq() -> None:
    """Terminate the QQ client so it can be restarted with the NapCat loader."""
    try:
        subprocess.run(
            ["taskkill", "/IM", "QQ.exe", "/F"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        time.sleep(5)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"结束 QQ 失败: {exc}")


def start_napcat() -> bool:
    """Start QQ through the NapCat loader.

    Returns:
        True when the launcher command was issued.
    """
    launcher = NAPCAT_DIR / "napimain.exe"
    inject = NAPCAT_DIR / "napiloader.dll"
    main_js = NAPCAT_DIR / "nativeLoader.cjs"
    for path in (launcher, inject, main_js, QQ_EXE):
        if not path.exists():
            log(f"启动所需文件缺失: {path}")
            return False
    try:
        subprocess.Popen(
            [str(launcher), str(QQ_EXE), str(inject), str(main_js).replace("\\", "/")],
            cwd=str(NAPCAT_DIR),
        )
        log("已用 NapCat 加载器拉起 QQ")
        return True
    except OSError as exc:
        log(f"拉起失败: {exc}")
        return False


def notify(title: str, message: str) -> None:
    """Show a self-dismissing Windows popup **without blocking**.

    以前用 ``subprocess.run`` 等弹窗关掉才继续，实测这个 WScript 弹窗会卡住好几分钟
    （用户没点、也没到超时），把看门狗的循环和后面的"写提醒文件"全堵住了。
    现在改成 Popen 丢出去就不管。

    Args:
        title: Popup title.
        message: Popup body.
    """
    script = (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$w.Popup('{message}', {POPUP_TIMEOUT_SECONDS}, '{title}', 48) | Out-Null"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001 - 弹不出来不影响主流程
        log(f"弹窗失败: {exc}")


def publish_alert(text: str) -> None:
    """Drop an alert file the desktop pet can pick up.

    为什么要写文件：QQ 掉线时，能提醒你的渠道只剩"不依赖 QQ 的那几个"——AstrBot 日志、
    Windows 弹窗、以及桌面桌宠。桌宠每两秒读一次这个文件，读到就冒一句。

    Args:
        text: Alert text.
    """
    try:
        ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_FILE.write_text(
            json.dumps({"ts": int(time.time()), "text": text}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log(f"写提醒文件失败: {exc}")


def restart_now() -> str:
    """Try to bring NapCat back right now (manual path, and AUTO_RESTART uses it too).

    Returns:
        A short result summary.
    """
    state = load_state()
    qq_up = process_running("QQ.exe")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # 第二次尝试前，只要 QQ 还开着（或状态未知）就先结束它——否则加载器注入不进去。
        if attempt == 2 and qq_up is not False:
            log("QQ 还在跑但 NapCat 没起来，结束 QQ 后用加载器重启")
            kill_qq()
        if not start_napcat():
            break
        time.sleep(START_WAIT_SECONDS)
        if connections_on_port(PORT):
            log(f"第 {attempt} 次重启成功，6199 已连上")
            state["fails"] = 0
            state["attempts"] = 0
            save_state(state)
            return f"已恢复（第 {attempt} 次重启成功）"
        log(f"第 {attempt} 次重启后仍未连上")
    log("重启没救回来，可能需要手动扫码登录")
    return "重启失败（可能要在 QQ 里重新登录）"


def status_text() -> str:
    """Describe the current connection state.

    Returns:
        A one-line human-readable status.
    """
    peers = connections_on_port(PORT)
    qq_up = process_running("QQ.exe")
    state = load_state()
    # 注意：正常工作时 NapCat 是注入在 QQ 进程里的，napimain.exe 启动完就退出了，
    # 所以判断在线只看 6199 上有没有连接。
    return (
        f"6199 连接: {len(peers)} 条"
        f"{('（对端 pid ' + str(peers[0][1]) + '）') if peers else ''}；"
        f"QQ={'在跑' if qq_up else ('没跑' if qq_up is False else '未知')}；"
        f"连续未连上 {int(state.get('fails', 0))} 次"
    )


def check_once(dry_run: bool = False) -> str:
    """Run one watchdog check and return a short result line.

    这是给插件和命令行共用的入口：断言连接、必要时重启 NapCat。

    Args:
        dry_run: 只报告不重启。

    Returns:
        这次检查的结果摘要。
    """
    peers = connections_on_port(PORT)
    qq_up = process_running("QQ.exe")
    napcat_up = process_running("napimain.exe") or process_running("NapCatWinBootMain.exe")
    qq_label = "在" if qq_up else ("不在" if qq_up is False else "未知")

    state = load_state()
    if peers:
        if state.get("fails"):
            log(f"已恢复：6199 上有 {len(peers)} 条连接")
        state["fails"] = 0
        state["attempts"] = 0
        save_state(state)
        return "在线"

    fails = int(state.get("fails", 0)) + 1
    state["fails"] = fails
    save_state(state)
    if fails < FAILS_TO_ACT:
        log(f"没连上（第 {fails}/{FAILS_TO_ACT} 次确认；QQ={qq_label}）")
        return f"第 {fails}/{FAILS_TO_ACT} 次确认中"

    if time.time() - float(state.get("last_alert", 0)) < ALERT_COOLDOWN_MINUTES * 60:
        log("仍在冷却期，跳过本次处理")
        return "冷却中（上次抢救失败）"

    log(
        f"判定掉线：6199 无连接（连续 {fails} 次），QQ={qq_label}，NapCat 加载器={napcat_up}"
        f"（自动重启={'开' if AUTO_RESTART else '关'}）",
    )
    if dry_run:
        log("dry-run：不重启任何进程")
        return "掉线（dry-run，未处理）"

    if not AUTO_RESTART:
        # 只提醒：**先把持久的提醒落下来**（日志 + 给桌宠的文件），再弹窗。
        # 弹窗可能被用户晾着，不能让它挡住这两件事（吃过一次亏）。
        state["last_alert"] = time.time()
        save_state(state)
        hint = (
            f"NapCat 连续 {FAILS_TO_ACT} 次没连上（6199 无连接，QQ={qq_label}）。\n"
            "自动重启已关闭，我没有动你的 QQ。\n"
            "要我自己重启：发 /看门狗 重启；要手动登录：打开 QQ 扫码/快速登录。\n"
            f"详情看 {LOG}"
        )
        log("已提醒：自动重启是关闭状态，没有动 QQ/NapCat")
        publish_alert(hint.replace("\n", " "))
        notify("轻语掉线了（未自动重启）", hint)
        return "掉线（已提醒，未重启）"

    result = restart_now()
    if result.startswith("已恢复"):
        return f"已自动恢复（{result}）"

    state["last_alert"] = time.time()
    save_state(state)
    log("自动重启没救回来，提醒用户（可能需要手动扫码登录）")
    hint = (
        f"NapCat 连续 {FAILS_TO_ACT} 次没连上，自动重启 {MAX_ATTEMPTS} 次也没成功。\n"
        "多半是 QQ 被踢下线需要重新登录，请打开 QQ 扫码/快速登录。\n"
        f"详情看 {LOG}"
    )
    publish_alert(hint.replace("\n", " "))
    notify("轻语掉线了", hint)
    return "抢救失败，已提醒"


def main() -> None:
    """Command line entry point."""
    if "--status" in sys.argv:
        print(status_text())
        return
    if "--restart" in sys.argv:
        print(restart_now())
        return
    print(check_once(dry_run="--dry-run" in sys.argv))


if __name__ == "__main__":
    main()
