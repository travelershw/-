#!/usr/bin/env node
/**
 * 把工具链档案解包到工具链目录。
 *
 * 正常情况下**不需要跑这个**(工具链已在磁盘上),只在换机器/重建时用。
 * 两个下载都是普通 ZIP,顶层带一个版本目录(`jdk-17.0.20.1+1/`、`cmdline-tools/`)。
 * Android SDK 比 JDK 挑剔:`sdkmanager` 要求命令行工具位于
 * `<sdk>/cmdline-tools/latest/`,这个布局在这里固定下来,不交给调用方。
 *
 * 用法: node tools/install-toolchain.js [--force]
 *
 * @module qingyu-phone/tools/install-toolchain
 */
import { existsSync, mkdirSync, readdirSync, renameSync, rmSync, statSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { TOOLCHAIN_DIR } from '../lib/toolchain.js';

const DOWNLOADS = `${TOOLCHAIN_DIR}/downloads`;
const JDK_DIR = `${TOOLCHAIN_DIR}/jdk`;
const SDK_DIR = `${TOOLCHAIN_DIR}/android-sdk`;

// 用 PowerShell 的 Expand-Archive 而不是 JS 解压库:档案约 180 MB,而 node 没有内置
// 解压,而这整个工程的原则就是不引任何依赖。
const PS = process.platform === 'win32' ? 'powershell.exe' : 'pwsh';

/**
 * 解压一个 ZIP,需要时先清空目标目录。
 * @param archive 档案路径。
 * @param destination 解压根目录。
 * @param force 是否先删掉已存在的目标。
 * @returns 解压结束。
 */
async function unzip(archive, destination, force) {
  if (existsSync(destination)) {
    if (!force) {
      console.log(`已存在,跳过解压: ${destination}`);
      return;
    }
    rmSync(destination, { recursive: true, force: true });
  }
  mkdirSync(destination, { recursive: true });
  console.log(`解压 ${archive} → ${destination}`);
  await new Promise((resolve, reject) => {
    const child = spawn(
      PS,
      ['-NoProfile', '-NonInteractive', '-Command', `Expand-Archive -LiteralPath '${archive}' -DestinationPath '${destination}' -Force`],
      { stdio: 'inherit' },
    );
    child.on('error', reject);
    child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`Expand-Archive 退出码 ${code}`))));
  });
}

/**
 * 找解压根里唯一那个需要"提上来"的目录。
 * @param root 解压根。
 * @returns 该目录路径;布局是平的时候返回 null。
 */
function soleDirectory(root) {
  const entries = readdirSync(root).filter((name) => !name.startsWith('.'));
  if (entries.length !== 1) return null;
  const only = `${root}/${entries[0]}`;
  return statSync(only).isDirectory() ? only : null;
}

const force = process.argv.includes('--force');

// --- JDK -------------------------------------------------------------------
const jdkArchive = `${DOWNLOADS}/temurin-jdk-17.zip`;
if (!existsSync(jdkArchive)) {
  console.error(`缺少 ${jdkArchive},先运行: node tools/fetch-toolchain.js jdk`);
  process.exit(1);
}

await unzip(jdkArchive, JDK_DIR, force);
// Adoptium 的档案把内容都套在一个版本目录下,提上来以后 JAVA_HOME 才能跨版本稳定。
const jdkInner = soleDirectory(JDK_DIR);
if (jdkInner !== null) {
  for (const entry of readdirSync(jdkInner)) {
    renameSync(`${jdkInner}/${entry}`, `${JDK_DIR}/${entry}`);
  }
  rmSync(jdkInner, { recursive: true, force: true });
}
const jdkJavac = `${JDK_DIR}/bin/javac.exe`;
if (!existsSync(jdkJavac)) {
  console.error(`JDK 布局异常:找不到 ${jdkJavac}`);
  console.error(`目录内容: ${readdirSync(JDK_DIR).join(', ')}`);
  process.exit(1);
}
console.log(`JDK 就绪: ${JDK_DIR}`);

// --- Android 命令行工具 ------------------------------------------------------
const sdkArchive = `${DOWNLOADS}/android-commandlinetools.zip`;
if (!existsSync(sdkArchive)) {
  console.error(`缺少 ${sdkArchive},先运行: node tools/fetch-toolchain.js cmdline-tools`);
  process.exit(1);
}

const staging = `${TOOLCHAIN_DIR}/cmdline-staging`;
await unzip(sdkArchive, staging, true);
// sdkmanager 硬性要求 <sdk>/cmdline-tools/latest/,所以档案里的 `cmdline-tools/`
// 要变成 `latest/`,不能直接摊平。
const sdkManager = `${SDK_DIR}/cmdline-tools/latest/bin/sdkmanager.bat`;
mkdirSync(`${SDK_DIR}/cmdline-tools`, { recursive: true });
const staged = `${staging}/cmdline-tools`;
if (!existsSync(staged)) {
  console.error(`解压布局异常:${staging} 下没有 cmdline-tools 目录`);
  console.error(`实际内容: ${readdirSync(staging).join(', ')}`);
  process.exit(1);
}
rmSync(`${SDK_DIR}/cmdline-tools/latest`, { recursive: true, force: true });
renameSync(staged, `${SDK_DIR}/cmdline-tools/latest`);
rmSync(staging, { recursive: true, force: true });

if (!existsSync(sdkManager)) {
  console.error(`sdkmanager 未就位:找不到 ${sdkManager}`);
  process.exit(1);
}
console.log(`Android 命令行工具就绪: ${sdkManager}`);

console.log('');
console.log('下一步(安装 SDK 组件,约 150–300 MB):');
console.log('  node tools/setup-android-sdk.js');
console.log('');
console.log('环境变量预览:');
console.log(`  JAVA_HOME=${JDK_DIR}`);
console.log(`  ANDROID_HOME=${SDK_DIR}`);
