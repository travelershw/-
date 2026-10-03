/**
 * 原生侧源码生成器(Java 源码以字符串形式返回)。
 *
 * 结构:
 *  - `MainActivity`:一个 WebView 承载打包在 APK 里的本地页面,并把权限/拍照的系统回调转发给桥;
 *  - `QingyuNative`:注入页面的桥(window.QingyuNative),权限、配置、录音、拍照、诊断;
 *  - `QingyuAudio`:16 kHz 单声道录音 + 三档耳机路由 + 独立的 SCO 试验;
 *  - `QingyuCamera`:FileProvider 拍照 + 缩放压缩。
 *
 * 要改原生行为,改这个文件里的字符串,再跑 `node tools/gen-android.js`。
 *
 * 注意:Java/XML 都写在 JS 模板字符串里,所以 Java 代码与注释里不能出现反引号;
 * 要写 Java 的转义引号得写成双反斜杠加引号。
 *
 * @module qingyu-phone/lib/android-sources
 */
import { APPLICATION_ID, DEFAULT_SERVER_ADDRESS, VERSION_NAME } from './android-project.js';

/** Java 源码所在包路径(相对 `app/src/main/java/`)。 */
export const PACKAGE_PATH = APPLICATION_ID.split('.').join('/');

/**
 * 主 Activity:一个 WebView 宿主,加载 `file:///android_asset/index.html`。
 *
 * 页面是打包在 APK 里的本地 asset,不是远程页面:断网也能打开,改页面不用重新部署
 * 服务器。地址/令牌保存在 SharedPreferences,加载时既通过查询参数带给页面(兜底),
 * 页面也能通过桥自己读写(首选)。
 *
 * 权限申请结果与拍照结果必须转发给桥,否则页面永远收不到回调。
 *
 * @returns {string} MainActivity.java 源码。
 */
export function mainActivityJava() {
  return `package ${APPLICATION_ID};

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Menu;
import android.view.MenuItem;
import android.view.WindowManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

/**
 * 轻语手机端的外壳。
 *
 * 页面在 assets 里,原生层只做四件事:开 WebView(给 JS、给 DOM 存储)、挂桥、把记住的
 * 服务器地址与令牌交给页面、把权限与拍照的系统回调转给桥。界面本身全部由页面负责。
 */
public class MainActivity extends Activity {

    private static final String TAG = "Qingyu";
    /** 打包进 APK 的本地页面。 */
    private static final String LOCAL_PAGE = "file:///android_asset/index.html";

    private WebView webView;
    private QingyuNative bridge;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        // 页面用 localStorage 兜底记设置,所以 DOM 存储要开
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(false);
        // 收到她的语音要能直接出声,不能等一次用户手势(页面仍会在首次触摸时 resume)
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(true);
        // 让家里那侧能分辨"装好的 App"和普通浏览器
        settings.setUserAgentString(settings.getUserAgentString() + " QingyuPhoneApp/${VERSION_NAME}");

        webView.setBackgroundColor(Color.parseColor("#15161a"));
        webView.setWebChromeClient(new WebChromeClient());

        // 页面是本地 asset:任何站外跳转都是配置错误,不是功能
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String scheme = uri.getScheme();
                if ("file".equals(scheme) || "about".equals(scheme) || "blob".equals(scheme) || "data".equals(scheme)) {
                    return false;
                }
                Toast.makeText(MainActivity.this, "已拦截站外跳转: " + uri.getHost(), Toast.LENGTH_SHORT).show();
                return true;
            }
        });

        // 桥的名字固定在页面里(QingyuNative),改名字要同时改页面。
        // 桥需要 Activity(申请权限、拉相机)与 WebView(把回调送回页面)。
        bridge = new QingyuNative(this, webView);
        webView.addJavascriptInterface(bridge, QingyuNative.NAME);

        setContentView(webView);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);

        // 别让系统在后台/熄屏时把渲染进程冻掉。
        //
        // 为什么必须这么做:对话模式的切句、发帧、播放全在页面 JS 里,而熄屏后 Chromium 会把
        // 后台 WebView 的渲染进程降级并节流 —— 2026-10-04 实测:手机黑屏 70 秒,页面的
        // 25 秒心跳只跳了一次(间隔被拉到 48.8 秒)。表现就是"插着线看着屏幕时好使,
        // 拔了线放兜里就不响"。RENDERER_PRIORITY_IMPORTANT + waivedWhenNotVisible=false
        // 就是官方给这种情况的开关(API 26+,低版本没有这个策略,只能靠前台服务 + 唤醒锁)。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            webView.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false);
        }

        loadLocalPage();
    }

    /**
     * 熄屏/切后台时不要把 WebView 停下来。
     *
     * 与上面的渲染优先级策略配套:光有优先级还不够,Chromium 在宿主 Activity 停止后
     * 仍可能暂停页面定时器;这里主动 onResume + resumeTimers 把页面留在"运行中"。
     * 代价是后台会多耗一点电,但对话模式本来就需要麦克风、CPU、WiFi 一直在线;
     * 真正长期耗电的是对话模式本身,拿锁/放锁由 beginLiveAudio()/endLiveAudio() 管。
     */
    @Override
    protected void onStop() {
        super.onStop();
        if (webView != null) {
            try {
                webView.resumeTimers();
                webView.onResume();
            } catch (Throwable error) {
                Log.w(TAG, "保持页面运行失败: " + error);
            }
        }
    }

    /**
     * 加载本地页面,并把记住的地址/令牌作为查询参数带过去。
     *
     * 参数只是兜底:页面首选通过桥读 SharedPreferences,这样改设置能立刻生效,也不会
     * 因为清掉 WebView 存储而丢失配置。
     */
    private void loadLocalPage() {
        SharedPreferences prefs = getSharedPreferences(QingyuNative.PREFS, MODE_PRIVATE);
        String address = prefs.getString(QingyuNative.KEY_ADDRESS, "");
        String token = prefs.getString(QingyuNative.KEY_TOKEN, "");

        StringBuilder url = new StringBuilder(LOCAL_PAGE).append("?app=1");
        if (address != null && !address.isEmpty()) {
            url.append("&address=").append(Uri.encode(address));
        }
        if (token != null && !token.isEmpty()) {
            url.append("&token=").append(Uri.encode(token));
        }
        webView.loadUrl(url.toString());
    }

    /** 录音/拍照的运行时权限结果必须转给桥,页面在等 onPermission。 */
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (bridge != null) {
            bridge.onPermissionResult(requestCode, permissions, grantResults);
        }
    }

    /** 相机的返回结果同样要转给桥(它才是拍完做缩放压缩的那一层)。 */
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (bridge != null) {
            bridge.onActivityResult(requestCode, resultCode, data);
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "重载页面");
        menu.add(0, 2, 1, "清除已保存的地址与令牌");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            loadLocalPage();
            return true;
        }
        if (item.getItemId() == 2) {
            getSharedPreferences(QingyuNative.PREFS, MODE_PRIVATE).edit().clear().apply();
            Toast.makeText(this, "已清除,页面里请重新填写地址", Toast.LENGTH_SHORT).show();
            loadLocalPage();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        // 别把麦克风留在录音状态:Activity 没了,页面也就收不到 stopRecording 了
        if (bridge != null) {
            bridge.shutdown();
            bridge = null;
        }
        if (webView != null) {
            webView.removeJavascriptInterface(QingyuNative.NAME);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
`;
}

/**
 * 注入到页面里的桥(`window.QingyuNative`)。
 *
 * 回调的落点:优先调 window.QingyuNative 上的同名函数,拿不到就退回 window.QingyuHooks
 * —— 页面会把回调同时挂到两处。这样即使 WebView 不允许给注入对象加属性,回调也不会
 * 静默丢失(这是真机上最难查的一类问题)。
 *
 * 所有方法都包了 try/catch:桥上的异常对 JS 来说只会变成一个看不见的
 * "Java exception was raised",没法排查。
 *
 * @returns {string} QingyuNative.java 源码。
 */
