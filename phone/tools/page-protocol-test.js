#!/usr/bin/env node
/**
 * 页面纯逻辑的自动测试 —— 阶段 2 里**唯一不需要手机就能跑**的验证。
 *
 * 录音、拍照、蓝牙 SCO 这些都要真机;但"录上来的 PCM 怎么封成 WAV"和"发给家里的
 * JSON 帧长什么样"是纯计算,错了现场很难看出是格式问题还是路由问题。这里把
 * `lib/page/pure.js` 的每个函数按家里 phone_bridge.py 的期望逐字段断言一遍。
 *
 * 用法: node tools/page-protocol-test.js
 *
 * @module qingyu-phone/tools/page-protocol-test
 */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = fileURLToPath(new URL('..', import.meta.url));
const pure = require(`${ROOT}lib/page/pure.js`);

const failures = [];

/**
 * 断言一项,并打印结果。
 * @param {string} label 检查项。
 * @param {boolean} ok 是否通过。
 * @param {string} [detail] 补充信息。
 */
function check(label, ok, detail) {
  if (ok) {
    console.log(`ok   ${label}${detail ? `  (${detail})` : ''}`);
  } else {
    console.log(`FAIL ${label}${detail ? `  (${detail})` : ''}`);
    failures.push(label);
  }
}

/**
 * 断言两个值相等。
 * @param {string} label 检查项。
 * @param {unknown} actual 实际值。
 * @param {unknown} expected 期望值。
 */
function equal(label, actual, expected) {
  check(label, actual === expected, `实际 ${JSON.stringify(actual)} / 期望 ${JSON.stringify(expected)}`);
}

/** 造一段确定性的假 PCM(不是随机数:失败时要能复现)。 */
function fakePcm(byteLength) {
  const bytes = new Uint8Array(byteLength);
  for (let i = 0; i < byteLength; i += 1) {
    // 16 bit 小端里放一个缓慢变化的正弦样值,方便人工看波形
    const sample = Math.round(12000 * Math.sin((i / 2) * 0.05));
    bytes[i] = sample & 0xff;
    if (i + 1 < byteLength && i % 2 === 0) { bytes[i + 1] = (sample >> 8) & 0xff; }
  }
  return bytes;
}

console.log(`被测模块 ${ROOT}lib/page/pure.js`);
console.log('');

// --- 1. WAV 头 -----------------------------------------------------------------
// 100 ms = 16000 * 0.1 * 2 字节 = 3200 字节;1.2 秒 = 38400 字节。这两个长度就是
// 录音线程每块的大小和一次典型说话的时长。
console.log('WAV 封装(16 kHz / 单声道 / 16 bit)');
const chunk100ms = fakePcm(3200);
const wav = pure.pcmToWav(chunk100ms, 16000, 1);
const header = pure.readWavHeader(wav);

equal('RIFF 魔数', header.riff, 'RIFF');
equal('WAVE 魔数', header.wave, 'WAVE');
equal('fmt 块标记', header.fmt, 'fmt ');
equal('fmt 块长度 16', header.fmtSize, 16);
equal('编码格式 1(PCM)', header.audioFormat, 1);
equal('声道数 1', header.channels, 1);
equal('采样率 16000', header.sampleRate, 16000);
equal('字节率 32000', header.byteRate, 32000);
equal('块对齐 2 字节', header.blockAlign, 2);
equal('位深 16', header.bitsPerSample, 16);
equal('data 块标记', header.data, 'data');
equal('data 长度 = PCM 长度', header.dataSize, 3200);
equal('文件总长 = 44 + PCM 长度', wav.byteLength, 44 + 3200);
equal('RIFF 块大小 = 36 + PCM 长度', header.riffSize, 36 + 3200);

// 采样数据必须原样落在 44 字节之后,不能错位或多一层转换
const payload = new Uint8Array(wav, 44);
let payloadIdentical = payload.length === chunk100ms.length;
for (let i = 0; payloadIdentical && i < payload.length; i += 1) {
  payloadIdentical = payload[i] === chunk100ms[i];
}
check('PCM 数据原样落位(偏移 44)', payloadIdentical);

// 1.2 秒:家里报的 secs 就是这个量级,长度字段必须跟着走
const wavLong = pure.pcmToWav(fakePcm(38400), 16000, 1);
const headerLong = pure.readWavHeader(wavLong);
equal('1.2 秒样本的 data 长度', headerLong.dataSize, 38400);
equal('1.2 秒样本的文件总长', wavLong.byteLength, 38444);
equal('1.2 秒样本的 RIFF 块大小', headerLong.riffSize, 38436);

