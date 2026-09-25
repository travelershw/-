"""首次运行 / 「设置」窗口：选运行方式、填自己的 API Key、或连别人的 AstrBot。

三种运行方式：

1. **独立模式**（默认，推荐给大多数人）：她自带大脑，填你自己的 API Key（DeepSeek/智谱/…）；
2. **连我自己的 AstrBot**：你本机跑着 AstrBot + 桌宠插件，地址默认 `ws://127.0.0.1:6198`，
   本机用可以不填密钥；
3. **连别人的 AstrBot**：对方把地址和**密钥**给你（跨机器必须填密钥，否则连不上）。
"""

import threading

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

import brain
import petlink

MODE_LOCAL = "独立模式（用我自己的 API Key）"
MODE_OWN = "连我自己的 AstrBot（本机）"
MODE_REMOTE = "连别人的 AstrBot（需要对方给密钥）"

# 预设：名字 -> (base_url, 对话模型, 描述模型的地址/名字)
PRESETS: dict[str, tuple[str, str, str, str]] = {
    "DeepSeek（推荐，便宜）": ("https://api.deepseek.com/v1", "deepseek-chat", "", ""),
    "智谱 GLM（有免费视觉模型）": (
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-flash",
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-4v-flash",
    ),
    "阿里通义千问（DashScope 兼容）": (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-vl-max",
    ),
    "OpenAI": (
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
    ),
    "月之暗面 Kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "", ""),
    "本地 Ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:7b", "", ""),
    "自定义（自己填地址）": ("", "", "", ""),
}


