"""表情层：状态 → Look（表情键）→ QPainter 占位小人。

形象先用画出来的占位小人（黑框眼镜、及肩黑发、抱一本大书），以后换立绘/Live2D 时
只要把 :func:`paint_character` 换成画图片，状态机与其它逻辑都不用动：
``Look.key`` 就是"表情资源名"，一一对应。

表情优先级（从高到低）：睡着 > 生气/别过头 > 害羞 > 低落 > 很开心 > 微笑 > 平静看书。
"""

from dataclasses import dataclass
from math import sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPen,
)

from state import PetState

# 配色（也和以后给画师的色卡对得上）
SKIN = QColor(255, 231, 214)
SKIN_SHADOW = QColor(240, 208, 190)
HAIR = QColor(38, 36, 48)
HAIR_LIGHT = QColor(66, 62, 82)
DRESS = QColor(58, 74, 112)
DRESS_LIGHT = QColor(84, 104, 150)
BOOK = QColor(158, 62, 66)
BOOK_PAGE = QColor(246, 240, 226)
GLASS = QColor(26, 32, 46)
BLUSH = QColor(255, 152, 152, 150)
LINE = QColor(52, 48, 58)


@dataclass
class Look:
    """一个表情：画法与名字都从这里来。"""

    key: str
    label: str
    eyes: str = "normal"
    mouth: str = "smile"
    blush: bool = False
    zzz: bool = False
    sweat: bool = False
    sparkle: bool = False
    tilt: float = 0.0


LOOKS: dict[str, Look] = {
    "sleepy": Look("sleepy", "困了", eyes="closed", mouth="small", zzz=True, tilt=6),
    "huffy": Look("huffy", "别过头", eyes="angry", mouth="frown", tilt=-7),
    "shy": Look("shy", "害羞", eyes="happy", mouth="small", blush=True),
    "down": Look("down", "低落", eyes="sad", mouth="flat", tilt=4),
    "happy": Look("happy", "很开心", eyes="happy", mouth="open", sparkle=True),
    "smile": Look("smile", "微笑", eyes="normal", mouth="smile"),
    "calm": Look("calm", "看书", eyes="normal", mouth="flat"),
    "surprised": Look("surprised", "惊讶", eyes="wide", mouth="open"),
}

# 点击她时说的话，按表情分组（以后可以按好感档再细分）
LINES: dict[str, tuple[str, ...]] = {
    "sleepy": ("唔…让我再趴一会儿…", "别戳了，书正看到一半呢…"),
    "huffy": ("哼，不理你了。", "干嘛呀，我正在生气呢。"),
    "shy": ("诶？你、你别突然碰我…", "这页正好看呢…"),
    "down": ("……没人理我。", "抱一会儿书。"),
    "happy": ("今天心情不错哦~", "要不要一起看书？"),
    "smile": ("嗯？怎么啦~", "我在这儿呢。"),
    "calm": ("我正看着书呢。", "有事就说吧~"),
    "surprised": ("诶诶诶？", "吓我一跳…"),
}


def pick(state: PetState) -> Look:
    """把状态翻译成一个表情。

    Args:
        state: Current snapshot.

    Returns:
        The look to draw.
    """
    reason = state.mood_reason or ""
    if state.energy <= 30 or state.is_night:
        return LOOKS["sleepy"]
    if "被怼" in reason:
        return LOOKS["huffy"]
    if "被夸" in reason or "接她的话" in reason:
        return LOOKS["shy"]
    if "没人理" in reason or state.mood <= 39:
        return LOOKS["down"]
    if state.mood >= 80:
        return LOOKS["happy"]
    if state.mood >= 60:
        return LOOKS["smile"]
    return LOOKS["calm"]


def speak_look(look: Look) -> Look:
    """Return the same look with an open mouth (used while she is talking).

    Args:
        look: Base look.

    Returns:
        A look whose mouth animates.
    """
    return Look(
        key=look.key,
        label=look.label,
        eyes=look.eyes,
        mouth="talk",
        blush=look.blush,
        zzz=False,
        sweat=look.sweat,
        sparkle=look.sparkle,
        tilt=look.tilt,
    )


