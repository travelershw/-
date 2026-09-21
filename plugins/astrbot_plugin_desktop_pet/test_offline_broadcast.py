"""Offline regression test for the desktop_pet adapter's ``_broadcast`` path.

Guards the fix that removed the undefined name ``QUEUE_WHEN_OFFLINE``: whenever
no desktop pet was connected, ``_broadcast`` raised ``NameError`` from the
reply / proactive-message delivery path.

Importing the adapter is intentionally avoided (``astrbot.*`` needs a full
AstrBot runtime). The module is checked at the source level (parse + no
undefined flag), and the real ``_broadcast`` body is extracted from the AST,
compiled, and executed against a stub ``self`` to prove the empty-clients path
returns 0.
"""

import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).with_name("main.py")


def _broadcast_source() -> ast.AsyncFunctionDef:
    """Return the ``_broadcast`` function node from the adapter source."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_broadcast"
    )


def test_module_parses() -> None:
    """The adapter module is valid Python."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
    assert tree is not None


def test_undefined_offline_flag_removed() -> None:
    """The undefined ``QUEUE_WHEN_OFFLINE`` reference is gone."""
    src = MODULE.read_text(encoding="utf-8")
    assert "QUEUE_WHEN_OFFLINE" not in src
    assert "消息暂存（未实现）" not in src


def test_broadcast_empty_clients_returns_zero() -> None:
    """Execute the real ``_broadcast`` with no clients and expect 0.

    The method is compiled from its own AST node and called with a stub ``self``
    carrying an ``asyncio.Lock`` and an empty ``_clients`` set, so the actual
    code path (not just its shape) is exercised.
    """
    node = _broadcast_source()
    compiled = compile(ast.Module(body=[node], type_ignores=[]), str(MODULE), "exec")
    namespace: dict = {"json": json}
    # Safe: the function is extracted from this repo's own source.
    exec(compiled, namespace)
    broadcast = namespace["_broadcast"]

    async def run() -> int:
        fake = SimpleNamespace(_lock=asyncio.Lock(), _clients=set())
        return await broadcast(fake, {"type": "reply", "text": "hi"})

    assert asyncio.run(run()) == 0


def main() -> int:
    """Run every test_* function and report."""
    for name in sorted(globals()):
        if name.startswith("test_"):
            globals()[name]()
            print(f"PASS {name}")
    print("all offline broadcast tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
