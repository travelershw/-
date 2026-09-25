"""打包成可以分发的「轻语桌宠」：用户解压即用，不需要装 Python。

做三件事：

1. 用 PyInstaller 打成**单目录**（one-folder）的 exe，随包带默认立绘和《使用说明.txt》；
2. 排除用不到的 Qt 大模块（WebEngine / 3D / 多媒体…），把体积压下来；
3. 把结果压成 zip，方便发给别人。

用法（在 pet_desktop 目录下）:
    .\\.venv\\Scripts\\python.exe -X utf8 build_exe.py
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import paths

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
BUILD = ROOT / "_build"
DIST = ROOT / "_dist"
APP_NAME = "轻语桌宠"
VERSION = "1.3.0"
# 随包带的东西：默认立绘、（可选）使用说明
EXTRA = [("assets", "assets")]
# 用不到的 Qt 模块，排掉能省一两百 MB
# 注意：**不要**把 PySide6.QtMultimedia 加回来——摄像头（camera.py）靠它枚举设备与抓帧，
# 排掉之后 exe 版就点不动「用摄像头看一眼」（源码运行不受影响，所以很容易漏掉）。
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick3D",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtXml",
    "tkinter",
    "unittest",
    "pydoc_data",
]

GUIDE = """轻语桌宠 v{version} · 使用说明
=====================================

一、怎么开始（三步）
--------------------
1. 把整个「轻语桌宠」文件夹解压到任意位置（比如 D:\\轻语桌宠），别只复制 exe；
2. 双击「轻语桌宠.exe」——她会出现在屏幕右下角；
3. 第一次运行会弹出设置窗口：选运行方式 → 填好 → 点「测试连接」→「保存并关闭」。

### 三种运行方式（设置窗口里选）

**① 独立模式（默认，推荐）**：她自带大脑，直接连你自己的大模型账号。
   选一个服务商 → 粘贴你自己的 API Key → 测试 → 保存。Key 只存在本机 config.json，不上传。
   · DeepSeek   https://platform.deepseek.com      （推荐，便宜）
   · 智谱 GLM   https://open.bigmodel.cn           （glm-4v-flash 免费且能看图）
   · 通义千问   https://bailian.console.aliyun.com
   · OpenAI / Kimi / 本地 Ollama 也支持（选「自定义」自己填地址）

**② 连我自己的 AstrBot**：你本机已经跑着 AstrBot（装了 qingyu_core 和 desktop_pet 两个插件）时，
   选这一项，桌宠通道填 ws://127.0.0.1:6198，共享密钥留空，测试通过后保存。
   好处：她的记忆、好感与她 QQ 群里的那套完全打通。**模型费用由你那台 AstrBot 里的 Key 出。**
   注意：**连接断了不会自动重连**（避免在对方关机时反复敲门），会弹一句提示，
   右键 →「重连 AstrBot」手动再连一次。

**③ 连别人的 AstrBot**：别人把「地址 + 共享密钥」给你（例如 ws://他的域名:6198 + 一串密钥），
   选这一项并**必须填密钥**，否则对方会直接拒绝连接。用对方那台机器的模型额度，你这边不用出钱，
   但你说的话会进入对方机器人的记忆里——**要不要连，自己判断**。
   同样：断了不自动重连，右键 →「重连 AstrBot」。

二、怎么玩
----------
· 左键拖：挪位置（松手记住）
· 左键单击：她随口说一句
· 双击：打开聊天窗（打字回车即发）
· 右键 / 托盘图标：状态、看她刚在群里说了什么、看看我的屏幕、电脑状态、用摄像头看一眼、
  允许摄像头、截图用 JPEG（上传快）、今天天气、设置位置…、大小、鼠标穿透、设置、退出
· 「看看我的屏幕」：抓一张屏幕截图发给她（只在你点的时候抓，抓之前她会先把自己藏起来）
  ——独立模式下要在设置里填一个"看图模型"（如 glm-4v-flash、qwen-vl-max）她才能看到。
· 「截图用 JPEG（上传快）」：截图默认是 PNG（小字清楚）；换成 JPEG 体积更小、传得更快，
  代价是字迹略糊。这张图是要上传给模型的，网慢的时候切一下差别很明显。
· 「用摄像头看一眼」：**只有你点这一下才会拍**，一次一帧、发完就删；她最多在气泡里
  "提议看一眼"，提议本身不会拍照，也不会把照片发到群里。
