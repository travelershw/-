/**
 * 页面里的纯逻辑:WAV 封装、协议帧组装、base64 与二进制互转。
 *
 * 单独成文件有两个原因:
 * 1. 这些函数**不碰任何浏览器 API**,所以能用 `node tools/page-protocol-test.js`
 *    在没有手机、没有 WebView 的情况下自动验证 —— 这是阶段 2 唯一能自动测的部分;
 * 2. 页面里所有格式相关的地方(16 kHz 单声道 16 bit)只有一个实现,不会各处抄一份。
 *
 * 页面用 `<script src="pure.js"></script>` 引入,拿到 `window.QingyuPure`;
 * Node 里 `require('./pure.js')` 拿到同一批函数。
 *
 * @module qingyu-phone/lib/page/pure
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();
  } else {
    root.QingyuPure = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /** 上行语音格式,与家里 phone_bridge.py 的期望一致。 */
  var SAMPLE_RATE = 16000;
  var CHANNELS = 1;
  var BITS_PER_SAMPLE = 16;
  /** WAV 头固定 44 字节(PCM,无扩展块)。 */
  var WAV_HEADER_BYTES = 44;

  /**
   * 把任意输入统一成 Uint8Array 视图(不复制)。
   * @param {ArrayBuffer|Uint8Array|Array<number>} value 数据。
   * @returns {Uint8Array} 字节视图。
   */
  function toBytes(value) {
    if (value instanceof Uint8Array) { return value; }
    if (value instanceof ArrayBuffer) { return new Uint8Array(value); }
    if (Array.isArray(value)) { return Uint8Array.from(value); }
    throw new TypeError('需要 ArrayBuffer / Uint8Array / number[]');
  }

  /**
   * 往 DataView 里写一段 ASCII。
   * @param {DataView} view 目标。
   * @param {number} offset 起始偏移。
   * @param {string} text 内容。
   */
  function writeAscii(view, offset, text) {
    for (var i = 0; i < text.length; i++) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  }

  /**
   * 把多段 PCM 拼成一段。
   * @param {Array<Uint8Array|ArrayBuffer>} chunks 分块。
   * @returns {Uint8Array} 拼接结果。
   */
  function mergeChunks(chunks) {
    var total = 0;
    var parts = [];
    for (var i = 0; i < chunks.length; i++) {
      var part = toBytes(chunks[i]);
      parts.push(part);
      total += part.length;
    }
    var merged = new Uint8Array(total);
    var offset = 0;
    for (var j = 0; j < parts.length; j++) {
      merged.set(parts[j], offset);
      offset += parts[j].length;
    }
    return merged;
  }

  /**
   * 给裸 PCM16 套一个标准 44 字节 WAV 头。
   *
   * 家里那侧两种都吃(wav 或裸 PCM16),发 wav 的好处是任何播放器都能直接放,
   * 现场排查时能立刻听出是"没录上"还是"识别错了"。
   *
   * @param {ArrayBuffer|Uint8Array} pcm 裸 PCM16LE 数据。
   * @param {number} [rate] 采样率,默认 16000。
   * @param {number} [channels] 声道数,默认 1。
   * @returns {ArrayBuffer} 完整 wav。
   */
  function pcmToWav(pcm, rate, channels) {
    var bytes = toBytes(pcm);
    var sampleRate = rate || SAMPLE_RATE;
    var channelCount = channels || CHANNELS;
    var blockAlign = channelCount * BITS_PER_SAMPLE / 8;
    var buffer = new ArrayBuffer(WAV_HEADER_BYTES + bytes.length);
    var view = new DataView(buffer);

    writeAscii(view, 0, 'RIFF');
    view.setUint32(4, 36 + bytes.length, true);          // RIFF 块大小 = 文件长度 - 8
    writeAscii(view, 8, 'WAVE');
    writeAscii(view, 12, 'fmt ');
    view.setUint32(16, 16, true);                        // fmt 块大小(PCM 固定 16)
    view.setUint16(20, 1, true);                         // 1 = PCM 未压缩
    view.setUint16(22, channelCount, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * blockAlign, true);    // 字节率
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, BITS_PER_SAMPLE, true);
    writeAscii(view, 36, 'data');
    view.setUint32(40, bytes.length, true);

    new Uint8Array(buffer, WAV_HEADER_BYTES).set(bytes);
    return buffer;
  }

  /**
   * 从 wav 里读回头部字段,主要给测试和现场排查用。
   * @param {ArrayBuffer|Uint8Array} wav 完整 wav。
   * @returns {object} 头部字段。
   */
  function readWavHeader(wav) {
    var bytes = toBytes(wav);
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var ascii = function (offset, length) {
      var text = '';
      for (var i = 0; i < length; i++) { text += String.fromCharCode(view.getUint8(offset + i)); }
      return text;
    };
    return {
      riff: ascii(0, 4),
      riffSize: view.getUint32(4, true),
      wave: ascii(8, 4),
      fmt: ascii(12, 4),
      fmtSize: view.getUint32(16, true),
      audioFormat: view.getUint16(20, true),
      channels: view.getUint16(22, true),
      sampleRate: view.getUint32(24, true),
      byteRate: view.getUint32(28, true),
      blockAlign: view.getUint16(32, true),
      bitsPerSample: view.getUint16(34, true),
      data: ascii(36, 4),
      dataSize: view.getUint32(40, true),
    };
  }

  /**
   * 算这段 PCM 有多少秒,按家里期望的精度(两位小数)。
   * @param {number} byteLength 字节数。
   * @param {number} [rate] 采样率。
   * @param {number} [channels] 声道数。
   * @returns {number} 秒数,两位小数。
   */
  function secondsOf(byteLength, rate, channels) {
    var sampleRate = rate || SAMPLE_RATE;
    var channelCount = channels || CHANNELS;
    var secs = byteLength / (sampleRate * channelCount * (BITS_PER_SAMPLE / 8));
    return Math.round(secs * 100) / 100;
  }

  /**
   * 组装一次"按住说话"的上行:JSON 帧 + 紧跟的二进制 wav。
   *
   * 协议(家里已实测,不要改字段名):
   *   {"type":"utterance","sample_rate":16000,"channels":1,"secs":1.2} + 二进制
   *
   * @param {ArrayBuffer|Uint8Array} pcm 裸 PCM16LE。
   * @param {number} [rate] 采样率。
   * @returns {{json:object, binary:ArrayBuffer, secs:number}} 帧。
   */
  function buildUtteranceFrame(pcm, rate) {
    var bytes = toBytes(pcm);
    var sampleRate = rate || SAMPLE_RATE;
    var secs = secondsOf(bytes.length, sampleRate, CHANNELS);
    return {
      json: { type: 'utterance', sample_rate: sampleRate, channels: CHANNELS, secs: secs },
      binary: pcmToWav(bytes, sampleRate, CHANNELS),
      secs: secs,
    };
  }

  /**
   * 组装一次拍照上行。
   *   {"type":"image","mime":"image/jpeg","text":"看看这个"} + 二进制 JPEG
   * @param {ArrayBuffer|Uint8Array} jpeg JPEG 字节。
   * @param {string} [text] 附言。
   * @returns {{json:object, binary:ArrayBuffer}} 帧。
   */
  function buildImageFrame(jpeg, text) {
    var bytes = toBytes(jpeg);
    return {
      json: { type: 'image', mime: 'image/jpeg', text: text || '看看这个' },
      // 复制一份,避免把同一个 buffer 交出去后被别处改动
      binary: bytes.slice().buffer,
    };
  }

  /** base64 字母表,自己实现是为了不依赖 btoa/atob(它们在处理二进制时容易踩坑)。 */
  var B64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

  /**
   * 二进制 → base64。
   * @param {ArrayBuffer|Uint8Array} value 数据。
   * @returns {string} base64。
   */
  function bytesToBase64(value) {
    var bytes = toBytes(value);
    var out = '';
    for (var i = 0; i < bytes.length; i += 3) {
      var b0 = bytes[i];
      var b1 = i + 1 < bytes.length ? bytes[i + 1] : -1;
      var b2 = i + 2 < bytes.length ? bytes[i + 2] : -1;
      out += B64_CHARS.charAt(b0 >> 2);
      out += B64_CHARS.charAt(((b0 & 3) << 4) | (b1 === -1 ? 0 : b1 >> 4));
      out += b1 === -1 ? '=' : B64_CHARS.charAt(((b1 & 15) << 2) | (b2 === -1 ? 0 : b2 >> 6));
      out += b2 === -1 ? '=' : B64_CHARS.charAt(b2 & 63);
    }
    return out;
  }

  /**
   * base64 → 二进制。原生桥用 base64 把 PCM / JPEG 递进来,这里就是入口。
   * 原生用的是 android.util.Base64.NO_WRAP,所以只需要容忍换行和空格。
   * @param {string} text base64 文本。
   * @returns {Uint8Array} 字节。
   */
  function base64ToBytes(text) {
    var clean = String(text || '').replace(/[^A-Za-z0-9+/=]/g, '');
    var length = clean.length;
    while (length > 0 && clean.charAt(length - 1) === '=') { length -= 1; }
    var outLength = Math.floor(length * 3 / 4);
    var out = new Uint8Array(outLength);
    var accumulator = 0;
    var bits = 0;
    var index = 0;
    for (var i = 0; i < length; i++) {
      var value = B64_CHARS.indexOf(clean.charAt(i));
      if (value < 0) { continue; }
      accumulator = (accumulator << 6) | value;
      bits += 6;
      if (bits >= 8) {
        bits -= 8;
        out[index] = (accumulator >> bits) & 0xff;
        index += 1;
      }
    }
    return out.subarray(0, index);
  }

  /** 电平(dBFS)→ 0..1 的条形宽度,给页面画电平条。 */
  function levelToRatio(dbfs) {
    if (typeof dbfs !== 'number' || !isFinite(dbfs)) { return 0; }
    var clamped = Math.max(-60, Math.min(0, dbfs));
    return (clamped + 60) / 60;
  }

  /**
   * 设备名像不像耳机/蓝牙音频设备。
   * @param {string} name 设备名(可能带 " (TYPE_xxx)" 后缀)。
   * @returns {boolean} 像耳机为 true。
   */
  function looksLikeHeadset(name) {
    if (!name) { return false; }
    return /wh-|wf-|sony|headset|buds|airpods|bluetooth|ble_/i.test(String(name));
  }

  /** 权限状态 → 中文。 */
  function permissionText(value) {
    if (value === 'granted') { return '已允许'; }
    if (value === 'denied') { return '被拒'; }
    return '未知';
  }

  /**
   * 原生的"列表"字段有两种形态,统一成数组。
   *
   * 为什么会有两种:原生用 `JSONObject.put(name, List)` 时,org.json 会把这个 List
   * 写成**字符串**(Java 的 `[A, B, C]` 形式),只有显式 `JSONArray` 才是真数组。
   * 2026-10-03 在真机上实测到后果:`available.filter is not a function` 抛 TypeError,
   * 而它恰好排在"把这段录音排上发送"之前 —— 于是**每句话都被静默丢掉**(页面只留一句
   * `Script error.`)。所以这里两种都收:数组原样返回,字符串当成"一个整体条目"
   * (判断"有没有蓝牙"这类用途,只需要整串里能不能匹配到关键字)。
   * @param {*} value 原生字段值。
   * @returns {Array} 数组(这个函数永远不抛)。
   */
  function asList(value) {
    if (Array.isArray(value)) { return value; }
    if (value === null || value === undefined || value === '') { return []; }
    return [String(value)];
  }

  /** 列表 → 一行文本,空列表显示 (空)。字符串形态同样收(见 asList)。 */
  function listText(list) {
    var items = asList(list);
    return items.length ? items.join(' / ') : '(空)';
  }

  /** 耳机路由方式 → 中文。 */
  function scoModeText(mode) {
    if (mode === 'new') { return '新接口 setCommunicationDevice'; }
    if (mode === 'legacy') { return '老接口 startBluetoothSco'; }
    if (mode === 'phone') { return '只用手机麦(退路)'; }
    return '自动(先新接口,不成就换老接口)';
  }

  /** 蓝牙 profile 状态文字 → 是否已连接。 */
  function isConnectedState(state) {
    return /STATE_CONNECTED/.test(String(state || ''));
  }

  /**
   * 把 SCO 试验的返回值整理成一句结论 + 等级。
   * @param {object} result probeSco() 的返回值。
   * @returns {{level: string, text: string, stepCount: number}} level 取 ok / bad。
   */
  function probeSummary(result) {
    var r = result || {};
    if (r.error) {
      return { level: 'bad', text: 'SCO 试验失败:' + r.error, stepCount: 0 };
    }
    return {
      level: r.ok ? 'ok' : 'bad',
      text: r.conclusion || '(试验没有给出结论)',
      stepCount: (r.steps || []).length,
    };
  }

  /**
   * 本机到底拿不拿得到蓝牙麦克风。
   *
   * 三条线索任一成立就算能拿到:scoOn 为真、可用通信设备里有蓝牙项、输入设备里出现
   * TYPE_BLUETOOTH_SCO / TYPE_BLE_HEADSET。
   * @param {object} route getAudioRoute() 解析后的对象。
   * @returns {boolean} 能拿到蓝牙输入为 true。
   */
  function bluetoothInputAvailable(route) {
    var r = route || {};
    if (r.scoOn === true) { return true; }
    if (r.recording && /BLUETOOTH_SCO|BLE_HEADSET/.test(String(r.routedDeviceType || ''))) { return true; }
    if (asList(r.availableCommunication).some(function (entry) { return /bluetooth|ble_/i.test(String(entry)); })) {
      return true;
    }
    return asList(r.inputDevices).some(function (entry) { return /TYPE_BLUETOOTH_SCO|TYPE_BLE_HEADSET/.test(String(entry)); });
  }

  /**
   * 该不该自动降级到手机麦。
   *
   * 只有"当前是默认的 auto 方式"且"本机拿不到蓝牙输入"时才算 —— 用户手动选了实验方式
   * (new / legacy)时不插手,免得跟人对着干。
   * @param {object} route 路由对象。
   * @param {string} scoMode 当前方式。
   * @returns {boolean} 该降级为 true。
   */
  function shouldAutoDowngrade(route, scoMode) {
    if (scoMode !== 'auto') { return false; }
    return !bluetoothInputAvailable(route);
  }

  /**
   * 自动降级时给用户看的那句话。
   * @param {object} route 路由对象。
   * @returns {string} 结论文字。
   */
  function autoDowngradeNotice(route) {
    var r = route || {};
    var headset = isConnectedState(r.headsetProfileState) || isConnectedState(r.a2dpProfileState);
    if (headset) {
      return '本机不向 App 开放蓝牙麦克风(HFP 已连接但系统不给 SCO 输入)→ 已自动改用手机麦;耳机继续用来放她的声音。'
        + '想用耳机麦的话,插一副有线/USB 耳麦,App 会自动切过去。';
    }
    return '没检测到蓝牙麦克风输入 → 已自动改用手机麦(插上蓝牙耳机或耳麦后,点『刷新路由』再看一次)。';
  }

  /**
   * 断线重连的等待时间:指数退避,上限 10 秒。
   * @param {number} attempt 第几次重连(从 1 开始)。
   * @returns {number} 毫秒。
   */
  function nextReconnectDelay(attempt) {
    var n = Math.max(1, Math.floor(attempt || 1));
    return Math.min(10000, 1000 * Math.pow(2, n - 1));
  }

  /** 分发版的 Tailscale 地址(与原生默认值一致)。 */
  var TAILSCALE_URL = 'wss://laptop-u3hj61a7.tail35209a.ts.net:8443/';

  /**
   * 生成「同一个 WiFi」预设:由手机自己的局域网 IP 猜出网段。
   *
   * 猜不出网段时给标准占位地址,并在提示里写清"把 IP 换成电脑的局域网地址"。
   * @param {string} wifiIp 手机自己的局域网 IP(原生 getWifiIp() 给的),可能为空。
   * @returns {{url: string, hint: string, precise: boolean}} 预设地址与提示。
   */
  function buildLanPreset(wifiIp) {
    var ip = String(wifiIp || '').trim();
    var match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(ip);
    if (match) {
      var prefix = match[1] + '.' + match[2] + '.' + match[3];
      return {
        url: 'ws://' + prefix + '.100:6201/',
        hint: '你的手机在这个网段(' + ip + '):把地址里的 IP 换成家里电脑的局域网地址(前三段通常一样,只改最后一段)。'
          + '电脑那侧要用 --host 0.0.0.0 启动中转,并允许 Windows 防火墙放行 6201。',
        precise: true,
      };
    }
    return {
      url: 'ws://192.168.1.100:6201/',
      hint: '把地址里的 IP 换成家里那台电脑的局域网地址(手机和电脑在同一个 WiFi 下)。'
        + '电脑那侧要用 --host 0.0.0.0 启动中转,并允许 Windows 防火墙放行 6201。',
      precise: false,
    };
  }

  /**
   * 要不要显示首次使用引导(没填过地址就显示)。
   * @param {string} address 已保存的服务器地址。
   * @param {boolean} dismissed 用户是否已经关掉过。
   * @returns {boolean} 显示为 true。
   */
  function shouldShowGuide(address, dismissed) {
    if (dismissed === true) { return false; }
    return !String(address || '').trim();
  }

  /**
   * 把 getAudioRoute() 的 JSON 变成一张"能直接定位问题"的诊断清单。
   *
   * 这里最容易搞错的一点:蓝牙耳机的输入设备在 SCO/HFP 建立**之前**通常不在输入设备
   * 列表里,所以未录音时 inputDevices 里没有耳机是正常的。判断耳机在不在,要看
   * availableCommunication(API31+)/connectedHeadsets(老 API)与已配对列表。
   *
   * @param {object} route getAudioRoute() 解析后的对象。
   * @returns {{verdict: {level: string, text: string}, rows: Array<{label: string, value: string, level: string}>}}
   *          level 取值 ok / warn / bad / ''(空表示中性)。
   */
  function summarizeRoute(route) {
    var r = route || {};
    var sdkInt = typeof r.sdkInt === 'number' ? r.sdkInt : 0;
    var modern = sdkInt >= 31;
    var available = asList(r.availableCommunication);
    // connectedHeadsets 在蓝牙代理就绪前是字符串("未就绪…"),不是数组 —— 不能当成"没连"
    var connected = Array.isArray(r.connectedHeadsets) ? r.connectedHeadsets : [];
    var connectedReady = Array.isArray(r.connectedHeadsets);
    var connectedText = connectedReady ? listText(connected) : String(r.connectedHeadsets || '(空)');
    var bonded = asList(r.bondedDevices);
    // 注意:Android 12+ 的可用通信设备里本来就有听筒/扬声器这些非蓝牙项,所以不能只看
    // 列表非空,必须找蓝牙类的那几条(原生给的条目带 "[TYPE_BLUETOOTH_SCO(7,蓝牙SCO)]")。
    var bluetoothEntries = available.filter(function (entry) { return /bluetooth|ble_/i.test(String(entry)); });
    var bluetoothSeen = bluetoothEntries.length > 0 || connected.some(looksLikeHeadset);
    var routed = r.routedDevice || '';
    var routedIsPhoneMic = !!routed && !looksLikeHeadset(routed);
    var lastAttempt = r.lastAttempt || '';
    var lastFailed = /失败/.test(lastAttempt);
    var hfpConnected = isConnectedState(r.headsetProfileState);
    var a2dpConnected = isConnectedState(r.a2dpProfileState);

    // 结论按排查顺序给:权限 → 用户选的退路 → 蓝牙开关 → 可用通话设备 → 录音时实际走哪个设备
    var level = 'warn';
    var text = '没看到耳机:确认它已连接,并打开『通话音频』';
    if (r.audioPermission !== 'granted') {
      level = 'bad';
      text = '还没给麦克风权限:点『申请权限』,或在系统设置里允许轻语使用麦克风';
    } else if (r.headsetPermission === 'denied') {
      level = 'bad';
      text = '还没给『附近的设备』权限(Android 12+ 必需):点『申请权限』';
    } else if (r.scoMode === 'phone') {
      level = 'warn';
      text = '当前是『只用手机麦』方式(你选的):耳机只用来放声音';
    } else if (r.btEnabled === false) {
      level = 'bad';
      text = '手机蓝牙没开:先打开蓝牙并连上耳机';
    } else if (r.recording && routedIsPhoneMic) {
      level = 'bad';
      text = '正在录音,但实际输入是「' + routed + '」而不是耳机:检查耳机的『通话音频』开关';
    } else if (r.recording && routed) {
      level = 'ok';
      text = '正在用耳机麦克风收音:' + routed;
    } else if (lastFailed) {
      // 已经试过档位但全失败:把上次的结论原样端出来,这是最有用的信息
      level = 'bad';
      text = '上次录音的路由尝试:' + lastAttempt + ' —— 试着把『SCO 方式』换成另一种(华为上多半要用『老接口』)';
    } else if (modern && bluetoothEntries.length === 0) {
      level = 'bad';
      if (a2dpConnected && !hfpConnected) {
        text = '耳机只连上了媒体音频(A2DP),通话(HFP)没建立:去 设置 → 蓝牙 → 耳机 → 打开『通话音频』,或取消配对重连;点『SCO 试验』看时间线';
      } else if (a2dpConnected && hfpConnected) {
        text = 'HFP 报已连接,但系统没给出蓝牙通话设备:点『SCO 试验』看 SCO 到底能不能建起来(华为上可能不给三方 App)';
      } else {
        text = '系统报不出可用的通话设备:去 设置 → 蓝牙 → 已配对设备 → 耳机 → 打开『通话音频』(HFP)';
      }
    } else if (bluetoothSeen) {
      level = 'ok';
      text = '耳机可用:' + (connected[0] || bluetoothEntries[0]) + '(未录音时输入设备列表里没有它是正常的)';
    }

    // 原生那边算出的 hint 更贴合现场,有就用它
    var hint = (typeof r.hint === 'string' && r.hint) ? r.hint : text;

    var rows = [
      { label: '当前使用的麦克风', value: r.currentMic || '(未知)', level: r.currentMicKind === 'wired' ? 'ok' : (r.currentMicKind === 'bluetooth' ? 'ok' : 'warn') },
      { label: 'SCO 方式', value: scoModeText(r.scoMode), level: r.scoMode === 'phone' ? 'warn' : '' },
      {
        label: '上次档位尝试',
        value: lastAttempt || '(还没录过音;按下说话会依次尝试)',
        level: lastAttempt ? (lastFailed ? 'bad' : 'ok') : '',
      },
      { label: '蓝牙', value: r.btEnabled ? '已开启' : '未开启 / 读不到', level: r.btEnabled ? 'ok' : 'bad' },
      {
        label: '权限',
        value: '录音 ' + permissionText(r.audioPermission) + ' · 附近的设备 ' + permissionText(r.headsetPermission),
        level: (r.audioPermission === 'granted' && r.headsetPermission !== 'denied') ? 'ok' : 'bad',
      },
      {
        label: '可用通信设备(API31+)',
        value: listText(available),
        level: bluetoothSeen ? 'ok' : (modern ? 'bad' : ''),
      },
      { label: '已连接耳机', value: connectedText, level: connectedReady ? (connected.length ? 'ok' : 'warn') : '' },
      {
        label: 'HFP 状态(通话通道)',
        value: r.headsetProfileState || '—',
        level: hfpConnected ? 'ok' : (r.headsetProfileState ? 'warn' : ''),
      },
      {
        label: 'A2DP 状态(媒体音频)',
        value: r.a2dpProfileState || '—',
        level: a2dpConnected ? 'ok' : (r.a2dpProfileState ? 'warn' : ''),
      },
      { label: '已配对设备', value: listText(bonded), level: bonded.length ? '' : 'warn' },
      {
        label: '当前通信设备',
        value: r.communicationDevice || '(未选中,录音时才选)',
        level: r.communicationDevice ? 'ok' : '',
      },
      {
        label: 'AudioRecord 实际输入',
        value: routed || '(未录音,这时看不到属正常)',
        level: r.recording ? ((routedIsPhoneMic && r.scoMode !== 'phone') ? 'bad' : 'ok') : '',
      },
      { label: '输入设备类型', value: r.routedDeviceType || '(未录音)', level: '' },
      { label: '首选设备(档3)', value: r.preferredDevice || '(未设置)', level: '' },
      {
        label: 'SCO 生效',
        value: r.scoOn ? '是' : '否',
        level: r.scoOn ? 'ok' : (r.recording && r.scoMode !== 'phone' ? 'bad' : ''),
      },
      { label: 'AudioManager mode', value: r.mode || '—', level: '' },
      { label: '全部输入设备', value: listText(r.inputDevices), level: '' },
      { label: '输出设备(A2DP 看这里)', value: listText(r.outputDevices), level: '' },
      { label: 'Android SDK', value: sdkInt ? String(sdkInt) : '未知', level: '' },
    ];
    return { verdict: { level: level, text: hint }, rows: rows };
  }

  return {
    SAMPLE_RATE: SAMPLE_RATE,
    CHANNELS: CHANNELS,
    BITS_PER_SAMPLE: BITS_PER_SAMPLE,
    WAV_HEADER_BYTES: WAV_HEADER_BYTES,
    mergeChunks: mergeChunks,
    pcmToWav: pcmToWav,
    readWavHeader: readWavHeader,
    secondsOf: secondsOf,
    buildUtteranceFrame: buildUtteranceFrame,
    buildImageFrame: buildImageFrame,
    bytesToBase64: bytesToBase64,
    base64ToBytes: base64ToBytes,
    levelToRatio: levelToRatio,
    looksLikeHeadset: looksLikeHeadset,
    summarizeRoute: summarizeRoute,
    scoModeText: scoModeText,
    probeSummary: probeSummary,
    isConnectedState: isConnectedState,
    bluetoothInputAvailable: bluetoothInputAvailable,
    shouldAutoDowngrade: shouldAutoDowngrade,
    autoDowngradeNotice: autoDowngradeNotice,
    nextReconnectDelay: nextReconnectDelay,
    buildLanPreset: buildLanPreset,
    shouldShowGuide: shouldShowGuide,
    TAILSCALE_URL: TAILSCALE_URL,
  };
});
