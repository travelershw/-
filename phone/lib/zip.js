/**
 * 最小 ZIP 写入器(零依赖)。
 *
 * 为什么不用 PowerShell 的 `Compress-Archive`:这台机器上 Node 起子进程有两条已知的坑 ——
 * spawn `.bat` 会 EINVAL,受限沙箱下带管道 stdio 的子进程会 EPERM。既然本工程的原则是
 * "零依赖",最稳的就是用 `zlib.deflateRawSync` 自己写:本地头 + 中央目录 + EOCD,
 * 顺便把中文文件名标上 UTF-8 标志位,让 Windows 资源管理器和手机的压缩 App 都能正常显示。
 *
 * 抽出来是因为有两个使用者:分发包(`tools/make-dist.js`)与异地测试包
 * (`tools/make-remote-kit.js`)。后者要靠**重新压缩**把 100 MB 的 Tailscale 官方 APK
 * 变小 —— 那个 APK 里 97 MB 是**未压缩**存放的 `libgojni.so`,deflate 一遍能省掉一半,
 * 而 APK 本身逐字节不变(解压出来还是官方原包,签名照样有效)。
 *
 * @module qingyu-phone/lib/zip
 */
import { deflateRawSync } from 'node:zlib';

/** CRC-32 查表(按需构建一次)。 */
let crcTable = null;

/**
 * 算 CRC-32(ZIP 要求)。
 * @param {Buffer} buffer 数据。
 * @returns {number} 无符号 32 位校验值。
 */
function crc32(buffer) {
  if (crcTable === null) {
    crcTable = new Int32Array(256);
    for (let i = 0; i < 256; i += 1) {
      let value = i;
      for (let bit = 0; bit < 8; bit += 1) {
        value = (value & 1) !== 0 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
      }
      crcTable[i] = value;
    }
  }
  let crc = 0xffffffff;
  for (let i = 0; i < buffer.length; i += 1) {
    crc = (crc >>> 8) ^ crcTable[(crc ^ buffer[i]) & 0xff];
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/**
 * 把日期转成 DOS 时间/日期(ZIP 头里用的老格式)。
 * @param {Date} date 时间。
 * @returns {{time: number, date: number}} DOS 字段。
 */
function dosStamp(date) {
  const year = Math.max(1980, date.getFullYear());
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  };
}

/**
 * 生成一个 ZIP 包。
 *
 * @param {Array<{name: string, data: Buffer}>} entries 条目(名字 + 内容)。
 * @param {{level?: number}} [options] 压缩级别(默认 9,越大越慢越省地方)。
 * @returns {Buffer} 完整 zip。
 */
export function buildZip(entries, options = {}) {
  const level = options.level ?? 9;
  const stamp = dosStamp(new Date());
  const chunks = [];
  const centralParts = [];
  let offset = 0;

  for (const entry of entries) {
    const nameBytes = Buffer.from(entry.name, 'utf8');
    const data = entry.data;
    const compressed = deflateRawSync(data, { level });
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);        // 本地文件头签名
    local.writeUInt16LE(20, 4);                 // 需要的版本 2.0
    local.writeUInt16LE(0x0800, 6);             // 标志位:文件名是 UTF-8
    local.writeUInt16LE(8, 8);                  // 压缩方式:deflate
    local.writeUInt16LE(stamp.time, 10);
    local.writeUInt16LE(stamp.date, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);                 // 扩展区长度
    chunks.push(local, nameBytes, compressed);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);      // 中央目录签名
    central.writeUInt16LE(20, 4);              // 制作版本
    central.writeUInt16LE(20, 6);              // 需要的版本
    central.writeUInt16LE(0x0800, 8);          // 同样的 UTF-8 标志
    central.writeUInt16LE(8, 10);
    central.writeUInt16LE(stamp.time, 12);
    central.writeUInt16LE(stamp.date, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(compressed.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30);              // 扩展区
    central.writeUInt16LE(0, 32);              // 注释
    central.writeUInt16LE(0, 34);              // 起始磁盘
    central.writeUInt16LE(0, 36);              // 内部属性
    central.writeUInt32LE(0, 38);              // 外部属性
    central.writeUInt32LE(offset, 42);         // 本地头偏移
    centralParts.push(central, nameBytes);

    offset += local.length + nameBytes.length + compressed.length;
  }

  const centralBuf = Buffer.concat(centralParts);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);           // EOCD 签名
  eocd.writeUInt16LE(0, 4);                    // 本磁盘号
  eocd.writeUInt16LE(0, 6);                    // 中央目录起始磁盘
  eocd.writeUInt16LE(entries.length, 8);       // 本磁盘条目数
  eocd.writeUInt16LE(entries.length, 10);      // 总条目数
  eocd.writeUInt32LE(centralBuf.length, 12);   // 中央目录大小
  eocd.writeUInt32LE(offset, 16);              // 中央目录偏移
  eocd.writeUInt16LE(0, 20);                   // 注释长度

  return Buffer.concat([...chunks, centralBuf, eocd]);
}
