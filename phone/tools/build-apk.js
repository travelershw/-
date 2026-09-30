#!/usr/bin/env node
/**
 * 构建可侧载的 APK。
 *
 * 构建环境在这里被**完全钉死**,原因都是这台机器上的实测坑:
 *  - `JAVA_HOME` 指向工具链里的 JDK 17:系统 java 是 11,AGP 8.7.3 直接拒绝;
 *  - `ANDROID_USER_HOME` 指向**纯 ASCII** 路径(工具链里的 `android-user-home`):
 *    中文用户名会让 Android 工具链把 `.android/` 写成乱码路径,报
 *    `IOException: 文件名、目录名或卷标语法不正确`;
 *  - **不设** `ANDROID_SDK_HOME`:它和 `ANDROID_USER_HOME` 同时存在会让 AGP 抛
 *    `AndroidLocationsException`;
 *  - `local.properties` 的 `sdk.dir` 必须是**反斜杠且反斜杠成对转义**,正斜杠 AGP 会拒;
 *  - 命令经 `cmd.exe /c` 走:Node 直接 spawn `.bat` 会 `EINVAL`;
 *  - `chcp 65001`:工程路径旁边就是非 ASCII 用户目录,Gradle 输出不加 UTF-8 会乱码。
 *
 * 用法: node tools/build-apk.js [--task assembleDebug] [--offline]
 *
 * @module qingyu-phone/tools/build-apk
 */