# 绘制用的设计坐标系：220 x 250，脚底在 y=246，水平中心 x=110。
# 调用方只需要把画笔缩放到自己的窗口大小，形象本身永远按这套坐标画——
# 以后交给画师出图时，也按这个比例给你（宽高比 22:25）。
DESIGN_W = 220
DESIGN_H = 250
FEET_Y = 242
MID_X = 110
HEAD_TOP = 52
EYE_Y = 104
EYE_DX = 20
MOUTH_Y = 126
BLUSH_Y = 118


def _eye(painter: QPainter, x: float, y: float, look: Look, blink: bool) -> None:
    """Draw one eye.

    Args:
        painter: Active painter.
        x: Eye centre x.
        y: Eye centre y.
        look: Current look.
        blink: Whether the eyes are mid-blink.
    """
    pen = QPen(LINE, 3.4, Qt.SolidLine, Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if blink or look.eyes == "closed":
        painter.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
        return
    if look.eyes == "happy":
        painter.drawArc(QRectF(x - 11, y - 2, 22, 16), 20 * 16, 140 * 16)
        return
    if look.eyes == "angry":
        painter.drawLine(QPointF(x - 10, y - 8), QPointF(x + 9, y + 2))
        painter.drawLine(QPointF(x - 9, y + 2), QPointF(x + 10, y - 8))
        return
    if look.eyes == "sad":
        painter.drawArc(QRectF(x - 10, y - 4, 20, 14), 200 * 16, 140 * 16)
        return
    width = 15.0 if look.eyes == "wide" else 12.0
    height = 17.0 if look.eyes == "wide" else 14.0
    painter.setBrush(QBrush(QColor(58, 54, 74)))
    painter.drawEllipse(QRectF(x - width / 2, y - height / 2, width, height))
    painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
    painter.drawEllipse(QRectF(x - 1, y - height / 2 + 2, 4.5, 4.5))


def _mouth(painter: QPainter, x: float, y: float, look: Look, phase: float) -> None:
    """Draw the mouth.

    Args:
        painter: Active painter.
        x: Mouth centre x.
        y: Mouth centre y.
        look: Current look.
        phase: Animation phase in radians (for talking).
    """
    painter.setPen(QPen(LINE, 2.6, Qt.SolidLine, Qt.RoundCap))
    painter.setBrush(Qt.NoBrush)
    style = look.mouth
    if style == "talk":
        open_amount = 3.0 + 3.5 * abs(sin(phase))
        painter.setBrush(QBrush(QColor(150, 76, 84)))
        painter.drawEllipse(QRectF(x - 6, y - 2, 12, open_amount + 3))
        return
    if style == "open":
        painter.setBrush(QBrush(QColor(150, 76, 84)))
        painter.drawEllipse(QRectF(x - 6, y - 2, 12, 9))
        return
    if style == "smile":
        painter.drawArc(QRectF(x - 9, y - 6, 18, 12), 200 * 16, 140 * 16)
        return
    if style == "frown":
        painter.drawArc(QRectF(x - 9, y - 2, 18, 12), 20 * 16, 140 * 16)
        return
    if style == "small":
        painter.drawEllipse(QRectF(x - 3, y - 1, 6, 4))
        return
    painter.drawLine(QPointF(x - 7, y), QPointF(x + 7, y))


def _paint_effects(painter: QPainter, look: Look, t: float) -> None:
    """画表情特效（脸红、zzz、星星、汗），画在立绘/占位小人之上。

    Args:
        painter: Active painter in design coordinates.
        look: Current look.
        t: Seconds since start.
    """
    if look.blush:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(BLUSH))
        painter.drawEllipse(QRectF(MID_X - 42, BLUSH_Y - 5, 20, 10))
        painter.drawEllipse(QRectF(MID_X + 22, BLUSH_Y - 5, 20, 10))
    if look.zzz:
        painter.setPen(QPen(QColor(120, 130, 170), 2.6))
        painter.setFont(QFont("Segoe UI", 11))
        for index in range(3):
            painter.drawText(
                QPointF(160 + index * 8, 84 - index * 14 - (t * 5 % 12)),
                "z",
            )
    if look.sparkle:
        painter.setPen(QPen(QColor(255, 206, 92), 2.4))
        for dx, dy in ((52, 62), (168, 92)):
            painter.drawLine(QPointF(dx - 6, dy), QPointF(dx + 6, dy))
            painter.drawLine(QPointF(dx, dy - 6), QPointF(dx, dy + 6))
    if look.sweat:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(140, 196, 240, 220)))
        painter.drawEllipse(QRectF(150, 74, 10, 14))


