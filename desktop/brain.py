"""独立版的大脑：桌宠自己直连大模型，不依赖 AstrBot / NapCat。

一份配置（``config.json`` 的 ``llm`` / ``vision``）就能跑：

- **对话**：OpenAI 兼容的 ``/chat/completions``，流式返回，逐字冒泡；
- **记忆**：她可以在回复里写 ``[[记住: 内容]]``，这条会被存进 ``pet_data.db`` 并在以后
  相关时注入提示词（按相关度 × 新鲜度 × 未用次数挑，和主项目里的记忆层同一个思路）；
- **状态**：心情/精力/好感按事件走（被夸、被怼、被冷落、深夜…），桌宠拿它决定表情；
- **看屏幕**：模型支持视觉就直接发图；不支持的（比如 deepseek-chat）可以再配一个
  "描述模型"（如 glm-4v-flash / qwen-vl），先把图转成文字再喂给主模型。

信号接口与 ``petlink.PetLinkClient`` 一模一样（``ready/chunk/done/failed`` + ``send``），
所以 ``pet.py`` 不用为两种模式写两套逻辑。
"""

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, Signal

import paths
from persona import PERSONA

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
HISTORY_TURNS = 12
MEMORY_INJECT = 3
REQUEST_TIMEOUT = 90.0
# 记住东西的标记：她自己在回复里写，客户端解析后从显示文本里去掉
REMEMBER_RE = re.compile(r"\[\[\s*(?:记住|remember)\s*[:：]\s*(.+?)\s*\]\]", re.S)
STOPWORDS = set("的了是我你他她它们在有和与及就都而也不很这那请问一下怎么什么吗呢吧啊呀哦嘛")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    role    TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    text        TEXT NOT NULL UNIQUE,
    weight      REAL NOT NULL DEFAULT 1.0,
    last_used   INTEGER NOT NULL DEFAULT 0,
    use_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    """Open the pet's own database.

    ``check_same_thread=False``：连接在界面线程里建、在回答线程里用（每次访问都持有
    同一把锁，是安全的）；不加这个参数 sqlite 会直接抛
    "SQLite objects created in a thread can only be used in that same thread"。

    Returns:
        An open connection (schema created on first use).
    """
    connection = sqlite3.connect(paths.DATA_DB, timeout=10.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


# --------------------------------------------------------------------- 小工具


def _terms(query: str) -> list[str]:
    """Split a query into 2~3 character Chinese n-grams plus ASCII words.

    Args:
        query: Raw text.

    Returns:
        Search terms.
    """
    terms: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9_.+-]+|[^A-Za-z0-9_.+-]+", query or ""):
        if re.fullmatch(r"[A-Za-z0-9_.+-]{2,}", chunk):
            terms.append(chunk.lower())
            continue
        chars = "".join(ch for ch in chunk if ch not in STOPWORDS)
        for size in (3, 2):
            for start in range(0, max(0, len(chars) - size + 1)):
                term = chars[start : start + size]
                if term and term not in terms:
                    terms.append(term)
    return terms[:20]