import { execFileSync } from 'node:child_process';
import { createWriteStream, existsSync, mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { GRADLE_VERSION } from '../lib/android-project.js';
import {
  ANDROID_DIR,
  ANDROID_USER_HOME,
  GRADLE_DIST,
  GRADLE_USER_HOME,
  GRADLE_ZIP,
  JDK_HOME,
  SDK_ROOT,
  TOOLCHAIN_DIR,
  missingToolchain,
} from '../lib/toolchain.js';

const task = (() => {
  const flag = process.argv.indexOf('--task');
  return flag !== -1 && process.argv[flag + 1] !== undefined ? process.argv[flag + 1] : 'assembleDebug';
})();
const offline = process.argv.includes('--offline');

/** Windows 路径:cmd.exe 把正斜杠当选项引导符,一律换成反斜杠。 */
const win = (value) => value.replace(/\//g, '\\');

/**
 * 下载一个文件,带进度行。
 * @param url 来源 URL。
 * @param target 落盘路径。
 * @returns 写完后 resolve。
 */
async function download(url, target) {
  mkdirSync(win(target.slice(0, target.lastIndexOf('/'))), { recursive: true });
  const res = await fetch(url);
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status} for ${url}`);
  const total = Number(res.headers.get('content-length')) || 0;
  let seen = 0;
  let lastReport = Date.now();
  const source = Readable.fromWeb(res.body);
  source.on('data', (chunk) => {
    seen += chunk.length;
    if (Date.now() - lastReport > 2000) {
      lastReport = Date.now();
      const pct = total === 0 ? '' : ` ${((seen / total) * 100).toFixed(0)}%`;
      process.stdout.write(`\r  gradle: ${(seen / 1024 ** 2).toFixed(1)} MB${total === 0 ? '' : ` / ${(total / 1024 ** 2).toFixed(0)} MB`}${pct}   `);
    }
  });
  await pipeline(source, createWriteStream(target));
  process.stdout.write('\r'.padEnd(70) + '\r');
}

// --- 工具链自检 -------------------------------------------------------------
// Gradle 发行包缺失时才去下载:正常情况下磁盘上那份已经就位,一个字节都不用拉。
if (!existsSync(`${GRADLE_DIST}/bin/gradle.bat`)) {
  const url = `https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip`;
  console.log(`缺少 Gradle ${GRADLE_VERSION},开始下载(约 130 MB)…`);
  if (!existsSync(GRADLE_ZIP)) {
    await download(url, GRADLE_ZIP);
    console.log(`  已保存 ${(statSync(GRADLE_ZIP).size / 1024 ** 2).toFixed(0)} MB`);
  }
  console.log('解压 Gradle…');
  // 用 PowerShell 的 Expand-Archive:本工程故意不引任何 npm 依赖,没法用 JS 解压。
  execFileSync('powershell.exe', [
    '-NoProfile',
    '-NonInteractive',
    '-Command',
    `Expand-Archive -LiteralPath '${win(GRADLE_ZIP)}' -DestinationPath '${win(TOOLCHAIN_DIR)}' -Force`,
  ], { stdio: 'inherit' });
}

const missing = missingToolchain();
if (missing.length > 0) {
  console.error('工具链不完整:');
  for (const line of missing) console.error(`  ${line}`);
  process.exit(1);
}
if (!existsSync(`${ANDROID_DIR}/settings.gradle`)) {
  console.error(`缺少生成的工程: ${ANDROID_DIR}/settings.gradle`);
  console.error('请先运行: node tools/gen-android.js');
  process.exit(1);
}

// `local.properties` 是 AGP 找 SDK 的方式。
// 两个转义陷阱都会变成同一句没用的
// `IOException: 文件名、目录名或卷标语法不正确`(来自 AGP 的 validateSdkPath):
// 路径必须用反斜杠(AGP 不接受 Gradle 自己能接受的正斜杠形式),而且这是 Java
// properties 文件,每个分隔符都要写成两个。先归一化、再转义才是对的 —— 对还带着
// 正斜杠的路径做转义会得到 `dir\\sub` 这种不存在的路径。
const sdkDirWindows = win(SDK_ROOT);
const sdkDirEscaped = sdkDirWindows.replace(/\\/g, '\\\\');
writeFileSync(`${ANDROID_DIR}/local.properties`, `sdk.dir=${sdkDirEscaped}\n`, 'utf8');
console.log(`local.properties: sdk.dir=${sdkDirEscaped}`);

mkdirSync(ANDROID_USER_HOME, { recursive: true });

const gradleArgs = [
  '/d',
  '/c',
  'chcp 65001 >nul',
  '&&',
  `${win(GRADLE_DIST)}\\bin\\gradle.bat`,
  '--project-dir',
  win(ANDROID_DIR),
  '--no-daemon',
  '--console=plain',
  // 受限进程令牌下原生文件监视器起不来
  '--no-watch-fs',
  ...(offline ? ['--offline'] : []),
  task,
];

console.log('');
console.log(`JDK        ${JDK_HOME}`);
console.log(`SDK        ${SDK_ROOT}`);
console.log(`Gradle     ${GRADLE_DIST}`);
console.log(`依赖缓存   ${GRADLE_USER_HOME}(复用已有热缓存)`);
console.log(`ASCII 用户目录 ${ANDROID_USER_HOME}`);
console.log(`任务       ${task}${offline ? '(离线)' : ''}`);
console.log('');
console.log('(首次构建要下载 AGP / androidx 依赖,通常 3–8 分钟)');
console.log('');

const started = Date.now();
try {
  execFileSync('cmd.exe', gradleArgs, {
    env: {
      ...process.env,
      JAVA_HOME: JDK_HOME,
      ANDROID_HOME: SDK_ROOT,
      ANDROID_SDK_ROOT: SDK_ROOT,
      // 唯一一处缓存重定向。再设一个 ANDROID_SDK_HOME 就会让 AGP 抛
      // AndroidLocationsException("several environment variables contain different paths")。
      ANDROID_USER_HOME,
      GRADLE_USER_HOME,
      JAVA_TOOL_OPTIONS: '-Dfile.encoding=UTF-8',
    },
    stdio: 'inherit',
  });
} catch (error) {
  console.error('');
  console.error(`构建失败(用时 ${Math.round((Date.now() - started) / 1000)} 秒)`);
  process.exit(error.status ?? 1);
}

console.log('');
console.log(`构建成功,用时 ${Math.round((Date.now() - started) / 1000)} 秒`);

// 报出产物与字节数,省得自己去 build 目录里翻。
const apkDirs = [
  `${ANDROID_DIR}/app/build/outputs/apk/debug`,
  `${ANDROID_DIR}/app/build/outputs/apk/release`,
];
let found = 0;
for (const dir of apkDirs) {
  if (!existsSync(dir)) continue;
  for (const name of readdirSync(dir)) {
    if (!name.endsWith('.apk')) continue;
    const path = `${dir}/${name}`;
    const bytes = statSync(path).size;
    // 带 assets 的 WebView 外壳约 1–2 MB;不到 1 MB 时换成 KB 更好读。
    const size = bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(2)} MB`;
    console.log(`  APK  ${win(path)}`);
    console.log(`       大小 ${size}(${bytes} 字节)`);
    found += 1;
  }
}
if (found === 0) {
  console.error('未找到 APK 产物,请检查上面的构建输出');
  process.exit(1);
}
console.log('');
console.log('下一步(静态校验,不需要手机):');
console.log('  powershell -NoProfile -ExecutionPolicy Bypass -File tools/verify-apk.ps1');
console.log('  node tools/verify-dex.js');
