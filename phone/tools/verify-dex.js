#!/usr/bin/env node
/**
 * 静态校验 APK 的内容:dex 里到底有没有我们的类,页面到底有没有被打包进去。
 *
 * 为什么不用 `dexdump.exe`:`dexdump` 读不了 zip 里的 `.dex`
 * (`mem_map_windows.cc: Couldn't get file size`),而在受限沙箱下 Node 起子进程
 * 用管道 stdio 会被拒(EPERM)。自己读 ZIP + `zlib` 解压两样都不需要:十几行代码、
 * 零依赖,而且正好回答唯一还没答案的问题 —— javac/d8 究竟把我们的类放进产物了没有,
 * 还是 manifest 只是"声称"它存在。
 *
 * 页面那一项同理:`assets/index.html` 必须真的在包里,而且必须是**没被搞坏编码**的
 * UTF-8(页面里全是中文,编码错了会变成乱码,构建却照样成功)。
 *
 * 用法: node tools/verify-dex.js [apk路径]
 *
 * @module qingyu-phone/tools/verify-dex
 */
import { readFileSync } from 'node:fs';
import { inflateRawSync } from 'node:zlib';
import { APPLICATION_ID } from '../lib/android-project.js';
import { APK_PATH } from '../lib/toolchain.js';

const apk = process.argv[2] ?? APK_PATH;
const buffer = readFileSync(apk);

// --- 极简 ZIP 读取 ----------------------------------------------------------
// 走中央目录(签名 0x02014b50)而不是扫本地头:只有中央目录记录了真实大小。
const EOCD_SIGNATURE = 0x06054b50;
let eocd = -1;
for (let i = buffer.length - 22; i >= 0 && i > buffer.length - 65558; i -= 1) {
  if (buffer.readUInt32LE(i) === EOCD_SIGNATURE) {
    eocd = i;
    break;
  }
}
if (eocd === -1) {
  console.error('FAIL: 不是 zip 包(找不到 end-of-central-directory 记录)');
  process.exit(1);
}

const entryCount = buffer.readUInt16LE(eocd + 10);
let offset = buffer.readUInt32LE(eocd + 16);

/**
 * @typedef {object} ZipEntry
 * @property {string} name
 * @property {number} method
 * @property {number} compressedSize
 * @property {number} size
 * @property {number} localHeaderOffset
 */

/** @type {ZipEntry[]} */
const entries = [];
for (let i = 0; i < entryCount; i += 1) {
  if (buffer.readUInt32LE(offset) !== 0x02014b50) break;
  const nameLength = buffer.readUInt16LE(offset + 28);
  const extraLength = buffer.readUInt16LE(offset + 30);
  const commentLength = buffer.readUInt16LE(offset + 32);
  entries.push({
    name: buffer.toString('utf8', offset + 46, offset + 46 + nameLength),
    method: buffer.readUInt16LE(offset + 10),
    compressedSize: buffer.readUInt32LE(offset + 20),
    size: buffer.readUInt32LE(offset + 24),
    localHeaderOffset: buffer.readUInt32LE(offset + 42),
  });
  offset += 46 + nameLength + extraLength + commentLength;
}

/**
 * 取出一个条目,deflate 的顺手解压。
 * @param {ZipEntry} entry 条目。
 * @returns {Buffer} 条目内容。
 */
function extract(entry) {
  // 本地头里重复了名字/扩展区长度,不保证与中央目录一致,所以数据偏移按本地头算。
  const local = entry.localHeaderOffset;
  const nameLength = buffer.readUInt16LE(local + 26);
  const extraLength = buffer.readUInt16LE(local + 28);
  const start = local + 30 + nameLength + extraLength;
  const raw = buffer.subarray(start, start + entry.compressedSize);
  return entry.method === 0 ? Buffer.from(raw) : inflateRawSync(raw);
}

const failures = [];
const ok = (label, detail) => console.log(`ok   ${label}${detail ? `  (${detail})` : ''}`);
const bad = (label, detail) => {
  console.log(`FAIL ${label}${detail ? `  (${detail})` : ''}`);
  failures.push(label);
};

console.log(`APK    ${apk}`);
console.log(`条目   ${entries.length} 个`);
console.log('');

// --- 1. dex 与类 -----------------------------------------------------------------
const dexEntries = entries.filter((entry) => /^classes\d*\.dex$/.test(entry.name));
if (dexEntries.length === 0) {
  console.error('FAIL: APK 里没有 classes.dex');
  process.exit(1);
}
console.log(`dex 文件: ${dexEntries.map((entry) => entry.name).join(', ')}`);
console.log('');