def paint_character(painter: QPainter, look: Look, *, t: float) -> None:
    """Draw 轻语 into a 220x250 design box.

    有立绘就画立绘（等比缩放、水平居中、脚底对齐），没有就画占位小人；两种情况都会在
    上面补画特效（脸红/zzz/星星/汗），所以**只给一张图也能看出情绪**。

    调用方负责缩放（``painter.scale(w / DESIGN_W, h / DESIGN_H)``）。

    Args:
        painter: Painter positioned at the design box top-left.
        look: Look to draw.
        t: Seconds since start, drives breathing/blinking/talking.
    """
    import sprite

    breathe = sin(t * 1.6) * 2.0
    sway = sin(t * 2.2) * 1.3 if look.mouth == "talk" else 0.0
    painter.save()
    pixmap = sprite.pixmap_for(look.key)
    if pixmap is not None:
        # 立绘：只做呼吸/摇摆/轻微倾斜，五官是画师画的，不动
        painter.translate(MID_X, FEET_Y)
        painter.rotate(look.tilt * 0.6 + sway * 0.5)
        painter.translate(-MID_X, -FEET_Y + breathe)
        x, y, width, height = sprite.fitted_rect(pixmap, DESIGN_W, DESIGN_H, FEET_Y)
        painter.drawPixmap(
            QRectF(x, y, width, height),
            pixmap,
            QRectF(pixmap.rect()),
        )
        painter.translate(MID_X, FEET_Y - breathe)
        painter.rotate(-(look.tilt * 0.6 + sway * 0.5))
        painter.translate(-MID_X, -FEET_Y)
        _paint_effects(painter, look, t)
        painter.restore()
        return
    # 没有立绘：画占位小人（呼吸 + 摇摆 + 倾斜都作用在整个人身上）
    painter.translate(MID_X, FEET_Y + breathe)
    painter.rotate(look.tilt + sway)
    painter.translate(-MID_X, -FEET_Y)

    # 腿与鞋
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(SKIN_SHADOW))
    painter.drawRoundedRect(QRectF(88, 196, 15, 42), 7, 7)
    painter.drawRoundedRect(QRectF(117, 196, 15, 42), 7, 7)
    painter.setBrush(QBrush(QColor(58, 56, 70)))
    painter.drawRoundedRect(QRectF(84, 232, 22, 12), 5, 5)
    painter.drawRoundedRect(QRectF(114, 232, 22, 12), 5, 5)

    # 裙子（比书宽，两侧和下摆都要露出来）
    dress = QLinearGradient(0, 150, 0, 216)
    dress.setColorAt(0, DRESS_LIGHT)
    dress.setColorAt(1, DRESS)
    painter.setBrush(QBrush(dress))
    painter.drawRoundedRect(QRectF(62, 150, 96, 70), 16, 16)
    painter.setBrush(QBrush(DRESS))
    painter.drawRoundedRect(QRectF(70, 206, 80, 12), 6, 6)

    # 抱着的书（胸前一横本，后画，压住裙子中间）
    painter.save()
    painter.translate(MID_X, 172)
    painter.rotate(-3)
    painter.translate(-MID_X, -172)
    painter.setBrush(QBrush(BOOK))
    painter.drawRoundedRect(QRectF(78, 146, 64, 48), 5, 5)
    painter.setBrush(QBrush(BOOK_PAGE))
    painter.drawRoundedRect(QRectF(83, 151, 54, 38), 3, 3)
    painter.setPen(QPen(QColor(206, 196, 178), 1.4))
    for index in range(4):
        y = 158 + index * 8
        painter.drawLine(QPointF(89, y), QPointF(131, y))
    painter.setPen(Qt.NoPen)
    painter.restore()

    # 手臂搭在书上
    painter.setBrush(QBrush(SKIN))
    painter.drawRoundedRect(QRectF(50, 152, 20, 34), 9, 9)
    painter.drawRoundedRect(QRectF(150, 152, 20, 34), 9, 9)

    # 脖子
    painter.setBrush(QBrush(SKIN_SHADOW))
    painter.drawRoundedRect(QRectF(103, 132, 14, 20), 6, 6)

    # 后发 + 两侧及肩长发
    painter.setBrush(QBrush(HAIR))
    painter.drawEllipse(QRectF(60, 46, 100, 96))
    painter.drawRoundedRect(QRectF(56, 96, 18, 100), 9, 9)
    painter.drawRoundedRect(QRectF(146, 96, 18, 100), 9, 9)

    # 脸
    painter.setBrush(QBrush(SKIN))
    painter.drawEllipse(QRectF(68, HEAD_TOP, 84, 88))

    # 刘海（盖住额头）
    painter.setBrush(QBrush(HAIR_LIGHT))
    painter.drawChord(QRectF(66, HEAD_TOP - 10, 88, 78), 0, 180 * 16)
    painter.setBrush(QBrush(HAIR))
    painter.drawChord(QRectF(64, HEAD_TOP - 12, 92, 70), 0, 180 * 16)

    # 眼镜（黑框）
    painter.setPen(QPen(GLASS, 3.0))
    painter.setBrush(QBrush(QColor(255, 255, 255, 40)))
    painter.drawRoundedRect(QRectF(MID_X - EYE_DX - 13, EYE_Y - 11, 26, 22), 6, 6)
    painter.drawRoundedRect(QRectF(MID_X + EYE_DX - 13, EYE_Y - 11, 26, 22), 6, 6)
    painter.drawLine(QPointF(MID_X - EYE_DX + 13, EYE_Y), QPointF(MID_X + EYE_DX - 13, EYE_Y))
    painter.drawLine(QPointF(MID_X - EYE_DX - 13, EYE_Y - 1), QPointF(66, EYE_Y + 2))
    painter.drawLine(QPointF(MID_X + EYE_DX + 13, EYE_Y - 1), QPointF(154, EYE_Y + 2))

    # 脸红了 = _paint_effects 统一画（立绘和占位小人共用）

    # 眼睛、嘴
    blink = (t % 4.6) < 0.12
    _eye(painter, MID_X - EYE_DX, EYE_Y, look, blink)
    _eye(painter, MID_X + EYE_DX, EYE_Y, look, blink)
    _mouth(painter, MID_X, MOUTH_Y, look, t * 9.0)
    _paint_effects(painter, look, t)
    painter.restore()


