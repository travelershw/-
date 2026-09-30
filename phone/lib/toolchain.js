/**
 * 工具链路径的单一来源。
 *
 * Android 构建工具整套约 1.1 GB(JDK 17、Android SDK android-35 / build-tools 35.0.0、
 * Gradle 8.11.1 发行包,以及已经热好的 Gradle 依赖缓存 `gradle-home`)。如果磁盘上
 * 已经有一份,就**原地复用**,不复制、不重新下载。
 *
 * 三条硬约束(踩过的坑,别改):
 * 1. 必须用工具链里的 JDK 17 —— 系统 java 是 11,AGP 8.7.3 直接拒绝;
 * 2. `ANDROID_USER_HOME` 必须指向**纯 ASCII 路径**(中文用户名会让 AGP/sdkmanager 崩),
 *    这里用的是工具链自带的 `android-user-home`(全 ASCII),不落到用户主目录;
 * 3. **不要**同时设 `ANDROID_SDK_HOME` 与 `ANDROID_USER_HOME`,AGP 会抛
 *    `AndroidLocationsException`。
 *
 * 复用是"原地"的:构建只往工具链的缓存目录(`gradle-home`、`android-user-home`)写,
 * 不碰那份工具链里的任何源码。
 *
 * **那份工具链在哪,由本机自己说,仓库里不写死路径**:
 * 优先环境变量 `QINGYU_SHARED_TOOLCHAIN`,其次是 `.toolchain.local`(一行路径,已被
 * `.gitignore` 排除);两者都没有就用本工程自建的 `.toolchain/`。
 * 想直接指定整份工具链(连自建的也算),设 `QINGYU_TOOLCHAIN`。
 *
 * @module qingyu-phone/lib/toolchain
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { BUILD_TOOLS, GRADLE_VERSION, SDK_PLATFORM } from './android-project.js';

/**
 * 工程根目录(本文件在 `lib/` 下)。
 *
 * `fileURLToPath` 在 Windows 上会带一个结尾反斜杠,这里去掉:拼出来的路径会进入
 * `cmd.exe` 的命令行,出现 `\/` 这种双分隔符容易被 cmd 误读成开关。
 */
export const ROOT = fileURLToPath(new URL('..', import.meta.url)).replace(/[\\/]+$/, '');

/** 本机的工具链提示文件(一行路径);这个文件不进仓库,所以仓库里看不到任何本机路径。 */
const LOCAL_HINT = `${ROOT}/.toolchain.local`;

/** 磁盘上现成的那份工具链(没有就用空字符串,退回到本工程自建)。 */
const SHARED_TOOLCHAIN = process.env.QINGYU_SHARED_TOOLCHAIN
  ?? (existsSync(LOCAL_HINT) ? readFileSync(LOCAL_HINT, 'utf8').trim() : '');

/** 工具链根目录:`QINGYU_TOOLCHAIN` > 本机提示的那份 > 本工程自建。 */
export const TOOLCHAIN_DIR = process.env.QINGYU_TOOLCHAIN
  ?? (SHARED_TOOLCHAIN && existsSync(SHARED_TOOLCHAIN) ? SHARED_TOOLCHAIN : `${ROOT}/.toolchain`);

export const JDK_HOME = `${TOOLCHAIN_DIR}/jdk`;
export const SDK_ROOT = `${TOOLCHAIN_DIR}/android-sdk`;
export const SDK_PLATFORM_DIR = `${SDK_ROOT}/platforms/android-${SDK_PLATFORM}`;
export const BUILD_TOOLS_DIR = `${SDK_ROOT}/build-tools/${BUILD_TOOLS}`;
export const PLATFORM_TOOLS_DIR = `${SDK_ROOT}/platform-tools`;
export const GRADLE_DIST = `${TOOLCHAIN_DIR}/gradle-${GRADLE_VERSION}`;
export const GRADLE_ZIP = `${TOOLCHAIN_DIR}/downloads/gradle-${GRADLE_VERSION}-bin.zip`;

/**
 * Gradle 依赖缓存。故意共用现成那份:里面已经缓存了 AGP 及其全部传递依赖,
 * 换一份空的就必须联网重下两百多个 jar。
 */
export const GRADLE_USER_HOME = `${TOOLCHAIN_DIR}/gradle-home`;

/**
 * Android 工具链的"用户目录"(AGP 的 preferences、debug.keystore、分析开关)。
 *
 * 整个工程只有**这一个** `ANDROID_USER_HOME`,而且它一定在纯 ASCII 路径上:
 * 中文用户名会让 AGP 把 `.android/` 写成乱码路径,报
 * `IOException: 文件名、目录名或卷标语法不正确`。
 * 再补一句:绝对不能同时设 `ANDROID_SDK_HOME`,AGP 会抛 AndroidLocationsException。
 */
export const ANDROID_USER_HOME = `${TOOLCHAIN_DIR}/android-user-home`;

/** 生成物目录(可删可重建)。 */
export const ANDROID_DIR = `${ROOT}/android`;
export const ASSET_DIR = `${ANDROID_DIR}/app/src/main/assets`;
export const APK_PATH = `${ANDROID_DIR}/app/build/outputs/apk/debug/app-debug.apk`;

/**
 * 检查工具链关键路径是否就位。
 * @returns {string[]} 缺失项的说明,空数组表示齐全。
 */
export function missingToolchain() {
  const required = [
    [`${JDK_HOME}/bin/java.exe`, 'JDK 17(node tools/fetch-toolchain.js jdk && node tools/install-toolchain.js)'],
    [SDK_PLATFORM_DIR, 'Android platform(node tools/setup-android-sdk.js)'],
    [BUILD_TOOLS_DIR, 'build-tools(node tools/setup-android-sdk.js)'],
    [`${GRADLE_DIST}/bin/gradle.bat`, 'Gradle 发行包(node tools/build-apk.js 会自动下载)'],
  ];
  return required.filter(([path]) => !existsSync(path)).map(([path, hint]) => `缺少 ${path} —— ${hint}`);
}