/** @type {Buffer[]} */
const dexFiles = [];
for (const entry of dexEntries) {
  const dex = extract(entry);
  const header = dex.toString('ascii', 0, 8);
  console.log(`  ${entry.name}: ${dex.length} 字节, magic=${JSON.stringify(header.slice(0, 4))}`);
  if (!header.startsWith('dex\n')) {
    failures.push(`${entry.name} 不是 DEX 文件`);
    continue;
  }
  dexFiles.push(dex);
}

// DEX 里的类型名是 modified UTF-8 描述符,类名会长成 `Ldev/qingyu/phone/MainActivity;`。
// 直接在解压后的字节里搜,就不用为了回答一个问题去实现 DEX 解析器。
//
// 检查的是**所有 dex 的并集**,不是逐个文件查:minSdk 24 下也能分裂出多个 dex,
// 我们的类可能落在 classes2.dex 里,要求每个 dex 都包含每个类会误杀好构建。
const searchable = Buffer.concat(dexFiles);
const packagePath = APPLICATION_ID.replace(/\./g, '/');
const wantedClasses = [
  `L${packagePath}/MainActivity;`,
  `L${packagePath}/QingyuNative;`,
  // 阶段 2 新增:录音(强制蓝牙耳机麦)与拍照
  `L${packagePath}/QingyuAudio;`,
  // 播放她的话要用原生 AudioTrack(Web Audio 走媒体流,出不了耳机)
  `L${packagePath}/QingyuPlayer;`,
  `L${packagePath}/QingyuCamera;`,
  // 后台保活前台服务
  `L${packagePath}/QingyuService;`,
  'Landroid/app/Service;',
  'Landroid/app/Notification$Builder;',
  'Landroid/webkit/WebView;',
  'Landroid/media/AudioRecord;',
  'Landroid/media/AudioTrack;',
  'Landroid/media/AudioAttributes;',
  'Landroidx/core/content/FileProvider;',
];
for (const descriptor of wantedClasses) {
  const present = searchable.includes(Buffer.from(descriptor, 'utf8'));
  const where = present
    ? dexEntries.filter((entry, index) => dexFiles[index]?.includes(Buffer.from(descriptor, 'utf8'))).map((entry) => entry.name)
    : [];
  if (present) {
    ok(`dex 里有 ${descriptor}`, where.join(', '));
  } else {
    bad(`dex 里有 ${descriptor}`, '构建成功但类不在产物里,多半是被裁掉了');
  }
}

// --- 2. 桥的方法名必须真的在 dex 里 -------------------------------------------------
// 类在 dex 里不等于方法在:接口名字写错会在运行时报 "Java exception was raised" 而不是
// 构建失败。这里直接在 dex 的字符串表里找方法名与关键常量。
console.log('');
const wantedStrings = [
  ['startRecording', '录音入口'],
  ['stopRecording', '停止录音'],
  ['getAudioRoute', '路由查询'],
  ['probeSco', 'SCO 独立试验'],
  ['setScoMode', 'SCO 方式切换'],
  ['getScoMode', 'SCO 方式读取'],
  ['requestBtPermission', '蓝牙权限申请'],
  ['setAllowPreferredDevice', '档3 开关'],
  ['setCommunicationDevice', '新接口(档1)'],
  ['startBluetoothSco', '老接口(档2)'],
  ['setPreferredDevice', '档3 首选设备'],
  ['onRouteAttempt', '档位尝试回调'],
  ['getProfileConnectionState', 'HFP/A2DP 直查'],
  ['closeProfileProxy', 'HFP 代理释放'],
  ['android.permission.BLUETOOTH_SCAN', 'BLUETOOTH_SCAN 常量'],
  // 设备类型映射必须真的编进去:现场 dump 里出现 TYPE_25/TYPE_21 时人根本看不懂那是什么
  ['TYPE_BLUETOOTH_SCO(7,蓝牙SCO)', '类型映射 蓝牙SCO'],
  ['TYPE_BLUETOOTH_A2DP(8,蓝牙A2DP)', '类型映射 蓝牙A2DP'],
  ['TYPE_BUILTIN_EARPIECE(1,听筒)', '类型映射 听筒'],
  ['TYPE_REMOTE_SUBMIX(25,远端混音(虚拟))', '类型映射 远端混音'],
  ['TYPE_BUS(21,总线(虚拟))', '类型映射 总线'],
  ['TYPE_BLE_HEADSET(26,BLE耳机)', '类型映射 BLE耳机'],
  ['TYPE_HEARING_AID(23,助听器)', '类型映射 助听器'],
  ['未就绪(蓝牙代理还没连上', 'HFP 代理未就绪提示'],
  ['startKeepAlive', '保活启动'],
  ['stopKeepAlive', '保活停止'],
  ['startForegroundService', '前台服务启动'],
  ['qingyu-keepalive', '前台服务通知渠道'],
  ['轻语 · 正在陪着你说话', '常驻通知标题'],
  ['dev.qingyu.phone.action.STOP', '通知上的停止动作'],
  ['android.permission.POST_NOTIFICATIONS', 'Android 13+ 通知权限常量'],
  ['currentMic', '当前麦克风字段'],
  // 播放她的话:必须用原生 AudioTrack + 通话用法,否则声音出不了耳机
  ['startPlayback', '原生播放启动'],
  ['writePlayback', '原生播放写数据'],
  ['finishPlayback', '原生播放收尾'],
  ['playbackRemainingMs', '原生播放剩余时长'],
  // 注意:AudioAttributes.USAGE_VOICE_COMMUNICATION 是 public static final int,
  // javac 会把它**内联**掉,dex 里就没有这个字段名了 —— 所以查我们自己那个字符串字面量
  ['voice_communication', '播放用通话用法(进耳机)'],
  ['playerRoute', '播放输出设备字段'],
  ['findWiredHeadsetInput', '有线/USB 耳麦优先'],
];
for (const [needle, label] of wantedStrings) {
  if (searchable.includes(Buffer.from(needle, 'utf8'))) {
    ok(`dex 含 ${needle}`, label);
  } else {
    bad(`dex 含 ${needle}`, label);
  }
}

