#!/usr/bin/env node
/**
 * Android 构建工具链的可续传下载器。
 *
 * 正常情况下**不需要跑这个**:工具链已经在磁盘上(见 `lib/toolchain.js`),本脚本
 * 只在换机器/工具链被删掉时用来重建。下载落到 `<工具链>/downloads/`,支持断点续传,
 * 已固定校验和的构件会做 SHA-256 校验。
 *
 * 用法:
 *   node tools/fetch-toolchain.js --list
 *   node tools/fetch-toolchain.js jdk cmdline-tools
 *   node tools/fetch-toolchain.js --all
 *
 * @module qingyu-phone/tools/fetch-toolchain
 */
import { createHash } from 'node:crypto';
import { createWriteStream, createReadStream, existsSync, mkdirSync, statSync, renameSync, unlinkSync } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { TOOLCHAIN_DIR } from '../lib/toolchain.js';

const DOWNLOADS = `${TOOLCHAIN_DIR}/downloads`;

/**
 * @typedef {object} Artifact
 * @property {string} name
 * @property {string} url
 * @property {string} file
 * @property {number} [bytes] - 已固定的期望大小
 * @property {string} [sha256] - 已固定的校验和
 * @property {string} note
 */

/** 固定构件。Adoptium 的 URL 带版本号,所以校验和是可验证的。 */
const ARTIFACTS = {
  jdk: {
    name: 'jdk',
    url: 'https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/OpenJDK17U-jdk_x64_windows_hotspot_17.0.20.1_1.zip',
    file: 'temurin-jdk-17.zip',
    sha256: 'e53a79c3c3d86865bd7e787903884331068e71321714ffd44f145785affc7cb0',
    note: 'Temurin JDK 17(便携版;AGP 8.7.3 需要 17+)',
  },
  'cmdline-tools': {
    name: 'cmdline-tools',
    url: 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip',
    file: 'android-commandlinetools.zip',
    note: 'Android SDK 命令行工具(sdkmanager、platform-tools)',
  },
};

/**
 * 解析 `--flag` 形式的参数。
 * @param argv 脚本之后的参数。
 * @returns 请求内容。
 */
function parseArgs(argv) {
  return {
    all: argv.includes('--all'),
    list: argv.includes('--list'),
    names: argv.filter((arg) => !arg.startsWith('--')),
  };
}

/**
 * 人类可读的字节数。
 * @param bytes 字节数。
 * @returns 简短的显示文本。
 */