// 空录音:头的长度字段不能变成负数或 NaN
const emptyHeader = pure.readWavHeader(pure.pcmToWav(new Uint8Array(0), 16000, 1));
equal('空录音的 data 长度 0', emptyHeader.dataSize, 0);
equal('空录音的文件总长 44', pure.pcmToWav(new Uint8Array(0), 16000, 1).byteLength, 44);

// --- 2. 上行帧 ------------------------------------------------------------------
console.log('');
console.log('上行帧(与 phone_bridge.py 对齐)');

const utterance = pure.buildUtteranceFrame(chunk100ms, 16000);
equal('utterance.type', utterance.json.type, 'utterance');
equal('utterance.sample_rate', utterance.json.sample_rate, 16000);
equal('utterance.channels', utterance.json.channels, 1);
equal('utterance.secs(100 ms 块)', utterance.json.secs, 0.1);
check('utterance 只带 4 个字段', Object.keys(utterance.json).length === 4, Object.keys(utterance.json).join(','));
equal('utterance 二进制是 wav', new DataView(utterance.binary).getUint32(0, false) === 0x52494646, true);

const utteranceLong = pure.buildUtteranceFrame(fakePcm(38400), 16000);
equal('utterance.secs(1.2 秒)', utteranceLong.json.secs, 1.2);
equal('秒数换算 16000 采样/秒', pure.secondsOf(32000, 16000, 1), 1);

const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0xd9]);
const image = pure.buildImageFrame(jpeg, '看看这个');
equal('image.type', image.json.type, 'image');
equal('image.mime', image.json.mime, 'image/jpeg');
equal('image.text', image.json.text, '看看这个');
check('image 只带 3 个字段', Object.keys(image.json).length === 3, Object.keys(image.json).join(','));
equal('image 二进制长度不变', new Uint8Array(image.binary).length, jpeg.length);
equal('image 保留 JPEG 起始标记', new Uint8Array(image.binary)[0], 0xff);
equal('image 保留 JPEG 结束标记', new Uint8Array(image.binary)[jpeg.length - 1], 0xd9);
// 交出去的二进制必须是副本:页面后面还会复用同一块内存(比如重发)
jpeg[0] = 0x00;
equal('image 二进制是副本,不受源数据改动影响', new Uint8Array(image.binary)[0], 0xff);

// --- 3. base64 往返 -------------------------------------------------------------
console.log('');
console.log('base64 互转(桥用 base64 递 PCM / JPEG)');

for (const length of [0, 1, 2, 3, 3200, 3201, 4321]) {
  const source = new Uint8Array(length);
  for (let i = 0; i < length; i += 1) { source[i] = (i * 37 + 11) & 0xff; }
  const text = pure.bytesToBase64(source);
  const back = pure.base64ToBytes(text);
  let same = back.length === length;
  for (let i = 0; same && i < length; i += 1) { same = back[i] === source[i]; }
  check(`base64 往返一致(${length} 字节)`, same, `base64 ${text.length} 字符`);
}

// 原生 android.util.Base64 在长数据上会插入换行,解码端必须容忍
const padded = pure.bytesToBase64(chunk100ms).replace(/(.{76})/g, '$1\n');
check('解码容忍换行(NOWRAP 之外的实现)', pure.base64ToBytes(padded).length === 3200);

// --- 4. 分块拼接与电平 -----------------------------------------------------------
console.log('');
console.log('分块拼接与电平换算');

const merged = pure.mergeChunks([new Uint8Array([1, 2]), new Uint8Array([3]), new Uint8Array([]), new Uint8Array([4, 5, 6])]);
check('分块按顺序拼接', merged.length === 6 && merged.join(',') === '1,2,3,4,5,6', merged.join(','));

// 10 块 100 ms(1 秒)是"按住说话"的典型路径
const tenChunks = [];
for (let i = 0; i < 10; i += 1) { tenChunks.push(chunk100ms); }
equal('10 块拼接后长度', pure.mergeChunks(tenChunks).length, 32000);

equal('电平 -60 dBFS → 0', pure.levelToRatio(-60), 0);
equal('电平 0 dBFS → 1', pure.levelToRatio(0), 1);
equal('电平 -30 dBFS → 0.5', pure.levelToRatio(-30), 0.5);
equal('电平超出下限被夹住', pure.levelToRatio(-200), 0);
equal('电平非数字 → 0', pure.levelToRatio(NaN), 0);