def quiet_hint(state: PetState) -> str:
    """A short line about what she is doing right now (phase A bubbles).

    Args:
        state: Current snapshot.

    Returns:
        Text for the bubble, or an empty string.
    """
    if not state.online:
        return "她好像不在…（AstrBot 或 QQ 没开）"
    if state.error:
        return state.error
    if state.energy <= 30:
        return "有点困了…"
    if state.mood_reason:
        return f"{state.mood_reason}（{state.mood_at}）"
    return ""


def selftest() -> None:
    """Print the state→look mapping for a few synthetic states."""
    cases = [
        ("精力足+开心", PetState(energy=80, mood=85, mood_reason="被夸")),
        ("普通", PetState(energy=70, mood=65)),
        ("被怼", PetState(energy=70, mood=70, mood_reason="被怼")),
        ("被无视", PetState(energy=60, mood=60, mood_reason="说了话没人理")),
        ("心情低", PetState(energy=60, mood=35)),
        ("精力低", PetState(energy=20, mood=70)),
        ("深夜", PetState(energy=70, mood=70, is_night=True)),
    ]
    for label, state in cases:
        look = pick(state)
        print(f"    {label:10} → {look.key:10} {look.label}（眼睛={look.eyes} 嘴={look.mouth}"
              f"{' 脸红' if look.blush else ''}{' zzz' if look.zzz else ''}）")


if __name__ == "__main__":
    selftest()