export function qingyuNativeJava() {
  return `package ${APPLICATION_ID};

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.PowerManager;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONObject;

import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 暴露给页面的原生桥。
 *
 * 每个方法是独立线程调用的,不要在这里存可变状态;共享状态都在 QingyuAudio /
 * QingyuCamera 里,由它们自己保证线程安全。
 */
public class QingyuNative implements QingyuAudio.Listener, QingyuCamera.Listener {

    /** 页面里用的名字:window.QingyuNative。改这里要同时改页面。 */
    public static final String NAME = "QingyuNative";
    public static final String PREFS = "qingyu";
    public static final String KEY_ADDRESS = "address";
    public static final String KEY_TOKEN = "token";
    public static final String VERSION = "${VERSION_NAME}";
    public static final String DEFAULT_ADDRESS = "${DEFAULT_SERVER_ADDRESS}";

    /** 权限请求码,必须唯一;拍照请求码在 QingyuCamera 里(0x5101)。 */
    private static final int REQUEST_AUDIO_PERMISSION = 0x5102;
    private static final int REQUEST_CAMERA_PERMISSION = 0x5103;
    private static final int REQUEST_BT_PERMISSION = 0x5104;
    private static final int REQUEST_NOTIFICATION_PERMISSION = 0x5105;

    private static final String TAG = "Qingyu";

    private final Context context;
    // 弱引用:Activity/WebView 生命周期比桥短,强引用会在旋转屏幕时把旧的整棵视图树留住
    private final WeakReference<Activity> activityRef;
    private final WeakReference<WebView> webViewRef;
    /** 对话/录音期间拿住的 CPU 与 WiFi 锁(见 beginLiveAudio)。 */
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private final QingyuAudio audio;
    private final QingyuCamera camera;

    /**
     * @param activity 宿主 Activity,用来申请权限与拉起相机。
     * @param webView 承载页面的 WebView,用来把回调送回 JS。
     */
    public QingyuNative(Activity activity, WebView webView) {
        this.context = activity.getApplicationContext();
        this.activityRef = new WeakReference<>(activity);
        this.webViewRef = new WeakReference<>(webView);
        this.audio = new QingyuAudio(activity, this);
        this.camera = new QingyuCamera(activity, this);
    }

    // ---------------------------------------------------------------- 设备与权限

    /**
     * 设备与能力信息,给页面的诊断面板用。
     * @return JSON 字符串;失败时也返回一个带 error 字段的 JSON,不抛异常。
     */
    @JavascriptInterface
    public String getInfo() {
        try {
            JSONObject info = new JSONObject();
            info.put("bridge", NAME);
            info.put("app_version", VERSION);
            info.put("android_version", Build.VERSION.RELEASE);
            info.put("sdkInt", Build.VERSION.SDK_INT);
            info.put("device", Build.MANUFACTURER + " " + Build.MODEL);
            info.put("abi", Build.SUPPORTED_ABIS.length > 0 ? Build.SUPPORTED_ABIS[0] : "");
            info.put("recordPermission", permissionState(Manifest.permission.RECORD_AUDIO));
            info.put("cameraPermission", permissionState(Manifest.permission.CAMERA));
            info.put("hasMicrophone", hasFeature(PackageManager.FEATURE_MICROPHONE));
            info.put("hasCamera", hasFeature(PackageManager.FEATURE_CAMERA_ANY));
            info.put("bluetoothScoSupported", audio.bluetoothScoSupported());
            info.put("scoOffCall", audio.scoOffCallAvailable());
            info.put("default_address", DEFAULT_ADDRESS);
            return info.toString();
        } catch (Throwable error) {
            return "{\\"error\\":\\"getInfo 失败: " + error.getClass().getSimpleName() + "\\"}";
        }
    }

    /**
     * 把页面日志打进 logcat:adb logcat -s Qingyu。
     * @param message 页面传来的文本。
     */
    @JavascriptInterface
    public void log(String message) {
        try {
            Log.i(TAG, message == null ? "(null)" : message);
        } catch (Throwable ignored) {
            // 打日志失败没有补救手段,也不能再抛回 JS
        }
    }

    /**
     * 申请录音权限(Android 12+ 连 BLUETOOTH_CONNECT 一起要,否则选不了蓝牙设备)。
     * 结果通过 onPermission 逐个权限回来。
     * @return 请求是否发出去了。
     */
    @JavascriptInterface
    public boolean requestAudioPermission() {
        Activity activity = activityRef.get();
        if (activity == null) {
            return false;
        }
        try {
            List<String> wanted = new ArrayList<>();
            if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                wanted.add(Manifest.permission.RECORD_AUDIO);
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                    && activity.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                wanted.add(Manifest.permission.BLUETOOTH_CONNECT);
            }
            if (wanted.isEmpty()) {
                // 已经都有了:直接报当前状态,页面不用干等系统回调
                callJs("onPermission", "'" + Manifest.permission.RECORD_AUDIO + "',true");
                return true;
            }
            activity.requestPermissions(wanted.toArray(new String[0]), REQUEST_AUDIO_PERMISSION);
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "requestAudioPermission 失败: " + error);
            return false;
        }
    }

    /**
     * 申请蓝牙相关权限:BLUETOOTH_CONNECT 与 BLUETOOTH_SCAN。
     *
     * Android 12+ 没有 CONNECT 就枚举不了蓝牙音频设备;鸿蒙上往往还要 SCAN,否则
     * "可用通信设备"里什么都看不到。Android 11 及以下这些运行时权限不存在,直接回报已允许。
     * @return 请求是否发出去了。
     */
    @JavascriptInterface
    public boolean requestBtPermission() {
        Activity activity = activityRef.get();
        if (activity == null) {
            return false;
        }
        try {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
                callJs("onPermission", "'" + Manifest.permission.BLUETOOTH_CONNECT + "',true");
                return true;
            }
            List<String> wanted = new ArrayList<>();
            if (activity.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                wanted.add(Manifest.permission.BLUETOOTH_CONNECT);
            }
            if (activity.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) {
                wanted.add(Manifest.permission.BLUETOOTH_SCAN);
            }
            if (wanted.isEmpty()) {
                callJs("onPermission", "'" + Manifest.permission.BLUETOOTH_CONNECT + "',true");
                return true;
            }
            activity.requestPermissions(wanted.toArray(new String[0]), REQUEST_BT_PERMISSION);
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "requestBtPermission 失败: " + error);
            return false;
        }
    }

    /**
     * 设置耳机路由方式,让现场能手动在两条路之间切换试。
     * @param mode auto / new / legacy / phone。
     * @return 实际生效的方式。
     */
    @JavascriptInterface
    public String setScoMode(String mode) {
        try {
            String effective = audio.setScoMode(mode);
            Log.i(TAG, "setScoMode(" + mode + ") → " + effective);
            return effective;
        } catch (Throwable error) {
            Log.w(TAG, "setScoMode 失败: " + error);
            return audio.getScoMode();
        }
    }

    /** @return 当前耳机路由方式。 */
    @JavascriptInterface
    public String getScoMode() {
        try {
            return audio.getScoMode();
        } catch (Throwable error) {
            return "auto";
        }
    }

    /**
     * 是否允许档 3(AudioRecord.setPreferredDevice 直接指定蓝牙设备)。
     * @param allow 允许为 true。
     * @return 生效后的值。
     */
    @JavascriptInterface
    public boolean setAllowPreferredDevice(boolean allow) {
        try {
            return audio.setAllowPreferredDevice(allow);
        } catch (Throwable error) {
            Log.w(TAG, "setAllowPreferredDevice 失败: " + error);
            return false;
        }
    }

    /** @return 档 3 是否允许。 */
    @JavascriptInterface
    public boolean getAllowPreferredDevice() {
        try {
            return audio.isPreferredDeviceAllowed();
        } catch (Throwable error) {
            return false;
        }
    }

    /**
     * 手机自己的局域网 IP,给页面上的「同一个 WiFi」预设用(用来猜网段)。
     *
     * 直接读 NetworkInterface,不需要任何权限;Tailscale 的 100.x 属于 CGNAT、不是
     * site-local,所以不会被误当成局域网地址。
     * @return 形如 192.168.1.23;读不到返回空字符串。
     */
    @JavascriptInterface
    public String getWifiIp() {
        try {
            java.util.Enumeration<java.net.NetworkInterface> interfaces = java.net.NetworkInterface.getNetworkInterfaces();
            String fallback = "";
            while (interfaces != null && interfaces.hasMoreElements()) {
                java.net.NetworkInterface network = interfaces.nextElement();
                if (network == null || !network.isUp() || network.isLoopback()) {
                    continue;
                }
                java.util.Enumeration<java.net.InetAddress> addresses = network.getInetAddresses();
                while (addresses.hasMoreElements()) {
                    java.net.InetAddress address = addresses.nextElement();
                    if (!(address instanceof java.net.Inet4Address) || !address.isSiteLocalAddress()) {
                        continue;
                    }
                    String host = address.getHostAddress();
                    if (host == null || host.isEmpty()) {
                        continue;
                    }
                    // WiFi 网卡优先(名字通常以 wlan 开头),其它网卡只作兜底
                    String name = network.getName() == null ? "" : network.getName().toLowerCase(Locale.US);
                    if (name.startsWith("wlan")) {
                        return host;
                    }
                    if (fallback.isEmpty()) {
                        fallback = host;
                    }
                }
            }
            return fallback;
        } catch (Throwable error) {
            Log.w(TAG, "读本机局域网 IP 失败: " + error);
            return "";
        }
    }

    /**
     * 申请相机权限。结果通过 onPermission 回来。
     * @return 请求是否发出去了。
     */
    @JavascriptInterface
    public boolean requestCameraPermission() {
        Activity activity = activityRef.get();
        if (activity == null) {
            return false;
        }
        try {
            if (activity.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                callJs("onPermission", "'" + Manifest.permission.CAMERA + "',true");
                return true;
            }
            activity.requestPermissions(new String[]{Manifest.permission.CAMERA}, REQUEST_CAMERA_PERMISSION);
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "requestCameraPermission 失败: " + error);
            return false;
        }
    }

    // ---------------------------------------------------------------- 录音

    /**
     * 开始录音(16 kHz / 单声道 / PCM16,强制走蓝牙耳机麦) —— 单句模式,30 秒上限。
     * @return 是否真的开起来了;false 时原因会通过 onRecordingStopped 送到页面。
     */
    @JavascriptInterface
    public boolean startRecording() {
        try {
            boolean started = audio.start(30);
            Log.i(TAG, "startRecording → " + started);
            return started;
        } catch (Throwable error) {
            Log.w(TAG, "startRecording 失败: " + error);
            return false;
        }
    }

    /**
     * 开始**对话模式**:连续听,上限放宽到 10 分钟。
     *
     * 页面按静音把这一整段切成一句句自动发送(切句在页面侧做,原生只负责持续供数据);
     * 上限只是兜底,退出对话模式时页面会自己 stopRecording。
     * @return 是否真的开起来了。
     */
    @JavascriptInterface
    public boolean startConversation() {
        try {
            boolean started = audio.start(600);
            Log.i(TAG, "startConversation → " + started);
            return started;
        } catch (Throwable error) {
            Log.w(TAG, "startConversation 失败: " + error);
            return false;
        }
    }

    /** 停止录音并释放麦克风(幂等,重复调用不会崩)。 */
    @JavascriptInterface
    public void stopRecording() {
        try {
            audio.stop();
        } catch (Throwable error) {
            Log.w(TAG, "stopRecording 失败: " + error);
        }
    }

    /**
     * 当前音频路由 + 诊断文本。这是"麦到底走耳机还是走手机"的直接证据。
     * @return JSON:recording / mode / scoOn / communicationDevice / routedDevice /
     *         routedDeviceType / preferredDevice / btEnabled / headsetPermission /
     *         audioPermission / availableCommunication / connectedHeadsets /
     *         bondedDevices / headsetProfileState / a2dpProfileState / inputDevices /
     *         outputDevices / sdkInt / scoMode / allowPreferredDevice / lastAttempt /
     *         lastProbe / diagnostics / hint。
     */
    @JavascriptInterface
    public String getAudioRoute() {
        try {
            return audio.routeJson();
        } catch (Throwable error) {
            return "{\\"error\\":\\"getAudioRoute 失败: " + error.getClass().getSimpleName() + "\\"}";
        }
    }

    /**
     * 开始放她的声音(原生 AudioTrack,优先走耳机通话链路)。
     * @param sampleRate 家里下发的采样率(24000)。
     * @return 是否开起来了(false 时页面退回 Web Audio 外放)。
     */
    @JavascriptInterface
    public boolean startPlayback(int sampleRate) {
        try {
            boolean ok = audio.startPlayback(sampleRate);
            Log.i(TAG, "startPlayback(" + sampleRate + ") → " + ok);
            return ok;
        } catch (Throwable error) {
            Log.w(TAG, "startPlayback 失败: " + error);
            return false;
        }
    }

    /**
     * 写一段她的 PCM(base64)。
     * @param base64Pcm PCM16 小端单声道。
     */
    @JavascriptInterface
    public void writePlayback(String base64Pcm) {
        try {
            audio.player().write(base64Pcm);
        } catch (Throwable error) {
            Log.w(TAG, "writePlayback 失败: " + error);
        }
    }

    /** 这一轮数据发完了:让缓冲区里的放完再释放(尾巴不会被切)。 */
    @JavascriptInterface
    public void finishPlayback() {
        try {
            audio.player().finish();
        } catch (Throwable error) {
            Log.w(TAG, "finishPlayback 失败: " + error);
        }
    }

    /** 还剩多少毫秒放完 —— 页面用它决定什么时候重新开麦。 */
    @JavascriptInterface
    public int playbackRemainingMs() {
        try {
            return audio.player().remainingMs();
        } catch (Throwable error) {
            return 0;
        }
    }

    /** 立刻停掉她的声音(幂等)。 */
    @JavascriptInterface
    public void stopPlayback() {
        try {
            audio.player().stop();
        } catch (Throwable error) {
            Log.w(TAG, "stopPlayback 失败: " + error);
        }
    }

    /**
     * 对话/录音期间拿住 CPU 与 WiFi:熄屏后系统会把 CPU 睡下去、把 WiFi 省电化,
     * 页面被节流 + 网络断断续续,表现就是"看着好使、放兜里就不响"。
     *
     * 只在**开始说话/开始对话**时拿,结束就还回去 —— 长期占着会明显掉电。
     * wake lock 另外带 30 分钟上限兜底:万一页面崩了没来得及还,锁也会自己过期。
     *
     * @return 拿到的锁的名字(空字符串表示没拿到,靠日志排查)。
     */
    @JavascriptInterface
    public String beginLiveAudio() {
        StringBuilder got = new StringBuilder();
        Activity activity = activityRef.get();
        if (activity == null) {
            return "";
        }
        try {
            if (wakeLock == null) {
                PowerManager power = (PowerManager) activity.getSystemService(Context.POWER_SERVICE);
                if (power != null) {
                    wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "qingyu:talk");
                    wakeLock.setReferenceCounted(false);
                }
            }
            if (wakeLock != null && !wakeLock.isHeld()) {
                wakeLock.acquire(30 * 60 * 1000L);
                got.append("cpu");
            }
            if (wifiLock == null) {
                WifiManager wifi = (WifiManager) activity.getApplicationContext()
                        .getSystemService(Context.WIFI_SERVICE);
                if (wifi != null) {
                    wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "qingyu:wifi");
                    wifiLock.setReferenceCounted(false);
                }
            }
            if (wifiLock != null && !wifiLock.isHeld()) {
                wifiLock.acquire();
                if (got.length() > 0) {
                    got.append("+");
                }
                got.append("wifi");
            }
            Log.i(TAG, "beginLiveAudio → " + got);
        } catch (Throwable error) {
            Log.w(TAG, "拿唤醒锁失败: " + error);
        }
        return got.toString();
    }

    /** 说完/关掉对话模式:把锁还回去(幂等)。 */
    @JavascriptInterface
    public void endLiveAudio() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
            if (wifiLock != null && wifiLock.isHeld()) {
                wifiLock.release();
            }
            Log.i(TAG, "endLiveAudio:锁已释放");
        } catch (Throwable error) {
            Log.w(TAG, "释放唤醒锁失败: " + error);
        }
    }

    /**
     * 独立的 SCO 试验:不录音、不用点击说话,直接把蓝牙通话链路按时间线跑一遍,
     * 每一步的结果都记下来。现场判断"系统到底给不给这条链路"就靠它。
     * @return JSON:{ok, conclusion, steps:[...], text:"多行时间线"};text 同时进 diagnostics。
     */
    @JavascriptInterface
    public String probeSco() {
        try {
            return audio.probeSco();
        } catch (Throwable error) {
            Log.w(TAG, "probeSco 失败: " + error);
            return "{\\"error\\":\\"probeSco 失败: " + error.getClass().getSimpleName() + "\\"}";
        }
    }

    // ---------------------------------------------------------------- 拍照

    /**
     * 拉系统相机拍一张。拍完会缩放 + 压成 JPEG,再通过 onPhoto 把 base64 交给页面。
     * @param text 附言(跟着 image 帧一起发),可以为空。
     * @return 相机是否被拉起来了;缺权限时返回 false,页面应先调 requestCameraPermission()。
     */
    @JavascriptInterface
    public boolean capturePhoto(String text) {
        Activity activity = activityRef.get();
        if (activity == null) {
            cameraError("页面还在,但宿主 Activity 不在了(请重新打开 App)");
            return false;
        }
        try {
            if (activity.checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                Log.w(TAG, "capturePhoto 缺少 CAMERA 权限");
                cameraError("还没有相机权限:在系统弹窗里允许,或在设置里给轻语开相机权限");
                return false;
            }
            return camera.capture(text);
        } catch (Throwable error) {
            Log.w(TAG, "capturePhoto 失败: " + error);
            cameraError("拍照调用出错:" + error.getClass().getSimpleName() + " " + error.getMessage());
            return false;
        }
    }

    /** 把拍照失败的原因送回页面(页面会写进帧日志并显示出来)。 */
    private void cameraError(String reason) {
        try {
            callJs("onCameraError", "'" + escapeJs(reason) + "'");
        } catch (Throwable error) {
            Log.w(TAG, "报拍照失败原因出错: " + error);
        }
    }

    // ---------------------------------------------------------------- 配置读写

    /**
     * 读回保存的服务器地址与令牌。
     * @return JSON 字符串,含 address / token / default_address。
     */
    @JavascriptInterface
    public String getConfig() {
        try {
            SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            JSONObject config = new JSONObject();
            config.put("address", prefs.getString(KEY_ADDRESS, ""));
            config.put("token", prefs.getString(KEY_TOKEN, ""));
            config.put("default_address", DEFAULT_ADDRESS);
            return config.toString();
        } catch (Throwable error) {
            return "{\\"error\\":\\"getConfig 失败: " + error.getClass().getSimpleName() + "\\"}";
        }
    }

    /**
     * 保存服务器地址与令牌。
     * @param address 服务器地址,允许为空(表示恢复默认)。
     * @param token 访问令牌,允许为空。
     * @return 是否写入成功。
     */
    @JavascriptInterface
    public boolean saveConfig(String address, String token) {
        try {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .edit()
                    .putString(KEY_ADDRESS, address == null ? "" : address.trim())
                    .putString(KEY_TOKEN, token == null ? "" : token.trim())
                    .apply();
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "saveConfig 失败: " + error);
            return false;
        }
    }

    // ---------------------------------------------------------------- 系统回调(由 MainActivity 转发)

    /**
     * 权限结果转发。逐个权限报给页面,而不是只报一个总的结果。
     * @param requestCode 请求码。
     * @param permissions 权限名。
     * @param grantResults 结果。
     */
    public void onPermissionResult(int requestCode, String[] permissions, int[] grantResults) {
        if (permissions == null) {
            return;
        }
        for (int i = 0; i < permissions.length; i++) {
            boolean granted = i < grantResults.length && grantResults[i] == PackageManager.PERMISSION_GRANTED;
            Log.i(TAG, "权限 " + permissions[i] + " → " + (granted ? "granted" : "denied"));
            callJs("onPermission", "'" + permissions[i] + "'," + granted);
        }
    }

    /**
     * 相机结果转发。
     * @param requestCode 请求码。
     * @param resultCode 结果码。
     * @param data 相机返回的 Intent(用 EXTRA_OUTPUT 时通常是空的)。
     */
    public void onActivityResult(int requestCode, int resultCode, Intent data) {
        camera.onActivityResult(requestCode, resultCode, data);
    }

    /** Activity 销毁时调用:停止录音、释放麦克风与蓝牙代理。 */
    public void shutdown() {
        try {
            audio.release();
        } catch (Throwable error) {
            Log.w(TAG, "shutdown 释放音频资源失败: " + error);
        }
    }

    // ---------------------------------------------------------------- 后台保活

    /**
     * 启动前台服务保活(连接期间用),免得切后台/熄屏被系统掐断连接。
     * Android 13+ 顺带要一下通知权限,否则常驻通知不显示。
     * @return 是否发出去了。
     */
    @JavascriptInterface
    public boolean startKeepAlive() {
        Activity activity = activityRef.get();
        if (activity == null) {
            return false;
        }
        try {
            if (Build.VERSION.SDK_INT >= 33
                    && activity.checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
                // 通知权限被拒也不影响服务本身,所以只是顺带请求一次
                activity.requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, REQUEST_NOTIFICATION_PERMISSION);
            }
            QingyuService.start(activity);
            Log.i(TAG, "已请求前台保活");
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "startKeepAlive 失败: " + error);
            return false;
        }
    }

    /** 停止前台保活。 */
    @JavascriptInterface
    public void stopKeepAlive() {
        Activity activity = activityRef.get();
        if (activity == null) {
            return;
        }
        try {
            QingyuService.stop(activity);
            Log.i(TAG, "已停止前台保活");
        } catch (Throwable error) {
            Log.w(TAG, "stopKeepAlive 失败: " + error);
        }
    }

    // ---------------------------------------------------------------- 录音/拍照回调

    @Override
    public void onChunk(String base64Pcm) {
        callJs("onAudio", "'" + base64Pcm + "'");
    }

    @Override
    public void onLevel(double dbfs) {
        // 用 Locale.US:某些地区的小数点是逗号,到了 JS 里会变成语法错误
        callJs("onLevel", String.format(Locale.US, "%.2f", dbfs));
    }

    @Override
    public void onAutoStop() {
        // 到本次时长上限被强制停掉:页面要据此把"正在录音"的界面收回来
        Log.i(TAG, "录音到达本次时长上限,已自动停止");
        callJs("onRecordingStopped", "'timeout'");
    }

    @Override
    public void onFailed(String reason) {
        // 录音根本没起来(没权限 / 没有可用的通话设备 / 麦克风被占):把可操作的原因交给页面
        callJs("onRecordingStopped", "'" + escapeJs(reason) + "'");
        callJs("onRoute", "'" + escapeJs(audio.routeJson()) + "'");
    }

    @Override
    public void onRouteUpdate(String routeJson) {
        callJs("onRoute", "'" + escapeJs(routeJson) + "'");
    }

    @Override
    public void onRouteAttempt(String text) {
        // 每一档的实测结果都实时写到页面上,现场不用连电脑就能看到
        callJs("onRouteAttempt", "'" + escapeJs(text) + "'");
    }

    @Override
    public void onPhoto(String base64Jpeg, String text) {
        callJs("onPhoto", "'" + base64Jpeg + "','" + escapeJs(text) + "'");
    }

    @Override
    public void onCameraError(String reason) {
        // 拍照没能拉起来的真正原因(权限/没有相机应用/上一次卡住…),页面会写进日志并显示
        callJs("onCameraError", "'" + escapeJs(reason) + "'");
    }

    /**
     * 把一次原生回调送进页面。
     *
     * 先试 window.QingyuNative 上的同名函数(页面的首选落点),拿不到再退回
     * window.QingyuHooks。evaluateJavascript 必须在主线程执行,所以统一 post 到 WebView。
     * @param function 回调名,例如 onAudio。
     * @param arguments 已经拼好的 JS 参数列表(字符串参数自带单引号)。
     */
    private void callJs(String function, String arguments) {
        WebView view = webViewRef.get();
        if (view == null) {
            return;
        }
        String script = "window.QingyuNative&&window.QingyuNative." + function
                + "?window.QingyuNative." + function + "(" + arguments + ")"
                + ":(window.QingyuHooks&&window.QingyuHooks." + function
                + "?window.QingyuHooks." + function + "(" + arguments + "):0)";
        view.post(() -> {
            WebView target = webViewRef.get();
            if (target == null) {
                return;
            }
            try {
                target.evaluateJavascript(script, null);
            } catch (Throwable error) {
                Log.w(TAG, "回调 " + function + " 到页面失败: " + error);
            }
        });
    }

    /**
     * 转义要嵌进单引号 JS 字符串里的文本(拍照附言与诊断文本都是用户可控的)。
     * @param value 原文。
     * @return 转义后的文本。
     */
    private static String escapeJs(String value) {
        if (value == null) {
            return "";
        }
        return value.replace("\\\\", "\\\\\\\\").replace("'", "\\\\'").replace("\\n", "\\\\n").replace("\\r", "\\\\r");
    }

    /**
     * 查询某个运行时权限的当前状态。
     * @param permission 权限名。
     * @return granted / denied / unknown。
     */
    private String permissionState(String permission) {
        try {
            return context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED ? "granted" : "denied";
        } catch (Throwable error) {
            return "unknown";
        }
    }

    /**
     * 查询系统特性。
     * @param feature PackageManager 的特性名。
     * @return 是否有该硬件。
     */
    private boolean hasFeature(String feature) {
        try {
            return context.getPackageManager().hasSystemFeature(feature);
        } catch (Throwable error) {
            return false;
        }
    }
}
`;
}

/**
 * 录音:16 kHz / 单声道 / PCM16,并强制把麦切到蓝牙耳机。
 *
 * 三件事必须按顺序做对,否则录到的是手机自己的麦:
 *  1. `setMode(MODE_IN_COMMUNICATION)`:让系统按"通话"处理音频路由;
 *  2. Android 12+ 试 `setCommunicationDevice()`,不成就换老的
 *     `startBluetoothSco()` + `setBluetoothScoOn(true)`;
 *  3. `AudioSource` 用 `VOICE_COMMUNICATION`(它才会走通信设备),拿不到再退回
 *     `VOICE_RECOGNITION`。
 *
 * 关键一点:每一档的成败都按实际生效的路由(getRoutedDevice().type)判断,不看接口
 * 返回值 —— 华为/鸿蒙上 setCommunicationDevice 会返回 true 但麦并没有切过去。
 *
 * @returns {string} QingyuAudio.java 源码。
 */