// --- 5. 耳机诊断清单 --------------------------------------------------------------
// 真机上"看不到耳机"是高频问题,判断逻辑必须可测。下面几份 route 都是现场会遇到的样子。
console.log('');
console.log('耳机诊断清单(summarizeRoute)');

/** 造一份 getAudioRoute() 的返回值,只写关心的字段。 */
function routeOf(overrides) {
  return Object.assign({
    recording: false,
    mode: 'MODE_NORMAL',
    scoOn: false,
    communicationDevice: '',
    routedDevice: '',
    btEnabled: true,
    headsetPermission: 'granted',
    audioPermission: 'granted',
    availableCommunication: [],
    connectedHeadsets: [],
    bondedDevices: [],
    inputDevices: [],
    outputDevices: [],
    sdkInt: 34,
  }, overrides);
}

/** 取清单里的某一行。 */
function row(view, label) {
  return view.rows.find((item) => item.label === label);
}

check('WH-CH520(带类型后缀)被认成耳机', pure.looksLikeHeadset('WH-CH520 (TYPE_BLUETOOTH_SCO)'));
check('Bluetooth 字样被认成耳机', pure.looksLikeHeadset('Bluetooth headset'));
check('手机内置麦不算耳机', !pure.looksLikeHeadset('Built-in Mic (TYPE_BUILTIN_MIC)'));
check('空名字不算耳机', !pure.looksLikeHeadset(''));

// 情况 1:一切正常 —— 耳机可用
const okView = pure.summarizeRoute(routeOf({
  availableCommunication: ['WH-CH520 (TYPE_BLUETOOTH_SCO)', 'Built-in Speaker (TYPE_BUILTIN_SPEAKER)'],
  connectedHeadsets: ['WH-CH520'],
  bondedDevices: ['WH-CH520'],
}));
equal('耳机可用时结论是 ok', okView.verdict.level, 'ok');
check('结论里带上耳机名', okView.verdict.text.includes('WH-CH520'), okView.verdict.text);
equal('可用通信设备行是 ok', row(okView, '可用通信设备(API31+)').level, 'ok');
equal('已连接耳机行是 ok', row(okView, '已连接耳机').level, 'ok');

// 情况 2:Android 12+ 但系统报不出可用通话设备(耳机没开 HFP)—— 就是现场那个毛病
const noHfpView = pure.summarizeRoute(routeOf({
  availableCommunication: ['Built-in Speaker (TYPE_BUILTIN_SPEAKER)'],
  bondedDevices: ['WH-CH520'],
}));
equal('没有可用通话设备时结论是 bad', noHfpView.verdict.level, 'bad');
check('结论里给出通话音频指引', noHfpView.verdict.text.includes('通话音频'), noHfpView.verdict.text);
equal('可用通信设备行标红', row(noHfpView, '可用通信设备(API31+)').level, 'bad');

// 情况 3:没给『附近的设备』权限
const deniedView = pure.summarizeRoute(routeOf({ headsetPermission: 'denied' }));
equal('权限被拒时结论是 bad', deniedView.verdict.level, 'bad');
check('结论里点名附近的设备权限', deniedView.verdict.text.includes('附近的设备'), deniedView.verdict.text);

// 情况 4:蓝牙没开
const btOffView = pure.summarizeRoute(routeOf({ btEnabled: false }));
check('蓝牙关着时提示开蓝牙', btOffView.verdict.text.includes('蓝牙'), btOffView.verdict.text);
equal('蓝牙行标红', row(btOffView, '蓝牙').level, 'bad');

// 情况 5:在录音,但实际走的是手机麦 —— 最需要被看见的一种
const phoneMicView = pure.summarizeRoute(routeOf({
  recording: true,
  mode: 'MODE_IN_COMMUNICATION',
  routedDevice: 'Built-in Mic (TYPE_BUILTIN_MIC)',
  availableCommunication: ['WH-CH520 (TYPE_BLUETOOTH_SCO)'],
  connectedHeadsets: ['WH-CH520'],
}));
equal('用手机麦录音时结论是 bad', phoneMicView.verdict.level, 'bad');
check('结论里指出实际输入设备', phoneMicView.verdict.text.includes('Built-in Mic'), phoneMicView.verdict.text);
equal('实际输入行标红', row(phoneMicView, 'AudioRecord 实际输入').level, 'bad');