· 「今天天气」：天气 / 空气质量 / 日出日落。第一次用要先「设置位置…」填个城市名
  （有些城市要带"市"字，比如"开封市"）；位置只存在本机，**不会用你的 IP 去猜**。
  这是本程序唯一会联网查的外部数据，请求只带你自己填的经纬度（详见 docs/PRIVACY.md）。
· 「视频通话（她能一直看到你）」：打开后摄像头**常开**，你说完一句话她就看到当时的你
  （只在说话那一刻交出一帧，不是每帧都传）；右上角小窗显示"她看到的画面"。
  默认关；关掉立刻释放摄像头，锁屏自动停。
· 「电脑状态」：看本机状态读数（闲置多久、有没有锁屏、当前前台程序的**进程名**、摄像头列表）。
  只写在本机，不上传、不联网。
· 语音相关（**默认全部关闭**，要你自己打开）：
  「听一句（5 秒）」点一次录一句；「允许麦克风」是总开关；
  「对话模式（不用点，直接说）」打开后麦克风常开、你说完停顿一下她就接话
  （锁屏会暂停，她说话时不会听，3 分钟没动静自动退出）。
  「下载本机模型…」下识别模型（78 MB）和静音检测模型（0.6 MB），只下一次。
  识别默认在**本机**做，录音发出去就删，**音频不出这台机器**。

三、她会记事
------------
聊天里说过的重要事情（考试、约定、爱好）她会自己记下来，下次相关时自然提一句。
· 觉得记错了：直接跟她说，或删掉程序目录里的 pet_data.db（会连同聊天记录一起清空）。
· 连 AstrBot 模式下，记忆由那台 AstrBot 管（对方那边可以 /记忆 查看、清理）。

四、数据存在哪
--------------
独立模式下全都在你解压出来的文件夹里，不上传任何地方：
  config.json     设置和 API Key（**别把这个文件发别人**）
  pet_data.db     聊天记录 + 她记住的事 + 心情状态
  shots\\         屏幕截图（只留最近 20 张）
  pc_state.json   本机状态读数（闲置/锁屏/前台程序名/摄像头列表），只在本机
  weather.json    最近一次天气读数 + 你填的位置，只在本机
  clips\\          麦克风录下的片段（识别完立即删除；开了 mic_keep_clip 才留最近 10 段）
  models\\         语音识别模型 + 静音检测模型（自己下的，**可以删**，删了再下就是）
  assets\\        她的立绘（想换图就把图片丢进来，右键→「重新读取立绘」）
  pet_log.txt     运行日志（出问题时看这个；也能用 `轻语桌宠.exe --ask "问题"` 自检）

  注意：桌宠正常退出时会用内存里的设置**重写 config.json**，所以想手工改配置
  请先退出桌宠，否则一关就被覆盖。

五、换立绘
----------
把图片（png/jpg/webp）放进 assets\\，右键 →「重新读取立绘」。
· 只放一张：叫什么都行；· 想按表情分别给：命名 calm/smile/happy/shy/down/huffy/sleepy/surprised。
· 透明底或纯色底都行，纯色底/绿幕会自动抠。

六、常见问题
------------
· 她说"还没填 API Key" → 右键 →「设置」，选独立模式填 Key。
· 401 / invalid key → Key 复制错了或没充值；去服务商后台确认。
· 余额不足 → 充值，或换 glm-4-flash 这类免费额度大的模型。
· 连 AstrBot 提示"密钥不对" → 对方开着的通道需要密钥；把他的密钥填进去。
· 连 AstrBot 提示"连不上" → 对方机器没开、端口没通、或地址写错（要带 ws:// 和端口）；
  改好设置后右键 →「重连 AstrBot」（**它不会自动重连**，避免在对方关机时反复敲门）。
· 她不说话 / 窗口找不到 → 看右下角托盘（^）里的「轻语」图标，双击叫她出来。
· 她说"还没设位置" → 右键 →「设置位置…」填城市名；查不到时加个「市」再试
  （实测"开封"会先撞上四川的同名村子，"开封市"才对），气泡里会报出"省 + 城市"给你核对。
· 摄像头抓不到 / 一片黑 → 刚开摄像头头几帧还没曝光稳定，程序会自动挑最亮的一帧；
  还是黑就看看镜头是不是被挡了、或别的程序占着摄像头。
· 天气查不到 → 检查网络；这个功能走 api.open-meteo.com（不需要 API Key）。
· 点「听一句」说"没下识别模型" → 右键 →「下载本机模型…」（约 79 MB，只下一次）；
  国内网络下它会自动先试镜像，失败再试官方源。
