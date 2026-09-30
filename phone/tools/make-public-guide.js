#!/usr/bin/env node
/**
 * 生成**公开版**的《使用说明-异地手机.txt`:`dist/使用说明-异地手机-公开版.txt`。
 *
 * 为什么单独有一个工具:异地测试包里那份说明是**带你令牌**的(发给某一个具体的人),
 * 而 Release 资产是公开的 —— 公开版必须把那一步改成"作者单独发给你"。
 * 两份说明共用 `remoteKitGuide()`,所以除了令牌那一段,内容永远一致。
 *
 * 用法: node tools/make-public-guide.js
 *
 * @module qingyu-phone/tools/make-public-guide
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { APP_LABEL, TAILSCALE_SERVER_URL, VERSION_NAME } from '../lib/android-project.js';
import { remoteKitGuide } from '../lib/dist-docs.js';
import { ROOT } from '../lib/toolchain.js';

const TAILSCALE_VERSION = '1.102.4';
const TAILSCALE_SHA256 = '7ecfb863e08f5fbd1ecd70235d8c34ba135a4124bdd8e166b9d6fb962782e0b5';
const TAILSCALE_SOURCE_URL = 'https://github.com/tailscale/tailscale-android/releases';
const MACHINE_NAME = 'laptop-u3hj61a7';

const OUT_DIR = `${ROOT}/dist`;
const OUT = `${OUT_DIR}/install-guide-remote-test.txt`;

/**
 * Release 资产名只能是 ASCII:GitHub 会把上传文件名里的非 ASCII 字符**替换成 `-` 与 `.`**
 * (实测 `轻语-手机端-v0.2.0.apk` 变成了 `-.-v0.2.0.apk`),所以公开版说明里的文件名
 * 必须跟 Release 上那三个 ASCII 名字一致。zip 里的中文名不受影响。
 */
const QINGYU_ASSET = `qingyu-phone-v${VERSION_NAME}.apk`;
const TAILSCALE_ASSET = `tailscale-android-${TAILSCALE_VERSION}.apk`;

const guide = remoteKitGuide({
  version: VERSION_NAME,
  tailscaleVersion: TAILSCALE_VERSION,
  tailscaleApkFileName: TAILSCALE_ASSET,
  qingyuApkFileName: QINGYU_ASSET,
  // 公开版不写轻语 APK 的摘要:它在 Release 上,值随构建变,写在正文里容易过期
  qingyuSha256: '(见 Release 页面上的 assets 与下面的校验值)',
  tailscaleSha256: TAILSCALE_SHA256,
  tailnetUrl: TAILSCALE_SERVER_URL,
  machineName: MACHINE_NAME,
  token: '', // 空 = 公开版:那一步改成"作者会单独发给你"
  tailscaleSourceUrl: TAILSCALE_SOURCE_URL,
});

mkdirSync(OUT_DIR, { recursive: true });
writeFileSync(OUT, guide, 'utf8');
console.log(`公开版说明 ${OUT}`);
console.log(`      ${Buffer.byteLength(guide, 'utf8')} 字节`);
console.log(`      含"令牌"字样: ${guide.includes('令牌填这一串') ? '是(不对,公开版不该有)' : '否 ✓'}`);