// 情况 6:在录音,且真的走了耳机麦
const headsetView = pure.summarizeRoute(routeOf({
  recording: true,
  mode: 'MODE_IN_COMMUNICATION',
  scoOn: true,
  communicationDevice: 'WH-CH520 (TYPE_BLUETOOTH_SCO)',
  routedDevice: 'WH-CH520 (TYPE_BLUETOOTH_SCO)',
  availableCommunication: ['WH-CH520 (TYPE_BLUETOOTH_SCO)'],
  connectedHeadsets: ['WH-CH520'],
}));
equal('耳机收音时结论是 ok', headsetView.verdict.level, 'ok');
equal('SCO 行是 ok', row(headsetView, 'SCO 生效').level, 'ok');
equal('实际输入行是 ok', row(headsetView, 'AudioRecord 实际输入').level, 'ok');

// 情况 7:原生给了 hint 就优先显示原生那句(它知道的现场信息更全)
equal('原生 hint 优先', pure.summarizeRoute(routeOf({ hint: '原生给的结论' })).verdict.text, '原生给的结论');

// 情况 8:SCO 方式与档位尝试 —— 现场在两条路之间切换,看的就是这两行
equal('方式 auto 的中文', pure.scoModeText('auto'), '自动(先新接口,不成就换老接口)');
equal('方式 new 的中文', pure.scoModeText('new'), '新接口 setCommunicationDevice');
equal('方式 legacy 的中文', pure.scoModeText('legacy'), '老接口 startBluetoothSco');
check('方式 phone 的中文含手机麦', pure.scoModeText('phone').includes('手机麦'), pure.scoModeText('phone'));

const failedView = pure.summarizeRoute(routeOf({
  scoMode: 'auto',
  lastAttempt: '档1 新接口 setCommunicationDevice(WH-CH520):接口返回 true,400 ms 后 routedDevice=手机麦克风, type=TYPE_BUILTIN_MIC → 失败',
}));
equal('档位失败的清单行标红', row(failedView, '上次档位尝试').level, 'bad');
check('结论里复述档位尝试', /失败/.test(failedView.verdict.text), failedView.verdict.text);
equal('SCO 方式行显示方式名', row(failedView, 'SCO 方式').value, '自动(先新接口,不成就换老接口)');

// 情况 9:用户显式选了"只用手机麦"的退路 —— 这时不该报成故障,而是明确告知
const phoneModeView = pure.summarizeRoute(routeOf({
  scoMode: 'phone',
  recording: true,
  mode: 'MODE_IN_COMMUNICATION',
  routedDevice: 'Built-in Mic (TYPE_BUILTIN_MIC)',
  routedDeviceType: 'TYPE_BUILTIN_MIC (15)',
}));
equal('退路方式下结论是 warn', phoneModeView.verdict.level, 'warn');
check('退路方式下标明耳机只放声音', phoneModeView.verdict.text.includes('耳机只用来放声音'), phoneModeView.verdict.text);
check('退路方式下实际输入行不标红', row(phoneModeView, 'AudioRecord 实际输入').level !== 'bad');

// 情况 10:档3 相关字段要能显示出来
const tier3View = pure.summarizeRoute(routeOf({
  preferredDevice: 'WH-CH520 (TYPE_BLUETOOTH_SCO)',
  routedDeviceType: 'TYPE_BLUETOOTH_SCO (7)',
}));
equal('首选设备行显示设备名', row(tier3View, '首选设备(档3)').value, 'WH-CH520 (TYPE_BLUETOOTH_SCO)');
equal('输入设备类型行显示类型', row(tier3View, '输入设备类型').value, 'TYPE_BLUETOOTH_SCO (7)');

// 情况 11:HFP/A2DP 状态 —— 现场最关键的两条(媒体音频通了、通话没通)
check('STATE_CONNECTED 算已连接', pure.isConnectedState('STATE_CONNECTED(2)'));
check('STATE_DISCONNECTED 不算已连接', !pure.isConnectedState('STATE_DISCONNECTED(0)'));