// --- 3. 打包进去的本地页面 --------------------------------------------------------
console.log('');
const assetEntry = entries.find((entry) => entry.name === 'assets/index.html');
if (assetEntry === undefined) {
  bad('APK 里有 assets/index.html', '页面没被打包,应用会白屏');
} else {
  const raw = extract(assetEntry);
  ok('APK 里有 assets/index.html', `${raw.length} 字节`);

  let html = null;
  try {
    // fatal: true —— 编码坏掉时直接抛,而不是悄悄塞一堆 U+FFFD
    html = new TextDecoder('utf-8', { fatal: true }).decode(raw);
    ok('页面是合法 UTF-8');
  } catch (error) {
    bad('页面是合法 UTF-8', `解码失败:${error.message}`);
  }

  if (html !== null) {
    // 阶段 2 的断言:录音、拍照、路由显示、排队播放这几条链路在页面里必须都有落点
    const markers = [
      ['桥名 window.QingyuNative', 'QingyuNative'],
      ['默认地址(Tailscale)', 'laptop-u3hj61a7.tail35209a.ts.net'],
      ['引用了 pure.js', 'src="pure.js"'],
      ['开始对话按钮(开关式)', '开始对话'],
      ['单句按钮', '说一句'],
      ['调原生开对话模式', 'startConversation'],
      ['对话模式切句参数', 'CONV_SILENCE_MS'],
      ['原生播放她的话', 'startPlayback'],
      ['原生播放收尾', 'finishPlayback'],
      ['调原生开始录音', 'startRecording'],
      ['调原生停止录音', 'stopRecording'],
      ['读原生音频路由', 'getAudioRoute'],
      ['调原生拍照', 'capturePhoto'],
      ['原生录音回调 onAudio', 'onAudio'],
      ['原生电平回调 onLevel', 'onLevel'],
      ['原生拍照回调 onPhoto', 'onPhoto'],
      ['原生权限回调 onPermission', 'onPermission'],
      ['申请蓝牙权限按钮', 'requestBtPermission'],
      ['一进页面就查路由', "logRoute('进入页面')"],
      ['按下说话前先记路由', "logRoute('按下前')"],
      ['诊断清单渲染', 'summarizeRoute'],
      ['耳机诊断结论行', 'r-verdict'],
      ['原生主动回报路由', 'onRoute'],
      ['档位尝试回调', 'onRouteAttempt'],
      ['SCO 方式下拉框', 'setScoMode'],
      ['档3 开关', 'setAllowPreferredDevice'],
      ['复制诊断按钮', 'copyText'],
      ['诊断全文取用', 'route.diagnostics'],
      ['SCO 试验按钮', 'probeSco'],
      ['SCO 试验结果区', 'r-probe'],
      ['自动降级到手机麦', 'autoDowngradeIfNeeded'],
      ['断线自动重连', 'scheduleReconnect'],
      ['回前台立刻重连', 'visibilitychange'],
      ['连接时开保活', 'startKeepAlive'],
      ['断开时关保活', 'stopKeepAlive'],
      ['华为耗电管理提示', 'batteryHint'],
      ['地址预设:家里(Tailscale)', 'presetTailscale'],
      ['地址预设:同一个 WiFi', 'presetLan'],
      ['首次使用引导卡片', 'guideCard'],
      ['引导可关闭', 'qingyu.guideDismissed'],
      ['令牌留空说明(tailnet)', 'tailnet 身份认证'],
      ['页面显示版本号', 'appVersion'],
      ['本次录音结果回显', 'showLastRecording'],
      ['握手帧处理', 'audio_begin'],
      ['Web Audio 排队播放', 'createBuffer'],
      ['audio_end 统计', 'first_audio_ms'],
      ['用到上行秒数(协议 secs)', 'frame.secs'],
    ];
    for (const [label, marker] of markers) {
      if (html.includes(marker)) {
        ok(`页面含${label}`);
      } else {
        bad(`页面含${label}`, `找不到 ${JSON.stringify(marker)}`);
      }
    }

    // --- 3. 页面脚本必须能解析,而且取的元素 id 必须真的存在 -------------------------
    // 这两条是最容易"构建成功但一打开就白屏"的原因:脚本里一个拼错的括号,或者
    // el.address 取到一个不存在的 id,页面就直接死了,而 Gradle 一声不响。
    const scriptMatch = /<script>([\s\S]*?)<\/script>/.exec(html);
    if (scriptMatch === null) {
      bad('页面里有 script 块');
    } else {
      const script = scriptMatch[1];
      try {
        // 只编译不执行:和浏览器一样在同一个 JS 引擎里过一遍语法
        new Function(script);
        ok('页面脚本语法正确', `${script.length} 字符`);
      } catch (error) {
        bad('页面脚本语法正确', String(error.message));
      }

      const definedIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
      const referenced = new Set([
        ...[...script.matchAll(/el\['([^']+)'\]/g)].map((match) => match[1]),
        ...[...script.matchAll(/\bel\.([A-Za-z_][A-Za-z0-9_]*)/g)].map((match) => match[1]),
      ]);
      const missing = [...referenced].filter((id) => !definedIds.has(id));
      if (missing.length === 0) {
        ok('页面取用的元素 id 都存在', `${referenced.size} 个`);
      } else {
        bad('页面取用的元素 id 都存在', `页面上没有: ${missing.join(', ')}`);
      }
    }
  }
}

// --- 4. 页面的纯逻辑必须跟着一起打包,而且语法正确 ------------------------------------
// pure.js 是页面封 WAV / 组帧用的那份代码,同时也是 tools/page-protocol-test.js
// 直接 require 的那一份。它要是没进包,页面会静默降级成"录不了音",极难查。
const pureEntry = entries.find((entry) => entry.name === 'assets/pure.js');
if (pureEntry === undefined) {
  bad('APK 里有 assets/pure.js', '页面的纯逻辑没打包,录音/拍照都发不出去');
} else {
  const raw = extract(pureEntry);
  ok('APK 里有 assets/pure.js', `${raw.length} 字节`);
  let code = null;
  try {
    code = new TextDecoder('utf-8', { fatal: true }).decode(raw);
    ok('pure.js 是合法 UTF-8');
  } catch (error) {
    bad('pure.js 是合法 UTF-8', `解码失败:${error.message}`);
  }
  if (code !== null) {
    try {
      new Function(code);
      ok('pure.js 语法正确');
    } catch (error) {
      bad('pure.js 语法正确', String(error.message));
    }
    for (const name of ['pcmToWav', 'buildUtteranceFrame', 'buildImageFrame', 'base64ToBytes', 'mergeChunks',
                        'looksLikeHeadset', 'summarizeRoute', 'scoModeText', 'probeSummary',
                        'bluetoothInputAvailable', 'shouldAutoDowngrade', 'autoDowngradeNotice', 'nextReconnectDelay',
                        'buildLanPreset', 'shouldShowGuide', 'TAILSCALE_URL']) {
      if (code.includes(`${name}:`)) {
        ok(`pure.js 导出 ${name}`);
      } else {
        bad(`pure.js 导出 ${name}`);
      }
    }
    // 上行帧的字段名必须与 phone_bridge.py 一致,少一个字段家里就认不出来
    for (const field of ["type: 'utterance'", 'sample_rate', 'secs']) {
      if (code.includes(field)) {
        ok(`pure.js 的 utterance 帧含 ${field}`);
      } else {
        bad(`pure.js 的 utterance 帧含 ${field}`);
      }
    }
    // 现场最需要一眼看到的那两行(麦克风 / 自动降级)必须在渲染逻辑里
    for (const needle of ['当前使用的麦克风', 'currentMic', 'autoDowngradeNotice', 'nextReconnectDelay']) {
      if (code.includes(needle)) {
        ok(`pure.js 含 ${needle}`);
      } else {
        bad(`pure.js 含 ${needle}`);
      }
    }
  }
}

console.log('');
if (failures.length > 0) {
  console.log('FAILURES:');
  for (const line of failures) console.log(`  ${line}`);
  process.exit(1);
}
console.log('PASS: 我们的类与本地页面都真的在 APK 里');
