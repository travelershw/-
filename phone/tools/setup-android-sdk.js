#!/usr/bin/env node
/**
 * 安装构建 APK 所需的 Android SDK 组件。
 *
 * 正常情况下**不需要跑这个**(SDK 已在磁盘上并且 `licenses/` 已接受),只在换机器时用。
 * 三条反直觉的约束,都是实测踩出来的:
 *
 * 1. **非 ASCII 用户目录会让 `sdkmanager` 直接崩。** Windows 用户名含中文时,JVM 把缓存
 *    写到乱码路径(`C:\Users\<乱码>\.android\cache\…`),每个仓库 manifest 都
 *    `NoSuchFileException`,工具最后报"IO exception while downloading manifest",
 *    看起来像网络问题。把 `ANDROID_USER_HOME` 指到 ASCII 目录就好。
 * 2. **`sdkmanager` 需要 JDK 17+**,系统 java 可能是更老的,所以 `JAVA_HOME` 钉死在
 *    工具链 JDK 上,不继承环境。
 * 3. **`sdkmanager.bat` 不能被 Node 直接 spawn**(EINVAL),受限沙箱下带管道 stdio 的
 *    子进程又会 EPERM。两个都绕开:经 `cmd.exe` 走,stdio 完全继承,许可证答案从文件喂。
 *
 * 用法: node tools/setup-android-sdk.js [--packages a,b,c]
 *
 * @module qingyu-phone/tools/setup-android-sdk
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { ANDROID_USER_HOME, JDK_HOME, SDK_ROOT } from '../lib/toolchain.js';

const SDKMANAGER = `${SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager.bat`;

/** 构建需要的组件。`platform-tools` 带来 adb,platform + build-tools 是编译目标。 */
const DEFAULT_PACKAGES = ['platform-tools', 'platforms;android-35', 'build-tools;35.0.0'];

const packages = (() => {
  const flag = process.argv.indexOf('--packages');
  if (flag !== -1 && process.argv[flag + 1] !== undefined) return process.argv[flag + 1].split(',').map((s) => s.trim());
  return DEFAULT_PACKAGES;
})();

if (!existsSync(SDKMANAGER)) {
  console.error(`缺少 sdkmanager:${SDKMANAGER}`);
  console.error('先运行: node tools/fetch-toolchain.js cmdline-tools && node tools/install-toolchain.js');
  process.exit(1);
}
if (!existsSync(`${JDK_HOME}/bin/java.exe`)) {
  console.error(`缺少工具链 JDK:${JDK_HOME}`);
  console.error('先运行: node tools/fetch-toolchain.js jdk && node tools/install-toolchain.js');
  process.exit(1);
}

mkdirSync(ANDROID_USER_HOME, { recursive: true });

/**
 * 让 sdkmanager 在非 ASCII 用户名下也能跑的环境。
 *
 * `ANDROID_USER_HOME` 是关键:所有 Android 缓存都必须离开非 ASCII 的用户主目录。
 * 注意这里**不设** `ANDROID_SDK_HOME`(与 ANDROID_USER_HOME 并存会让 AGP 崩)。
 * @returns 子进程环境。
 */
function sdkEnv() {
  return {
    ...process.env,
    JAVA_HOME: JDK_HOME,
    ANDROID_HOME: SDK_ROOT,
    ANDROID_SDK_ROOT: SDK_ROOT,
    ANDROID_USER_HOME,
    JAVA_TOOL_OPTIONS: '-Dfile.encoding=UTF-8',
    JAVA_TOOL_OPTIONS_QUIET: '1',
  };
}

/** 许可证答案文件,通过 shell 重定向喂给 sdkmanager。 */
const YES_FILE = `${ANDROID_USER_HOME}/yes.txt`;
writeFileSync(YES_FILE, 'y\r\n'.repeat(80), 'utf8');

/**
 * 跑 sdkmanager,stdio 完全继承。
 *
 * 两个 Windows 细节,搞错了都很难查:
 * - `cmd.exe` 把正斜杠当选项引导符,所有路径必须转成反斜杠,否则被当成非法文件名;
 * - 命令必须作为**独立的 argv 项**传,绝不能拼成一整条带引号的字符串:Node 会为
 *   `CreateProcess` 重新加引号,它在已经带引号的 `cmd /c` 载荷外再套一层就把命令毁了。
 *
 * 故意不捕获输出:受限沙箱禁止带管道 stdio 的子进程,而且流式输出才能看见几分钟的下载进度。
 * @param args sdkmanager 参数。
 * @param stdinFile 重定向进 stdin 的文件(许可证答案)。
 * @returns 无。
 */
function runSdkManager(args, stdinFile) {
  const toWindows = (value) => value.replace(/\//g, '\\');
  const argv = ['/d', '/c', toWindows(SDKMANAGER), ...args, `--sdk_root=${toWindows(SDK_ROOT)}`];
  if (stdinFile !== undefined) {
    argv.push('<', toWindows(stdinFile));
  }
  execFileSync('cmd.exe', argv, { env: sdkEnv(), stdio: 'inherit' });
}

console.log(`JDK        ${JDK_HOME}`);
console.log(`SDK        ${SDK_ROOT}`);
console.log(`用户目录   ${ANDROID_USER_HOME}  (必须纯 ASCII,否则 sdkmanager 写不了缓存)`);
console.log('');

// 先接受许可证:不接受后面什么都装不了,而且这也是第一次验证仓库能连上。
console.log('接受 SDK 许可…');
runSdkManager(['--licenses'], YES_FILE);

console.log('');
console.log(`安装组件: ${packages.join(', ')}`);
console.log('(platform + build-tools + platform-tools,约 150–300 MB)');
console.log('');
runSdkManager(packages, YES_FILE);

console.log('');
console.log('已安装组件:');
runSdkManager(['--list_installed']);

console.log('');
console.log('SDK 就绪。下一步: node tools/build-apk.js');