const mediaOnly = pure.summarizeRoute(routeOf({
  a2dpProfileState: 'STATE_CONNECTED(2)',
  headsetProfileState: 'STATE_DISCONNECTED(0)',
  connectedHeadsets: ['WH-CH520'],
  availableCommunication: ['LMR-AL00 [TYPE_BUILTIN_EARPIECE(1,听筒)]', '扬声器 [TYPE_BUILTIN_SPEAKER(2,扬声器)]'],
}));
equal('A2DP 已连时该行是 ok', row(mediaOnly, 'A2DP 状态(媒体音频)').level, 'ok');
equal('HFP 未连时该行是 warn', row(mediaOnly, 'HFP 状态(通话通道)').level, 'warn');
check('结论指出只连了媒体音频', /媒体音频/.test(mediaOnly.verdict.text), mediaOnly.verdict.text);
check('结论里给出 SCO 试验指引', /SCO 试验/.test(mediaOnly.verdict.text), mediaOnly.verdict.text);

// 情况 12:蓝牙代理还没就绪时 connectedHeadsets 是字符串而不是空数组 —— 不能当成"没连"
const pendingView = pure.summarizeRoute(routeOf({
  connectedHeadsets: '未就绪(蓝牙代理还没连上,过一两秒点『刷新路由』再看)',
}));
check('未就绪文字原样显示', row(pendingView, '已连接耳机').value.includes('未就绪'), row(pendingView, '已连接耳机').value);
equal('未就绪时该行不标红也不标绿', row(pendingView, '已连接耳机').level, '');

// 情况 13:SCO 试验结果的归纳
equal('试验成功时结论是 ok', pure.probeSummary({ ok: true, conclusion: '建起来了', steps: ['a', 'b'] }).level, 'ok');
const probeFail = pure.probeSummary({ ok: false, conclusion: '系统没给这条链路', steps: ['a'] });
equal('试验失败时结论是 bad', probeFail.level, 'bad');
equal('试验步数被带出来', probeFail.stepCount, 1);
check('试验返回 error 时也有话说', pure.probeSummary({ error: 'x' }).text.includes('SCO 试验失败'), pure.probeSummary({ error: 'x' }).text);

// --- 6. 自动降级 / 当前麦克风 / 重连 -------------------------------------------------
// 真机结论:华为 LMR-AL00 不给三方 App 蓝牙 SCO 输入。这一组全是围绕"能用"写的。
console.log('');
console.log('自动降级、当前麦克风与重连');

// 真机那份 route 的样子:HFP/A2DP 都连上,但系统里没有任何蓝牙输入
const huaweiRoute = routeOf({
  scoOn: false,
  headsetProfileState: 'STATE_CONNECTED(2)',
  a2dpProfileState: 'STATE_CONNECTED(2)',
  connectedHeadsets: ['WH-CH520'],
  availableCommunication: ['LMR-AL00 [TYPE_BUILTIN_EARPIECE(1,听筒)]', '扬声器 [TYPE_BUILTIN_SPEAKER(2,扬声器)]'],
  inputDevices: ['内置麦克风 [TYPE_BUILTIN_MIC(15,内置麦克风), 信号源]',
    '远端混音 [TYPE_REMOTE_SUBMIX(25,远端混音(虚拟)), 信号源]'],
  currentMic: '手机麦 [TYPE_BUILTIN_MIC(15,内置麦克风)](SCO 方式=只用手机麦)',
  currentMicKind: 'phone',
});

check('真机那种情况判定为拿不到蓝牙输入', !pure.bluetoothInputAvailable(huaweiRoute));
check('auto 方式下自动降级', pure.shouldAutoDowngrade(huaweiRoute, 'auto'));
check('已经是手机麦时不再降级', !pure.shouldAutoDowngrade(huaweiRoute, 'phone'));
check('用户手动选实验方式时不插手', !pure.shouldAutoDowngrade(huaweiRoute, 'legacy'));
check('有蓝牙输入时不降级', !pure.shouldAutoDowngrade(routeOf({
  scoOn: true,
  availableCommunication: ['WH-CH520 [TYPE_BLUETOOTH_SCO(7,蓝牙SCO)]'],
}), 'auto'));
check('输入设备里出现 SCO 也算有蓝牙输入', pure.bluetoothInputAvailable(routeOf({
  inputDevices: ['WH-CH520 [TYPE_BLUETOOTH_SCO(7,蓝牙SCO), 信号源]'],
})));

const notice = pure.autoDowngradeNotice(huaweiRoute);
check('降级说明点出"不开放蓝牙麦克风"', notice.includes('不向 App 开放蓝牙麦克风'), notice);
check('降级说明点出改用手机麦', notice.includes('手机麦'), notice);
check('降级说明点出耳机继续放声音', notice.includes('放她的声音'), notice);
check('降级说明给出有线耳麦这条出路', notice.includes('有线/USB 耳麦'), notice);
check('没连耳机时降级说明换措辞', !pure.autoDowngradeNotice(routeOf({})).includes('HFP 已连接'));