function human(bytes) {
  if (bytes > 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes > 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

/**
 * 问服务器构件多大、支不支持续传。
 * @param url 构件地址。
 * @returns 探测结果。
 */
async function probe(url) {
  const res = await fetch(url, { method: 'GET', headers: { range: 'bytes=0-0' } });
  if (res.status === 206) {
    const range = res.headers.get('content-range') ?? '';
    const total = Number(range.split('/')[1]);
    res.body?.cancel();
    return { size: Number.isFinite(total) ? total : null, ranged: true };
  }
  const size = Number(res.headers.get('content-length'));
  res.body?.cancel();
  return { size: Number.isFinite(size) ? size : null, ranged: res.headers.get('accept-ranges') === 'bytes' };
}

/**
 * 算文件的 SHA-256。
 * @param path 文件路径。
 * @returns 小写十六进制摘要。
 */
async function sha256Of(path) {
  const hash = createHash('sha256');
  await pipeline(createReadStream(path), hash);
  return hash.digest('hex');
}

/**
 * 校验下载结果。
 * @param artifact 构件描述。
 * @param path 落盘文件。
 * @returns 校验是否通过。
 */
async function finish(artifact, path) {
  if (artifact.sha256 === undefined) {
    console.log(`${artifact.name}: 未固定校验和,跳过校验(解压阶段会校验结构)`);
    return true;
  }
  const digest = await sha256Of(path);
  if (digest !== artifact.sha256) {
    console.log(`${artifact.name}: SHA-256 不匹配!\n  期望 ${artifact.sha256}\n  实际 ${digest}`);
    return false;
  }
  console.log(`${artifact.name}: SHA-256 校验通过`);
  return true;
}

/**
 * 下载一个构件,服务端允许时续传。
 * @param artifact 构件。
 * @returns 是否就位且校验通过。
 */
async function download(artifact) {
  mkdirSync(DOWNLOADS, { recursive: true });
  const target = `${DOWNLOADS}/${artifact.file}`;
  const partial = `${target}.part`;

  if (existsSync(target)) {
    const size = statSync(target).size;
    if (artifact.sha256 !== undefined) {
      const digest = await sha256Of(target);
      if (digest === artifact.sha256) {
        console.log(`${artifact.name}: 已存在且校验通过 (${human(size)})`);
        return true;
      }
      console.log(`${artifact.name}: 校验不匹配,重新下载`);
      unlinkSync(target);
    } else {
      console.log(`${artifact.name}: 已存在 (${human(size)},未固定校验和)`);
      return true;
    }
  }

  const remote = await probe(artifact.url);
  const total = artifact.bytes ?? remote.size;

  let start = 0;
  if (existsSync(partial)) {
    start = statSync(partial).size;
    if (total !== null && start >= total) {
      renameSync(partial, target);
      console.log(`${artifact.name}: 续传完成 (${human(start)})`);
      return finish(artifact, target);
    }
    if (!remote.ranged) {
      console.log(`${artifact.name}: 服务端不支持续传,从头发起`);
      unlinkSync(partial);
      start = 0;
    }
  }

  console.log(`${artifact.name}: ${start > 0 ? `从 ${human(start)} 续传` : '开始下载'}${total === null ? '' : `,共 ${human(total)}`}`);

  const headers = start > 0 ? { range: `bytes=${start}-` } : {};
  const res = await fetch(artifact.url, { headers });
  if (!res.ok && res.status !== 206) throw new Error(`${artifact.name}: HTTP ${res.status}`);
  if (!res.body) throw new Error(`${artifact.name}: 空响应体`);

  const stream = createWriteStream(partial, { flags: start > 0 ? 'a' : 'w' });
  let seen = start;
  let lastReport = Date.now();
  const source = Readable.fromWeb(res.body);
  source.on('data', (chunk) => {
    seen += chunk.length;
    if (Date.now() - lastReport > 3000) {
      lastReport = Date.now();
      const pct = total === null ? '' : ` ${((seen / total) * 100).toFixed(1)}%`;
      process.stdout.write(`\r  ${artifact.name}: ${human(seen)}${total === null ? '' : ' / ' + human(total)}${pct}   `);
    }
  });
  await pipeline(source, stream);
  process.stdout.write('\r'.padEnd(80) + '\r');

  renameSync(partial, target);
  console.log(`${artifact.name}: 下载完成 (${human(statSync(target).size)})`);
  return finish(artifact, target);
}

const request = parseArgs(process.argv.slice(2));

if (request.list) {
  console.log(`下载目录: ${DOWNLOADS}\n`);
  for (const artifact of Object.values(ARTIFACTS)) {
    const target = `${DOWNLOADS}/${artifact.file}`;
    const state = existsSync(target) ? `已下载 ${human(statSync(target).size)}` : '未下载';
    console.log(`  ${artifact.name.padEnd(14)} ${state.padEnd(20)} ${artifact.note}`);
  }
  process.exit(0);
}

const names = request.all ? Object.keys(ARTIFACTS) : request.names;
if (names.length === 0) {
  console.error('用法: node tools/fetch-toolchain.js [--all | --list | <name>...]');
  console.error(`可用: ${Object.keys(ARTIFACTS).join(', ')}`);
  process.exit(2);
}

const started = Date.now();
const results = [];
for (const name of names) {
  const artifact = ARTIFACTS[name];
  if (artifact === undefined) {
    console.error(`未知构件: ${name}`);
    results.push(false);
    continue;
  }
  try {
    results.push(await download(artifact));
  } catch (error) {
    console.error(`${name}: 失败 — ${error.message}`);
    results.push(false);
  }
}

console.log('');
console.log(`用时 ${Math.round((Date.now() - started) / 1000)} 秒,${results.filter(Boolean).length}/${results.length} 成功`);
process.exit(results.every(Boolean) ? 0 : 1);
