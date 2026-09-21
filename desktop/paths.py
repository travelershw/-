"""路径解析：源码运行和打包成 exe 之后都能找到自己的东西。

打包后（PyInstaller）：

- **程序目录** = exe 所在目录（数据、配置、立绘、截图、日志都放这儿，解压即用）；
- **资源目录** = PyInstaller 解出来的临时目录（只读，装的是随包发的默认立绘），
  第一次运行会把默认立绘复制到程序目录，用户以后想换图直接改程序目录里的 assets 就行。
"""

import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# 程序目录（可写）
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
# 随包资源目录（只读）
RESOURCE = Path(getattr(sys, "_MEIPASS", BASE)) if FROZEN else Path(__file__).resolve().parent

CONFIG = BASE / "config.json"
DATA_DB = BASE / "pet_data.db"
SHOTS = BASE / "shots"
ASSETS = BASE / "assets"
LOG = BASE / "pet_log.txt"
BUNDLED_ASSETS = RESOURCE / "assets"
USER_GUIDE = BASE / "使用说明.txt"


def ensure_layout() -> None:
    """Create the writable folders and seed the bundled art on first run."""
    for folder in (SHOTS, ASSETS):
        folder.mkdir(parents=True, exist_ok=True)
    if FROZEN and BUNDLED_ASSETS.is_dir():
        existing = [
            path
            for path in ASSETS.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}
        ]
        if not existing:
            for source in BUNDLED_ASSETS.iterdir():
                target = ASSETS / source.name
                if source.is_dir() or target.exists():
                    continue
                try:
                    shutil.copy2(source, target)
                except OSError:
                    pass


def describe() -> str:
    """One-line description of where things live.

    Returns:
        Chinese summary.
    """
    return f"程序目录 {BASE}｜资源目录 {RESOURCE}｜打包运行 {FROZEN}"
