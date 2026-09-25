"""Offline regression tests for the desktop pet stability fixes.

Covers three fixes without touching the network:

1. ``setup_window`` test button: the outcome is marshalled back to the GUI
   thread through a Qt signal, and the button is re-enabled even when the test
   itself raises.
2. ``chat.ChatClient._session``: task exceptions are retrieved, pending tasks
   are cancelled and awaited, no automatic reconnect happens, and one failure
   signal is emitted on an abnormal disconnect.
3. ``chat.ChatWindow.rebind``: after a manual reconnect, the open chat window
   is rewired to the new client and detached from the old one.

Run with the desktop pet's venv Python (has PySide6 + websockets), headless:

    set QT_QPA_PLATFORM=offscreen
    python test_stability_fixes.py
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio
import sys
import time

import setup_window
import websockets
from chat import ChatClient, ChatWindow
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication


def ensure_app() -> QApplication:
    """Return (or create) the single QApplication, headless."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Pump the GUI event loop until the predicate is true or time runs out."""
    app = ensure_app()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# Fix 2: setup_window test button returns to the GUI thread via a signal.
# --------------------------------------------------------------------------


def _spy_status_updates(dialog) -> list:
    """Monkeypatch the status label's ``setText`` to record the calling thread.

    Args:
        dialog: The setup dialog under test.

    Returns:
        The list of ``QThread`` objects that called ``setText`` (test-only spy).
    """
    threads: list = []
    original = dialog.status.setText

    def spy(text: str) -> None:
        threads.append(QThread.currentThread())
        original(text)

    dialog.status.setText = spy
    return threads


def test_setup_button_re_enables_on_success() -> None:
    app = ensure_app()
    main_thread = app.thread()
    setup_window.brain.test_connection = lambda _config: (
        True,
        "连通成功，模型回：收到",
    )

    dialog = setup_window.SetupDialog({"mode": "local"})
    assert dialog.test_button.isEnabled()
    threads = _spy_status_updates(dialog)
    dialog._test()
    assert not dialog.test_button.isEnabled(), "button must be disabled while testing"

    assert wait_until(dialog.test_button.isEnabled)
    assert threads, "the status label was never updated"
    assert all(thread == main_thread for thread in threads), threads
    assert "连通成功" in dialog.status.text()


def test_setup_button_re_enables_on_exception() -> None:
    app = ensure_app()
    main_thread = app.thread()

    def boom(_config):
        raise RuntimeError("boom")

    setup_window.brain.test_connection = boom

    dialog = setup_window.SetupDialog({"mode": "local"})
    threads = _spy_status_updates(dialog)
    dialog._test()

    assert wait_until(dialog.test_button.isEnabled)
    assert threads, "the status label was never updated"
    assert all(thread == main_thread for thread in threads), threads
    assert "RuntimeError" in dialog.status.text()
    assert "boom" in dialog.status.text()


def test_setup_dialog_opens_in_every_mode() -> None:
    """The dialog must build in all three modes (it used to crash in AstrBot mode).

    真实事故（2026-09-25 16:34:42）：用户点「设置」直接崩——
    ``mode.currentTextChanged`` 在控件建好之前就连上了 ``_sync``，
    而构造里给"连 AstrBot"模式调 ``setCurrentText`` 会立刻触发它，此时 ``self.llm_box``
    还不存在。这里把三种模式都构造一遍，顺便验证联动开关仍然生效。
    """
    ensure_app()
    for config in (
        {"mode": "local"},
        {"mode": "astrbot", "desktop_url": "ws://127.0.0.1:6198"},
        {"mode": "astrbot", "desktop_url": "ws://10.0.0.9:6198"},
    ):
        dialog = setup_window.SetupDialog(dict(config))
        # 切换运行方式要能实时联动（连接必须还在，只是挪到了控件建好之后）
        dialog.mode.setCurrentText(setup_window.MODE_LOCAL)
        assert dialog.llm_box.isEnabled()
        assert not dialog.link_box.isEnabled()
        dialog.mode.setCurrentText(setup_window.MODE_OWN)
        assert not dialog.llm_box.isEnabled()
        assert dialog.link_box.isEnabled()
        dialog.deleteLater()