export function qingyuAudioJava() {
  return `package ${APPLICATION_ID};

import android.Manifest;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothHeadset;
import android.bluetooth.BluetoothProfile;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 16 kHz / 单声道 / PCM16 录音,强制走蓝牙耳机麦。
 *
 * 每满 100 ms(3200 字节)回调一块 PCM 与一次电平,页面把它们拼起来封 WAV。
 * 时长上限是兜底:单句 30 秒、对话模式 10 分钟(由 start(seconds) 传入),
 * 免得忘了关时麦克风一直开着。
 *
 * 另外提供一个独立的 probeSco():不录音,只把蓝牙通话链路按时间线跑一遍并逐步记录,
 * 用来回答"系统到底给不给三方 App 这条链路"。
 */
public class QingyuAudio {

    /** 录音回调。实现方负责把数据送回页面/日志。 */
    public interface Listener {
        void onChunk(String base64Pcm);
        void onLevel(double dbfs);
        void onAutoStop();
        /** 录音没能开起来的原因(要有可操作的建议,页面会直接显示给用户)。 */
        void onFailed(String reason);
        /** 开始录音后的路由快照:这时 SCO 才刚建起来,设备名最有参考价值。 */
        void onRouteUpdate(String routeJson);
        /** 某一档路由尝试的结果(一行文字),页面会实时写进日志。 */
        void onRouteAttempt(String text);
    }

    public static final int SAMPLE_RATE = 16000;
    public static final int CHANNELS = 1;
    /** 100 ms @ 16 kHz / 单声道 / 16 bit。 */
    private static final int CHUNK_BYTES = 3200;
    /** 单句模式的默认上限(秒)。对话模式由 start(600) 放宽,页面退出时自己停。 */
    private static final int MAX_SECONDS = 30;
    private static final String TAG = "Qingyu";

    /** 档 1 用新接口(setCommunicationDevice)后等多久再查路由。 */
    private static final int NEW_TIER_WAIT_MS = 400;
    /** 档 2 用老接口(startBluetoothSco)后等多久再查路由 —— SCO 建立本来就慢。 */
    private static final int LEGACY_TIER_WAIT_MS = 600;
    /** 档 3(setPreferredDevice)后等多久再查路由。 */
    private static final int PREFERRED_TIER_WAIT_MS = 400;
    /** 挂有线/USB 耳麦后等多久再查路由。 */
    private static final int WIRED_TIER_WAIT_MS = 200;

    /** SCO 试验:每 200 ms 采一次,共 10 次(2 秒)。 */
    private static final int PROBE_INTERVAL_MS = 200;
    private static final int PROBE_SAMPLES = 10;

    private static final String PREFS = "qingyu";
    private static final String KEY_SCO_MODE = "sco_mode";
    private static final String KEY_ALLOW_PREFERRED = "allow_preferred_device";

    private final Context context;
    private final AudioManager audioManager;
    private final Listener listener;

    private AudioRecord record;
    private Thread worker;
    private volatile boolean recording;
    private volatile boolean autoStopped;
    /** 本次录音的时长上限(秒):单句 30,对话模式 600。 */
    private volatile int maxSeconds = MAX_SECONDS;
    private boolean modeChanged;
    private boolean scoStarted;
    private AudioDeviceInfo scoDevice;
    /** 这一轮录音是否已经报过一次路由(SCO 建起来之后那一次)。 */
    private volatile boolean routeReported;

    /** 耳机路由方式:auto(先新后老)/ new / legacy / phone(只用手机麦)。 */
    private volatile String scoMode = "auto";
    /** 档 3(AudioRecord.setPreferredDevice)是实验性的,默认禁用,要在页面上显式打开。 */
    private volatile boolean allowPreferredDevice;
    /** 这一轮的档位尝试记录,失败时原样报给用户。 */
    private final List<String> attemptLog = new ArrayList<>();
    private volatile String lastAttempt = "";
    /** 最近一次 SCO 试验的完整时间线,进 diagnostics。 */
    private volatile String lastProbeText = "";
    /** 放她的声音(原生 AudioTrack,优先走耳机通话链路)。 */
    private final QingyuPlayer player;

    /** HFP 代理:getProfileProxy 是异步的,必须保存监听器并等 onServiceConnected。 */
    private volatile BluetoothHeadset headsetProxy;
    private volatile boolean headsetProxyRequested;
    /** 最近一次真的收到音频数据的时刻(用来发现"路由看起来好了、其实没有声音")。 */
    private volatile long lastDataAt;
    /** 已经为此降级过一次就不再重复降级(免得来回切换)。 */
    private volatile boolean fellBackToPhoneMic;
    /** "被别的 App 静音"这件事只报一次。 */
    private volatile boolean silenceReported;
    /** 给耳机多久时间送第一块数据;超过就认为它那路是死的。 */
    private static final long SILENT_ROUTE_GRACE_MS = 1500;
    /** 低于这个电平就当作"数字静音"(全零块)—— 真麦克风再安静也有本底噪声。 */
    private static final double SILENT_FLOOR_DBFS = -90.0;

    /**
     * @param context Activity(取 AudioManager 要用)。
     * @param listener 回调。
     */
    public QingyuAudio(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.audioManager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        this.listener = listener;
        // 播放归音频这边一起管:routeJson() 才能把"她的声音从哪个设备出来"一起报上去
        this.player = new QingyuPlayer(this.context, this.audioManager);
        try {
            // 路由方式是现场试出来的结论,必须跨重启记住
            this.scoMode = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_SCO_MODE, "auto");
            this.allowPreferredDevice = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_ALLOW_PREFERRED, false);
        } catch (Throwable error) {
            Log.w(TAG, "读 SCO 方式失败: " + error);
        }
    }

    /** 播放器(页面通过桥调用它放她的声音)。 */
    public QingyuPlayer player() {
        return player;
    }

    /**
     * 开始放她的声音。
     *
     * 关键判断:**这一轮能不能用通信用法**(用错了就"耳机不出声"):
     * · 有线/USB 耳麦 → 可以,声音直接进耳麦;
     * · 我们这轮真的把 SCO 建起来了 → 可以;
     * · 耳机那路已经被判定为"没有声音"(降级过)→ 不行,走媒体用法 —— 这时 SCO 已经放掉,
     *   A2DP 恢复,声音从 A2DP 进耳机(音质反而更好);
     * · 还发现系统里挂着**不是我们开的** SCO(上次异常退出/别的 App 留下的)→ 先清掉它,
     *   否则它压着 A2DP,媒体也出不去,耳机就彻底没声音。
     *
     * @param rate 采样率。
     * @return 是否开起来了。
     */
    public synchronized boolean startPlayback(int rate) {
        boolean allowVoice;
        if (fellBackToPhoneMic) {
            allowVoice = false;
        } else if (hasWiredHeadset()) {
            allowVoice = true;
        } else if (scoStarted) {
            allowVoice = true;
        } else {
            if (scoOn()) {
                // 不是我们开的 SCO:清掉,让 A2DP 回来
                Log.i(TAG, "发现会话外的 SCO,先关掉它再播放");
                try {
                    audioManager.setBluetoothScoOn(false);
                    audioManager.stopBluetoothSco();
                } catch (Throwable error) {
                    Log.w(TAG, "关掉遗留 SCO 失败: " + error);
                }
            }
            allowVoice = false;
        }
        Log.i(TAG, "startPlayback(" + rate + ") allowVoice=" + allowVoice);
        return player.start(rate, allowVoice);
    }

    /**
     * 现在哪些 App 在用麦克风、我们有没有被系统静音。
     *
     * 为什么需要它:2026-10-04 实测到"数据一直在流、但每一块都是数字零"的状态 ——
     * 那是 Android 对**被抢占的录音方**的标准做法(另一个 App 拿着麦克风时,后来者收到静音)。
     * 有这一行就能一眼分清是"耳机没供麦"还是"麦克风被别人占着",不用再猜。
     *
     * @return 一行摘要,例如 "会话 1234(我们,录音中) | 会话 5678(别的 App)"。
     */
    private String activeRecordersText() {
        if (audioManager == null) {
            return "(读不到)";
        }
        try {
            int ours = ourSessionId();
            int others = 0;
            StringBuilder sb = new StringBuilder();
            for (AudioRecordingConfiguration cfg : audioManager.getActiveRecordingConfigurations()) {
                if (sb.length() > 0) {
                    sb.append(" | ");
                }
                int session = cfg.getClientAudioSessionId();
                boolean mine = ours != 0 && session == ours;
                if (!mine) {
                    others += 1;
                }
                sb.append("会话 ").append(session);
                sb.append(mine ? "(我们," : "(别的 App,");
                sb.append(cfg.isClientSilenced() ? "被静音)" : "录音中)");
            }
            if (sb.length() == 0) {
                return "(没有 App 在录音)";
            }
            if (others > 0) {
                sb.append("  ← 另有 ").append(others).append(" 个 App 也在用麦克风");
            }
            return sb.toString();
        } catch (Throwable error) {
            return "(读取失败: " + error.getClass().getSimpleName() + ")";
        }
    }

    /** 我们自己是不是正被系统静音(另一个 App 拿着麦克风时的典型状态)。 */
    private boolean isOurRecordingSilenced() {
        if (audioManager == null) {
            return false;
        }
        int ours = ourSessionId();
        if (ours == 0) {
            return false;
        }
        try {
            for (AudioRecordingConfiguration cfg : audioManager.getActiveRecordingConfigurations()) {
                if (cfg.getClientAudioSessionId() == ours) {
                    return cfg.isClientSilenced();
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "查录音静音状态失败: " + error);
        }
        return false;
    }

    /** 我们这条 AudioRecord 的 session id(0 表示现在没在录)。 */
    private int ourSessionId() {
        AudioRecord active = record;
        if (active == null) {
            return 0;
        }
        try {
            return active.getAudioSessionId();
        } catch (Throwable error) {
            return 0;
        }
    }

    /** 现在有没有有线/USB 耳麦(这两种走通信用法总是对的)。 */
    private boolean hasWiredHeadset() {        if (audioManager == null) {
            return false;
        }
        try {
            for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                int type = device.getType();
                if (type == AudioDeviceInfo.TYPE_WIRED_HEADSET || type == AudioDeviceInfo.TYPE_USB_HEADSET) {
                    return true;
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "查有线耳麦失败: " + error);
        }
        return false;
    }

    /** @return 是否正在录音。 */
    public synchronized boolean isRecording() {
        return recording;
    }

    /**
     * 设置耳机路由方式。
     * @param mode auto(先新后老)/ new(只用新接口)/ legacy(只用老接口)/ phone(只用手机麦,退路)。
     * @return 实际生效的方式。
     */
    public String setScoMode(String mode) {
        String wanted = mode == null ? "" : mode.trim().toLowerCase(Locale.US);
        if (!"auto".equals(wanted) && !"new".equals(wanted) && !"legacy".equals(wanted) && !"phone".equals(wanted)) {
            Log.w(TAG, "未知的 SCO 方式: " + mode);
            return scoMode;
        }
        scoMode = wanted;
        try {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_SCO_MODE, wanted).apply();
        } catch (Throwable error) {
            Log.w(TAG, "存 SCO 方式失败: " + error);
        }
        Log.i(TAG, "SCO 方式已设为 " + wanted);
        return scoMode;
    }

    /** @return 当前耳机路由方式。 */
    public String getScoMode() {
        return scoMode;
    }

    /**
     * 是否允许档 3(AudioRecord.setPreferredDevice 直接指定蓝牙设备)。
     * @param allow 允许为 true。
     * @return 生效后的值。
     */
    public boolean setAllowPreferredDevice(boolean allow) {
        allowPreferredDevice = allow;
        try {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_ALLOW_PREFERRED, allow).apply();
        } catch (Throwable error) {
            Log.w(TAG, "存档3开关失败: " + error);
        }
        return allowPreferredDevice;
    }

    /** @return 档 3 是否允许。 */
    public boolean isPreferredDeviceAllowed() {
        return allowPreferredDevice;
    }

    /**
     * 开始录音。
     *
     * 路由走"三档尝试",每一档都按实际生效的路由判断成败。三档都不成就明确失败,
     * 绝不静默用手机麦。
     *
     * 注意:这个方法里最多会等 400+600 ms(档 3 再 400 ms)。它跑在 WebView 的桥线程上,
     * 不阻塞界面渲染;音频在等之前就已经开始采集了(先起线程再切路由),所以不会丢开头。
     *
     * @return 是否真的开起来了(权限、设备、路由任一环节失败都返回 false)。
     */
    public synchronized boolean start() {
        return start(MAX_SECONDS);
    }

    /**
     * 带时长上限的启动。
     *
     * @param seconds 本次录音的上限秒数(单句 30;对话模式 600,页面退出时自己停)。
     * @return 是否真的开起来了。
     */
    public synchronized boolean start(int seconds) {
        if (recording) {
            return true;
        }
        maxSeconds = Math.max(5, Math.min(3600, seconds));
        if (audioManager == null) {
            fail("系统 AudioManager 不可用");
            return false;
        }
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail("还没给麦克风权限:点页面上的『申请权限』,或在系统设置里允许轻语使用麦克风");
            return false;
        }

        record = openRecord();
        if (record == null) {
            fail("麦克风打不开(可能被别的应用占用了)");
            restoreRoute();
            return false;
        }
        try {
            record.startRecording();
        } catch (Throwable error) {
            Log.w(TAG, "startRecording 抛异常: " + error);
        }
        if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            releaseRecord();
            restoreRoute();
            fail("麦克风没能进入录音状态,请重按一次");
            return false;
        }

        // 先把采集线程起起来:路由切换要等几百毫秒,这期间的数据不能被丢掉
        recording = true;
        autoStopped = false;
        routeReported = false;
        // 以"此刻"作为基准:开录后 1.5 秒内没听到任何真实声音,就认为这条路由是死的
        lastDataAt = SystemClock.elapsedRealtime();
        fellBackToPhoneMic = false;
        silenceReported = false;
        worker = new Thread(this::loop, "qingyu-audio");
        worker.start();

        boolean routed = routeToHeadset();
        if (!routed) {
            // **耳机不成也要继续录**:以前这里直接 stop() 并报错返回,结果用户对着耳机说话
            // 时 App 一个字节都不录、页面立刻弹"录音没起来" —— 2026-10-04 用户报的
            // "无法识别我说话"就是这个死路(auto 方式下耳机路由失败 → 干脆不录)。
            // 现在的做法:如实报出耳机为什么不行,然后**用手机麦接着录**,由页面把
            // 「当前使用的麦克风」显示清楚。宁可音质差一点,也不能让人白说。
            String detail = attemptLog.isEmpty() ? "" : ":" + join("; ", attemptLog);
            restoreRoute();
            fellBackToPhoneMic = true;
            lastAttempt = "耳机路由失败(方式 " + scoMode + ")" + detail + " → 改用手机麦";
            try {
                listener.onRouteAttempt(lastAttempt);
            } catch (Throwable error) {
                Log.w(TAG, "报降级失败: " + error);
            }
            Log.w(TAG, "耳机路由失败,改用手机麦:" + lastAttempt);
        }

        Log.i(TAG, "开始录音,路由 " + routeJson());
        return true;
    }

    /**
     * 报一次失败原因给页面(并顺手把当前路由打进 logcat,方便现场对照)。
     * @param reason 给用户看的一句话,要能直接照做。
     */
    private void fail(String reason) {
        Log.w(TAG, "录音未能开始:" + reason);
        try {
            listener.onFailed(reason);
        } catch (Throwable error) {
            Log.w(TAG, "回报失败原因出错: " + error);
        }
    }

    /** 停止录音并释放麦克风。幂等:重复调用、没在录时调用都不会崩。 */
    public synchronized void stop() {
        boolean wasRecording = recording;
        recording = false;

        Thread thread = worker;
        worker = null;
        AudioRecord active = record;
        record = null;

        if (active != null) {
            // 先 stop 让阻塞中的 read 返回,再 join 线程,最后 release
            try {
                active.stop();
            } catch (Throwable error) {
                Log.w(TAG, "AudioRecord.stop 失败: " + error);
            }
            if (thread != null && thread != Thread.currentThread()) {
                try {
                    thread.join(500);
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                }
            }
            try {
                active.release();
            } catch (Throwable error) {
                Log.w(TAG, "AudioRecord.release 失败: " + error);
            }
        }

        restoreRoute();
        if (wasRecording) {
            Log.i(TAG, "已停止录音并还原音频路由");
        }
    }

    /** Activity 销毁时调用:停止录音并关掉蓝牙代理。 */
    public synchronized void release() {
        stop();
        BluetoothAdapter adapter = bluetoothAdapter();
        BluetoothHeadset proxy = headsetProxy;
        if (adapter != null && proxy != null) {
            try {
                adapter.closeProfileProxy(BluetoothProfile.HEADSET, proxy);
            } catch (Throwable error) {
                Log.w(TAG, "关蓝牙代理失败: " + error);
            }
        }
        headsetProxy = null;
        headsetProxyRequested = false;
    }

    /**
     * 当前音频路由 + 诊断信息。
     * @return JSON,字段见 QingyuNative.getAudioRoute 的说明。
     */
    public String routeJson() {
        try {
            JSONObject json = new JSONObject();
            json.put("recording", recording);
            json.put("mode", modeName());
            json.put("scoOn", scoOn());
            json.put("communicationDevice", communicationDeviceName());
            // 没在录音时 AudioRecord 根本不存在,routedDevice 只能是空 —— 这不是故障
            json.put("routedDevice", routedDeviceName());
            json.put("routedDeviceType", routedDeviceTypeName());
            json.put("preferredDevice", preferredDeviceName());
            json.put("btEnabled", bluetoothEnabled());
            json.put("headsetPermission", headsetPermissionState());
            json.put("audioPermission", audioPermissionState());
            json.put("availableCommunication", toJsonArray(availableCommunicationList()));
            // 代理没就绪时这里会是字符串"未就绪…",不是空数组 —— 空数组会被读成"真的没连"
            json.put("connectedHeadsets", connectedHeadsetsValue());
            json.put("bondedDevices", toJsonArray(bondedDeviceNames()));
            json.put("headsetProfileState", profileStateText(BluetoothProfile.HEADSET));
            json.put("a2dpProfileState", profileStateText(BluetoothProfile.A2DP));
            json.put("inputDevices", toJsonArray(deviceList(AudioManager.GET_DEVICES_INPUTS)));
            json.put("outputDevices", toJsonArray(deviceList(AudioManager.GET_DEVICES_OUTPUTS)));
            json.put("sdkInt", Build.VERSION.SDK_INT);
            json.put("sampleRate", SAMPLE_RATE);
            json.put("scoMode", scoMode);
            json.put("allowPreferredDevice", allowPreferredDevice);
            json.put("lastAttempt", lastAttempt);
            json.put("lastProbe", lastProbeText);
            // 一眼看清现在用的是哪个麦克风:耳机麦 / 有线耳麦 / 手机麦
            json.put("currentMic", currentMicLabel());
            json.put("currentMicKind", currentMicKind());
            // 她的声音现在从哪个设备出来、用的哪种用法(不用猜,看这两条)
            json.put("playerRoute", player.routedName());
            json.put("playerUsage", player.usageName());
            // 现在谁在用麦克风、我们是不是被系统静音了 —— "收到全零"最常见的原因就是这个
            json.put("activeRecorders", activeRecordersText());
            // 一整段可直接粘贴的纯文本:页面的「复制诊断」按钮就用它
            json.put("diagnostics", diagnostics());
            json.put("hint", hint());
            return json.toString();
        } catch (Throwable error) {
            return "{\\"error\\":\\"routeJson 失败: " + error.getClass().getSimpleName() + "\\"}";
        }
    }

    /**
     * 独立的 SCO 试验:不录音,把蓝牙通话链路按时间线跑一遍,每一步都记结果。
     *
     * 存在的理由:现场(华为鸿蒙)出现过"setCommunicationDevice 返回 true 但麦没切过去",
     * 也出现过"SCO 建立要一秒多,一枪打不中就被判失败"。所以这里不录音、不猜,只把每一步
     * 的实测状态按时间采下来,让人自己看结论。
     *
     * 大约耗时 3 秒(400 ms + 10 x 200 ms),跑在桥线程上,不阻塞界面渲染。
     * @return JSON:{ok, conclusion, steps:[...], text:"多行时间线"}。
     */
    public synchronized String probeSco() {
        List<String> steps = new ArrayList<>();
        StringBuilder text = new StringBuilder();
        text.append("SCO 试验 ").append(timestamp()).append('\\n');
        long startedAt = SystemClock.elapsedRealtime();
        boolean sawType7 = false;
        boolean sawScoOn = false;
        boolean probeOk = false;
        String conclusion;

        if (recording) {
            conclusion = "正在录音,先松手停止录音再做这个试验";
            text.append(conclusion).append('\\n');
            lastProbeText = text.toString();
            return probeResultJson(false, conclusion, steps, text.toString());
        }

        try {
            step(steps, text, startedAt, "开始:方式=" + scoMode
                    + ", mode=" + modeName()
                    + ", scoOn=" + rawScoOn()
                    + ", HFP=" + profileStateText(BluetoothProfile.HEADSET)
                    + ", A2DP=" + profileStateText(BluetoothProfile.A2DP));
            step(steps, text, startedAt, "输入设备=" + join(" / ", deviceList(AudioManager.GET_DEVICES_INPUTS)));
            step(steps, text, startedAt, "可用通信设备=" + join(" / ", availableCommunicationList()));

            setCommunicationMode();
            step(steps, text, startedAt, "已 setMode(MODE_IN_COMMUNICATION),现在 mode=" + modeName());

            AudioDeviceInfo device = findCommunicationDevice();
            if (device == null) {
                step(steps, text, startedAt, "档1 跳过:可用通信设备里没有蓝牙项");
            } else {
                boolean accepted = false;
                try {
                    accepted = audioManager.setCommunicationDevice(device);
                } catch (Throwable error) {
                    step(steps, text, startedAt, "档1 setCommunicationDevice 异常: " + error);
                }
                step(steps, text, startedAt, "档1 setCommunicationDevice(" + device.getProductName()
                        + ") → " + accepted);
                sleep(NEW_TIER_WAIT_MS);
                step(steps, text, startedAt, "档1 之后(" + NEW_TIER_WAIT_MS + " ms):communicationDevice="
                        + orNone(communicationDeviceName()));
                List<String> afterNew = deviceList(AudioManager.GET_DEVICES_INPUTS);
                sawType7 = sawType7 || hasBluetoothScoInput(afterNew);
                step(steps, text, startedAt, "档1 之后输入设备=" + join(" / ", afterNew)
                        + (hasBluetoothScoInput(afterNew) ? "  ← 出现蓝牙 SCO" : ""));
            }

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                try {
                    audioManager.clearCommunicationDevice();
                } catch (Throwable error) {
                    Log.w(TAG, "clearCommunicationDevice 失败: " + error);
                }
            }
            audioManager.startBluetoothSco();
            audioManager.setBluetoothScoOn(true);
            scoStarted = true;
            step(steps, text, startedAt, "档2 已 startBluetoothSco + setBluetoothScoOn(true)");

            // SCO 建立是异步的:每 200 ms 采一次,共 2 秒,别一枪打不中就判失败
            for (int i = 1; i <= PROBE_SAMPLES; i++) {
                sleep(PROBE_INTERVAL_MS);
                boolean on = rawScoOn();
                List<String> inputs = deviceList(AudioManager.GET_DEVICES_INPUTS);
                boolean type7 = hasBluetoothScoInput(inputs);
                sawScoOn = sawScoOn || on;
                sawType7 = sawType7 || type7;
                step(steps, text, startedAt, "档2 采样 " + i + "/" + PROBE_SAMPLES
                        + ": scoOn=" + on
                        + ", HFP=" + profileStateText(BluetoothProfile.HEADSET)
                        + ", A2DP=" + profileStateText(BluetoothProfile.A2DP)
                        + ", 输入设备=" + join(" / ", inputs));
                if (on && type7) {
                    step(steps, text, startedAt, "SCO 已建立,提前结束采样");
                    break;
                }
            }
            probeOk = sawType7 || sawScoOn;
        } catch (Throwable error) {
            step(steps, text, startedAt, "试验异常: " + error);
        } finally {
            // 收尾:老接口和新接口都要还原,别把系统音频状态留在通信模式
            try {
                audioManager.stopBluetoothSco();
                audioManager.setBluetoothScoOn(false);
            } catch (Throwable error) {
                Log.w(TAG, "收尾停 SCO 失败: " + error);
            }
            scoStarted = false;
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    audioManager.clearCommunicationDevice();
                }
                audioManager.setMode(AudioManager.MODE_NORMAL);
                modeChanged = false;
            } catch (Throwable error) {
                Log.w(TAG, "收尾还原路由失败: " + error);
            }
            step(steps, text, startedAt, "收尾:stopBluetoothSco + setBluetoothScoOn(false) + clearCommunicationDevice + MODE_NORMAL");
        }

        boolean a2dpConnected = profileStateText(BluetoothProfile.A2DP).startsWith("STATE_CONNECTED");
        boolean hfpConnected = profileStateText(BluetoothProfile.HEADSET).startsWith("STATE_CONNECTED");
        if (sawType7) {
            conclusion = "蓝牙 SCO 链路建起来了:输入设备里出现了 TYPE_BLUETOOTH_SCO(7) ⇒ 录音应当能走耳机";
        } else if (sawScoOn) {
            conclusion = "系统说 SCO 开了(isBluetoothScoOn=true),但输入设备里始终没有 TYPE_BLUETOOTH_SCO(7) ⇒ 系统没把 SCO 输入暴露给普通 App 的录音";
        } else if (hfpConnected) {
            conclusion = "HFP 报已连接,但 2 秒内 SCO 没起来、设备表里也没有 TYPE_BLUETOOTH_SCO(7) ⇒ 系统没给这条链路:可试 ①在蓝牙设置里关掉再打开『通话音频』 ②取消配对后重连 ③用有线或 USB-C 麦克风";
        } else if (a2dpConnected) {
            conclusion = "A2DP(媒体音频)是连着的,但 HFP/SCO 一直没建立 ⇒ 耳机只被当成播放设备:检查『通话音频』是否真的打开,或取消配对重连";
        } else {
            conclusion = "SCO 与 A2DP 都没建立 ⇒ 这台手机的音频里看不到耳机,先在系统蓝牙里确认它真的连着";
        }
        text.append("结论:").append(conclusion).append('\\n');
        lastProbeText = text.toString();
        Log.i(TAG, "SCO 试验结果:" + conclusion);
        return probeResultJson(probeOk, conclusion, steps, text.toString());
    }

    /**
     * 拼 SCO 试验的返回 JSON。
     * @param ok 是否观察到 SCO 建立。
     * @param conclusion 结论文字。
     * @param steps 时间线。
     * @param text 多行文本。
     * @return JSON 字符串。
     */
    private String probeResultJson(boolean ok, String conclusion, List<String> steps, String text) {
        try {
            JSONObject json = new JSONObject();
            json.put("ok", ok);
            json.put("conclusion", conclusion);
            JSONArray array = new JSONArray();
            for (String line : steps) {
                array.put(line);
            }
            json.put("steps", array);
            json.put("text", text);
            return json.toString();
        } catch (Throwable error) {
            return "{\\"error\\":\\"probeSco 结果拼装失败\\"}";
        }
    }

    /**
     * 记一行试验时间线。
     * @param steps 步骤列表。
     * @param text 多行文本。
     * @param startedAt 开始时间(用来算相对秒数)。
     * @param message 内容。
     */
    private void step(List<String> steps, StringBuilder text, long startedAt, String message) {
        String line = "[" + String.format(Locale.US, "%.2f", (SystemClock.elapsedRealtime() - startedAt) / 1000.0) + "s] " + message;
        steps.add(line);
        text.append(line).append('\\n');
        Log.i(TAG, line);
    }

    /**
     * 输入设备列表里有没有蓝牙 SCO —— 这是"链路到底通没通"的硬证据。
     * @param entries 设备列表文字。
     * @return 出现蓝牙 SCO / BLE 耳机为 true。
     */
    private boolean hasBluetoothScoInput(List<String> entries) {
        for (String entry : entries) {
            if (entry.contains("TYPE_BLUETOOTH_SCO") || entry.contains("TYPE_BLE_HEADSET")) {
                return true;
            }
        }
        return false;
    }

    /**
     * 拼一份人能读、能直接粘到聊天里的诊断文本。
     *
     * 远程排障全靠它:现场只要点一下「复制诊断」,这边就能看到完整的设备/权限/路由/档位
     * 尝试/最近一次 SCO 试验,不用再一轮轮猜。
     * @return 多行纯文本。
     */
    public String diagnostics() {
        StringBuilder sb = new StringBuilder();
        try {
            sb.append("轻语诊断 ").append(timestamp()).append('\\n');
            sb.append("应用 ").append(QingyuNative.VERSION)
                    .append(" / Android ").append(Build.VERSION.RELEASE)
                    .append(" (SDK ").append(Build.VERSION.SDK_INT).append(")")
                    .append(" / ").append(Build.MANUFACTURER).append(' ').append(Build.MODEL).append('\\n');
            sb.append("SCO 方式 ").append(scoMode)
                    .append(allowPreferredDevice ? "(已允许档3 首选设备)" : "(档3 未启用)").append('\\n');
            sb.append("录音中 ").append(recording).append('\\n');
            sb.append("AudioManager mode ").append(modeName()).append('\\n');
            sb.append("SCO 生效 ").append(scoOn()).append('\\n');
            sb.append("蓝牙开关 ").append(bluetoothEnabled()).append('\\n');
            sb.append("HFP 状态 ").append(profileStateText(BluetoothProfile.HEADSET)).append('\\n');
            sb.append("A2DP 状态 ").append(profileStateText(BluetoothProfile.A2DP)).append('\\n');
            sb.append("权限 录音=").append(audioPermissionState())
                    .append(" 附近的设备=").append(headsetPermissionState()).append('\\n');
            sb.append("可用通信设备 ").append(join(" / ", availableCommunicationList())).append('\\n');
            sb.append("已连接耳机 ").append(connectedHeadsetsText()).append('\\n');
            sb.append("已配对设备 ").append(join(" / ", bondedDeviceNames())).append('\\n');
            sb.append("当前通信设备 ").append(orNone(communicationDeviceName())).append('\\n');
            sb.append("实际输入设备 ").append(orNone(routedDeviceName()))
                    .append(" [").append(orNone(routedDeviceTypeName())).append("]").append('\\n');
            sb.append("首选设备 ").append(orNone(preferredDeviceName())).append('\\n');
            sb.append("输入设备 ").append(join(" / ", deviceList(AudioManager.GET_DEVICES_INPUTS))).append('\\n');
            sb.append("输出设备 ").append(join(" / ", deviceList(AudioManager.GET_DEVICES_OUTPUTS))).append('\\n');
            sb.append("档位尝试:").append('\\n');
            if (attemptLog.isEmpty()) {
                sb.append("  (本次启动还没录过音;点「点击说话」会依次试)").append('\\n');
            } else {
                for (String line : attemptLog) {
                    sb.append("  ").append(line).append('\\n');
                }
            }
            if (lastProbeText.isEmpty()) {
                sb.append("SCO 试验:(还没做过;点页面上的『SCO 试验』)").append('\\n');
            } else {
                sb.append("SCO 试验:").append('\\n').append(lastProbeText);
            }
            sb.append("结论 ").append(hint()).append('\\n');
        } catch (Throwable error) {
            sb.append("生成诊断失败: ").append(error).append('\\n');
        }
        return sb.toString();
    }

    /** @return 当前时间,诊断文本用。 */
    private String timestamp() {
        try {
            return new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new java.util.Date());
        } catch (Throwable error) {
            return "?";
        }
    }

    /** @return 空字符串换成 (无),诊断文本用。 */
    private String orNone(String value) {
        return value == null || value.isEmpty() ? "(无)" : value;
    }

    /**
     * 手写 join:minSdk 24 上不能直接用 String.join(API 26+)。
     * @param separator 分隔符。
     * @param items 条目。
     * @return 拼好的文本,空列表返回 (空)。
     */
    private String join(String separator, List<String> items) {
        if (items == null || items.isEmpty()) {
            return "(空)";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) {
                sb.append(separator);
            }
            sb.append(items.get(i));
        }
        return sb.toString();
    }

    /**
     * List&lt;String&gt; → JSON 数组。
     *
     * 必须显式转换:直接 json.put(name, list) 时 org.json 会把它写成**字符串**
     * (Java 的 [A, B, C] 形式),页面按数组用就会抛
     * "available.filter is not a function" —— 2026-10-03 实测到这句异常把每轮录音都
     * 静默丢掉了(它排在"把录音排上发送"之前)。只有 JSONArray 才是真数组。
     * @param items 条目。
     * @return JSON 数组(永不为 null)。
     */
    private JSONArray toJsonArray(List<String> items) {
        JSONArray array = new JSONArray();
        if (items != null) {
            for (String item : items) {
                array.put(item);
            }
        }
        return array;
    }

    /**
     * 生成一句用户能直接照做的结论。
     *
     * 顺序就是排查顺序:权限 → 用户选的退路 → 蓝牙开关 → 上次档位尝试 → 可用通话设备 →
     * 录音时实际走的设备。关键认知:蓝牙耳机的输入设备在 SCO/HFP 链路建立之前可能根本不在
     * 输入设备列表里,所以"没录音时 inputDevices 里没有耳机"是正常的。
     * @return 一句话提示。
     */
    private String hint() {
        if (!"granted".equals(audioPermissionState())) {
            return "还没给麦克风权限:点『申请权限』,或在系统设置里允许轻语使用麦克风";
        }
        if ("denied".equals(headsetPermissionState())) {
            return "还没给『附近的设备』权限(Android 12+ 必需):点『申请权限』";
        }
        if ("phone".equals(scoMode)) {
            return "当前是『只用手机麦』方式:耳机只用来放声音。要试耳机麦请把『SCO 方式』换回自动或老接口";
        }
        if (!bluetoothEnabled()) {
            return "手机蓝牙没开:先打开蓝牙并连上耳机";
        }

        String routed = routedDeviceName();
        if (recording) {
            if (routed.isEmpty()) {
                return "正在录音,但还没读出实际输入设备:看『AudioRecord 实际输入』这一行,或重按一次";
            }
            if (isBluetoothName(routed)) {
                return "正在用耳机麦克风收音,路由正确";
            }
            return "正在录音,但实际输入是「" + routed + "」而不是耳机:请检查耳机『通话音频』开关";
        }

        // 有线/USB 耳麦是最稳的路;有就直说
        AudioDeviceInfo wired = findWiredHeadsetInput();
        if (wired != null) {
            return "检测到 " + wired.getProductName() + " [有线/USB 耳麦]:录音会自动用它,不依赖蓝牙 SCO";
        }

        // 已经试过档位但全失败:把上一次的结论原样端出来,这是最有用的信息
        if (!lastAttempt.isEmpty() && lastAttempt.contains("失败")) {
            return "上次录音的路由尝试:" + lastAttempt + " —— 点『SCO 试验』看时间线,并把『SCO 方式』换成另一种";
        }

        boolean modern = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S;
        boolean hasBluetoothComm = false;
        for (String entry : availableCommunicationList()) {
            if (entry.contains("BLUETOOTH") || entry.contains("BLE_")) {
                hasBluetoothComm = true;
                break;
            }
        }
        boolean hfpConnected = profileStateText(BluetoothProfile.HEADSET).startsWith("STATE_CONNECTED");
        boolean a2dpConnected = profileStateText(BluetoothProfile.A2DP).startsWith("STATE_CONNECTED");

        if (modern && !hasBluetoothComm) {
            if (a2dpConnected && !hfpConnected) {
                return "耳机只连上了媒体音频(A2DP),通话(HFP)没建立:去 设置 → 蓝牙 → 耳机 → 打开『通话音频』,或取消配对重连;点『SCO 试验』看时间线";
            }
            if (a2dpConnected && hfpConnected) {
                return "HFP 报已连接,但系统没给出蓝牙通话设备:点『SCO 试验』看 SCO 到底能不能建起来(华为上可能不给三方 App)";
            }
            return "系统报不出可用的蓝牙通话设备:去 设置 → 蓝牙 → 已配对设备 → 耳机 → 打开『通话音频』(HFP),再回来刷新";
        }
        if (modern) {
            return "耳机可用。未录音时输入设备列表里本来就不会有它(SCO 只在录音时才建),点击说话时会切过去";
        }
        return "点击说话会自动切到耳机;若仍用手机麦,请检查耳机『通话音频』是否打开";
    }

    /** @return 手机是否有蓝牙硬件(耳机连没连是另一回事)。 */
    public boolean bluetoothScoSupported() {
        try {
            return context.getPackageManager().hasSystemFeature(PackageManager.FEATURE_BLUETOOTH);
        } catch (Throwable error) {
            return false;
        }
    }

    /**
     * 系统是否允许非通话状态使用蓝牙 SCO。
     * @return 可用为 true;Android 12+ 没授 BLUETOOTH_CONNECT 时会被系统拒绝,按不可用上报。
     */
    public boolean scoOffCallAvailable() {
        try {
            return audioManager != null && audioManager.isBluetoothScoAvailableOffCall();
        } catch (Throwable error) {
            return false;
        }
    }

    // ---------------------------------------------------------------- 内部实现

    /**
     * 按当前方式把音频路由切到蓝牙耳机,最多尝试三档。
     *
     * 每一档的成败都按实际路由判断(AudioRecord.getRoutedDevice() 的 type 是不是蓝牙),
     * 不看接口返回值:华为/鸿蒙上 setCommunicationDevice 会返回 true,但麦并没有真的切过去。
     *
     * @return 是否确认切到了蓝牙耳机。
     */
    private boolean routeToHeadset() {
        attemptLog.clear();
        lastAttempt = "";

        // 档 W:有线 / USB / BLE 耳麦优先。
        // 这台华为不把蓝牙 SCO 输入给三方 App,所以"耳机麦"唯一还能走的路就是有线或 USB。
        AudioDeviceInfo wired = findWiredHeadsetInput();
        if (wired != null) {
            boolean ok = attachInput(wired, WIRED_TIER_WAIT_MS);
            attempt("档W 有线/USB 耳麦 " + wired.getProductName() + " [" + audioTypeLabel(wired.getType())
                    + "]:" + WIRED_TIER_WAIT_MS + " ms 后 " + routeSnapshot() + (ok ? " → 成功" : " → 失败"));
            if (ok) {
                lastAttempt = attemptLog.get(attemptLog.size() - 1);
                return true;
            }
        }

        if ("phone".equals(scoMode)) {
            // 用户显式选的退路(或本机自动降级):不碰蓝牙,就用手机麦
            attempt("档0 只用手机麦:跳过蓝牙路由尝试" + (wired == null ? "(也没找到有线/USB 耳麦)" : ""));
            lastAttempt = "档0 只用手机麦";
            return true;
        }

        setCommunicationMode();

        boolean routed;
        if ("legacy".equals(scoMode)) {
            routed = tryLegacy(LEGACY_TIER_WAIT_MS);
        } else if ("new".equals(scoMode)) {
            routed = tryNew(NEW_TIER_WAIT_MS);
        } else {
            // auto:先试新接口,不成就换老接口。华为上往往只有老的能通。
            routed = tryNew(NEW_TIER_WAIT_MS) || tryLegacy(LEGACY_TIER_WAIT_MS);
        }
        if (!routed && allowPreferredDevice) {
            routed = tryPreferred(PREFERRED_TIER_WAIT_MS);
        }

        if (routed) {
            for (int i = attemptLog.size() - 1; i >= 0; i--) {
                if (attemptLog.get(i).contains("成功")) {
                    lastAttempt = attemptLog.get(i);
                    break;
                }
            }
        } else {
            lastAttempt = join(" | ", attemptLog);
        }
        return routed;
    }

    /** 切到"通信"模式:MODE_IN_COMMUNICATION 是通信设备路由生效的前提。 */
    private void setCommunicationMode() {
        try {
            if (audioManager.getMode() != AudioManager.MODE_IN_COMMUNICATION) {
                audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
                modeChanged = true;
            }
        } catch (Throwable error) {
            Log.w(TAG, "setMode(MODE_IN_COMMUNICATION) 失败: " + error);
        }
    }

    /**
     * 档 1:新接口 setCommunicationDevice(Android 12+)。
     * @param waitMs 切换后等多久再查实际路由。
     * @return 是否成功切到蓝牙。
     */
    private boolean tryNew(int waitMs) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            attempt("档1 新接口 setCommunicationDevice:系统低于 Android 12,跳过");
            return false;
        }
        AudioDeviceInfo device = findCommunicationDevice();
        if (device == null) {
            attempt("档1 新接口 setCommunicationDevice:失败 —— 系统没报出可用的蓝牙通信设备");
            return false;
        }
        boolean accepted;
        try {
            scoDevice = device;
            accepted = audioManager.setCommunicationDevice(device);
        } catch (Throwable error) {
            attempt("档1 新接口 setCommunicationDevice:异常 " + error);
            return false;
        }
        sleep(waitMs);
        boolean routed = isRoutedToBluetooth();
        attempt("档1 新接口 setCommunicationDevice(" + device.getProductName() + "):接口返回 " + accepted
                + "," + waitMs + " ms 后 " + routeSnapshot() + (routed ? " → 成功" : " → 失败"));
        return routed;
    }

    /**
     * 档 2:老接口 startBluetoothSco + setBluetoothScoOn(微信这类 App 在华为上走的就是它)。
     * @param waitMs 建立 SCO 后等多久再查实际路由。
     * @return 是否成功切到蓝牙。
     */
    private boolean tryLegacy(int waitMs) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // 新接口可能还占着路由,先清掉,否则测不出老路到底能不能通
                audioManager.clearCommunicationDevice();
            }
            audioManager.startBluetoothSco();
            audioManager.setBluetoothScoOn(true);
            scoStarted = true;
        } catch (Throwable error) {
            attempt("档2 老接口 startBluetoothSco:异常 " + error);
            return false;
        }
        sleep(waitMs);
        boolean routed = isRoutedToBluetooth();
        attempt("档2 老接口 startBluetoothSco + setBluetoothScoOn(true):" + waitMs + " ms 后 "
                + routeSnapshot() + ", scoOn=" + rawScoOn() + (routed ? " → 成功" : " → 失败"));
        return routed;
    }

    /**
     * 档 3(实验,默认禁用):直接给 AudioRecord 指定蓝牙输入设备。
     * @param waitMs 指定后等多久再查实际路由。
     * @return 是否成功切到蓝牙。
     */
    private boolean tryPreferred(int waitMs) {
        AudioDeviceInfo device = findCommunicationDevice();
        if (device == null) {
            attempt("档3 首选设备:没有可指定的蓝牙输入设备");
            return false;
        }
        AudioRecord active = record;
        if (active == null) {
            attempt("档3 首选设备:AudioRecord 已释放,跳过");
            return false;
        }
        boolean accepted;
        try {
            accepted = active.setPreferredDevice(device);
        } catch (Throwable error) {
            attempt("档3 首选设备 setPreferredDevice:异常 " + error);
            return false;
        }
        sleep(waitMs);
        boolean routed = isRoutedToBluetooth();
        attempt("档3 AudioRecord.setPreferredDevice(" + device.getProductName() + "):接口返回 " + accepted
                + "," + waitMs + " ms 后 " + routeSnapshot() + (routed ? " → 成功" : " → 失败"));
        return routed;
    }

    /**
     * 把某个输入设备挂到录音上(有线/USB 耳麦走这条)。
     *
     * 两条都试:Android 12+ 的 setCommunicationDevice(如果它是通信设备),以及
     * AudioRecord.setPreferredDevice;最后**按实际路由**(getRoutedDevice)判断成没成。
     * @param device 目标输入设备。
     * @param waitMs 挂上后等多久再查。
     * @return 是否真的切过去了。
     */
    private boolean attachInput(AudioDeviceInfo device, int waitMs) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                audioManager.setCommunicationDevice(device);
            }
        } catch (Throwable error) {
            Log.w(TAG, "setCommunicationDevice 失败: " + error);
        }
        try {
            AudioRecord active = record;
            if (active != null) {
                active.setPreferredDevice(device);
            }
        } catch (Throwable error) {
            Log.w(TAG, "setPreferredDevice 失败: " + error);
        }
        sleep(waitMs);
        return isRoutedToType(device.getType());
    }

    /**
     * 当前 AudioRecord 的路由是不是指定的类型。
     * @param type AudioDeviceInfo.TYPE_xxx。
     * @return 是则为 true。
     */
    private boolean isRoutedToType(int type) {
        AudioRecord active = record;
        if (active == null) {
            return false;
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            return routed != null && routed.getType() == type;
        } catch (Throwable error) {
            return false;
        }
    }

    /**
     * 找有线 / USB / BLE 耳麦的输入设备。
     *
     * 蓝牙 SCO 被系统挡住之后,这是唯一还能拿到"耳机上的麦克风"的路子,所以优先级排在最前。
     * @return 设备,没有则 null。
     */
    private AudioDeviceInfo findWiredHeadsetInput() {
        try {
            for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
                int type = device.getType();
                if (type == AudioDeviceInfo.TYPE_WIRED_HEADSET || type == AudioDeviceInfo.TYPE_USB_HEADSET
                        || type == AudioDeviceInfo.TYPE_BLE_HEADSET) {
                    return device;
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "找有线/USB 耳麦失败: " + error);
        }
        return null;
    }

    /**
     * "当前使用的麦克风"这一行的内容。
     *
     * 判定顺序(与 currentMicKind 一致):
     *  1. 正在录音 → 直接报 AudioRecord 实际路由到的设备(唯一的一手证据);
     *  2. 有有线/USB/BLE 耳麦输入 → 报它(不依赖蓝牙 SCO,本机唯一可用的"耳机麦");
     *  3. SCO 方式不是 phone 且系统报得出蓝牙通信设备 → 报蓝牙耳机;
     *  4. 否则手机内置麦。
     * @return 一行说明。
     */
    private String currentMicLabel() {
        AudioRecord active = record;
        if (active != null) {
            try {
                AudioDeviceInfo routed = active.getRoutedDevice();
                if (routed != null) {
                    return String.valueOf(routed.getProductName()) + " [" + audioTypeLabel(routed.getType()) + "](正在录音)";
                }
            } catch (Throwable error) {
                Log.w(TAG, "读实际输入失败: " + error);
            }
        }
        AudioDeviceInfo wired = findWiredHeadsetInput();
        if (wired != null) {
            return String.valueOf(wired.getProductName()) + " [" + audioTypeLabel(wired.getType())
                    + "](有线/USB 耳麦,不依赖蓝牙 SCO)";
        }
        if (!"phone".equals(scoMode)) {
            AudioDeviceInfo bluetooth = findCommunicationDevice();
            if (bluetooth != null) {
                return String.valueOf(bluetooth.getProductName()) + "[蓝牙耳机]";
            }
        }
        return "手机麦 [TYPE_BUILTIN_MIC(15,内置麦克风)]"
                + ("phone".equals(scoMode) ? "(SCO 方式=只用手机麦)" : "");
    }

    /**
     * "当前使用的麦克风"的类别。
     * @return wired / bluetooth / phone。
     */
    private String currentMicKind() {
        AudioRecord active = record;
        if (active != null) {
            try {
                AudioDeviceInfo routed = active.getRoutedDevice();
                if (routed != null) {
                    return kindOfType(routed.getType());
                }
            } catch (Throwable error) {
                // 落到下面的推断
            }
        }
        if (findWiredHeadsetInput() != null) {
            return "wired";
        }
        if (!"phone".equals(scoMode) && findCommunicationDevice() != null) {
            return "bluetooth";
        }
        return "phone";
    }

    /**
     * 设备类型 → 麦克风类别。
     * @param type AudioDeviceInfo.TYPE_xxx。
     * @return wired / bluetooth / phone。
     */
    private String kindOfType(int type) {
        if (type == AudioDeviceInfo.TYPE_WIRED_HEADSET || type == AudioDeviceInfo.TYPE_USB_HEADSET
                || type == AudioDeviceInfo.TYPE_BLE_HEADSET) {
            return "wired";
        }
        if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
            return "bluetooth";
        }
        return "phone";
    }

    /**
     * 记一条档位尝试,同时打进 logcat 并通知页面。
     * @param text 一行说明(要说清试了什么、结果如何)。
     */
    private void attempt(String text) {
        attemptLog.add(text);
        Log.i(TAG, "路由尝试 " + text);
        try {
            listener.onRouteAttempt(text);
        } catch (Throwable error) {
            Log.w(TAG, "回报档位尝试失败: " + error);
        }
    }

    /**
     * 当前 AudioRecord 到底有没有走蓝牙。
     * @return 是蓝牙 SCO / LE 耳机为 true。
     */
    private boolean isRoutedToBluetooth() {
        AudioRecord active = record;
        if (active == null) {
            return false;
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            if (routed == null) {
                return false;
            }
            int type = routed.getType();
            return type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET;
        } catch (Throwable error) {
            Log.w(TAG, "getRoutedDevice 失败: " + error);
            return false;
        }
    }

    /** @return 诊断用的一行路由快照:实际输入设备名 + 类型。 */
    private String routeSnapshot() {
        AudioRecord active = record;
        if (active == null) {
            return "routedDevice=(无 AudioRecord), type=(无)";
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            if (routed == null) {
                return "routedDevice=(系统未给出), type=(无)";
            }
            return "routedDevice=" + routed.getProductName() + ", type=" + audioTypeLabel(routed.getType());
        } catch (Throwable error) {
            return "routedDevice=读取失败(" + error.getClass().getSimpleName() + ")";
        }
    }

    /** @return isBluetoothScoOn 的原始值,读不到返回 false。 */
    private boolean rawScoOn() {
        try {
            return audioManager.isBluetoothScoOn();
        } catch (Throwable error) {
            return false;
        }
    }

    /**
     * 睡一会儿(路由切换是异步的,必须等)。
     * @param millis 毫秒。
     */
    private void sleep(int millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        }
    }

    /** 还原音频路由,别影响系统其它应用(老接口和新接口都要收尾)。 */
    private void restoreRoute() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                audioManager.clearCommunicationDevice();
            }
        } catch (Throwable error) {
            Log.w(TAG, "clearCommunicationDevice 失败: " + error);
        }
        if (scoStarted) {
            try {
                audioManager.setBluetoothScoOn(false);
                audioManager.stopBluetoothSco();
            } catch (Throwable error) {
                Log.w(TAG, "停 SCO 失败: " + error);
            }
        }
        scoStarted = false;
        scoDevice = null;
        try {
            if (modeChanged) {
                audioManager.setMode(AudioManager.MODE_NORMAL);
                modeChanged = false;
            }
        } catch (Throwable error) {
            Log.w(TAG, "还原 setMode 失败: " + error);
        }
    }

    /**
     * 开一个 AudioRecord:优先 VOICE_COMMUNICATION(通信设备路由才生效),失败退回 VOICE_RECOGNITION。
     * @return 就绪的 AudioRecord,全失败返回 null。
     */
    private AudioRecord openRecord() {
        AudioRecord candidate = build(MediaRecorder.AudioSource.VOICE_COMMUNICATION);
        if (candidate != null) {
            return candidate;
        }
        Log.w(TAG, "VOICE_COMMUNICATION 起不来,退回 VOICE_RECOGNITION");
        return build(MediaRecorder.AudioSource.VOICE_RECOGNITION);
    }

    /**
     * 按指定音源建 AudioRecord。
     * @param source MediaRecorder.AudioSource。
     * @return 初始化成功的 AudioRecord,否则 null。
     */
    private AudioRecord build(int source) {
        int minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (minBuffer <= 0) {
            minBuffer = CHUNK_BYTES * 4;
        }
        int bufferSize = Math.max(minBuffer, CHUNK_BYTES * 4);
        try {
            AudioRecord candidate = new AudioRecord(source, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, bufferSize);
            if (candidate.getState() == AudioRecord.STATE_INITIALIZED) {
                return candidate;
            }
            candidate.release();
        } catch (Throwable error) {
            Log.w(TAG, "AudioRecord 创建失败(source=" + source + "): " + error);
        }
        return null;
    }

    /** 释放 AudioRecord(不碰路由)。 */
    private void releaseRecord() {        AudioRecord active = record;
        record = null;
        if (active == null) {
            return;
        }
        try {
            active.stop();
        } catch (Throwable ignored) {
            // 已经不在录音状态时会抛,忽略
        }
        try {
            active.release();
        } catch (Throwable ignored) {
            // 同上
        }
    }

    /**
     * 检查"耳机那路是不是在装死":SCO / 通话设备都报成功,但一个字节的音频都没来。
     *
     * 为什么必须查:2026-10-04 实测到这种状态(vivo V2046A + WH-CH520)——
     * scoOn=true、routedDevice=TYPE_BLUETOOTH_SCO(7),可 AudioRecord **永远没有数据**。
     * 页面那边表现为电平停在 -100.0、一个回调都没有,用户说什么都没反应,而 App 还以为
     * "耳机路由成功"。这里发现之后直接切回手机麦,并把原因交给页面显示 ——
     * 宁可音质差一点,也不能让他对着耳机白说。
     */
    private void checkSilentHeadset() {
        if (fellBackToPhoneMic || !recording) {
            return;
        }
        // 最近 1.5 秒内收到过真实声音就不算"耳机装死"。
        // 注意:这个判断必须看"最近",不能看"曾经" —— SCO 是开录约 1 秒后才接管的,
        // 开头那 1 秒手机麦的真实声音会让"曾经"型标记永远为真,降级就永远不会触发。
        if (SystemClock.elapsedRealtime() - lastDataAt <= SILENT_ROUTE_GRACE_MS) {
            return;
        }
        // 先分清"我们被系统静音"这一种:那不是耳机的问题,而是**另一个 App 正拿着麦克风**
        // (Android 对后来者就是给静音)。这时该提示用户关掉那个 App,而不是怪耳机。
        if (isOurRecordingSilenced()) {
            if (!silenceReported) {
                silenceReported = true;
                lastAttempt = "另一个 App 正在用麦克风(我们只收到静音) → 关掉微信语音/录音机之类再试";
                Log.w(TAG, lastAttempt);
                try {
                    listener.onRouteAttempt(lastAttempt);
                } catch (Throwable error) {
                    Log.w(TAG, "报静音原因失败: " + error);
                }
            }
            return;
        }
        if (!routedToBluetoothInput()) {
            return;
        }
        fellBackToPhoneMic = true;
        String reason = "耳机那路收不到声音(SCO 建起来了但只有数字零)";
        Log.w(TAG, reason + " → 自动改用手机麦");
        restoreRoute();
        // **必须重开 AudioRecord**:只放掉 SCO 是不够的 —— 那条录音实例还停在旧路由上,
        // 会继续吐数字零(实测:降级后 routedDevice 已显示内置麦,电平却还是 -100)。
        restartRecordForPhoneMic();
        lastAttempt = "自动降级:" + reason + " → 手机麦";
        try {
            listener.onRouteAttempt(lastAttempt);
        } catch (Throwable error) {
            Log.w(TAG, "报降级失败: " + error);
        }
    }

    /** 降级到手机麦时重开录音实例,让它按"现在这支麦"重新路由。 */
    private void restartRecordForPhoneMic() {
        AudioRecord old = record;
        record = null;
        if (old != null) {
            try {
                old.stop();
            } catch (Throwable ignored) {
                // 已停
            }
            try {
                old.release();
            } catch (Throwable ignored) {
                // 已释放
            }
        }
        AudioRecord fresh = openRecord();
        if (fresh == null) {
            Log.w(TAG, "重开 AudioRecord 失败");
            return;
        }
        try {
            fresh.startRecording();
        } catch (Throwable error) {
            Log.w(TAG, "重开录音失败: " + error);
        }
        if (fresh.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            Log.w(TAG, "重开的 AudioRecord 没进入录音状态");
            try {
                fresh.release();
            } catch (Throwable ignored) {
                // 忽略
            }
            return;
        }
        record = fresh;
        Log.i(TAG, "已重开 AudioRecord,改用手机麦");
    }

    /**
     * 这一轮的输入是不是真的挂在蓝牙上。
     *
     * 三个线索任一成立就算:scoStarted / isBluetoothScoOn / 通信设备是蓝牙设备。
     * 只看前两个会漏 —— 实测到过 setCommunicationDevice(SCO) 成功、可 isBluetoothScoOn()
     * 却是 false 的状态(那时页面显示 SCO=false,降级判断被跳过,用户就一直对着静音说话)。
     *
     * @return 挂在蓝牙输入上为 true。
     */
    private boolean routedToBluetoothInput() {
        if (scoStarted) {
            return true;
        }
        if (scoOn()) {
            return true;
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                AudioDeviceInfo comm = audioManager.getCommunicationDevice();
                if (comm != null) {
                    int type = comm.getType();
                    if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET) {
                        return true;
                    }
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "查通信设备失败: " + error);
        }
        try {
            AudioDeviceInfo preferred = scoDevice;
            if (preferred != null && preferred.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
                return true;
            }
        } catch (Throwable ignored) {
            // 拿不到就算没有
        }
        return false;
    }

    /** 录音线程:攒满 100 ms 就回一块,顺带回电平;到本次时长上限自动停。 */
    private void loop() {
        byte[] chunk = new byte[CHUNK_BYTES];
        int filled = 0;
        long startedAt = SystemClock.elapsedRealtime();

        while (recording) {
            if (SystemClock.elapsedRealtime() - startedAt >= maxSeconds * 1000L) {
                autoStopped = true;
                break;
            }
            AudioRecord active = record;
            if (active == null) {
                break;
            }
            // 单块出错不能把整条录音线程带走:以前没有这层保护,循环里任何异常都会让线程
            // 静默退出 —— 页面表现为"电平停在某个值再也不动"(2026-10-04 排查时踩到)。
            try {
            int read;
            try {
                // **非阻塞**读:以前用阻塞读,遇到"SCO 建起来了但耳机不送数据"时
                // 会一直卡在这里 —— 页面一个回调都收不到(电平停在 -100),表现就是
                // "说什么都没反应"(2026-10-04 实测:vivo + WH-CH520,耳机麦那路零数据)。
                // 非阻塞之后我们能发现"这么久没数据",从而自动降级到手机麦。
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    read = active.read(chunk, filled, chunk.length - filled, AudioRecord.READ_NON_BLOCKING);
                } else {
                    read = active.read(chunk, filled, chunk.length - filled);
                }
            } catch (Throwable error) {
                Log.w(TAG, "AudioRecord.read 异常: " + error);
                break;
            }
            if (read <= 0) {
                // 没有数据:顺手检查"耳机一直在装死"这件事(SCO 建起来了却收不到任何声音)
                if (SystemClock.elapsedRealtime() - startedAt > SILENT_ROUTE_GRACE_MS) {
                    checkSilentHeadset();
                }
                try {
                    Thread.sleep(10);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    break;
                }
                continue;
            }
            filled += read;
            if (filled >= chunk.length) {
                byte[] block = new byte[CHUNK_BYTES];
                System.arraycopy(chunk, 0, block, 0, CHUNK_BYTES);
                double level = levelDbfs(block);
                // 数据在流不等于有声音:SCO 那路可能一直送**数字零**(全零块的电平就是 -100)。
                // 只有电平高于"数字静音线"才算真的收到音频;否则也可能是耳机在装死。
                if (level > SILENT_FLOOR_DBFS) {
                    lastDataAt = SystemClock.elapsedRealtime();
                } else if (SystemClock.elapsedRealtime() - startedAt > SILENT_ROUTE_GRACE_MS) {
                    checkSilentHeadset();
                }
                listener.onLevel(level);
                listener.onChunk(Base64.encodeToString(block, Base64.NO_WRAP));
                // 第一块到手说明 SCO 已经建起来了,这时再报一次路由:设备名最有参考价值
                if (!routeReported) {
                    routeReported = true;
                    try {
                        listener.onRouteUpdate(routeJson());
                    } catch (Throwable error) {
                        Log.w(TAG, "回报路由失败: " + error);
                    }
                }
                filled = 0;
            }
            } catch (Throwable error) {
                // 这一块处理出错:记一笔、歇 10 ms 继续,不能让整条录音线程退出
                Log.w(TAG, "录音循环单块异常(已忽略继续): " + error);
                try {
                    Thread.sleep(10);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }

        // 收尾:把最后不足 100 ms 的一块也交出去,不然结尾会丢音(16 bit 要求偶数字节)
        if (autoStopped) {
            listener.onAutoStop();
            stop();
            return;
        }
        int even = filled & ~1;
        if (even > 0) {
            byte[] tail = new byte[even];
            System.arraycopy(chunk, 0, tail, 0, even);
            listener.onLevel(levelDbfs(tail));
            listener.onChunk(Base64.encodeToString(tail, Base64.NO_WRAP));
        }
    }

    /**
     * 算一块 PCM 的电平。
     * @param pcm 16 bit 小端 PCM。
     * @return dBFS(满量程 0,静音约 -100)。
     */
    private double levelDbfs(byte[] pcm) {
        int samples = pcm.length / 2;
        if (samples == 0) {
            return -100.0;
        }
        long sum = 0;
        for (int i = 0; i < samples; i++) {
            int low = pcm[i * 2] & 0xff;
            int high = pcm[i * 2 + 1];
            int sample = (high << 8) | low;
            sum += (long) sample * sample;
        }
        double rms = Math.sqrt((double) sum / samples);
        if (rms < 1.0) {
            return -100.0;
        }
        double dbfs = 20.0 * Math.log10(rms / 32768.0);
        return dbfs < -100.0 ? -100.0 : dbfs;
    }

    /**
     * AudioRecord 实际走的输入设备名。
     * @return 录音时是 getRoutedDevice() 的名字;没在录音时是空字符串(那时 AudioRecord 不存在,
     *         列表里也通常看不到蓝牙设备,这属于正常现象)。
     */
    private String routedDeviceName() {
        AudioRecord active = record;
        if (active == null) {
            return "";
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            if (routed != null) {
                return String.valueOf(routed.getProductName()) + " [" + audioTypeLabel(routed.getType()) + "]";
            }
        } catch (Throwable error) {
            Log.w(TAG, "getRoutedDevice 失败: " + error);
        }
        return "";
    }

    /**
     * AudioRecord 实际走的输入设备类型(符号名 + 编号 + 中文,现场一眼能看出是不是 SCO)。
     * @return 例如 "TYPE_BUILTIN_MIC(15,内置麦克风)";没在录音时返回空字符串。
     */
    private String routedDeviceTypeName() {
        AudioRecord active = record;
        if (active == null) {
            return "";
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            if (routed != null) {
                return audioTypeLabel(routed.getType());
            }
        } catch (Throwable error) {
            Log.w(TAG, "读实际输入类型失败: " + error);
        }
        return "";
    }

    /**
     * AudioRecord 的"首选设备"(档 3 设过的那个)。
     * @return 名字 + 类型;没设过返回空字符串。
     */
    private String preferredDeviceName() {
        AudioRecord active = record;
        if (active == null) {
            return "";
        }
        try {
            AudioDeviceInfo preferred = active.getPreferredDevice();
            if (preferred != null) {
                return String.valueOf(preferred.getProductName()) + " [" + audioTypeLabel(preferred.getType()) + "]";
            }
        } catch (Throwable error) {
            Log.w(TAG, "读首选设备失败: " + error);
        }
        return "";
    }

    /**
     * setCommunicationDevice 选中的设备名(Android 12+)。
     * @return 设备名,没选中或系统更老则空字符串。
     */
    private String communicationDeviceName() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try {
                AudioDeviceInfo device = audioManager.getCommunicationDevice();
                if (device != null) {
                    return String.valueOf(device.getProductName()) + " [" + audioTypeLabel(device.getType()) + "]";
                }
            } catch (Throwable error) {
                Log.w(TAG, "getCommunicationDevice 失败: " + error);
            }
            return "";
        }
        return scoDevice == null ? "" : String.valueOf(scoDevice.getProductName());
    }

    /** @return 通信设备是不是蓝牙耳机(Android 12+ 用它判断 SCO 是否真的生效)。 */
    private boolean scoOn() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                AudioDeviceInfo device = audioManager.getCommunicationDevice();
                if (device != null && isBluetoothDevice(device)) {
                    return true;
                }
            }
        } catch (Throwable error) {
            // Android 12+ 没授 BLUETOOTH_CONNECT 时可能抛,按未生效处理
        }
        return rawScoOn();
    }

    /** @return AudioManager 的 mode 名字,页面直接显示。 */
    private String modeName() {
        try {
            int mode = audioManager.getMode();
            if (mode == AudioManager.MODE_IN_COMMUNICATION) {
                return "MODE_IN_COMMUNICATION";
            }
            if (mode == AudioManager.MODE_NORMAL) {
                return "MODE_NORMAL";
            }
            if (mode == AudioManager.MODE_IN_CALL) {
                return "MODE_IN_CALL";
            }
            return "MODE_" + mode;
        } catch (Throwable error) {
            return "unknown";
        }
    }

    /**
     * 找可用的通信设备(耳机)。
     *
     * Android 12+ 用 getAvailableCommunicationDevices() —— 这个接口不依赖 SCO 已建立,
     * 所以未录音时也能看到耳机在不在;更早的版本只能退回去扫输入设备列表。
     * @return 蓝牙通信设备,没有则 null。
     */
    private AudioDeviceInfo findCommunicationDevice() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            try {
                for (AudioDeviceInfo device : audioManager.getAvailableCommunicationDevices()) {
                    if (isBluetoothDevice(device)) {
                        return device;
                    }
                }
            } catch (Throwable error) {
                Log.w(TAG, "getAvailableCommunicationDevices 失败: " + error);
            }
            return null;
        }
        for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
            if (isBluetoothDevice(device)) {
                return device;
            }
        }
        return null;
    }

    /**
     * 可用通信设备清单(Android 12+),带类型标签,页面直接显示。
     * @return 「名字 [TYPE_xxx(编号,中文)]」列表;系统更老时返回空列表。
     */
    private List<String> availableCommunicationList() {
        List<String> entries = new ArrayList<>();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return entries;
        }
        try {
            for (AudioDeviceInfo device : audioManager.getAvailableCommunicationDevices()) {
                entries.add(String.valueOf(device.getProductName()) + " [" + audioTypeLabel(device.getType()) + "]");
            }
        } catch (Throwable error) {
            Log.w(TAG, "getAvailableCommunicationDevices 失败: " + error);
        }
        return entries;
    }

    /**
     * 已连接耳机(HFP)。
     *
     * 走 BluetoothHeadset 代理,而 getProfileProxy 是异步的:代理没就绪时必须报"未就绪",
     * 不能报空数组 —— 空数组会被读成"耳机真的没连",现场就被误导过。
     * @return 就绪时是 JSONArray(可能是空数组),没就绪时是说明文字。
     */
    private Object connectedHeadsetsValue() {
        ensureHeadsetProxy();
        BluetoothHeadset proxy = headsetProxy;
        if (proxy == null) {
            return "未就绪(蓝牙代理还没连上,过一两秒点『刷新路由』再看)";
        }
        try {
            JSONArray array = new JSONArray();
            for (BluetoothDevice device : proxy.getConnectedDevices()) {
                array.put(deviceName(device));
            }
            return array;
        } catch (Throwable error) {
            return "读取失败(" + error.getClass().getSimpleName() + ")";
        }
    }

    /** @return 已连接耳机的文字形式,诊断文本用。 */
    private String connectedHeadsetsText() {
        Object value = connectedHeadsetsValue();
        if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            List<String> names = new ArrayList<>();
            for (int i = 0; i < array.length(); i++) {
                names.add(array.optString(i));
            }
            return join(" / ", names);
        }
        return String.valueOf(value);
    }

    /**
     * 确保 HFP 代理已经请求过。第一次调用只是发起请求,真正拿到代理要等
     * ServiceListener.onServiceConnected(所以第一次刷新会显示"未就绪")。
     */
    private void ensureHeadsetProxy() {
        if (headsetProxy != null || headsetProxyRequested) {
            return;
        }
        BluetoothAdapter adapter = bluetoothAdapter();
        if (adapter == null) {
            return;
        }
        headsetProxyRequested = true;
        try {
            boolean requested = adapter.getProfileProxy(context, new BluetoothProfile.ServiceListener() {
                @Override
                public void onServiceConnected(int profile, BluetoothProfile proxy) {
                    if (profile == BluetoothProfile.HEADSET) {
                        headsetProxy = (BluetoothHeadset) proxy;
                        Log.i(TAG, "蓝牙 HFP 代理已就绪");
                    }
                }

                @Override
                public void onServiceDisconnected(int profile) {
                    headsetProxy = null;
                    headsetProxyRequested = false;
                }
            }, BluetoothProfile.HEADSET);
            if (!requested) {
                headsetProxyRequested = false;
            }
        } catch (Throwable error) {
            headsetProxyRequested = false;
            Log.w(TAG, "请求 HFP 代理失败: " + error);
        }
    }

    /**
     * HFP / A2DP 的连接状态(不依赖代理,Android 12+ 可用)。
     *
     * A2DP 的状态能直接说明"耳机是不是真的作为音频设备连上了";HFP 的状态说明"通话通道
     * 有没有建立"。这两条是排查耳机问题的第一手证据。
     * @param profile BluetoothProfile.HEADSET 或 BluetoothProfile.A2DP。
     * @return 例如 "STATE_CONNECTED(2)";API 31 以下报"未知(API<31)"。
     */
    private String profileStateText(int profile) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return "未知(API<31)";
        }
        BluetoothAdapter adapter = bluetoothAdapter();
        if (adapter == null) {
            return "无蓝牙适配器";
        }
        try {
            return stateText(adapter.getProfileConnectionState(profile));
        } catch (Throwable error) {
            Log.w(TAG, "getProfileConnectionState 失败: " + error);
            return "读取失败(" + error.getClass().getSimpleName() + ")";
        }
    }

    /**
     * 蓝牙 profile 状态码 → 文字。
     * @param state BluetoothProfile.STATE_xxx。
     * @return 带编号的文字。
     */
    private String stateText(int state) {
        if (state == BluetoothProfile.STATE_CONNECTED) {
            return "STATE_CONNECTED(2)";
        }
        if (state == BluetoothProfile.STATE_CONNECTING) {
            return "STATE_CONNECTING(1)";
        }
        if (state == BluetoothProfile.STATE_DISCONNECTING) {
            return "STATE_DISCONNECTING(3)";
        }
        if (state == BluetoothProfile.STATE_DISCONNECTED) {
            return "STATE_DISCONNECTED(0)";
        }
        return "STATE_" + state + "(" + state + ")";
    }

    /**
     * 已配对设备(包含没连上的,用来确认耳机到底配对过没有)。
     * @return 设备名列表。
     */
    private List<String> bondedDeviceNames() {
        List<String> names = new ArrayList<>();
        BluetoothAdapter adapter = bluetoothAdapter();
        if (adapter == null) {
            return names;
        }
        try {
            for (BluetoothDevice device : adapter.getBondedDevices()) {
                names.add(deviceName(device));
            }
        } catch (Throwable error) {
            Log.w(TAG, "getBondedDevices 失败(多半是没授 BLUETOOTH_CONNECT): " + error);
        }
        return names;
    }

    /**
     * 某一类音频设备清单。
     * @param which AudioManager.GET_DEVICES_INPUTS 或 GET_DEVICES_OUTPUTS。
     * @return 「名字 [TYPE_xxx(编号,中文), 信号源/非信号源]」列表。
     */
    private List<String> deviceList(int which) {
        List<String> entries = new ArrayList<>();
        try {
            for (AudioDeviceInfo device : audioManager.getDevices(which)) {
                entries.add(String.valueOf(device.getProductName())
                        + " [" + audioTypeLabel(device.getType()) + ", " + (device.isSource() ? "信号源" : "非信号源") + "]");
            }
        } catch (Throwable error) {
            Log.w(TAG, "枚举音频设备失败: " + error);
        }
        return entries;
    }

    /** @return 蓝牙是否开着;Android 12+ 没授 BLUETOOTH_CONNECT 时读不到,按 false 上报。 */
    private boolean bluetoothEnabled() {
        BluetoothAdapter adapter = bluetoothAdapter();
        if (adapter == null) {
            return false;
        }
        try {
            return adapter.isEnabled();
        } catch (Throwable error) {
            Log.w(TAG, "isEnabled 失败(多半是没授 BLUETOOTH_CONNECT): " + error);
            return false;
        }
    }

    /** @return BLUETOOTH_CONNECT 的授权状态;Android 12 以下没有这个运行时权限,视为已允许。 */
    private String headsetPermissionState() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return "granted";
        }
        try {
            return context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
                    ? "granted" : "denied";
        } catch (Throwable error) {
            return "unknown";
        }
    }

    /** @return RECORD_AUDIO 的授权状态。 */
    private String audioPermissionState() {
        try {
            return context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
                    ? "granted" : "denied";
        } catch (Throwable error) {
            return "unknown";
        }
    }

    /** @return 蓝牙适配器(拿不到就是没蓝牙硬件)。 */
    private BluetoothAdapter bluetoothAdapter() {
        try {
            return BluetoothAdapter.getDefaultAdapter();
        } catch (Throwable error) {
            return null;
        }
    }

    /**
     * 蓝牙设备名(Android 12+ 读名字要 BLUETOOTH_CONNECT,读不到就退回地址)。
     * @param device 蓝牙设备。
     * @return 名字或 MAC 地址。
     */
    private String deviceName(BluetoothDevice device) {
        try {
            String name = device.getName();
            return name == null || name.isEmpty() ? device.getAddress() : name;
        } catch (Throwable error) {
            return device.getAddress();
        }
    }

    /**
     * 设备名像不像耳机(给 hint 和页面判断用)。
     * @param name 设备名。
     * @return 名字里带 WH-/WF-/headset/Sony 之类,或者带 BLUETOOTH 类型标记。
     */
    private boolean isBluetoothName(String name) {
        if (name == null) {
            return false;
        }
        String lower = name.toLowerCase(Locale.US);
        return lower.contains("bluetooth") || lower.contains("wh-") || lower.contains("wf-")
                || lower.contains("headset") || lower.contains("sony") || lower.contains("buds")
                || lower.contains("airpods");
    }

    /**
     * 音频设备类型标签:符号名 + 编号 + 中文。
     *
     * 必须带上编号和中文 —— 现场 dump 里出现 "TYPE_25"/"TYPE_21" 这种纯数字时,人根本
     * 判断不了那是什么设备。排查蓝牙耳机时,关键就是看有没有 TYPE_BLUETOOTH_SCO(7) 与
     * TYPE_BLUETOOTH_A2DP(8)。
     * @param type AudioDeviceInfo.TYPE_xxx。
     * @return 例如 "TYPE_BLUETOOTH_SCO(7,蓝牙SCO)"。
     */
    private String audioTypeLabel(int type) {
        switch (type) {
            case AudioDeviceInfo.TYPE_UNKNOWN:
                return "TYPE_UNKNOWN(0,未知)";
            case AudioDeviceInfo.TYPE_BUILTIN_EARPIECE:
                return "TYPE_BUILTIN_EARPIECE(1,听筒)";
            case AudioDeviceInfo.TYPE_BUILTIN_SPEAKER:
                return "TYPE_BUILTIN_SPEAKER(2,扬声器)";
            case AudioDeviceInfo.TYPE_WIRED_HEADSET:
                return "TYPE_WIRED_HEADSET(3,有线耳麦)";
            case AudioDeviceInfo.TYPE_WIRED_HEADPHONES:
                return "TYPE_WIRED_HEADPHONES(4,有线耳机)";
            case AudioDeviceInfo.TYPE_LINE_ANALOG:
                return "TYPE_LINE_ANALOG(5,模拟线路)";
            case AudioDeviceInfo.TYPE_LINE_DIGITAL:
                return "TYPE_LINE_DIGITAL(6,数字线路)";
            case AudioDeviceInfo.TYPE_BLUETOOTH_SCO:
                return "TYPE_BLUETOOTH_SCO(7,蓝牙SCO)";
            case AudioDeviceInfo.TYPE_BLUETOOTH_A2DP:
                return "TYPE_BLUETOOTH_A2DP(8,蓝牙A2DP)";
            case AudioDeviceInfo.TYPE_HDMI:
                return "TYPE_HDMI(9,HDMI)";
            case AudioDeviceInfo.TYPE_HDMI_ARC:
                return "TYPE_HDMI_ARC(10,HDMI ARC)";
            case AudioDeviceInfo.TYPE_USB_DEVICE:
                return "TYPE_USB_DEVICE(11,USB设备)";
            case AudioDeviceInfo.TYPE_USB_ACCESSORY:
                return "TYPE_USB_ACCESSORY(12,USB配件)";
            case AudioDeviceInfo.TYPE_DOCK:
                return "TYPE_DOCK(13,底座)";
            case AudioDeviceInfo.TYPE_FM:
                return "TYPE_FM(14,调频)";
            case AudioDeviceInfo.TYPE_BUILTIN_MIC:
                return "TYPE_BUILTIN_MIC(15,内置麦克风)";
            case AudioDeviceInfo.TYPE_FM_TUNER:
                return "TYPE_FM_TUNER(16,调频收音)";
            case AudioDeviceInfo.TYPE_TV_TUNER:
                return "TYPE_TV_TUNER(17,电视调谐)";
            case AudioDeviceInfo.TYPE_TELEPHONY:
                return "TYPE_TELEPHONY(18,电话)";
            case AudioDeviceInfo.TYPE_AUX_LINE:
                return "TYPE_AUX_LINE(19,AUX)";
            case AudioDeviceInfo.TYPE_IP:
                return "TYPE_IP(20,IP音频)";
            case AudioDeviceInfo.TYPE_BUS:
                return "TYPE_BUS(21,总线(虚拟))";
            case AudioDeviceInfo.TYPE_USB_HEADSET:
                return "TYPE_USB_HEADSET(22,USB耳麦)";
            case AudioDeviceInfo.TYPE_HEARING_AID:
                return "TYPE_HEARING_AID(23,助听器)";
            case AudioDeviceInfo.TYPE_BUILTIN_SPEAKER_SAFE:
                return "TYPE_BUILTIN_SPEAKER_SAFE(24,安全扬声器)";
            case AudioDeviceInfo.TYPE_REMOTE_SUBMIX:
                return "TYPE_REMOTE_SUBMIX(25,远端混音(虚拟))";
            case AudioDeviceInfo.TYPE_BLE_HEADSET:
                return "TYPE_BLE_HEADSET(26,BLE耳机)";
            case AudioDeviceInfo.TYPE_BLE_SPEAKER:
                return "TYPE_BLE_SPEAKER(27,BLE扬声器)";
            // 28 是 TYPE_ECHO_REFERENCE,但它是 @SystemApi(不在公开 SDK 里),编译期引用不到,
            // 只能落到 default 分支按编号显示
            case AudioDeviceInfo.TYPE_HDMI_EARC:
                return "TYPE_HDMI_EARC(29,HDMI eARC)";
            case AudioDeviceInfo.TYPE_BLE_BROADCAST:
                return "TYPE_BLE_BROADCAST(30,BLE广播)";
            default:
                return "TYPE_" + type + "(" + type + ",未知类型)";
        }
    }

    /**
     * 这个设备算不算蓝牙音频设备。
     * @param device 音频设备。
     * @return TYPE_BLUETOOTH_SCO / TYPE_BLUETOOTH_A2DP / TYPE_BLE_HEADSET / TYPE_BLE_SPEAKER,
     *         或者产品名里带 Bluetooth 的。
     */
    private boolean isBluetoothDevice(AudioDeviceInfo device) {
        int type = device.getType();
        if (type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
                || type == AudioDeviceInfo.TYPE_BLE_HEADSET || type == AudioDeviceInfo.TYPE_BLE_SPEAKER) {
            return true;
        }
        String name = device.getProductName() == null ? "" : device.getProductName().toString();
        return name.toLowerCase(Locale.US).contains("bluetooth");
    }
}
`;
}

