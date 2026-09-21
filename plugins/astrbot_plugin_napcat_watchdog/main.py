r"""连接看门狗：NapCat/QQ 掉线就**提醒你**（不再自动重启 QQ）。

被 QQ 强制下线时，AstrBot 这边只是"收不到消息"，日志里一片安静——2026-09-17 就这样
闷了 10 个小时。这个插件在 AstrBot 进程里每 2 分钟检查一次 6199 端口上有没有 NapCat
的反连：

- 连续 2 次没有 → 判定掉线；
- **只提醒**：Windows 弹窗 + 写日志 + 给桌面桌宠留一条（QQ 断了她只能回到桌面说话）；
- **不自动动你的 QQ**（2026-09-18 改）。以前会自动结束 QQ 再拉起 NapCat，好处是能自己
  恢复，坏处是你正在用 QQ 时它突然被杀掉重开，而且没有任何提示。现在要不要重启由你决定：
  发 ``/看门狗 重启``，或 ``/看门狗 自动重启 开`` 恢复旧的自动抢救。

判定逻辑复用 ``PROJECT_ROOT\migration_tools\napcat_watchdog.py``，
所以手动跑脚本和插件跑的是同一套代码。想临时关掉：``/看门狗 关闭``。

指令：``/看门狗``（状态）、``/看门狗 查``（立刻检查）、``/看门狗 重启``（手动重启 NapCat）、
``/看门狗 自动重启 开|关``、``/看门狗 关闭|开启``（关掉整个看门狗）
"""

import asyncio
import importlib.util
import time
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger

SCRIPT = Path(__file__).with_name("watchdog_runtime.py")
CHECK_SECONDS = 120


def _load_watchdog():
    """Load the standalone watchdog module so both share one implementation.

    Returns:
        The loaded module, or None when the file is missing/broken.
    """
    if not SCRIPT.exists():
        logger.error(f"napcat_watchdog: 找不到 {SCRIPT}")
        return None
    try:
        spec = importlib.util.spec_from_file_location("napcat_watchdog", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001 - 加载失败就当没这功能
        logger.error(f"napcat_watchdog: 加载脚本失败: {type(exc).__name__}: {exc}")
        return None


@register(
    "napcat_watchdog",
    "migration",
    "连接看门狗：NapCat/QQ 掉线只提醒不自动重启（/看门狗 查｜重启｜自动重启）",
    "1.1.0",
)
class NapCatWatchdogPlugin(Star):
    """Watches the NapCat↔AstrBot link and tells you when it drops (no auto-restart)."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._module = _load_watchdog()
        self._enabled = self._module is not None
        self._task: asyncio.Task | None = None
        self._last_result = "还没检查过"
        self._last_check_at = 0.0

    async def initialize(self) -> None:
        """Start the background check loop."""
        if not self._enabled:
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(f"napcat_watchdog: 已启动，每 {CHECK_SECONDS} 秒检查一次 6199 连接")

    async def _loop(self) -> None:
        """Check the connection forever, sleeping between checks."""
        while True:
            try:
                if self._enabled:
                    await self._check()
            except Exception as exc:  # noqa: BLE001 - 循环不能因为一次异常停掉
                logger.error(f"napcat_watchdog: 检查异常 {type(exc).__name__}: {exc}")
            await asyncio.sleep(CHECK_SECONDS)

    async def _check(self, dry_run: bool = False) -> str:
        """Run one check in a worker thread (netstat/tasklist are blocking).

        Args:
            dry_run: Only report, do not restart anything.

        Returns:
            The result summary.
        """
        result = await asyncio.to_thread(self._module.check_once, dry_run)
        self._last_result = str(result)
        self._last_check_at = time.time()
        if result not in {"在线"}:
            logger.warning(f"napcat_watchdog: {result}")
        return self._last_result

    @filter.command("看门狗")
    async def watchdog_command(self, event: AstrMessageEvent):
        """查看或操作连接看门狗（管理员）。

        用法：``/看门狗`` 看状态；``/看门狗 查`` 立刻检查一次；
        ``/看门狗 重启`` 手动重启 NapCat；``/看门狗 自动重启 开|关`` 开关自动抢救；
        ``/看门狗 关闭``／``/看门狗 开启`` 开关整个看门狗。

        Args:
            event: 指令消息事件。

        Yields:
            状态或执行结果文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        if self._module is None:
            yield event.plain_result(f"看门狗没加载起来（找不到或读不了 {SCRIPT}）。")
            return
        argument = (event.message_str or "").replace("看门狗", "", 1).strip()
        auto_state = "开" if getattr(self._module, "AUTO_RESTART", False) else "关"
        if argument in {"关闭", "停", "off"}:
            self._enabled = False
            yield event.plain_result("好，看门狗整个停了（不会提醒也不会重启）。")
            return
        if argument in {"开启", "开", "on"}:
            self._enabled = True
            yield event.plain_result(
                f"看门狗已打开：每 {CHECK_SECONDS} 秒查一次，连续 2 次连不上就提醒你"
                f"（自动重启现在是「{auto_state}」）。",
            )
            return
        if argument.startswith("自动重启"):
            want = argument.replace("自动重启", "", 1).strip() in {"开", "开启", "on", "true", "1"}
            self._module.AUTO_RESTART = want
            yield event.plain_result(
                "自动重启已打开：判定掉线后会结束 QQ 再用加载器拉起 NapCat。"
                if want
                else "自动重启已关闭：掉线只提醒，不动你的 QQ（要重启发 /看门狗 重启）。",
            )
            return
        if argument in {"重启", "重连", "restart"}:
            result = await asyncio.to_thread(self._module.restart_now)
            yield event.plain_result(f"手动重启：{result}")
            return
        if argument in {"查", "检查"}:
            result = await self._check()
            yield event.plain_result(f"检查结果：{result}\n{self._module.status_text()}")
            return
        age = (
            f"{int(time.time() - self._last_check_at)} 秒前"
            if self._last_check_at
            else "还没检查过"
        )
        yield event.plain_result(
            f"看门狗：{'开启' if self._enabled else '关闭'}，每 {CHECK_SECONDS} 秒检查一次，"
            f"自动重启：{auto_state}，最近一次 {age}：{self._last_result}\n"
            f"{self._module.status_text()}\n"
            "可用：/看门狗 查｜/看门狗 重启｜/看门狗 自动重启 开|关｜/看门狗 关闭｜/看门狗 开启",
        )

    async def terminate(self) -> None:
        """Stop the background loop when the plugin unloads."""
        if self._task:
            self._task.cancel()
