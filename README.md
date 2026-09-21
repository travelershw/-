# 轻语（Qingyu）

一个以 AstrBot 为载体的群聊陪伴机器人及其桌面桌宠、历史实现代码的公开发布仓库。

“轻语”由三部分组成：

- **桌面桌宠**（`desktop/`）：一个可独立运行的桌面小宠物，也通过本机 WebSocket 接到 AstrBot，让她的记忆、好感度、心情与 QQ 群里的“同一个人”完全打通。
- **AstrBot 自定义插件**（`plugins/`）：9 个自定义插件，承载轻语 Agent 的核心逻辑（人格、好感、群味、知识库、表情包、看门狗等）。
- **历史实现**（`legacy-cpp/`）：早期用 C++ 写的机器人原型（直连 DeepSeek / NapCat 的 AI 源码），作为历史实现保留。

> 本仓库发布的是**完整工程源码与历史实现**，**不是**任何机器的运行备份。仓库内不含运行数据、外部图片、密钥等私密内容（详见 [docs/PRIVACY.md](docs/PRIVACY.md)）。

## 目录结构

```
qingyu-public/
├── README.md                     # 本文件
├── LICENSE                       # AGPL-3.0（用户选定）
├── THIRD_PARTY_NOTICES.md        # 第三方许可证声明
├── .gitignore
├── docs/
│   ├── INSTALL.md                # 安装 / 构建 / 配置说明
│   └── PRIVACY.md                # 隐私与发布范围说明
├── desktop/                      # 桌宠 Python 源码（无立绘，程序 fallback 绘制）
│   └── requirements.txt
├── plugins/                      # 9 个自定义 AstrBot 插件
│                                 #   （看门狗插件内置 watchdog_runtime.py 运行逻辑）
├── legacy-cpp/                   # C++ AI 源码（CMakeLists、THIRD_PARTY_LICENSES.txt 等）
└── vendor/
    └── AstrBot/                  # AstrBot 完整上游核心源码 + dashboard
                                  #   （排除所有用户 data/deps/env/build/git/私密文档/migration_tools）
```

### 9 个插件

| 插件目录 | 作用 |
| --- | --- |
| `qingyu_core` | 轻语 Agent 核心：群味管长度、好感度管语气、引用/叫名字、记忆、周报 |
| `qingyu_affection` | 好感度系统：按说话语气在 0~100 间微调好感度，并按好感度切换回答语气 |
| `desktop_pet` | 桌面桌宠通道：本机 WebSocket 平台适配器，桌宠说的话算用户本人的消息（记忆/好感/心情与 QQ 打通） |
| `group_knowledge` | 群聊知识库：`knowledge_lookup` 检索工具 + 群聊 `/知识库` 指令 |
| `meme_search` | 按关键词联网搜表情包并发送，给轻语提供 `send_meme` 工具 |
| `deepseek_search` | DeepSeek 原生 `web_search` 联网检索工具 |
| `random_chime` | 群聊随机插嘴（默认白名单为空；需管理员显式开启目标群） |
| `napcat_watchdog` | 连接看门狗：NapCat/QQ 掉线提醒/重启，运行逻辑内置在插件内的 `watchdog_runtime.py` |
| `qq_parity` | 从自研机器人迁移时补齐的特色功能（时间注入、按用户冷却、短消息风格、防注入、引用清理等） |

## 快速开始

- **只想跑桌宠**：看 [docs/INSTALL.md](docs/INSTALL.md) 的“桌宠”一节（`python pet.py`）。
- **想跑完整的群聊机器人**：需要 AstrBot 4.28.x + `plugins/` 下插件，见 [docs/INSTALL.md](docs/INSTALL.md)。
- **构建历史 C++ 实现**：见 [docs/INSTALL.md](docs/INSTALL.md) 的“legacy-cpp”一节。

## 许可证

- 本仓库（根目录）采用 **GNU Affero General Public License v3.0（AGPL-3.0）**，详见根目录 `LICENSE`。
- `vendor/AstrBot/` 是 AstrBot 上游代码，其自带的 `vendor/AstrBot/LICENSE` 予以保留。
- 根目录 AGPL-3.0 **不覆盖**任何第三方组件自身的许可证；各依赖的许可证以 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 为准。

## 隐私与数据

仓库内**不包含**任何运行数据、外部图片（立绘/截图）、密钥或真实身份信息；文档示例统一使用虚构 QQ 号 `10001`。详见 [docs/PRIVACY.md](docs/PRIVACY.md)。

## 免责声明

本项目仅供学习与个人使用。使用中产生的网络请求、模型调用费用、账号与数据风险由使用者自行承担；第三方依赖的可再分发性以其各自许可证为准，本仓库不保证所有闭源依赖均可再分发。
