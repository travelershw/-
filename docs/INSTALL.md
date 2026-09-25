# 安装与配置（INSTALL）

本文件覆盖三部分的安装/构建方式：桌宠（`desktop/`）、AstrBot 插件（`plugins/`）、历史 C++ 实现（`legacy-cpp/`）。

> 文档中所有路径均为占位示例；真实机器路径、QQ 号、密钥请用你自己的值替换。示例 QQ 一律用 `10001`。

---

## 一、桌宠（desktop/）

### 1. 依赖

桌宠是 Python 程序，依赖固定版本见 `desktop/requirements.txt`：

```
PySide6==6.11.2
websockets==17.1
httpx==0.28.1
pyinstaller==6.22.3
```

建议使用 **Python 3.10+**。

### 2. 运行（开发方式）

```powershell
# 在 desktop/ 目录下
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python pet.py
```

### 3. 打包成可分发 exe（可选）

打包前需要先建好虚拟环境并安装依赖（即上面的 `.venv`）：

```powershell
.\.venv\Scripts\activate
python build_exe.py
```

`build_exe.py` 内部会用 `.venv` 里的 Python 调 PyInstaller，产物输出到 `_dist/`（单目录 + zip）。

> **摄像头与打包**：`build_exe.py` 曾把 `PySide6.QtMultimedia` 排除在外，导致 exe 版点不动
> 「用摄像头看一眼」。**现在已改为随包带上**（Qt6Multimedia + ffmpeg 后端 + Windows 媒体后端
> 插件都在 `_internal` 里），代价是程序目录约 +21 MB（120.8 → 141.9 MB）、zip 约 +9 MB。
> 如果你自己改动了 `EXCLUDES`，注意**不要把 `PySide6.QtMultimedia` 加回去**——
> 源码运行不受影响，所以这个坑只在打包版才现形。

### 4. 桌宠说明

- **可独立运行**：不连 AstrBot 也能用。选“独立模式”后，**API Key 由用户自己在设置窗口里配置**，只写入本机 `config.json`，不上传。
- **无立绘，程序 fallback 绘制**：源码不带角色立绘资源，程序用内置的 fallback 绘制一个简易形象；想换图可自行把 png/jpg/webp 放进 `assets/`，右键桌宠 →「重新读取立绘」。
- **连 AstrBot（可选）**：桌宠通过本机 `ws://127.0.0.1:6198` 连接 AstrBot 的 `desktop_pet` 适配器，使记忆/好感/心情与 QQ 打通。

### 5. 桌宠的传感器（右键菜单）

