#!/usr/bin/env node
/**
 * 打一个给**人在外地**的朋友用的测试包:`dist/轻语-异地测试包.zip`。
 *
 * 和 `tools/make-dist.js` 的区别:那个包只装轻语自己,假设收件人和电脑在同一个 WiFi;
 * 这个包把 **Tailscale 官方 Android 客户端**一起带上 —— 异地要连上家里那台电脑,
 * 只能靠"节点共享 + 自己的 Tailscale 账号"(见迁移说明 2026-09-30 那一节)。
 *
 * 里面三样东西:
 *   · Tailscale-<版本>.apk        —— 官方原包,逐字节不改名内容只是重命名
 *   · 轻语-手机端-v<版本>.apk     —— 构建产物重命名
 *   · 使用说明-异地手机.txt       —— 从装、登录、接受共享到连上她,一路写到底
 *
 * Tailscale 的 APK 有 100 MB,而它里面 97 MB 是**未压缩**存放的 `libgojni.so`,
 * 所以外层 zip 用最高级别 deflate 一遍能省掉一半左右(实测见下方输出)——
 * 对"用微信/QQ 传文件"这件事很关键。解压出来的 APK 与官方原包逐字节一致。
 *
 * 用法:
 *   node tools/make-remote-kit.js
 * 可选环境变量:
 *   QINGYU_TAILSCALE_APK       指向别的 Tailscale APK(要同时改下面的固定摘要)
 *   QINGYU_BRIDGE_TOKEN        直接给令牌,不去读文件
 *   QINGYU_BRIDGE_TOKEN_FILE   令牌文件位置(默认 migration_tools/phone_bridge_token.txt)
 *
 * @module qingyu-phone/tools/make-remote-kit
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { APP_LABEL, TAILSCALE_SERVER_URL, VERSION_NAME } from '../lib/android-project.js';
import { remoteKitGuide } from '../lib/dist-docs.js';
import { APK_PATH, ROOT } from '../lib/toolchain.js';
import { buildZip } from '../lib/zip.js';

/** Tailscale Android 版本(官方仓库最新正式版)。 */
const TAILSCALE_VERSION = '1.102.4';

/**
 * 固定的官方摘要(GitHub Releases 资产自带的 `digest` 字段)。
 *
 * 钉死是刻意的:这个 APK 要跟着包发到别人手机上,一旦换版本就必须有人来改这一行,
 * 而不是"顺手用了一个不知道哪来的包"。
 */
const TAILSCALE_SHA256 = '7ecfb863e08f5fbd1ecd70235d8c34ba135a4124bdd8e166b9d6fb962782e0b5';

/** 官方发布页(写进说明里,让收件人自己也能核对)。 */
const TAILSCALE_SOURCE_URL = 'https://github.com/tailscale/tailscale-android/releases';

/** 作者那台电脑在 Tailscale 里的机器名(共享邀请里显示的就是它)。 */
const MACHINE_NAME = 'laptop-u3hj61a7';

const TAILSCALE_APK = process.env.QINGYU_TAILSCALE_APK
  ?? `${ROOT}/vendor/tailscale-android-universal-${TAILSCALE_VERSION}.apk`;
const TOKEN_FILE = process.env.QINGYU_BRIDGE_TOKEN_FILE
  ?? `${ROOT}/../migration_tools/phone_bridge_token.txt`;

const DIST_DIR = `${ROOT}/dist`;
const ZIP_NAME = `${APP_LABEL}-异地测试包.zip`;
const QINGYU_APK_NAME = `${APP_LABEL}-手机端-v${VERSION_NAME}.apk`;
const TAILSCALE_APK_NAME = `Tailscale-${TAILSCALE_VERSION}.apk`;

if (!existsSync(APK_PATH)) {
  console.error(`缺少轻语 APK: ${APK_PATH}`);
  console.error('请先运行: node tools/gen-android.js && node tools/build-apk.js');
  process.exit(1);
}
if (!existsSync(TAILSCALE_APK)) {
  console.error(`缺少 Tailscale APK: ${TAILSCALE_APK}`);
  console.error(`请从 ${TAILSCALE_SOURCE_URL} 下载 tailscale-android-universal-${TAILSCALE_VERSION}.apk`);
  console.error(`放到 vendor/ 下,或设 QINGYU_TAILSCALE_APK 指向它。期望摘要: ${TAILSCALE_SHA256}`);
  process.exit(1);
}