class SetupDialog(QDialog):
    """配置运行方式 / API Key / AstrBot 地址。"""

    # Emitted from the worker thread; the slot runs on the GUI thread.
    _test_result = Signal(bool, str)

    def __init__(self, config: dict, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("轻语桌宠 · 设置")
        self.setMinimumWidth(560)
        self.setFont(QFont("Microsoft YaHei UI", 10))

        llm = dict(config.get("llm") or {})
        vision = dict(config.get("vision") or {})

        self.mode = QComboBox(self)
        self.mode.addItems([MODE_LOCAL, MODE_OWN, MODE_REMOTE])
        # 注意：**不要**在这里就 connect `_sync`。下面 `setCurrentText` 会立刻触发它，
        # 而那时 `self.llm_box` 还没建出来——实测在"连 AstrBot"模式打开设置直接崩：
        #   AttributeError: 'SetupDialog' object has no attribute 'llm_box'
        # 信号的连接放到所有控件建好之后（见本函数末尾），初始状态由那次显式 `_sync` 负责。
        if str(config.get("mode")) == "astrbot":
            url = str(config.get("desktop_url") or "")
            self.mode.setCurrentText(
                MODE_OWN if url.startswith("ws://127.0.0.1") or not url else MODE_REMOTE
            )

        # ---- 独立模式：自己的 Key
        self.preset = QComboBox(self)
        self.preset.addItems(list(PRESETS))
        self.preset.currentTextChanged.connect(self._apply_preset)
        self.base_url = QLineEdit(llm.get("base_url", ""), self)
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.model = QLineEdit(llm.get("model", ""), self)
        self.model.setPlaceholderText("deepseek-chat")
        self.api_key = QLineEdit(llm.get("api_key", ""), self)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText(
            "粘贴你自己的 API Key（只存在本机 config.json）"
        )
        self.vision_model = QLineEdit(vision.get("model", ""), self)
        self.vision_model.setPlaceholderText(
            "留空 = 不看屏幕；如 glm-4v-flash / qwen-vl-max"
        )

        self.llm_box = QGroupBox("① 她的大脑：你自己的 API Key", self)
        llm_form = QFormLayout(self.llm_box)
        llm_form.addRow("服务商", self.preset)
        llm_form.addRow("接口地址", self.base_url)
        llm_form.addRow("对话模型", self.model)
        llm_form.addRow("API Key", self.api_key)
        llm_form.addRow("看图模型", self.vision_model)

        # ---- 连 AstrBot
        self.desktop_url = QLineEdit(
            str(config.get("desktop_url") or petlink.URL), self
        )
        self.desktop_url.setPlaceholderText(
            "ws://127.0.0.1:6198　或对方给的 ws://域名:端口"
        )
        self.desktop_secret = QLineEdit(str(config.get("desktop_secret") or ""), self)
        self.desktop_secret.setEchoMode(QLineEdit.Password)
        self.desktop_secret.setPlaceholderText("对方给你的密钥（自己本机用可以留空）")

        self.link_box = QGroupBox(
            "② 她的身体：连到 AstrBot（选「独立模式」时忽略这里）", self
        )
        link_form = QFormLayout(self.link_box)
        link_form.addRow("桌宠通道", self.desktop_url)
        link_form.addRow("共享密钥", self.desktop_secret)

        hint = QLabel(
            "独立模式：聊天由你自己的 Key 付费，数据全在本机；\n"
            "连 AstrBot：她会用那台 AstrBot 的记忆与好感（本机地址留空密钥即可；"
            "连别人的必须填对方给的密钥，否则会被拒绝）。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666")

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        self.test_button = QPushButton("测试连接", self)
        self.test_button.clicked.connect(self._test)
        save = QPushButton("保存并关闭", self)
        save.setDefault(True)
        save.clicked.connect(self._save)

        buttons = QHBoxLayout()
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("运行方式", self))
        layout.addWidget(self.mode)
        layout.addWidget(self.llm_box)
        layout.addWidget(self.link_box)
        layout.addWidget(hint)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self._guess_preset(llm.get("base_url", ""))
        self._sync(self.mode.currentText())
        # 控件齐了再连：这样切换"运行方式"能实时联动，而构造期间不会提前触发
        self.mode.currentTextChanged.connect(self._sync)
        self._test_result.connect(self._on_test_result)

    # ---------------------------------------------------------------- 交互

    def _guess_preset(self, base_url: str) -> None:
        """Select the preset matching the saved base_url.

        Args:
            base_url: Configured base url.
        """
        for name, (url, _model, _vurl, _vmodel) in PRESETS.items():
            if url and base_url and url == base_url:
                self.preset.setCurrentText(name)
                return

    def _apply_preset(self, name: str) -> None:
        """Fill the API fields from a preset.

        Args:
            name: Preset label.
        """
        url, model, _vision_url, vision_model = PRESETS.get(name, ("", "", "", ""))
        if url:
            self.base_url.setText(url)
            self.model.setText(model)
            self.vision_model.setText(vision_model)
        self._sync(self.mode.currentText())

    def _sync(self, _name: str = "") -> None:
        """Show/enable only what the selected mode needs.

        Args:
            _name: Combo text (unused).
        """
        mode = self.mode.currentText()
        self.llm_box.setEnabled(mode == MODE_LOCAL)
        self.link_box.setEnabled(mode != MODE_LOCAL)
        if mode == MODE_OWN and not self.desktop_url.text().strip():
            self.desktop_url.setText(petlink.URL)

    def _collect(self) -> dict:
        """Build the config patch from the form.

        Returns:
            A partial config dict.
        """
        mode = self.mode.currentText()
        if mode == MODE_LOCAL:
            return {
                "mode": "local",
                "llm": {
                    "base_url": self.base_url.text().strip() or brain.DEFAULT_BASE,
                    "model": self.model.text().strip() or brain.DEFAULT_MODEL,
                    "api_key": self.api_key.text().strip(),
                },
                "vision": {
                    "mode": "caption",
                    "base_url": self.base_url.text().strip() or brain.DEFAULT_BASE,
                    "model": self.vision_model.text().strip(),
                    "api_key": self.api_key.text().strip(),
                },
            }
        return {
            "mode": "astrbot",
            "desktop_url": self.desktop_url.text().strip() or petlink.URL,
            "desktop_secret": self.desktop_secret.text().strip(),
            "chat_transport": "desktop",
        }

    def _test(self) -> None:
        """Test whichever mode is selected (in a worker thread)."""
        self.status.setText("正在测试…")
        self.test_button.setEnabled(False)
        patch = self._collect()

        def run() -> None:
            try:
                if patch["mode"] == "local":
                    ok, message = brain.test_connection({"llm": patch["llm"]})
                else:
                    ok, message = petlink.test_astrbot(
                        patch.get("desktop_url", ""),
                        patch.get("desktop_secret", ""),
                    )
            except Exception as exc:  # noqa: BLE001 - a test failure must never freeze the button
                ok, message = False, f"{type(exc).__name__}: {exc}"
            self._test_result.emit(ok, message)

        threading.Thread(target=run, daemon=True).start()

    @Slot(bool, str)
    def _on_test_result(self, ok: bool, message: str) -> None:
        """Show the test outcome and re-enable the button (GUI thread).

        Args:
            ok: Whether the connection test succeeded.
            message: Human-readable result to display.
        """
        self.status.setText(("✅ " if ok else "❌ ") + message)
        self.status.setStyleSheet("color:#2b8a3e" if ok else "color:#c92a2a")
        self.test_button.setEnabled(True)

    def _save(self) -> None:
        """Write the config and close."""
        patch = self._collect()
        self.config.update(patch)
        self.accept()