| 菜单项 | 作用 | 说明 |
| --- | --- | --- |
| 看屏幕 | 抓一张屏幕截图发给她 | **只在你点击时抓**；抓前自动隐藏桌宠/气泡，免得"看到自己" |
| 截图用 JPEG（上传快） | 截图格式开关 | 默认 PNG（小字清楚）；JPEG 体积更小、上传更快但字略糊。这张图是要**上传到模型服务商**的，上行体积直接决定等待时间 |
| 电脑状态 | 看本机状态读数 | 闲置时长 / 是否锁屏 / 前台程序**进程名**（不含窗口标题）/ 摄像头列表；只写本机 `pc_state.json`，不上传 |
| 用摄像头看一眼 | 抓一帧 | **只有点这里才会拍**；一次一帧，发送后 60 秒删除；她最多"提议看一眼"，**提议本身不抓拍** |
| 允许摄像头 | 总开关 | 关掉后连提议都不会提 |
| 今天天气 | 天气 / 空气质量 / 日出日落 | 走 [Open-Meteo](https://open-meteo.com/)（**免 API Key、免注册**），只读 GET |
| 设置位置… | 填城市名 | 用它查一次经纬度，**存到本机 `config.json`**（`weather_place` / `weather_lat` / `weather_lon`）。**不会用 IP 反查你的位置**；不填就没法查天气（只会提示你去设） |

> **中文城市名的小坑（实测）**：直接在「设置位置…」里填「开封」这类名字，地图服务
> 可能先返回**同名的小村庄**（人口为空，甚至不在同一个省）。桌宠只认"有真实人口"的结果，
> 找不到就让你换个说法，**不会把你定位到别的省**；遇到查不到时加个「市」再试（例如「开封市」）
> 基本都能命中——抽样 28 个城市里有 11 个都是这样（吉林、宜昌、桂林、佛山、东莞……），
> 而「杭州」「苏州」「成都」这类不带「市」也能命中。
> 无论如何，落地的都是**你自己填的城市**，请核对气泡里报出的"省 + 城市"。

### 5. 桌宠关键配置

| 配置项 | 说明 |
| --- | --- |
| API Key | 独立模式下由用户在设置窗口填写，存于本机 `config.json` |
| `QINGYU_USER_ID` | 桌宠连 AstrBot 时的身份 ID，**必须与 `desktop_pet` 适配器配置的 `user_id` 一致**（默认用你的 QQ 号），否则记忆/好感不会落到同一个人身上 |
| `QINGYU_KNOWLEDGE_DIR` | 可覆盖知识库目录（默认取 AstrBot 数据目录下的知识库路径） |
| `screen_capture` / `camera_capture` / `pc_state` / `weather` | 四个传感器总开关（默认都开）。关掉即彻底停用对应读写 |
| `shot_format` | 截图格式：`png`（默认）或 `jpeg`；也可用右键菜单切换 |
| `camera_keep_frame` | 默认 `false`＝摄像头那一帧发送后删除；改成 `true` 会留在 `shots/` 里 |
| `weather_place` / `weather_lat` / `weather_lon` | 天气的位置。**默认是空的**，用右键菜单「设置位置…」填一次即可 |

> ⚠️ **改 `config.json` 前先退出桌宠**：桌宠在正常退出时会用内存里的设置**整体重写**
> `config.json`，边跑边改会被覆盖掉。

---

## 二、AstrBot 插件（plugins/）

### 1. 环境

需要 **AstrBot 4.28.x**（以 4.28.1 为准）。`vendor/AstrBot/` 提供完整上游核心源码 + dashboard，可据此自行部署 AstrBot，或使用官方发行版后安装插件。

### 2. 安装插件

把 `plugins/` 下某个插件目录整体放进 AstrBot 的 `data/plugins/`（或通过面板安装），重启 AstrBot 加载。9 个插件互相独立，但推荐至少安装 `qingyu_core`（核心）与 `qingyu_affection`（好感度）。

### 3. AstrBot 4.28.x 插件接口（本仓库插件均遵循）

- 每个插件是一个目录：`metadata.yaml`（`name` / `desc` / `author` / `version`）+ 入口 `main.py`。
- 入口继承 `astrbot.api.star.Star` 基类，用 `@register(name, author, desc, version)` 装饰器注册；构造函数接收 `Context`。
- 事件与指令：`from astrbot.api.event import AstrMessageEvent, filter`；用 `@filter.command("...")` 注册指令；常用字段/方法包括 `event.message_str`、`event.is_admin()`、`event.plain_result(...)`。
- 生命周期：`initialize()`、`terminate()`。
- 日志：`from astrbot.core import logger`。
- 插件运行数据路径：使用 `astrbot.core.utils.path_utils` 获取 AstrBot 数据目录，不要硬编码绝对路径。

### 4. 看门狗插件（napcat_watchdog）

- 运行逻辑**内置**在插件目录内的 `watchdog_runtime.py`，不依赖 `vendor/AstrBot/migration_tools`（该目录已从 vendor 中排除）。
- **默认 notify-only**：掉线只提醒（Windows 弹窗 + 日志 + 桌宠提示），**不自动重启 QQ**
  （公开版把 `AUTO_RESTART` 置为 `False`；作者本机是自己开着的，这个是发布默认值）。
  需要自动恢复时用 `/看门狗 自动重启 开` 自行打开，并注意下面的风险与节流参数。
- **节流参数**（`watchdog_runtime.py` 顶部）：`RESTART_COOLDOWN_MINUTES = 60`、
  `MAX_RESTARTS_PER_HOUR = 1`、`ALLOW_KILL_ALL_QQ = False`（只结束"正托管着 NapCat 的那个 PID"，
  不做"杀掉所有 QQ 进程"这种事）。
- **手动重启 QQ 有风险**：自动/手动重启会结束当前 QQ 再拉起，可能打断你正在进行的会话，请谨慎使用。
- **仅 Windows**：依赖 NapCat 注入 QQNT 的方式（`napimain.exe` / `napiloader.dll` 等）。
- 需配置环境变量：

| 环境变量 | 说明 |
| --- | --- |
| `QINGYU_NAPCAT_DIR` | NapCat 安装目录（含 `napimain.exe`、`napiloader.dll` 等） |
| `QINGYU_QQ_EXE` | QQNT 客户端的 `QQ.exe` 完整路径 |

- 指令（管理员）：`/看门狗`（状态）、`/看门狗 查`、`/看门狗 重启`、`/看门狗 自动重启 开|关`、`/看门狗 关闭|开启`。

### 5. 风格老师（qingyu_core）

- **默认老师名单为空**：没有指定老师时，不会选中任何人的发言作为老师专属样本；通用群统计仍可构建。需要通过下面的命令显式添加学习对象。
- `/风格 老师`：查看当前学习对象。
- `/风格 老师 加 <QQ 或昵称>`：添加学习对象（例如 `/风格 老师 加 10001`）。
- `/风格 老师 删 <QQ 或昵称>`：移除学习对象。

---

## 三、历史 C++ 实现（legacy-cpp/）

### 1. 构建

使用 CMake，通过 **FetchContent 拉取 mbedtls v3.6.3**（需启用子模块 / 联网获取）：

```powershell
cmake -B build -S .
cmake --build build
```

### 2. 依赖与证书

- **内置（bundled）**：`httplib.h`（cpp-httplib）、`json.hpp`（nlohmann/json），MIT 原声明保留。
- **FetchContent**：mbedtls v3.6.3（Apache-2.0）。
- **不含 cacert.pem**：仓库**不附带** CA 证书文件；如需 HTTPS 校验，请自行从 curl 官网下载：<https://curl.se/docs/caextract.html>，放到程序工作目录。
- **仅 Windows**：当前实现面向 Windows；公开包提供 CMake 配置，未包含依赖本机目录的旧 Visual Studio 工程。此发布轮未实际编译验证 C++ 或启动完整 AstrBot。

### 3. 配置

- 仓库提供 `config.example.txt` 作为模板；复制为 `config.txt` 后填入你自己的 API Key、地址等。
- **不要提交真实的 `config.txt`**（内含密钥），它已被 `.gitignore` 排除。