/**
 * 放她的声音:**原生 AudioTrack**,而不是 WebView 里的 Web Audio。
 *
 * 为什么要挪到原生(2026-10-03 用户报"声音从手机扬声器出来了"):
 * 页面的 Web Audio 输出走的是**媒体**流,而对话模式为了拿耳机麦克风一直挂着 SCO 通话链路 ——
 * SCO 一挂,A2DP(媒体)就被系统挂起,媒体流就没有去耳机的路了,只能从手机扬声器出来。
 * 原生这边用 `USAGE_VOICE_COMMUNICATION` 建 AudioTrack 并把首选设备指到 SCO,
 * 声音就和麦克风走同一条耳机链路(代价是音质变成"通话音质",耳机在 HFP 模式下本来就窄带)。
 *
 * 没接耳机时不这么干:`USAGE_VOICE_COMMUNICATION` 在没有耳机时会把声音送到**听筒**,
 * 那比扬声器还小声 —— 所以按"当前有没有 SCO/耳机设备"决定用通话用法还是媒体用法。
 *
 * @returns {string} QingyuPlayer.java 源码。
 */
export function qingyuPlayerJava() {
  return `package ${APPLICATION_ID};

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.util.Base64;
import android.util.Log;

/**
 * 播放她的 PCM:24 kHz / 单声道 / PCM16,优先走耳机(通话链路)。
 */
public class QingyuPlayer {

    private static final String TAG = "Qingyu";
    private static final int DEFAULT_RATE = 24000;
    /** 缓冲区:太小会断续,太大她的第一声会晚。约 300 ms。 */
    private static final int BUFFER_MS = 300;

    private final Context context;
    private final AudioManager audioManager;
    private volatile AudioTrack track;
    private volatile int sampleRate = DEFAULT_RATE;
    /** 已写入的帧数与"用户听到的"帧数,用来算还剩多久放完。 */
    private volatile long writtenFrames;
    private volatile long drainedFrames;
    /** 这一轮用的是通话用法(耳机)还是媒体用法(外放)。 */
    private volatile String usage = "media";

    public QingyuPlayer(Context context, AudioManager audioManager) {
        this.context = context;
        this.audioManager = audioManager;
    }

    /** 当前用的是哪种用法(进诊断,便于确认声音到底走哪条路)。 */
    public String usageName() {
        return usage;
    }

    /**
     * 开始一轮播放。
     * @param rate 采样率(家里下发的是 24000)。
     * @param allowVoice 是否允许用通信用法(只有"耳机那边真的能出声"时才该为 true)。
     * @return 是否开起来了。
     */
    public synchronized boolean start(int rate, boolean allowVoice) {
        stop();
        sampleRate = rate > 0 ? rate : DEFAULT_RATE;
        AudioDeviceInfo sco = allowVoice ? findVoiceOutput() : null;
        usage = sco != null ? "voice_communication" : "media";
        AudioAttributes attributes = new AudioAttributes.Builder()
                .setUsage("voice_communication".equals(usage)
                        ? AudioAttributes.USAGE_VOICE_COMMUNICATION
                        : AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();
        AudioFormat format = new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(sampleRate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .build();
        int minBytes = AudioTrack.getMinBufferSize(sampleRate,
                AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int bytes = Math.max(minBytes, sampleRate * 2 * BUFFER_MS / 1000);
        try {
            AudioTrack built = new AudioTrack.Builder()
                    .setAudioAttributes(attributes)
                    .setAudioFormat(format)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .setBufferSizeInBytes(bytes)
                    .build();
            if (built.getState() != AudioTrack.STATE_INITIALIZED) {
                built.release();
                Log.w(TAG, "AudioTrack 初始化失败");
                return false;
            }
            if (sco != null) {
                built.setPreferredDevice(sco);
            }
            built.play();
            track = built;
            writtenFrames = 0;
            drainedFrames = 0;
            Log.i(TAG, "播放开始 " + sampleRate + " Hz,用法=" + usage
                    + ",首选=" + (sco == null ? "无" : describe(sco)));
            return true;
        } catch (Throwable error) {
            Log.w(TAG, "建 AudioTrack 失败: " + error);
            return false;
        }
    }

    /**
     * 写一段 PCM(页面把二进制帧转成 base64 送过来)。
     * @param base64Pcm PCM16 小端单声道。
     */
    public synchronized void write(String base64Pcm) {
        AudioTrack active = track;
        if (active == null || base64Pcm == null || base64Pcm.isEmpty()) {
            return;
        }
        try {
            byte[] pcm = Base64.decode(base64Pcm, Base64.DEFAULT);
            int written = active.write(pcm, 0, pcm.length);
            if (written > 0) {
                writtenFrames += written / 2;
            }
        } catch (Throwable error) {
            Log.w(TAG, "写 PCM 失败: " + error);
        }
    }

    /**
     * 没有更多数据了:等在缓冲区里的放完再释放,不然尾巴会被切掉。
     */
    public synchronized void finish() {
        final AudioTrack active = track;
        if (active == null) {
            return;
        }
        new Thread(() -> {
            long limit = System.currentTimeMillis() + 15000;
            while (System.currentTimeMillis() < limit) {
                if (playbackHead(active) >= writtenFrames) {
                    break;
                }
                try {
                    Thread.sleep(50);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            synchronized (QingyuPlayer.this) {
                if (track == active) {
                    stop();
                }
            }
        }, "qingyu-player-drain").start();
    }

    /** 还剩多少毫秒放完(页面用它决定什么时候重新开麦)。 */
    public int remainingMs() {
        AudioTrack active = track;
        if (active == null) {
            return 0;
        }
        long left = writtenFrames - playbackHead(active);
        if (left <= 0) {
            return 0;
        }
        return (int) Math.min(60000L, left * 1000L / Math.max(1, sampleRate));
    }

    /** 她的声音现在实际从哪个设备出来(诊断里直接看这条)。 */
    public String routedName() {
        AudioTrack active = track;
        if (active == null) {
            return "(未在播放," + usage + ")";
        }
        try {
            AudioDeviceInfo routed = active.getRoutedDevice();
            return routed == null ? "(读不到," + usage + ")" : describe(routed) + "(" + usage + ")";
        } catch (Throwable error) {
            return "(读设备失败," + usage + ")";
        }
    }

    /** 停掉并释放(幂等)。 */
    public synchronized void stop() {
        AudioTrack active = track;
        track = null;
        if (active == null) {
            return;
        }
        try {
            drainedFrames = writtenFrames;
            active.stop();
        } catch (Throwable error) {
            Log.w(TAG, "AudioTrack.stop 异常: " + error);
        }
        try {
            active.release();
        } catch (Throwable error) {
            Log.w(TAG, "AudioTrack.release 异常: " + error);
        }
    }

    /** 播放头(帧)。有些设备在 stop 之后会抛,统一兜住。 */
    private long playbackHead(AudioTrack active) {
        try {
            long head = active.getPlaybackHeadPosition();
            return head < 0 ? 0 : head;
        } catch (Throwable error) {
            return writtenFrames;
        }
    }

    /** 找一个"该用通信用法"的输出设备。 */
    private AudioDeviceInfo findVoiceOutput() {
        if (audioManager == null) {
            return null;
        }
        // 蓝牙 SCO 只有**链路真的挂着**时才算:设备列表里常年挂着一条 SCO 输出设备,
        // 但没建立时用它做通信用法播放 → 声音进了没有音频的链路 → 用户听到的是"耳机不出声"
        // (2026-10-04 实测:vivo + WH-CH520 就是这个症状)。
        boolean scoUp = false;
        try {
            scoUp = audioManager.isBluetoothScoOn();
        } catch (Throwable ignored) {
            // 读不到就当没挂
        }
        try {
            for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                int type = device.getType();
                if (type == AudioDeviceInfo.TYPE_WIRED_HEADSET || type == AudioDeviceInfo.TYPE_USB_HEADSET) {
                    return device;
                }
                if ((type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO || type == AudioDeviceInfo.TYPE_BLE_HEADSET) && scoUp) {
                    return device;
                }
            }
        } catch (Throwable error) {
            Log.w(TAG, "列输出设备失败: " + error);
        }
        return null;
    }

    /** 设备 → "名字 [TYPE_x(编号,中文)]"。 */
    private String describe(AudioDeviceInfo device) {
        String name = device.getProductName() == null ? "" : device.getProductName().toString();
        return name + " [TYPE_" + device.getType() + "]";
    }
}
`;
}