· 说"这句太轻了" / 对话模式没反应 → Windows 声音设置里把**输入音量**调大一点，
  或在右键菜单确认「允许麦克风」是勾上的；笔记本上还要看一眼麦克风静音键。
· 对话模式一直不接话 → 你说的每句之后要**停顿约 0.7 秒**（程序靠这段静音判断你说完了）；
  环境很吵时可以把 config.json 里的 vad_silence_ms 调大一点。
· 想让她别总冒泡 → config.json 里 "muted_events": true。
· 关不掉 → 右键 →「退出桌宠」。

版本 {version}　形象可自备，代码不含任何 API Key。
"""


def stage_assets() -> Path:
    """Copy just the artwork into a staging folder (no ``.cut`` cache, no test junk).

    Returns:
        The staging folder to bundle as ``assets``.
    """
    stage = BUILD / "bundle_assets"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    if paths.ASSETS.is_dir():
        for path in paths.ASSETS.iterdir():
            if path.is_file() and path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg", ".gif"}:
                shutil.copy2(path, stage / path.name)
    (stage / "说明.txt").write_text(
        "把轻语的立绘放这里（png/jpg/webp），然后右键桌宠 →「重新读取立绘」。\n"
        "只放一张图时叫什么都行；想按表情分别给，命名 calm/smile/happy/shy/down/huffy/sleepy/surprised。\n"
        "透明底最好；纯白底、纯绿幕也会自动抠。\n",
        encoding="utf-8",
    )
    return stage


def build() -> Path:
    """Run PyInstaller and return the app folder.

    Returns:
        Path of the built application folder.
    """
    stage = stage_assets()
    command = [
        str(VENV_PY),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / "work"),
        "--specpath",
        str(BUILD),
        "--add-data",
        f"{stage}{';' if sys.platform == 'win32' else ':'}assets",
        # 本机语音识别是**延迟导入**的（`local_asr.load()` 里才 import sherpa_onnx），
        # PyInstaller 静态分析看不到它 —— 不加这一行，打包版的「听一句」会直接报
        # "没装本机识别引擎"，而源码运行完全正常（和 QtMultimedia 那次同一个坑）。
        "--collect-all",
        "sherpa_onnx",
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "pet.py"))
    print("打包中（几分钟）…", flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")
    return DIST / APP_NAME


def finalize(app_dir: Path) -> Path:
    """Write the user guide, a clean config and zip everything.

    Args:
        app_dir: Built application folder.

    Returns:
        Path of the distribution zip.
    """
    (app_dir / "使用说明.txt").write_text(
        GUIDE.format(version=VERSION),
        encoding="utf-8",
    )
    # 随包配置：不带任何 Key，且默认独立模式
    (app_dir / "config.json").write_text(
        '{\n'
        '  "mode": "local",\n'
        '  "llm": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": ""},\n'
        '  "vision": {"mode": "caption", "base_url": "", "model": "", "api_key": ""}\n'
        '}\n',
        encoding="utf-8",
    )
    for junk in ("pet_log.txt", "chat_log.jsonl", "pet_data.db"):
        path = app_dir / junk
        if path.exists():
            path.unlink()
    assets = app_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    # 把立绘也放一份到 exe 旁边：用户解开压缩包就能看见、能直接换，
    # 不用先运行一次才从 _internal 里复制出来。
    staged = BUILD / "bundle_assets"
    for path in sorted(staged.glob("*")):
        if path.is_file() and path.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg", ".gif"}:
            shutil.copy2(path, assets / path.name)
    (assets / "说明.txt").write_text(
        "把轻语的立绘放这里（png/jpg/webp），然后右键桌宠 →「重新读取立绘」。\n"
        "只放一张图时叫什么都行；想按表情分别给，命名 calm/smile/happy/shy/down/huffy/sleepy/surprised。\n"
        "透明底最好；纯白底、纯绿幕也会自动抠。\n",
        encoding="utf-8",
    )

    zip_path = DIST / f"{APP_NAME}_v{VERSION}_win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(APP_NAME) / path.relative_to(app_dir))
    return zip_path


def main() -> None:
    """Build, finalize, and report sizes."""
    for folder in (BUILD, DIST):
        shutil.rmtree(folder, ignore_errors=True)
    app_dir = build()
    zip_path = finalize(app_dir)
    size = sum(path.stat().st_size for path in app_dir.rglob("*") if path.is_file())
    print(f"\n程序目录: {app_dir}（{size / 1024 / 1024:.1f} MB）")
    print(f"分发包:   {zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"exe:      {app_dir / (APP_NAME + '.exe')}")


if __name__ == "__main__":
    main()
