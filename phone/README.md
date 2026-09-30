# 轻语 · 手机端（Android）

`phone/` 是「轻语」的 Android 客户端：**手机只负责听和说，她的脑子留在家里那台电脑上**。

手机上做的事只有三件——录音、播放她的声音、拍照；识别（ASR）、人格与记忆、语音合成（TTS）
全都在电脑那侧完成。所以换一台手机接上去，还是**同一个人**：好感度、记忆、心情都在电脑上。

> 这个目录是**源码**，不含任何构建产物、第三方二进制或运行数据（见下面的「目录」一节）。
> 想直接装 App 的话，装的是仓库 Release 里那个 APK。

## 目录

| 路径 | 内容 |
| --- | --- |
| `lib/android-project.js` | Gradle 工程 / manifest / 版本号 / 权限的**单一来源**（包名、versionCode、FileProvider、前台服务、蓝牙权限分档） |
| `lib/android-sources.js` | 生成的 Java 源码：`MainActivity` + `QingyuNative` 桥、`QingyuAudio`（三档 SCO 路由）、`QingyuCamera`、`QingyuService`（前台服务 + 保活） |
| `lib/page/index.html`、`lib/page/pure.js` | App 里的本地页面：连接、按住说话、拍照、SCO 试验、复制诊断 |
| `lib/dist-docs.js` | 分发包里的纯文本说明（含「异地测试包」那份） |
| `lib/zip.js` | 零依赖 ZIP 写入器（两个打包工具共用） |
| `lib/toolchain.js` | 工具链路径的单一来源 —— **仓库里不写死本机路径**，见下面「构建」 |
| `tools/gen-android.js` | 生成 Android 工程（`android/` 整个目录都是生成物） |
| `tools/build-apk.js` | 调 Gradle 出 APK |
| `tools/make-dist.js` | 打「分发包」（APK + 安装说明 + 版本说明） |
| `tools/make-remote-kit.js` | 打「异地测试包」（把 Tailscale 官方 APK 一起带上，摘要钉死） |
| `tools/make-public-guide.js` | 生成**公开版**说明（不带令牌，文件名用 Release 上的 ASCII 名） |
| `tools/page-protocol-test.js` | 页面/协议自测（不开 Gradle、不开手机） |
| `tools/verify-apk.ps1`、`tools/verify-dex.js` | 产物校验（签名、版本号、关键 Java 是否真的进了包） |
| `tools/fetch-toolchain.js`、`install-toolchain.js`、`setup-android-sdk.js` | 自建工具链（没有现成的那份时用） |

**不进仓库**（`.gitignore`）：`android/`（生成的工程与构建产物）、`vendor/`（打包时要塞进去的
Tailscale 官方 APK，100 MB）、`dist/`（打包产物，**里面带中转令牌**）、`.toolchain*`、`node_modules/`。

## 构建

需要 Node（本机构建用的是 v24，v18 以上应该都行）和一套 Android 工具链
（JDK 17 + Android SDK `android-35` / `build-tools 35.0.0` + Gradle 8.11.1，共约 1.1 GB）：

```bash
node tools/fetch-toolchain.js jdk      # 没有现成工具链时：下 JDK
node tools/install-toolchain.js
node tools/setup-android-sdk.js
```

磁盘上**已经有**一套工具链的话就原地复用，别重复下载：把路径写进 `phone-app/.toolchain.local`
（一行，已被 `.gitignore` 排除），或设环境变量 `QINGYU_SHARED_TOOLCHAIN=/path/to/.toolchain`。
整个工具链想换到别处就设 `QINGYU_TOOLCHAIN`。

```bash
node tools/gen-android.js              # 生成 android/
node tools/build-apk.js                # → android/app/build/outputs/apk/debug/app-debug.apk
node tools/page-protocol-test.js       # 页面与协议自测
```

三条踩过的坑，别改：

1. 必须用工具链里的 **JDK 17**（系统 java 是 11 时 AGP 8.7.3 直接拒绝）；
2. `ANDROID_USER_HOME` 必须指向**纯 ASCII 路径**（中文用户名会让 AGP 把 `.android/` 写成乱码路径）；
3. **不要**同时设 `ANDROID_SDK_HOME` 与 `ANDROID_USER_HOME`（AGP 会抛 `AndroidLocationsException`）。

