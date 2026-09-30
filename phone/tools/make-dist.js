#!/usr/bin/env node
/**
 * 打一个可以直接发给别人的分发包:`dist/轻语-手机端-v<版本>.zip`。
 *
 * 里面三样东西:
 *   · 轻语-手机端-v<版本>.apk   —— 构建产物重命名
 *   · 安装与使用说明.txt        —— 面向"拿到 APK 的另一个人"
 *   · 版本说明.txt              —— 这一版改了什么
 *
 * ZIP 是自己写的(见 `lib/zip.js`),不调 PowerShell 的 Compress-Archive:这台机器上
 * Node 起子进程有两条已知的坑 —— spawn `.bat` 会 EINVAL,受限沙箱下带管道 stdio 的
 * 子进程会 EPERM;而本工程的原则是"零依赖"。
 *
 * 用法: node tools/make-dist.js
 *
 * @module qingyu-phone/tools/make-dist
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { APP_LABEL, LAN_SERVER_URL_PLACEHOLDER, TAILSCALE_SERVER_URL, VERSION_CODE, VERSION_NAME } from '../lib/android-project.js';
import { installGuide, releaseNotes } from '../lib/dist-docs.js';
import { APK_PATH, ROOT } from '../lib/toolchain.js';
import { buildZip } from '../lib/zip.js';

const DIST_DIR = `${ROOT}/dist`;
const ZIP_NAME = `${APP_LABEL}-手机端-v${VERSION_NAME}.zip`;
const APK_NAME = `${APP_LABEL}-手机端-v${VERSION_NAME}.apk`;

if (!existsSync(APK_PATH)) {
  console.error(`缺少 APK: ${APK_PATH}`);
  console.error('请先运行: node tools/gen-android.js && node tools/build-apk.js');
  process.exit(1);
}

const apk = readFileSync(APK_PATH);
const apkSha256 = createHash('sha256').update(apk).digest('hex');

// --- 打包 -------------------------------------------------------------------

const guide = installGuide({
  version: VERSION_NAME,
  apkFileName: APK_NAME,
  apkSha256,
  tailscaleUrl: TAILSCALE_SERVER_URL,
  lanPlaceholder: LAN_SERVER_URL_PLACEHOLDER,
});
const notes = releaseNotes({ version: VERSION_NAME, versionCode: VERSION_CODE });

mkdirSync(DIST_DIR, { recursive: true });
const zipPath = `${DIST_DIR}/${ZIP_NAME}`;
const zip = buildZip([
  { name: APK_NAME, data: apk },
  { name: '安装与使用说明.txt', data: Buffer.from(guide, 'utf8') },
  { name: '版本说明.txt', data: Buffer.from(notes, 'utf8') },
]);
writeFileSync(zipPath, zip);

console.log('');
console.log(`APK   ${APK_PATH}`);
console.log(`      ${apk.length} 字节 / ${(apk.length / 1024 ** 2).toFixed(2)} MB`);
console.log(`      SHA-256 ${apkSha256}`);
console.log('');
console.log(`包内文件:`);
console.log(`  ${APK_NAME}  (${apk.length} 字节)`);
console.log(`  安装与使用说明.txt  (${Buffer.byteLength(guide, 'utf8')} 字节)`);
console.log(`  版本说明.txt  (${Buffer.byteLength(notes, 'utf8')} 字节)`);
console.log('');
console.log(`分发压缩包 ${zipPath}`);
console.log(`      ${statSync(zipPath).size} 字节 / ${(statSync(zipPath).size / 1024 ** 2).toFixed(2)} MB`);
console.log(`      SHA-256 ${createHash('sha256').update(zip).digest('hex')}`);
console.log('');
console.log('发给别人:直接把上面这个 zip 发过去,对方解压后把里面的 APK 装到手机即可。');