const qingyuApk = readFileSync(APK_PATH);
const tailscaleApk = readFileSync(TAILSCALE_APK);
const qingyuSha256 = createHash('sha256').update(qingyuApk).digest('hex');
const tailscaleSha256 = createHash('sha256').update(tailscaleApk).digest('hex');

if (tailscaleSha256 !== TAILSCALE_SHA256) {
  console.error('Tailscale APK 摘要与固定值不一致,拒绝打包:');
  console.error(`  文件   ${TAILSCALE_APK}`);
  console.error(`  实际   ${tailscaleSha256}`);
  console.error(`  期望   ${TAILSCALE_SHA256}`);
  console.error('确认过新版本就同时更新 TAILSCALE_VERSION / TAILSCALE_SHA256 两行。');
  process.exit(1);
}

let token = (process.env.QINGYU_BRIDGE_TOKEN ?? '').trim();
if (!token) {
  if (!existsSync(TOKEN_FILE)) {
    console.error(`没有令牌: ${TOKEN_FILE} 不存在,也没设 QINGYU_BRIDGE_TOKEN。`);
    console.error('令牌 = 手机中转启动时 --token 的那串(见 migration_tools/phone_bridge_token.txt)。');
    process.exit(1);
  }
  token = readFileSync(TOKEN_FILE, 'utf8').trim();
}
if (token.length < 16) {
  console.error(`令牌看起来不对(只有 ${token.length} 个字符),拒绝打包。`);
  process.exit(1);
}

const guide = remoteKitGuide({
  version: VERSION_NAME,
  tailscaleVersion: TAILSCALE_VERSION,
  tailscaleApkFileName: TAILSCALE_APK_NAME,
  qingyuApkFileName: QINGYU_APK_NAME,
  qingyuSha256,
  tailscaleSha256,
  tailnetUrl: TAILSCALE_SERVER_URL,
  machineName: MACHINE_NAME,
  token,
  tailscaleSourceUrl: TAILSCALE_SOURCE_URL,
});

mkdirSync(DIST_DIR, { recursive: true });
const zipPath = `${DIST_DIR}/${ZIP_NAME}`;
const zip = buildZip([
  { name: TAILSCALE_APK_NAME, data: tailscaleApk },
  { name: QINGYU_APK_NAME, data: qingyuApk },
  { name: '使用说明-异地手机.txt', data: Buffer.from(guide, 'utf8') },
]);
writeFileSync(zipPath, zip);

const mb = (n) => `${(n / 1024 ** 2).toFixed(2)} MB`;
console.log('');
console.log(`轻语 APK     ${QINGYU_APK_NAME}  ${mb(qingyuApk.length)}`);
console.log(`             SHA-256 ${qingyuSha256}`);
console.log(`Tailscale    ${TAILSCALE_APK_NAME}  ${mb(tailscaleApk.length)}`);
console.log(`             SHA-256 ${tailscaleSha256}(与官方资产一致)`);
console.log('');
console.log(`包内文件:`);
console.log(`  ${TAILSCALE_APK_NAME}  (${tailscaleApk.length} 字节)`);
console.log(`  ${QINGYU_APK_NAME}  (${qingyuApk.length} 字节)`);
console.log(`  使用说明-异地手机.txt  (${Buffer.byteLength(guide, 'utf8')} 字节)`);
console.log('');
console.log(`异地测试包 ${zipPath}`);
console.log(`      ${statSync(zipPath).size} 字节 / ${mb(statSync(zipPath).size)}`);
console.log(`      未压缩是 ${mb(tailscaleApk.length + qingyuApk.length)};省下来的部分全部来自 Tailscale`);
console.log(`      里那些未压缩存放的 libgojni.so(解压出来的 APK 与官方原包逐字节一致)`);
console.log(`      SHA-256 ${createHash('sha256').update(zip).digest('hex')}`);
console.log('');
console.log('发给对方:把这个 zip 传过去,对方解压得到两个 apk + 一份说明;');
console.log('另外**单独**把 Tailscale 的共享邀请链接发给对方(在 admin console → Machines → Share 里生成)。');