## 语音链路（手机 ⇄ 家里的中转）

手机跟电脑上的一个**中转服务**（`desktop/phone_bridge.py`）说话；中转再连 AstrBot 的
`glasses` 通道。识别与合成都在中转这侧做，所以**不用打开 AstrBot 的 STT，也不用重启 AstrBot**。

| 手机 → 服务 | 服务 → 手机 |
| --- | --- |
| `hello`（带设备信息，可选令牌） | `ready`（下行格式：PCM16 / 采样率 / 声道） |
| `utterance` + 二进制（16 kHz 单声道 WAV） | `you`（识别出来的文字 + 电平 + 识别耗时） |
| `text` / `image` + 二进制 | `her`（她的回复文字） |
| `ping` | `audio_begin` + 裸 PCM 分块 + `audio_end`（**边收边播**） |
| | `error`（说清原因，不静默失败） |

下行是 24 kHz 单声道裸 PCM；她的语音一到位就转发，所以是"边说边播"，不用等她整段合成完
（实测差 2~3 秒）。上行必须 16 kHz 单声道，因为识别模型只吃这个格式。

## 怎么连（两条路）

**同一个局域网**（不用装任何东西，只能在家里用）

```bash
python desktop/phone_bridge.py --host 0.0.0.0 --token <一串随机令牌>
```

手机里填 `ws://<电脑的 IPv4>:6201/`，并填上那串令牌。**这条路令牌必填**：中转只在
**监听回环**时才信任 Tailscale 的身份头，一旦对外监听就只认令牌。防火墙要放行 6201。

**人在外地**（走 Tailscale）

1. 电脑上：`tailscale serve --bg --https=8443 http://127.0.0.1:6201`
   —— `tailscale serve` 提供**真证书**并转发 WebSocket，所以不需要自建反向代理；
2. 手机装 Tailscale，用**自己的**账号登录（不需要跟电脑同一个账号）；
3. 电脑那侧在 admin console → Machines → 那台机器 → **Share**，把**共享邀请链接**发给对方；
   对方用浏览器打开、接受。共享出去的机器只对**这一个人**可见，他看不到你网络里的其它设备
   （而且共享机器默认是"隔离"的：只能被连、不能主动连出去）；
4. 手机里填 `wss://<机器名>.<tailnet>.ts.net:8443/` —— 注意**节点共享只能用完整域名访问**，
   好在 `tailscale serve` 的证书正好覆盖这个域名，一个字都不用改。

令牌跟**眼镜通道的密钥是分开的**：中转用 `--token <一串>` 启动，测试者拿到的那串只能开中转
入口，开不了通道本身。令牌由作者**单独**发给测试者，不进仓库、不进 Release。

## 已知限制（不是 bug）

1. **蓝牙耳机的麦克风**：部分手机（尤其华为/鸿蒙）不把蓝牙耳麦开放给第三方 App。这时 App 会
   自动改用手机麦，耳机仍然用来放她的声音，并在页面上写明原因；想知道本机到底给不给，
   点「SCO 试验」看结果。想用"耳机上的麦"，插一副**带麦的有线耳机或 USB-C 耳麦**，
   App 会自动优先用它（不走蓝牙 SCO）。
2. **必须家里那台电脑开着、中转在跑**，否则连不上 —— 她的脑子在那边。
3. 只有 **Android** 能用（iPhone 装不了 APK）。
4. 异地走 Tailscale 的中转服务器，比在同一个 WiFi 里慢一点（实测多 0.2~0.5 秒）。
5. 切后台/熄屏靠前台服务 + 常驻通知保活；华为/鸿蒙还需要在系统里允许「后台活动」。

## 隐私

- 手机上录的音、拍的照片会**送到你那台电脑上**处理；录音转完文字**立即删除**，照片留在中转
  程序旁边的 `phone_uploads/`（仓库布局里是 `desktop/phone_uploads/`），对话记录留在电脑侧。
- App 自己不往任何第三方服务器发东西；异地连接时的网络流量走 Tailscale 自己的组网
  （加密、点对点或经其中继）。
- 仓库里不含任何令牌、密钥或运行数据；文档里的地址一律写成
  `<机器名>.<tailnet>.ts.net` 这样的占位符。

## 许可证

与仓库根目录一致：**AGPL-3.0**。
