"""立绘接入：把一张人设图变成桌宠能用的皮肤（阶段 E 的第一步）。

约定（放进去就自动生效，不用改代码）：

```
pet_desktop/assets/
  light.png            默认立绘（也可以叫 default.png / base.png / 或者随便一张唯一图片）
  happy.png            按表情一张张给：calm/smile/happy/shy/down/huffy/sleepy/surprised
  ...
```

处理链：

1. **抠背景**：图片没有透明通道时，从四边做**洪水填充**（只吃连通的同色像素，不碰图内的
   同色区域），把纯色背景变透明，结果缓存成 ``assets/.cut/<名字>.png``（下次直接用）；
2. **缩放**：按设计框 220x250 等比缩放、水平居中、脚底对齐 y=242，绝不拉伸变形；
3. **遮罩**：按立绘 alpha 生成窗口遮罩，透明的地方点不到（不然一大块空白会挡住桌面）。

没有放图片时，``pet.py`` 会退回 ``faces.paint_character`` 画的占位小人。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QBitmap, QColor, QImage, QPixmap, QRegion

import paths

ROOT = paths.BASE
ASSETS = paths.ASSETS
CUT_DIR = ASSETS / ".cut"
EXTS = (".png", ".webp", ".jpg", ".jpeg", ".bmp", ".gif")
GENERIC_NAMES = ("light", "default", "base", "默认", "轻语")
# 抠背景时颜色容差（0~255）：默认保守；BACKDROP_MIN_LUMA=0 表示不按亮度设限
TOLERANCE = 42
# 抠背景的色差容差。默认保守（42）：只会删掉和背景色几乎一样的像素，**绝不会啃到人物**，
# 代价是背景有渐变/纸纹时会残留一圈浅色。这个值可以调（``cut_art.py --tolerance`` 或
# config.json 的 art_cut_tolerance），调大能清掉残留、但太大会啃到画面里的浅色部分
# ——实测这份插画人物本身很亮，容差放到 78 会把人整个吃掉，所以别一步跨太大。
BACKDROP_TOLERANCE = TOLERANCE
# 0 = 不按亮度设限（默认）。设成 200 表示"只有比这更亮的像素才算背景"，
# 背景是浅色、人物是深色时能防止误伤，但人物本身很亮时会把人吃掉。
BACKDROP_MIN_LUMA = 0
# 绿幕：g - max(r,b) 大于这个值就算背景（实测绿幕图在 116~131，留足余量）
GREEN_MARGIN = 40
# 羽化跨度（相对容差）：1.5 表示"离背景 1~1.5 倍容差"的像素按比例变半透明
FEATHER_SPAN = 1.5

_cache: dict[str, QPixmap] = {}
_loaded = False
_meta: dict = {}


@dataclass
class SpriteInfo:
    """What the pet needs to know about the current art."""

    name: str = ""
    width: int = 0
    height: int = 0
    cut: bool = False
    framed: bool = False
    looks: tuple[str, ...] = ()

    @property
    def aspect(self) -> float:
        """Width divided by height (1.0 when unknown).

        Returns:
            The aspect ratio.
        """
        if not self.height:
            return 1.0
        return self.width / self.height


LOOK_KEYS = (
    "calm",
    "smile",
    "happy",
    "shy",
    "down",
    "huffy",
    "sleepy",
    "surprised",
)


def _transparency(path: Path) -> float:
    """Share of fully transparent pixels (used to prefer real cut-outs).

    Args:
        path: Image file.

    Returns:
        0~1; 0 when the file cannot be read.
    """
    image = QImage(str(path))
    if image.isNull():
        return 0.0
    if not image.hasAlphaChannel():
        return 0.0
    image = image.convertToFormat(QImage.Format_ARGB32)
    buffer = image.bits()
    stride = image.bytesPerLine()
    step = max(1, image.width() // 200)
    clear = total = 0
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            total += 1
            if buffer[y * stride + x * 4 + 3] < 16:
                clear += 1
    return clear / total if total else 0.0


def _find_files() -> dict[str, Path]:
    """Map look keys (and ``light``) to image files.

    规矩：文件名正好是表情名（calm/smile/…）→ 那张图给这个表情；其它一律算"默认立绘"
    的候选（粘贴来的哈希名、中文名都行）。候选里**优先用真的带透明的**，其次用最新的
    ——这样"纯白底的旧图 + 真透明底的新图"同时放着时，会自动挑对。

    Returns:
        ``{key: path}``; an empty dict when ``assets/`` has no usable image.
    """
    if not ASSETS.is_dir():
        return {}
    found: dict[str, Path] = {}
    generics: list[Path] = []
    files = [
        path
        for path in sorted(ASSETS.iterdir())
        if path.is_file() and path.suffix.lower() in EXTS
    ]
    if not files and paths.BUNDLED_ASSETS.is_dir() and paths.BUNDLED_ASSETS != ASSETS:
        # 打包发出去之后：程序目录里还没放图，就先读随包带的默认立绘
        files = [
            path
            for path in sorted(paths.BUNDLED_ASSETS.iterdir())
            if path.is_file() and path.suffix.lower() in EXTS
        ]
    for path in files:
        key = path.stem.lower()
        if key in LOOK_KEYS:
            found.setdefault(key, path)
        else:
            generics.append(path)
    if generics:
        ranked = sorted(
            generics,
            key=lambda path: (_transparency(path), path.stat().st_mtime),
            reverse=True,
        )
        found["light"] = ranked[0]
    return found


def _is_flat_background(image: QImage) -> bool:
    """Heuristic: does the image have a uniform, non-transparent background?

    Args:
        image: Source image.

    Returns:
        True when the four corners agree and are opaque.
    """
    if image.hasAlphaChannel():
        # 已经有透明通道、而且边角是透明的，就不用再抠了
        corners = [
            image.pixelColor(0, 0),
            image.pixelColor(image.width() - 1, 0),
            image.pixelColor(0, image.height() - 1),
            image.pixelColor(image.width() - 1, image.height() - 1),
        ]
        if sum(1 for colour in corners if colour.alpha() < 16) >= 2:
            return False
    corners = [
        image.pixelColor(0, 0),
        image.pixelColor(image.width() - 1, 0),
        image.pixelColor(0, image.height() - 1),
        image.pixelColor(image.width() - 1, image.height() - 1),
    ]
    first = corners[0]
    for colour in corners[1:]:
        if (
            abs(colour.red() - first.red()) > TOLERANCE
            or abs(colour.green() - first.green()) > TOLERANCE
            or abs(colour.blue() - first.blue()) > TOLERANCE
        ):
            return False
    return True


def set_cut_options(tolerance: float | None = None, light_min: float | None = None) -> None:
    """Adjust the background-cut knobs (used by ``cut_art.py``).

    Args:
        tolerance: Colour-distance tolerance (bigger removes more).
        light_min: When set, only pixels at least this bright may count as background.
    """
    global BACKDROP_TOLERANCE, BACKDROP_MIN_LUMA
    if tolerance is not None:
        BACKDROP_TOLERANCE = float(tolerance)
    if light_min is not None:
        BACKDROP_MIN_LUMA = float(light_min)


def _strip_background(image: QImage) -> QImage:
    """Cut a flat background out of the image (scanline flood fill from the borders).

    2048x2048 这种大图也吃得消：按行成段填充（栈里放的是"段"不是"点"），并且直接改
    QImage 的像素缓冲（BGRA），不走 ``pixelColor`` 那种一个像素一次调用的慢路。

    抠完再做一遍**边缘羽化**：紧挨着背景、颜色又接近背景的像素给半透明，减轻白边。

    Args:
        image: Source image (RGB or RGBA).

    Returns:
        An ARGB32 image with the background removed and edges softened.
    """
    return _flood_cut(image, mode="flat")


def _green_level(red: int, green: int, blue: int) -> float:
    """How "green screen" a pixel is, in 0~1.

    绿幕导出会有噪点和明暗不均，所以不按"离某个绿色有多远"判，而是按"绿得有多明显"：
    ``g - max(r, b)`` 大于 40 就是背景，10~40 之间按比例给半透明（抗锯齿边缘）。

    Args:
        red: Red channel.
        green: Green channel.
        blue: Blue channel.

    Returns:
        1.0 for clear background, 0.0 for clearly not background.
    """
    if green <= 80:
        return 0.0
    margin = green - max(red, blue)
    if margin >= GREEN_MARGIN:
        return 1.0
    if margin <= 10:
        return 0.0
    return (margin - 10) / (GREEN_MARGIN - 10)


def _border_green_share(image: QImage) -> float:
    """Share of border pixels that look like a green screen.

    Args:
        image: Source image.

    Returns:
        0~1.
    """
    width, height = image.width(), image.height()
    source = image.convertToFormat(QImage.Format_ARGB32)
    buffer = source.bits()
    stride = source.bytesPerLine()
    step = max(1, min(width, height) // 256)
    total = green = 0
    for x in range(0, width, step):
        for y in (0, 1, height - 2, height - 1):
            index = y * stride + x * 4
            total += 1
            if _green_level(buffer[index + 2], buffer[index + 1], buffer[index]) >= 1.0:
                green += 1
    for y in range(0, height, step):
        for x in (0, 1, width - 2, width - 1):
            index = y * stride + x * 4
            total += 1
            if _green_level(buffer[index + 2], buffer[index + 1], buffer[index]) >= 1.0:
                green += 1
    return green / total if total else 0.0


def _flood_cut(image: QImage, *, mode: str) -> QImage:
    """Cut the background by flooding from the borders.

    Args:
        image: Source image.
        mode: ``flat``（按与角落同色的色差）或 ``chroma``（绿幕）.

    Returns:
        ARGB32 image with the background removed and edges softened.
    """
    source = image.convertToFormat(QImage.Format_ARGB32)
    width, height = source.width(), source.height()
    buffer = source.bits()
    stride = source.bytesPerLine()
    seed = (buffer[2], buffer[1], buffer[0])  # (0,0) 的 RGB，ARGB32 在内存里是 BGRA
    tolerance = BACKDROP_TOLERANCE
    min_luma = BACKDROP_MIN_LUMA

    def level(x: int, y: int) -> float:
        """Background-ness of a pixel: 1 = background, 0 = keep.

        Args:
            x: Pixel x.
            y: Pixel y.

        Returns:
            0~1.
        """
        index = y * stride + x * 4
        blue, green, red = buffer[index], buffer[index + 1], buffer[index + 2]
        if mode == "chroma":
            return _green_level(red, green, blue)
        if min_luma > 0:
            luma = (red * 299 + green * 587 + blue * 114) // 1000
            if luma < min_luma:
                return 0.0
        distance = max(abs(red - seed[0]), abs(green - seed[1]), abs(blue - seed[2]))
        if distance <= tolerance:
            return 1.0
        if distance >= tolerance * FEATHER_SPAN:
            return 0.0
        return 1.0 - (distance - tolerance) / (tolerance * (FEATHER_SPAN - 1.0))

    visited = bytearray(width * height)
    stack: list[tuple[int, int]] = []
    for x in range(width):
        stack.append((x, 0))
        stack.append((x, height - 1))
    for y in range(height):
        stack.append((0, y))
        stack.append((width - 1, y))
    while stack:
        x, y = stack.pop()
        if visited[y * width + x] or level(x, y) < 1.0:
            continue
        left = x
        while left > 0 and not visited[y * width + left - 1] and level(left - 1, y) >= 1.0:
            left -= 1
        right = x
        while (
            right + 1 < width
            and not visited[y * width + right + 1]
            and level(right + 1, y) >= 1.0
        ):
            right += 1
        for xx in range(left, right + 1):
            visited[y * width + xx] = 1
            buffer[y * stride + xx * 4 + 3] = 0
            for ny in (y - 1, y + 1):
                if 0 <= ny < height and not visited[ny * width + xx] and level(xx, ny) >= 1.0:
                    stack.append((xx, ny))

    # 边缘羽化：只处理"贴着透明像素"的那一圈，避免把人物身上的浅色也淡掉
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            index = y * stride + x * 4
            if buffer[index + 3] == 0:
                continue
            if min(
                buffer[index - 4 + 3],
                buffer[index + 4 + 3],
                buffer[index - stride + 3],
                buffer[index + stride + 3],
            ) > 40:
                continue
            value = level(x, y)
            if 0.0 < value < 1.0:
                buffer[index + 3] = int((1.0 - value) * 255)
    return source

def _round_card(image: QImage, radius_ratio: float = 0.10) -> QImage:
    """Round the corners of a full-bleed illustration.

    满幅插画（背景不是纯色、抠不出来）直接摆在桌面上就是一块硬边方图；圆角 + 轻微
    羽化以后像一张贴纸/立牌，观感好很多。已经抠过背景的图不会走这里。

    Args:
        image: Source image.
        radius_ratio: Corner radius as a fraction of the shorter side.

    Returns:
        ARGB32 image with rounded, anti-aliased corners.
    """
    from PySide6.QtGui import QPainterPath

    width, height = image.width(), image.height()
    radius = max(6.0, min(width, height) * radius_ratio)
    canvas = QImage(width, height, QImage.Format_ARGB32)
    canvas.fill(QColor(0, 0, 0, 0))
    path = QPainterPath()
    path.addRoundedRect(1.0, 1.0, width - 2.0, height - 2.0, radius, radius)
    from PySide6.QtGui import QPainter as _QPainter

    painter = _QPainter(canvas)
    painter.setRenderHint(_QPainter.Antialiasing)
    painter.setClipPath(path)
    painter.drawImage(0, 0, image.convertToFormat(QImage.Format_ARGB32))
    painter.end()
    return canvas


def _scan_regions(
    buffer,
    stride: int,
    width: int,
    height: int,
    *,
    opaque: bool,
) -> list[tuple[int, bool, list[tuple[int, int, int]]]]:
    """Find connected regions of opaque (or transparent) pixels.

    按行成段扫描，每个区域记成 ``(大小, 是否贴边, [(y, x0, x1), …])``——存"段"不存"点"，
    380 万像素的图也不会把内存吃满。

    Args:
        buffer: Pixel buffer (BGRA).
        stride: Bytes per line.
        width: Image width.
        height: Image height.
        opaque: True to collect opaque regions, False for transparent ones.

    Returns:
        Region list.
    """
    visited = bytearray(width * height)
    regions: list[tuple[int, bool, list[tuple[int, int, int]]]] = []

    def is_target(x: int, y: int) -> bool:
        alpha = buffer[y * stride + x * 4 + 3]
        return alpha >= 128 if opaque else alpha < 128

    for start_y in range(height):
        for start_x in range(width):
            index = start_y * width + start_x
            if visited[index] or not is_target(start_x, start_y):
                continue
            stack = [(start_x, start_y)]
            spans: list[tuple[int, int, int]] = []
            size = 0
            touches = False
            while stack:
                x, y = stack.pop()
                if visited[y * width + x] or not is_target(x, y):
                    continue
                left = x
                while left > 0 and not visited[y * width + left - 1] and is_target(left - 1, y):
                    left -= 1
                right = x
                while (
                    right + 1 < width
                    and not visited[y * width + right + 1]
                    and is_target(right + 1, y)
                ):
                    right += 1
                for xx in range(left, right + 1):
                    visited[y * width + xx] = 1
                spans.append((left, right, y))
                size += right - left + 1
                if left == 0 or right == width - 1 or y == 0 or y == height - 1:
                    touches = True
                for ny in (y - 1, y + 1):
                    if not 0 <= ny < height:
                        continue
                    for xx in range(left, right + 1):
                        if not visited[ny * width + xx] and is_target(xx, ny):
                            stack.append((xx, ny))
            regions.append((size, touches, spans))
    return regions


def _clean_mask(
    image: QImage,
    island_ratio: float = 0.002,
    hole_ratio: float = 0.0004,
) -> tuple[QImage, int, int]:
    """Tidy the alpha mask: drop stray specks, patch speckle-sized holes.

    Args:
        image: ARGB32 image after the background cut.
        island_ratio: Opaque islands smaller than this share of the image are removed.
        hole_ratio: Enclosed transparent holes smaller than this share are filled back.

    Returns:
        ``(image, removed_islands, filled_holes)``.
    """
    width, height = image.width(), image.height()
    buffer = image.bits()
    stride = image.bytesPerLine()
    total = width * height
    min_island = max(32, int(total * island_ratio))
    min_hole = max(24, int(total * hole_ratio))

    removed = 0
    for size, touches, spans in _scan_regions(buffer, stride, width, height, opaque=True):
        if touches or size >= min_island:
            continue
        for left, right, y in spans:
            for xx in range(left, right + 1):
                buffer[y * stride + xx * 4 + 3] = 0
        removed += 1

    filled = 0
    for size, touches, spans in _scan_regions(buffer, stride, width, height, opaque=False):
        if touches or size >= min_hole:
            continue
        for left, right, y in spans:
            for xx in range(left, right + 1):
                buffer[y * stride + xx * 4 + 3] = 255
        filled += 1
    return image, removed, filled


def _crop_to_content(image: QImage, pad_ratio: float = 0.02) -> QImage:
    """Crop away fully transparent margins.

    抠完背景/本来就是透明底时，画布四周往往留着一大圈空白；不裁掉的话人物在桌面上会
    显得又小又飘。裁到人物外框 + 2% 边距。

    Args:
        image: ARGB32 image.
        pad_ratio: Padding kept around the subject, as a fraction of its size.

    Returns:
        The cropped image (or the original when it has no transparent margin).
    """
    width, height = image.width(), image.height()
    buffer = image.bits()
    stride = image.bytesPerLine()
    step = max(1, min(width, height) // 512)
    left, top, right, bottom = width, height, -1, -1
    for y in range(0, height, step):
        for x in range(0, width, step):
            if buffer[y * stride + x * 4 + 3] > 16:
                left, top = min(left, x), min(top, y)
                right, bottom = max(right, x), max(bottom, y)
    if right < 0:
        return image
    pad_x = int((right - left) * pad_ratio) + 2
    pad_y = int((bottom - top) * pad_ratio) + 2
    rect = (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(width, right + pad_x) - max(0, left - pad_x),
        min(height, bottom + pad_y) - max(0, top - pad_y),
    )
    if rect[2] <= 0 or rect[3] <= 0:
        return image
    return image.copy(*rect)


def _load_image(path: Path, frame: str = "auto") -> QImage | None:
    """Load, cut/frame and cache one image.

    Args:
        path: Source image.
        frame: ``auto`` (round full-bleed art), ``rounded`` or ``none``.

    Returns:
        The ready-to-use image, or None when it cannot be read.
    """
    CUT_DIR.mkdir(parents=True, exist_ok=True)
    cached = CUT_DIR / f"{path.stem}.png"
    sidecar = CUT_DIR / f"{path.stem}.json"
    stat = path.stat()
    meta: dict = {}
    if cached.exists() and sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
        # 源图被换过（同样文件名覆盖）就重做，不然会一直用旧缓存
        if meta.get("src_size") != stat.st_size or meta.get("src_mtime") != int(stat.st_mtime):
            meta = {}
    if meta:
        image = QImage(str(cached))
        return None if image.isNull() else image

    image = QImage(str(path))
    if image.isNull():
        return None
    meta = {"cut": False, "framed": False, "src_size": stat.st_size, "src_mtime": int(stat.st_mtime)}
    green_share = _border_green_share(image)
    if green_share >= 0.5:
        # 绿幕导出最省事也最干净：按"绿得有多明显"抠，噪点、明暗不均都不怕
        image = _flood_cut(image, mode="chroma")
        meta["cut"] = "chroma"
    elif _is_flat_background(image):
        image = _flood_cut(image, mode="flat")
        meta["cut"] = "flat"
    elif frame in {"auto", "rounded"}:
        image = _round_card(image)
        meta["framed"] = True
    if meta["cut"] or image.hasAlphaChannel():
        image, removed, filled = _clean_mask(image)
        meta["specks_removed"] = removed
        meta["holes_filled"] = filled
        before = (image.width(), image.height())
        image = _crop_to_content(image)
        meta["cropped_from"] = list(before)
        meta["cropped_to"] = [image.width(), image.height()]
    image.save(str(cached), "PNG")
    sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return image


def load(force: bool = False, frame: str = "auto") -> SpriteInfo:
    """Load every sprite in ``assets/`` (cached after the first call).

    Args:
        force: Re-read the folder even if it was loaded before.
        frame: How to frame full-bleed art (``auto`` / ``rounded`` / ``none``）.

    Returns:
        Info about what is available.
    """
    global _loaded, _meta
    if _loaded and not force:
        return SpriteInfo(**_meta)
    _cache.clear()
    files = _find_files()
    for key, path in files.items():
        image = _load_image(path, frame)
        if image is not None:
            _cache[key] = QPixmap.fromImage(image)
    base = _cache.get("light") or next(iter(_cache.values()), None)
    meta_file = CUT_DIR / f"{Path(files['light']).stem}.json" if files else None
    meta = {}
    if meta_file is not None and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except ValueError:
            meta = {}
    _meta = {
        "name": files.get("light", Path("")).name if files else "",
        "width": base.width() if base else 0,
        "height": base.height() if base else 0,
        "cut": bool(meta.get("cut")),
        "framed": bool(meta.get("framed")),
        "looks": tuple(sorted(key for key in _cache if key != "light")),
    }
    _loaded = True
    return SpriteInfo(**_meta)


def has_sprite() -> bool:
    """Whether any usable art is present.

    Returns:
        True when at least one sprite loaded.
    """
    return bool(_cache) or bool(load().width)


def pixmap_for(look_key: str) -> QPixmap | None:
    """Pick the art for a look.

    Args:
        look_key: Look key such as ``happy``.

    Returns:
        The pixmap to draw, or None when there is no art at all.
    """
    if not _cache and not load().width:
        return None
    return _cache.get(look_key) or _cache.get("light")


def fitted_rect(pixmap: QPixmap, box_w: int, box_h: int, bottom: int) -> tuple[int, int, int, int]:
    """Fit a sprite inside the design box, centred and bottom-aligned.

    ``bottom``（脚底）同时是可用高度上限，否则高瘦的图会被顶部裁掉。

    Args:
        pixmap: Sprite.
        box_w: Design box width.
        box_h: Design box height (unused ceiling, kept for symmetry).
        bottom: y coordinate the sprite's bottom should sit on.

    Returns:
        ``(x, y, width, height)`` — never stretched.
    """
    available_h = max(1, min(box_h, bottom))
    scale = min(box_w / pixmap.width(), available_h / pixmap.height())
    width = max(1, int(pixmap.width() * scale))
    height = max(1, int(pixmap.height() * scale))
    x = (box_w - width) // 2
    y = bottom - height
    return x, y, width, height


def window_mask(
    pixmap: QPixmap,
    box_w: int,
    box_h: int,
    bottom: int,
    *,
    grow: int = 3,
    scale: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
) -> QRegion:
    """Build a click region from the sprite's alpha, in **window** pixels.

    注意 ``scale``/``dx``/``dy``：窗口可以按用户选的倍率放大，遮罩必须跟着一起缩放平移，
    否则放大后遮罩还停在设计坐标那 220x250 的范围里，把立绘右下角切掉。

    Args:
        pixmap: Sprite.
        box_w: Design box width.
        box_h: Design box height.
        bottom: Where the sprite's bottom sits (design coordinates).
        grow: Extra pixels around the silhouette (breathing makes her move).
        scale: Design → window scale factor.
        dx: Horizontal centring offset in window pixels.
        dy: Vertical centring offset in window pixels.

    Returns:
        A QRegion covering the non-transparent pixels.
    """
    x, y, width, height = fitted_rect(pixmap, box_w, box_h, bottom)
    # 直接在**窗口像素**上做遮罩：先把立绘缩到目标尺寸，再取 alpha，
    # 省掉 QRegion 的变换（PySide6 没暴露 QRegion.transformed）。
    target_w = max(1, round(width * scale))
    target_h = max(1, round(height * scale))
    scaled = pixmap.scaled(target_w, target_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    image = scaled.toImage().convertToFormat(QImage.Format_ARGB32)
    mask = QBitmap(image.width(), image.height())
    mask.fill(Qt.color0)
    from PySide6.QtGui import QPainter as _QPainter

    painter = _QPainter(mask)
    painter.drawImage(QPoint(0, 0), image.createAlphaMask())
    painter.end()
    region = QRegion(mask)
    region.translate(round(dx + x * scale), round(dy + y * scale))
    if grow:
        region = region.united(region.translated(0, -grow)).united(region.translated(0, grow))
    return region


def selftest() -> None:
    """Report what art is available and how it would be placed."""
    info = load(force=True)
    print(f"    assets 目录: {ASSETS}")
    if not info.width:
        print("    没有立绘（会用画出来的占位小人）")
        return
    print(
        f"    默认立绘 {info.name}: {info.width}x{info.height}"
        f"（宽高比 {info.aspect:.2f}"
        f"{'，已抠背景' if info.cut else ''}"
        f"{'，已加圆角贴纸边框' if info.framed else ''}）",
    )
    print(f"    按表情单独给的图: {info.looks or '（无，所有表情都用默认立绘）'}")
    from faces import DESIGN_H, DESIGN_W, FEET_Y

    pixmap = pixmap_for("light")
    x, y, width, height = fitted_rect(pixmap, DESIGN_W, DESIGN_H, FEET_Y)
    print(f"    放进设计框 {DESIGN_W}x{DESIGN_H}: x={x} y={y} {width}x{height}（居中、脚底 {FEET_Y}）")
    region = window_mask(pixmap, DESIGN_W, DESIGN_H, FEET_Y)
    print(f"    窗口遮罩: {region.boundingRect().width()}x{region.boundingRect().height()} px 可点区域")


if __name__ == "__main__":
    from PySide6.QtGui import QGuiApplication
    import sys

    QGuiApplication(sys.argv)
    selftest()