class Store:
    """Everything the standalone pet remembers about you."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.db = _connect()

    def add_message(self, role: str, text: str) -> None:
        """Append one line of conversation.

        Args:
            role: ``user`` or ``assistant``.
            text: Message text.
        """
        with self._lock:
            self.db.execute(
                "INSERT INTO messages (ts, role, text) VALUES (?, ?, ?)",
                (int(time.time()), role, text),
            )
            self.db.execute(
                "DELETE FROM messages WHERE id <= (SELECT MAX(id) - 400 FROM messages)",
            )
            self.db.commit()

    def history(self, turns: int = HISTORY_TURNS) -> list[dict]:
        """Recent conversation, oldest first.

        Args:
            turns: How many exchanges to return.

        Returns:
            Messages in OpenAI chat format.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT role, text FROM messages ORDER BY id DESC LIMIT ?",
                (turns * 2,),
            ).fetchall()
        return [{"role": str(row["role"]), "content": str(row["text"])} for row in reversed(rows)]

    def remember(self, text: str) -> bool:
        """Store one long-term note.

        Args:
            text: Note text.

        Returns:
            False when it was a duplicate or too short.
        """
        cleaned = " ".join(str(text).split())[:120]
        if len(cleaned) < 4:
            return False
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO memories (ts, text) VALUES (?, ?)",
                    (int(time.time()), cleaned),
                )
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def recall(self, query: str, limit: int = MEMORY_INJECT) -> list[str]:
        """Pick memories worth mentioning for this message.

        Args:
            query: The user's message.
            limit: Maximum notes.

        Returns:
            Memory texts (may be empty).
        """
        terms = _terms(query)
        with self._lock:
            rows = self.db.execute(
                "SELECT id, text, ts, weight, last_used, use_count FROM memories"
                " WHERE weight > 0.15 ORDER BY weight DESC, ts DESC LIMIT 80",
            ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        now = time.time()
        for row in rows:
            text = str(row["text"]).lower()
            hits = [term for term in terms if term in text]
            relevance = 0.0 if not hits else min(1.0, 0.5 + 0.2 * (len(hits) - 1))
            age_days = max(0.0, (now - int(row["ts"])) / 86400.0)
            score = 0.15 + 0.5 * relevance + 0.2 * min(1.0, age_days / 14.0)
            if int(row["last_used"]) and now - int(row["last_used"]) < 6 * 3600:
                score -= 0.3
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        # 一条命中（问"四级报名"命中"下个月要考四级"）时分数约 0.40，门槛得放到 0.34 左右；
        # 完全没命中的只有 0.15，会被挡掉。
        picked = [row for score, row in scored[:limit] if score > 0.34]
        out = []
        for row in picked:
            out.append(str(row["text"]))
            with self._lock:
                self.db.execute(
                    "UPDATE memories SET last_used = ?, use_count = use_count + 1,"
                    " weight = MAX(0.15, weight - 0.1) WHERE id = ?",
                    (int(now), int(row["id"])),
                )
                self.db.commit()
        return out

    def memory_count(self) -> int:
        """How many long-term notes are stored.

        Returns:
            Row count.
        """
        with self._lock:
            return int(self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def get(self, key: str, default: str = "") -> str:
        """Read one state value.

        Args:
            key: State key.
            default: Fallback.

        Returns:
            The stored value.
        """
        with self._lock:
            row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        """Write one state value.

        Args:
            key: State key.
            value: New value.
        """
        with self._lock:
            self.db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self.db.commit()


# --------------------------------------------------------------------- 心情


class Mood:
    """Mood / energy / affection, driven by events (ported from the main project)."""

    PRAISE = ("谢谢", "厉害", "好棒", "可爱", "喜欢", "靠谱", "聪明", "牛")
    ABUSE = ("滚", "傻", "蠢", "笨", "闭嘴", "垃圾", "废物", "烦人", "弱智")

    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def energy(self) -> int:
        """Current energy (0~100)."""
        return int(self.store.get("energy", "70"))

    @property
    def mood(self) -> int:
        """Current mood (0~100)."""
        return int(self.store.get("mood", "65"))

    @property
    def affection(self) -> int:
        """Affection toward the user (0~100)."""
        return int(self.store.get("affection", "60"))

    @property
    def last_event(self) -> str:
        """Description of the most recent mood change."""
        return self.store.get("mood_last", "")

    def _bump(self, *, energy: int = 0, mood: int = 0, affection: int = 0, reason: str) -> None:
        """Apply a change and record why.

        Args:
            energy: Energy delta.
            mood: Mood delta.
            affection: Affection delta.
            reason: Human-readable cause.
        """
        new_energy = max(0, min(100, self.energy + energy))
        new_mood = max(0, min(100, self.mood + mood))
        new_affection = max(0, min(100, self.affection + affection))
        self.store.set("energy", str(new_energy))
        self.store.set("mood", str(new_mood))
        self.store.set("affection", str(new_affection))
        stamp = datetime.now().strftime("%m-%d %H:%M")
        effect = " ".join(
            part
            for part in (
                f"精力 {energy:+d}" if energy else "",
                f"心情 {mood:+d}" if mood else "",
                f"好感 {affection:+d}" if affection else "",
            )
            if part
        )
        self.store.set("mood_last", f"{reason}｜{effect}｜{stamp}")

    def react(self, text: str, *, answered: bool = True) -> str:
        """Update state from the user's message.

        Args:
            text: What the user said.
            answered: Whether she is going to answer (she always does on the desktop).

        Returns:
            The event description (empty when nothing happened).
        """
        lowered = (text or "").lower()
        # 凌晨还在聊：她困，但被找来说话会开心一点
        hour = datetime.now().hour
        if any(word in lowered for word in self.PRAISE):
            self._bump(mood=2, energy=2, affection=1, reason="被夸")
            return "被夸"
        if any(word in lowered for word in self.ABUSE):
            self._bump(mood=-3, energy=-1, affection=-1, reason="被怼")
            return "被怼"
        if hour >= 23 or hour < 6:
            self._bump(energy=-1, mood=1, reason="深夜陪聊")
            return "深夜陪聊"
        self._bump(energy=-1, mood=1, affection=1, reason="有人找她说话")
        return "有人找她说话"

    def decay(self) -> None:
        """Let mood drift back toward neutral and energy recover while idle."""
        last = int(self.store.get("decay_at", "0") or 0)
        now = int(time.time())
        if now - last < 600:
            return
        self.store.set("decay_at", str(now))
        energy = self.energy
        mood = self.mood
        if mood < 65:
            mood = min(65, mood + 1)
        elif mood > 65:
            mood = max(65, mood - 1)
        if energy < 70:
            energy = min(70, energy + 2)
        self.store.set("energy", str(energy))
        self.store.set("mood", str(mood))


# --------------------------------------------------------------------- 客户端


class LocalBrainClient(QObject):
    """Standalone chat client: talks to the user's own LLM API."""

    chunk = Signal(str)
    done = Signal(str)
    failed = Signal(str)
    ready = Signal(str)

    def __init__(self, config: dict) -> None:
        super().__init__()
        self.config = config
        self.store = Store()
        self.mood = Mood(self.store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Lock()
        self._born = time.time()

    # ---- 与 PetLinkClient 相同的对外接口

    def start(self) -> None:
        """Announce readiness (nothing to connect: it is all local)."""
        model = self._llm().get("model") or DEFAULT_MODEL
        self.ready.emit(f"独立模式（{model}）")

    def stop(self) -> None:
        """Nothing to tear down."""
        self._stop.set()

    def send(self, text: str, image: str | None = None) -> None:
        """Ask her something, in a worker thread.

        Args:
            text: User message.
            image: Optional screenshot path.
        """
        text = (text or "").strip()
        if not text and not image:
            return
        if self._thread and self._thread.is_alive():
            self.failed.emit("上一条还在想呢，稍等一下下~")
            return
        self._thread = threading.Thread(
            target=self._answer,
            args=(text, image),
            name="pet-brain",
            daemon=True,
        )
        self._thread.start()

    # ---- 内部

    def _llm(self) -> dict:
        """LLM settings from the config.

        Returns:
            ``{base_url, model, api_key, ...}``.
        """
        section = self.config.get("llm") or {}
        return {
            "base_url": str(section.get("base_url") or DEFAULT_BASE).rstrip("/"),
            "model": str(section.get("model") or DEFAULT_MODEL),
            "api_key": str(section.get("api_key") or ""),
            "temperature": float(section.get("temperature") or 0.8),
            "max_tokens": int(section.get("max_tokens") or 600),
        }

    def _vision(self) -> dict:
        """Vision settings (a separate caption model when the main one is text-only).

        Returns:
            ``{mode, base_url, model, api_key}``.
        """
        section = self.config.get("vision") or {}
        return {
            "mode": str(section.get("mode") or "caption"),
            "base_url": str(section.get("base_url") or "").rstrip("/"),
            "model": str(section.get("model") or ""),
            "api_key": str(section.get("api_key") or ""),
        }

    def _system_prompt(self, memories: list[str]) -> str:
        """Build the system prompt (persona + state + memories).

        Args:
            memories: Recalled long-term notes.

        Returns:
            The prompt text.
        """
        now = datetime.now()
        lines = [
            PERSONA,
            "",
            "## 此刻",
            f"- 现在时间：{now:%Y年%m月%d日 %H:%M}（{'深夜' if now.hour >= 23 or now.hour < 6 else '白天'}）",
            f"- 你的状态：精力 {self.mood.energy}/100，心情 {self.mood.mood}/100，"
            f"对面前这个人的好感 {self.mood.affection}/100",
            "- 你住在这个人的电脑桌面上（一个总在最前的小窗口），"
            "他/她点你一下你会说句话，双击能跟你聊天，也能让你看看屏幕。",
            "",
            "## 记忆",
            "- 你可以在回复里写 [[记住: 内容]] 来记下一件以后还用得上的事（对方看不到这行标记）。",
            "- 只记长期有用的（身份、长期喜好、约定、重要经历），一次最多记一条。",
        ]
        if memories:
            lines.append("- 你记得这些旧事（可能过时，别当成事实背出来，合适时自然提一句）：")
            lines += [f"  · {item}" for item in memories]
        lines += [
            "",
            "## 桌宠场合的额外要求",
            "- 回答尽量短：闲聊一到两句，正经问题三到五句，除非对方明确要求详细。",
            "- 看不见屏幕就没看到就是没看到，不要编。",
        ]
        return "\n".join(lines)

    def _caption(self, image: str) -> str:
        """Turn a screenshot into text with the configured vision model.

        Args:
            image: Image path.

        Returns:
            Description text (empty when it cannot be done).
        """
        import base64

        vision = self._vision()
        if not (vision["base_url"] and vision["model"]):
            return ""
        try:
            data = base64.b64encode(Path(image).read_bytes()).decode()
        except OSError:
            return ""
        payload = {
            "model": vision["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "简要描述这张电脑屏幕截图：打开了哪些窗口/程序、"
                            "有什么文字或报错、正在做什么。三到五句话。",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{data}"},
                        },
                    ],
                },
            ],
            "max_tokens": 400,
        }
        key = vision["api_key"] or self._llm()["api_key"]
        try:
            response = httpx.post(
                f"{vision['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"]).strip()
        except Exception as exc:  # noqa: BLE001 - 描述失败就当没看到
            self.failed.emit(f"看屏幕失败（{type(exc).__name__}）")
            return ""

    def _answer(self, text: str, image: str | None) -> None:
        """One full turn: state → prompt → stream → memory.

        Args:
            text: User message.
            image: Optional screenshot.
        """
        try:
            self._answer_inner(text, image)
        except Exception as exc:  # noqa: BLE001 - 工作线程里出事必须报给界面，不能静默死掉
            import traceback

            self.failed.emit(f"出错了：{type(exc).__name__}: {exc}")
            logger_text = traceback.format_exc()
            try:
                (paths.BASE / "pet_log.txt").open("a", encoding="utf-8").write(
                    logger_text + "\n",
                )
            except OSError:
                pass

    def _answer_inner(self, text: str, image: str | None) -> None:
        """The actual turn body (kept separate so failures can be caught).

        Args:
            text: User message.
            image: Optional screenshot.
        """
        with self._busy:
            settings = self._llm()
            if not settings["api_key"]:
                self.failed.emit("还没填 API Key——右键桌宠 →「设置 API」，填好就能聊了。")
                return
            self.mood.decay()
            event = self.mood.react(text)
            question = text
            if image:
                caption = self._caption(image)
                note = f"[对方发来一张屏幕截图]{caption}" if caption else "[对方发来一张截图，但你没看清]"
                question = f"{text}\n{note}" if text else note
            memories = self.store.recall(text or question)
            messages = [{"role": "system", "content": self._system_prompt(memories)}]
            messages += self.store.history()
            messages.append({"role": "user", "content": question})
            self.store.add_message("user", text or "（发了张截图）")

            chunks: list[str] = []
            try:
                with httpx.stream(
                    "POST",
                    f"{settings['base_url']}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings['api_key']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings["model"],
                        "messages": messages,
                        "temperature": settings["temperature"],
                        "max_tokens": settings["max_tokens"],
                        "stream": True,
                    },
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if self._stop.is_set():
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            delta = json.loads(body)["choices"][0].get("delta") or {}
                        except (ValueError, KeyError, IndexError):
                            continue
                        piece = delta.get("content")
                        if piece:
                            chunks.append(str(piece))
                            self.chunk.emit(str(piece))
            except Exception as exc:  # noqa: BLE001 - 网络/额度问题都报给用户
                self.failed.emit(f"调用模型失败：{type(exc).__name__}: {exc}")
                return

            raw = "".join(chunks).strip()
            if not raw:
                self.failed.emit("模型没返回内容（检查模型名 / 余额 / 网络）")
                return
            clean, saved = self._extract_memories(raw)
            self.store.add_message("assistant", clean)
            if saved:
                self.store.set("last_saved_memory", saved)
            self.done.emit(clean)

    def _extract_memories(self, reply: str) -> tuple[str, str]:
        """Pull ``[[记住: …]]`` markers out of her reply.

        Args:
            reply: Raw reply text.

        Returns:
            ``(clean_text, saved_note)``.
        """
        saved = ""
        for match in REMEMBER_RE.finditer(reply):
            if self.store.remember(match.group(1)):
                saved = match.group(1).strip()
                break
        clean = REMEMBER_RE.sub("", reply).strip()
        return clean or reply.strip(), saved

    def stats(self) -> str:
        """Short status line for the menu bubble.

        Returns:
            Chinese summary.
        """
        return (
            f"独立模式｜精力 {self.mood.energy} 心情 {self.mood.mood} 好感 {self.mood.affection}\n"
            f"记忆 {self.store.memory_count()} 条｜最近：{self.mood.last_event or '还没有'}"
        )


def test_connection(config: dict) -> tuple[bool, str]:
    """Ping the configured model with a one-token request.

    Args:
        config: Full app config.

    Returns:
        ``(ok, message)``.
    """
    settings = {
        "base_url": str((config.get("llm") or {}).get("base_url") or DEFAULT_BASE).rstrip("/"),
        "model": str((config.get("llm") or {}).get("model") or DEFAULT_MODEL),
        "api_key": str((config.get("llm") or {}).get("api_key") or ""),
    }
    if not settings["api_key"]:
        return False, "还没填 API Key"
    try:
        response = httpx.post(
            f"{settings['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {settings['api_key']}"},
            json={
                "model": settings["model"],
                "messages": [{"role": "user", "content": "只回两个字：收到"}],
                "max_tokens": 16,
                "stream": False,
            },
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"连不上：{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        detail = response.text[:200].replace("\n", " ")
        return False, f"HTTP {response.status_code}：{detail}"
    try:
        answer = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError):
        return False, "返回结构看不懂（可能地址不是 OpenAI 兼容接口）"
    return True, f"连通成功，模型回：{str(answer).strip()[:40]}"