/**
 * 拍照:`ACTION_IMAGE_CAPTURE` + FileProvider,回来先缩放再压 JPEG。
 *
 * 为什么不把原图交给页面:现在手机随手一张就是 3–8 MB,base64 之后还要再涨 1/3,穿过
 * WebView 会卡住界面甚至 OOM。这里按长边 1280 缩放、质量 70 压成 JPEG,典型结果
 * 150–400 KB,直接能当 image 帧发。
 *
 * @returns {string} QingyuCamera.java 源码。
 */
export function qingyuCameraJava() {
  return `package ${APPLICATION_ID};

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.net.Uri;
import android.provider.MediaStore;
import android.util.Base64;
import android.util.Log;

import androidx.core.content.FileProvider;

import java.io.ByteArrayOutputStream;
import java.io.File;

/**
 * 拍照:拉系统相机,写到 FileProvider 暴露的 cacheDir/images 下,回来后缩放 + 压缩。
 */
public class QingyuCamera {

    /** 拍完的回调。用户取消时 base64 是空字符串,不是异常。 */
    public interface Listener {
        void onPhoto(String base64Jpeg, String text);

        /**
         * 拍照**没能拉起来**的原因(要能直接照做)。
         *
         * 为什么单独一条:以前失败只返回 false,页面只能说"取消或失败",用户完全不知道
         * 是权限、是没有相机应用、还是"上一次拍照卡住了" —— 2026-10-04 排查拍照功能时
         * 就是卡在这里(实际原因:上一次的 pending 没清,之后每次点都静默返回 false)。
         *
         * @param reason 给人看的一句话。
         */
        void onCameraError(String reason);
    }

    /** 拍照请求码,由 MainActivity 转发回来。 */
    public static final int REQUEST_CAPTURE = 0x5101;

    /** 长边上限:1280 够"看看这个"用,又能把体积压到几百 KB。 */
    private static final int MAX_LONG_EDGE = 1280;
    private static final int JPEG_QUALITY = 70;
    private static final String TAG = "Qingyu";

    private final Activity activity;
    private final Listener listener;

    private File pendingFile;
    private String pendingText = "";
    private boolean pending;

    /**
     * @param activity 宿主 Activity(拉起相机、拿 cacheDir)。
     * @param listener 回调。
     */
    public QingyuCamera(Activity activity, Listener listener) {
        this.activity = activity;
        this.listener = listener;
    }

    /**
     * 拉起系统相机。
     * @param text 附言,跟着 image 帧一起发。
     * @return 相机是否被拉起来了。
     */
    public boolean capture(String text) {
        if (pending) {
            // 上一次拍照没等到返回(相机被杀、进程被回收、页面重载都可能导致)。
            // 以前这里直接返回 false 且不清状态 → **之后每次点拍照都是静默失败**,
            // 用户看到的就是"拍照功能坏了"。现在:如实说明 + 把状态重置,下一次能正常用。
            Log.w(TAG, "上一次拍照还没结束,重置后重试");
            pending = false;
            cleanup(pendingFile);
            pendingFile = null;
            listener.onCameraError("上一次拍照没有正常返回(已自动重置)。请再点一次「拍照」");
        }
        pendingText = text == null ? "" : text;
        File file = null;
        try {
            // FileProvider 只暴露 cacheDir/images(cache-path name="images"),别放别处
            File dir = new File(activity.getCacheDir(), "images");
            if (!dir.exists() && !dir.mkdirs()) {
                Log.w(TAG, "建不了图片目录 " + dir);
                listener.onCameraError("建不了图片缓存目录,拍照没法存: " + dir);
                listener.onPhoto("", pendingText);
                return false;
            }
            file = File.createTempFile("qingyu-", ".jpg", dir);
            Uri uri = FileProvider.getUriForFile(activity, activity.getPackageName() + ".fileprovider", file);

            Intent intent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            intent.putExtra(MediaStore.EXTRA_OUTPUT, uri);
            intent.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);

            pendingFile = file;
            pending = true;
            activity.startActivityForResult(intent, REQUEST_CAPTURE);
            return true;
        } catch (ActivityNotFoundException error) {
            Log.w(TAG, "这台设备没有相机应用: " + error);
            cleanup(file);
            listener.onCameraError("这台设备上没有能拍照的应用(系统相机被删了或被禁用)");
            listener.onPhoto("", pendingText);
            return false;
        } catch (Throwable error) {
            Log.w(TAG, "拉起相机失败: " + error);
            cleanup(file);
            listener.onCameraError("拉起相机失败:" + error.getClass().getSimpleName() + " " + error.getMessage());
            listener.onPhoto("", pendingText);
            return false;
        }
    }

    /**
     * 处理相机返回。
     * @param requestCode 请求码。
     * @param resultCode 结果码。
     * @param data 相机返回的 Intent(用 EXTRA_OUTPUT 时通常为空)。
     */
    public void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != REQUEST_CAPTURE) {
            return;
        }
        File file = pendingFile;
        String text = pendingText;
        pendingFile = null;
        pendingText = "";
        pending = false;

        if (resultCode != Activity.RESULT_OK) {
            // 用户取消:回一个空 base64,页面据此提示"已取消",绝不能让流程卡住
            Log.i(TAG, "拍照被取消(resultCode=" + resultCode + ")");
            cleanup(file);
            listener.onPhoto("", text);
            return;
        }
        if (file == null || !file.exists() || file.length() == 0) {
            Log.w(TAG, "相机没有写出文件(某些相机应用不认 EXTRA_OUTPUT)");
            cleanup(file);
            listener.onPhoto("", text);
            return;
        }

        try {
            byte[] jpeg = shrink(file);
            Log.i(TAG, "拍照完成," + jpeg.length + " 字节 JPEG");
            cleanup(file);
            listener.onPhoto(Base64.encodeToString(jpeg, Base64.NO_WRAP), text);
        } catch (Throwable error) {
            Log.w(TAG, "处理照片失败: " + error);
            cleanup(file);
            listener.onPhoto("", text);
        }
    }

    /**
     * 读文件 → 按 EXIF 摆正 → 长边缩到 1280 → 压 JPEG(质量 70)。
     * @param source 相机写出的原始文件。
     * @return JPEG 字节。
     * @throws Exception 读不出尺寸/解码失败/压缩失败。
     */
    private byte[] shrink(File source) throws Exception {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(source.getAbsolutePath(), bounds);
        int longEdge = Math.max(bounds.outWidth, bounds.outHeight);
        if (longEdge <= 0) {
            throw new IllegalStateException("读不出图片尺寸");
        }

        // 先靠 inSampleSize 粗降采样,避免把 4000x3000 整张读进内存
        int sample = 1;
        while (longEdge / (sample * 2) >= MAX_LONG_EDGE) {
            sample *= 2;
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = sample;

        Bitmap bitmap = BitmapFactory.decodeFile(source.getAbsolutePath(), options);
        if (bitmap == null) {
            throw new IllegalStateException("解码失败");
        }
        bitmap = rotateIfNeeded(bitmap, source);
        bitmap = scaleToLongEdge(bitmap, MAX_LONG_EDGE);

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        boolean compressed = bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out);
        bitmap.recycle();
        if (!compressed) {
            throw new IllegalStateException("JPEG 压缩失败");
        }
        return out.toByteArray();
    }

    /**
     * 按 EXIF 方向摆正。
     *
     * 用 EXTRA_OUTPUT 拍照时,很多相机应用不旋转像素、只在 EXIF 里写方向;我们重新编码会
     * 丢掉那个标记,照片就会躺着。所以这里主动转正。
     * @param bitmap 原图。
     * @param source 原始文件(读 EXIF 用)。
     * @return 摆正后的位图(可能还是原来那张)。
     */
    private Bitmap rotateIfNeeded(Bitmap bitmap, File source) {
        int orientation = ExifInterface.ORIENTATION_NORMAL;
        try {
            ExifInterface exif = new ExifInterface(source.getAbsolutePath());
            orientation = exif.getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
        } catch (Throwable error) {
            return bitmap;
        }
        int degrees = 0;
        if (orientation == ExifInterface.ORIENTATION_ROTATE_90) {
            degrees = 90;
        } else if (orientation == ExifInterface.ORIENTATION_ROTATE_180) {
            degrees = 180;
        } else if (orientation == ExifInterface.ORIENTATION_ROTATE_270) {
            degrees = 270;
        }
        if (degrees == 0) {
            return bitmap;
        }
        Matrix matrix = new Matrix();
        matrix.postRotate(degrees);
        try {
            Bitmap rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), matrix, true);
            if (rotated != bitmap) {
                bitmap.recycle();
            }
            return rotated;
        } catch (Throwable error) {
            Log.w(TAG, "旋转照片失败: " + error);
            return bitmap;
        }
    }

    /**
     * 等比缩放到长边不超过上限。
     * @param bitmap 位图。
     * @param maxLongEdge 长边上限。
     * @return 缩放后的位图(本来就不大时返回原图)。
     */
    private Bitmap scaleToLongEdge(Bitmap bitmap, int maxLongEdge) {
        int width = bitmap.getWidth();
        int height = bitmap.getHeight();
        int longEdge = Math.max(width, height);
        if (longEdge <= maxLongEdge) {
            return bitmap;
        }
        float ratio = (float) maxLongEdge / longEdge;
        int targetWidth = Math.max(1, Math.round(width * ratio));
        int targetHeight = Math.max(1, Math.round(height * ratio));
        try {
            Bitmap scaled = Bitmap.createScaledBitmap(bitmap, targetWidth, targetHeight, true);
            if (scaled != bitmap) {
                bitmap.recycle();
            }
            return scaled;
        } catch (Throwable error) {
            Log.w(TAG, "缩放照片失败: " + error);
            return bitmap;
        }
    }

    /**
     * 删掉临时文件(成功或失败都要删,别把缓存塞满)。
     * @param file 文件,可为 null。
     */
    private void cleanup(File file) {
        if (file == null) {
            return;
        }
        try {
            if (file.exists() && !file.delete()) {
                Log.w(TAG, "删不掉临时照片 " + file);
            }
        } catch (Throwable error) {
            Log.w(TAG, "删除临时照片异常: " + error);
        }
    }
}
`;
}