// 当前麦克风那一行:有线耳麦 / 蓝牙 / 手机麦
const wiredRoute = routeOf({
  currentMic: 'USB Audio [TYPE_USB_HEADSET(22,USB耳麦)](有线/USB 耳麦,不依赖蓝牙 SCO)',
  currentMicKind: 'wired',
  inputDevices: ['USB Audio [TYPE_USB_HEADSET(22,USB耳麦), 信号源]'],
});
const wiredView = pure.summarizeRoute(wiredRoute);
equal('有线耳麦那一行显示设备名', row(wiredView, '当前使用的麦克风').value, wiredRoute.currentMic);
equal('有线耳麦算好结果', row(wiredView, '当前使用的麦克风').level, 'ok');
check('清单里永远有"当前使用的麦克风"这一行', row(okView, '当前使用的麦克风') !== undefined);

// 重连退避:1s、2s、4s、8s、10s、10s(上限)
equal('第 1 次重连等 1 秒', pure.nextReconnectDelay(1), 1000);
equal('第 2 次重连等 2 秒', pure.nextReconnectDelay(2), 2000);
equal('第 4 次重连等 8 秒', pure.nextReconnectDelay(4), 8000);
equal('第 5 次重连撞上限 10 秒', pure.nextReconnectDelay(5), 10000);
equal('第 99 次也封顶 10 秒', pure.nextReconnectDelay(99), 10000);
equal('异常输入按第 1 次算', pure.nextReconnectDelay(0), 1000);

// --- 7. 分发:地址预设与首次引导 -----------------------------------------------------
console.log('');
console.log('地址预设与首次引导(给别人装的时候用)');

equal('Tailscale 预设与原生默认一致', pure.TAILSCALE_URL, 'wss://laptop-u3hj61a7.tail35209a.ts.net:8443/');

const lanKnown = pure.buildLanPreset('192.168.1.23');
equal('按手机 IP 猜出网段', lanKnown.url, 'ws://192.168.1.100:6201/');
check('猜出网段时提示带上手机 IP', lanKnown.hint.includes('192.168.1.23'), lanKnown.hint);
check('提示里说要换成电脑的 IP', lanKnown.hint.includes('电脑'), lanKnown.hint);
equal('标记为已精确定位', lanKnown.precise, true);

equal('10 网段也能猜', pure.buildLanPreset('10.0.5.7').url, 'ws://10.0.5.100:6201/');

const lanUnknown = pure.buildLanPreset('');
equal('拿不到 IP 时用标准占位', lanUnknown.url, 'ws://192.168.1.100:6201/');
equal('占位时标记为不精确', lanUnknown.precise, false);
check('占位提示里写清要换 IP', lanUnknown.hint.includes('把地址里的 IP 换成'), lanUnknown.hint);
check('提示里给出 --host 0.0.0.0', lanUnknown.hint.includes('--host 0.0.0.0'), lanUnknown.hint);

check('没填过地址就显示引导', pure.shouldShowGuide('', false));
check('填过地址就不再显示引导', !pure.shouldShowGuide('wss://x', false));
check('用户关掉过就不再显示', !pure.shouldShowGuide('', true));
check('空白地址也算没填过', pure.shouldShowGuide('   ', false));

// 清单要齐全:页面就靠这些行判断问题出在哪一环
check('诊断清单包含全部关键行',
  ['当前使用的麦克风', 'SCO 方式', '上次档位尝试', '蓝牙', '权限', '可用通信设备(API31+)', '已连接耳机',
   'HFP 状态(通话通道)', 'A2DP 状态(媒体音频)', '已配对设备',
   '当前通信设备', 'AudioRecord 实际输入', '输入设备类型', '首选设备(档3)', 'SCO 生效',
   'AudioManager mode', '全部输入设备', '输出设备(A2DP 看这里)', 'Android SDK']
    .every((label) => row(okView, label) !== undefined),
  `${okView.rows.length} 行`);
check('空路由不炸', pure.summarizeRoute({}).rows.length > 0);

console.log('');
if (failures.length > 0) {
  console.log('FAILURES:');
  for (const line of failures) console.log(`  ${line}`);
  process.exit(1);
}
console.log('PASS: 页面纯逻辑(封 WAV、组帧、base64)与家里服务端的期望一致');