# --------------------------------------------------------------------------
# Fix 3: _session retrieves exceptions, cancels/awaits pending, no reconnect.
# --------------------------------------------------------------------------


class _FakeWS:
    """A websocket stub whose ``recv`` raises ConnectionClosed once."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        raise websockets.exceptions.ConnectionClosed(1006, "closed by server")


class _FakeConnect:
    """Async context manager standing in for ``websockets.connect``."""

    def __init__(self, ws) -> None:  # noqa: ANN001 - test stub
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN001
        return False


def _run_session(client: ChatClient, ws) -> list[dict]:  # noqa: ANN001 - test stub
    """Drive one ``_session`` on a fresh loop and collect unretrieved-exception reports."""
    connect_calls: list[bool] = []

    def fake_connect(*_args, **_kwargs):
        connect_calls.append(True)
        return _FakeConnect(ws)

    original = websockets.connect
    websockets.connect = fake_connect
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    reports: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        loop.run_until_complete(client._session())
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        websockets.connect = original
    return reports


def test_session_emits_one_failure_and_cleans_up() -> None:
    ensure_app()
    client = ChatClient(session_id="s1", carry_over=False)
    client._login = lambda: "token"
    failed: list[str] = []
    client.failed.connect(failed.append)

    reports = _run_session(client, _FakeWS())

    # One failure signal, and no "Task exception was never retrieved".
    assert failed == [
        "和 AstrBot 面板的连接断了（已停止自动重连）——右键→「重连 AstrBot」可以再试。",
    ], failed
    assert reports == [], reports


def test_session_stop_is_silent() -> None:
    """A user-initiated stop must not emit a failure signal."""

    class _BlockingWS:
        async def send(self, message: str) -> None:  # noqa: ANN001
            pass

        async def recv(self) -> str:
            await asyncio.Future()  # blocks until cancelled

    ensure_app()
    client = ChatClient(session_id="s1", carry_over=False)
    client._login = lambda: "token"
    client._stop.set()  # stop before the session body runs
    failed: list[str] = []
    client.failed.connect(failed.append)

    _run_session(client, _BlockingWS())
    assert failed == [], failed


def test_session_external_cancel_cleans_up_child_tasks() -> None:
    """Cancelling _session from outside must still cancel + await the child tasks."""

    class _BlockingWS:
        async def send(self, message: str) -> None:  # noqa: ANN001
            pass

        async def recv(self) -> str:
            await asyncio.Future()  # blocks forever, receiver stays pending

    ensure_app()
    client = ChatClient(session_id="s1", carry_over=False)
    client._login = lambda: "token"

    original = websockets.connect
    websockets.connect = lambda *_args, **_kwargs: _FakeConnect(_BlockingWS())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    reports: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        session_task = loop.create_task(client._session())
        # Let the session reach asyncio.wait (past the 0.6s bind sleep).
        loop.run_until_complete(asyncio.sleep(0.8))
        session_task.cancel()
        try:
            loop.run_until_complete(session_task)
        except asyncio.CancelledError:
            pass
        # No child tasks may remain and no exception may be left unretrieved.
        assert asyncio.all_tasks(loop) == set(), asyncio.all_tasks(loop)
        assert reports == [], reports
    finally:
        loop.close()
        asyncio.set_event_loop(None)
        websockets.connect = original


# --------------------------------------------------------------------------
# Fix 4: ChatWindow.rebind swaps the client and rewires its signals.
# --------------------------------------------------------------------------


def test_chat_window_rebind_rewires_signals() -> None:
    ensure_app()
    old = ChatClient()
    new = ChatClient()
    replies: list[str] = []

    window = ChatWindow(old, on_reply=replies.append)
    window.rebind(new)

    new.done.emit("hello")
    assert replies == ["hello"], replies

    old.done.emit("stale")  # old client is disconnected from the window
    assert replies == ["hello"], replies
    assert window.client is new


def main() -> int:
    """Run every test_* function and report."""
    for name in sorted(globals()):
        if name.startswith("test_"):
            globals()[name]()
            print(f"PASS {name}")
    print("all stability tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