/**
 * 保活用的前台服务。
 *
 * 为什么需要它:实测发现 App 一切后台/熄屏,WebSocket 就会在 10–80 秒内被系统掐断
 * (日志里的 no close frame)。挂一个前台服务(常驻通知)是 Android 上不靠"省电白名单"
 * 就能让进程活下来的正规手段。
 *
 * @returns {string} QingyuService.java 源码。
 */
export function qingyuServiceJava() {
  return `package ${APPLICATION_ID};

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * 常驻前台服务:保证连接在切后台/熄屏时不被系统掐掉。
 *
 * 通知上有"停止":点了就结束保活(连接会随之断开,由页面负责重连)。
 */
public class QingyuService extends Service {

    /** 启动保活。 */
    public static final String ACTION_START = "dev.qingyu.phone.action.START";
    /** 停止保活(通知上的按钮)。 */
    public static final String ACTION_STOP = "dev.qingyu.phone.action.STOP";

    private static final String CHANNEL_ID = "qingyu-keepalive";
    private static final int NOTIFICATION_ID = 0x7A01;
    private static final String TAG = "Qingyu";

    /**
     * 启动前台服务(Android 8+ 必须用 startForegroundService)。
     * @param context 上下文。
     */
    public static void start(Context context) {
        try {
            Intent intent = new Intent(context, QingyuService.class).setAction(ACTION_START);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
        } catch (Throwable error) {
            Log.w(TAG, "启动前台服务失败: " + error);
        }
    }

    /**
     * 停止前台服务。
     * @param context 上下文。
     */
    public static void stop(Context context) {
        try {
            context.stopService(new Intent(context, QingyuService.class));
        } catch (Throwable error) {
            Log.w(TAG, "停止前台服务失败: " + error);
        }
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            Log.i(TAG, "通知里点了停止:关闭前台保活");
            try {
                stopForeground(true);
            } catch (Throwable error) {
                Log.w(TAG, "stopForeground 失败: " + error);
            }
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            startForegroundInternal();
            Log.i(TAG, "前台服务已启动,保活生效");
            return START_STICKY;
        } catch (Throwable error) {
            Log.w(TAG, "startForeground 失败: " + error);
            stopSelf();
            return START_NOT_STICKY;
        }
    }

    /** 建通知渠道并进入前台(Android 10+ 用带类型的版本)。 */
    private void startForegroundInternal() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager != null) {
                NotificationChannel channel = new NotificationChannel(CHANNEL_ID, "轻语后台保持",
                        NotificationManager.IMPORTANCE_LOW);
                channel.setDescription("保持与家里那台电脑的连接,免得切后台就断开");
                channel.setShowBadge(false);
                manager.createNotificationChannel(channel);
            }
        }
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // 类型必须与 manifest 里声明的集合一致,否则 Android 14+ 会抛异常
            startForeground(NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE | ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    /**
     * 常驻通知:点一下回 App,另带一个"停止"。
     * @return 通知。
     */
    private Notification buildNotification() {
        Intent openIntent = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPending = PendingIntent.getActivity(this, 0, openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Intent stopIntent = new Intent(this, QingyuService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(this, 1, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        builder.setContentTitle("轻语 · 正在陪着你说话")
                .setContentText("保持与家里的连接;不用了就点这里回 App,或按「停止」")
                // 用系统自带的小图标,省得再画一份资源
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setOngoing(true)
                .setContentIntent(openPending)
                .addAction(0, "停止", stopPending);
        return builder.build();
    }
}
`;
}

/**
 * 图标资源:全部用矢量 XML 画,不生成任何二进制素材。
 * @returns {Record<string, string>} 文件名 → XML,相对 `res/`。
 */
export function iconResources() {
  const glyph = `<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp" android:height="108dp"
    android:viewportWidth="108" android:viewportHeight="108">
    <!-- 对话气泡:轻语 -->
    <path android:fillColor="#e8eaf0"
        android:pathData="M28,26 h52 a12,12 0 0 1 12,12 v26 a12,12 0 0 1 -12,12 h-24 l-16,14 v-14 h-12 a12,12 0 0 1 -12,-12 v-26 a12,12 0 0 1 12,-12 z" />
    <!-- 声波 -->
    <path android:fillColor="#5b8cff" android:pathData="M40,44 h5 v22 h-5 z" />
    <path android:fillColor="#5b8cff" android:pathData="M51,38 h5 v34 h-5 z" />
    <path android:fillColor="#8fb0ff" android:pathData="M62,47 h5 v16 h-5 z" />
</vector>
`;

  const adaptive = `<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/icon_background" />
    <foreground android:drawable="@drawable/ic_glyph" />
</adaptive-icon>
`;

  return {
    'drawable/ic_glyph.xml': glyph,
    'mipmap-anydpi-v26/ic_launcher.xml': adaptive,
    'mipmap-anydpi-v26/ic_launcher_round.xml': adaptive,
    // API 26 以下不认自适应图标,给一份兜底,否则老机器上没图标
    'drawable/ic_launcher_fallback.xml': glyph,
    'values/colors.xml': `<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="icon_background">#15161a</color>
</resources>
`,
  };
}

/**
 * API 26 以下的 mipmap 别名,让 `@mipmap/ic_launcher` 在旧系统上也能解析。
 * @returns {string} mipmap XML。
 */
export function legacyMipmap() {
  return `<?xml version="1.0" encoding="utf-8"?>
<bitmap xmlns:android="http://schemas.android.com/apk/res/android"
    android:src="@drawable/ic_launcher_fallback" />
`;
}

/**
 * Gradle wrapper 属性:把构建钉在固定 Gradle 版本上,而不是用机器上碰巧装的。
 * @param version Gradle 版本。
 * @returns {string} gradle-wrapper.properties 源码。
 */
export function gradleWrapperProperties(version) {
  return `distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\\://services.gradle.org/distributions/gradle-${version}-bin.zip
networkTimeout=10000
validateDistributionUrl=true
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
`;
}
