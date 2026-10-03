/**
 * Android 工程与版本号的单一来源。
 *
 * 这里只有字符串:manifest、Gradle 脚本、资源。`tools/gen-android.js` 把它们写进
 * `android/`,那个目录是**生成物**,可以随时删掉重建;阶段 2 加原生录音/拍照时
 * 只改本文件与 `lib/android-sources.js`。
 *
 * 版本组合是别人实测能在这台机器上构建成功的组合,不要随手升级:
 * AGP 8.7.3 需要 JDK 17(系统 java 11 会被拒),Gradle 8.11.1 与之配套。
 *
 * @module qingyu-phone/lib/android-project
 */

/** 包名。 */
export const APPLICATION_ID = 'dev.qingyu.phone';
/** 应用名(桌面图标下的名字)。 */
export const APP_LABEL = '轻语';
export const VERSION_NAME = '0.2.11';
export const VERSION_CODE = 13;

/**
 * 分发用的 Tailscale 地址(不带 token,页面里的「家里(Tailscale)」预设用它)。
 * 注意与 DEFAULT_SERVER_ADDRESS 的区别:那个带 `?token=<令牌>` 占位,是给人照着填的。
 */
export const TAILSCALE_SERVER_URL = 'wss://laptop-u3hj61a7.tail35209a.ts.net:8443/';
/** 同一个 WiFi 时的占位地址:IP 要换成电脑的局域网地址。 */
export const LAN_SERVER_URL_PLACEHOLDER = 'ws://192.168.1.100:6201/';

/** minSdk 24 覆盖 Android 7.0+;targetSdk/compileSdk 跟 build-tools 对齐。 */
export const MIN_SDK = 24;
export const TARGET_SDK = 35;
export const COMPILE_SDK = 35;
export const SDK_PLATFORM = 35;
export const BUILD_TOOLS = '35.0.0';
export const AGP_VERSION = '8.7.3';
export const GRADLE_VERSION = '8.11.1';

/** 家里那台电脑的 Tailscale 地址(页面里的默认服务器地址)。 */
export const DEFAULT_SERVER_ADDRESS = 'wss://laptop-u3hj61a7.tail35209a.ts.net:8443/?token=<令牌>';

/**
 * 与家里 `phone_bridge.py` 对齐的帧类型。
 * 只是留档 + 供校验脚本断言页面确实写了这些类型,不参与构建。
 */
export const PROTOCOL_OUT = ['hello', 'utterance', 'text', 'image', 'ping'];
export const PROTOCOL_IN = ['ready', 'you', 'her', 'audio_begin', 'audio_end', 'error'];

/**
 * AndroidManifest.xml。
 *
 * 权限现在就把阶段 2 要用的全部声明好(录音、拍照、蓝牙 SCO、FileProvider),
 * 免得阶段 2 再动工程结构。`usesCleartextTraffic` 保留给局域网明文调试,
 * 正常路径是 wss(Tailscale 签发的真证书)。
 *
 * `BLUETOOTH` 是 Android 11 及以下的名字,用 `maxSdkVersion="30"` 与新的
 * `BLUETOOTH_CONNECT`(Android 12+)共存,两者都声明才不会在新旧系统上各缺一半。
 *
 * @returns {string} AndroidManifest.xml 源码。
 */
export function manifestXml() {
  return `<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">

    <!-- 与家里通信(WebSocket over Tailscale) -->
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

    <!-- 阶段 2:点击说话录音 -->
    <uses-permission android:name="android.permission.RECORD_AUDIO" />
    <uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />

    <!-- 阶段 2:拍照上传 -->
    <uses-permission android:name="android.permission.CAMERA" />

    <!-- 阶段 2:蓝牙耳机(Sony WH-CH520)的 SCO 通道 -->
    <uses-permission android:name="android.permission.BLUETOOTH" android:maxSdkVersion="30" />
    <uses-permission android:name="android.permission.BLUETOOTH_CONNECT" />
    <!--
      BLUETOOTH_SCAN 名字上只跟"扫描"有关,但鸿蒙上枚举可用通信设备 / 查 BluetoothHeadset
      也会要它,不给就可能什么都扫不到。neverForLocation 声明不用于定位(不带这个标志的
      SCAN 在部分系统上会被拒)。
    -->
    <uses-permission
        android:name="android.permission.BLUETOOTH_SCAN"
        android:usesPermissionFlags="neverForLocation" />

    <!--
      后台保活:连接期间挂一个前台服务,免得一切后台/熄屏就被系统掐断 WebSocket。
      FOREGROUND_SERVICE_MICROPHONE 是 Android 14+ 的类型化权限;
      POST_NOTIFICATIONS 是 Android 13+ 才有的运行时权限(12 上没有,写了无害)。
    -->
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

    <!--
      对话模式熄屏也要能用:进程活着不等于页面在跑 —— 熄屏后 WebView 的 JS 会被节流
      (2026-10-04 实测心跳从 25 秒被拉长到 49 秒),所以对话期间还要拿住 CPU 与 WiFi:
      WAKE_LOCK 给 PowerManager 的 partial wake lock,CHANGE_WIFI_STATE/ACCESS_WIFI_STATE
      给 WifiManager 的高性能 wifi lock。
    -->
    <uses-permission android:name="android.permission.WAKE_LOCK" />
    <uses-permission android:name="android.permission.CHANGE_WIFI_STATE" />
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />

    <!-- 硬件不是必需的:没有摄像头/蓝牙的手机也应该能装(只是少了对应功能) -->
    <uses-feature android:name="android.hardware.camera" android:required="false" />
    <uses-feature android:name="android.hardware.camera.autofocus" android:required="false" />
    <uses-feature android:name="android.hardware.microphone" android:required="false" />
    <uses-feature android:name="android.hardware.bluetooth" android:required="false" />

    <application
        android:label="@string/app_name"
        android:icon="@mipmap/ic_launcher"
        android:roundIcon="@mipmap/ic_launcher"
        android:usesCleartextTraffic="true"
        android:supportsRtl="true"
        android:hardwareAccelerated="true">

        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:launchMode="singleTask"
            android:windowSoftInputMode="adjustResize"
            android:configChanges="orientation|screenSize|screenLayout|keyboardHidden|uiMode">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>

        <!--
          保活用的前台服务。foregroundServiceType 在 Android 14+ 会被强制执行;在 12 上
          写上也无害。microphone 是因为连接期间随时可能录音。
        -->
        <service
            android:name=".QingyuService"
            android:exported="false"
            android:foregroundServiceType="microphone|dataSync" />

        <!-- 阶段 2 拍照:把拍到的照片通过 content:// 交给相机/分享,而不是暴露文件路径 -->
        <provider
            android:name="androidx.core.content.FileProvider"
            android:authorities="\${applicationId}.fileprovider"
            android:exported="false"
            android:grantUriPermissions="true">
            <meta-data
                android:name="android.support.FILE_PROVIDER_PATHS"
                android:resource="@xml/file_paths" />
        </provider>
    </application>
</manifest>
`;
}

/**
 * FileProvider 允许对外暴露的目录。
 *
 * 只开两个 `images/` 子目录:阶段 2 拍的照片写在 `cacheDir/images/`,
 * 临时导出走 `externalCacheDir/images/`。不开整棵缓存树。
 *
 * @returns {string} res/xml/file_paths.xml 源码。
 */
export function filePathsXml() {
  // **两个 name 必须不同**:FileProvider 内部是 mRoots.put(name, root),同名的后者会
  // **覆盖**前者 —— 一开始两条都叫 images,于是只剩"外部缓存"那一根,而照片写在内部缓存
  // cache/images/,getUriForFile 就抛 IllegalArgumentException:
  // "Failed to find configured root that contains /data/data/<包名>/cache/images/…"
  // 表现是点「拍照」立刻报错、相机根本拉不起来(2026-10-03 用户报的"拍照无法使用")。
  return `<?xml version="1.0" encoding="utf-8"?>
<paths>
    <cache-path name="images" path="images/" />
    <external-cache-path name="external_images" path="images/" />
</paths>
`;
}

/**
 * Gradle settings,含仓库声明。
 * @returns {string} settings.gradle 源码。
 */
export function settingsGradle() {
  return `pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "qingyu-phone"
include ':app'
`;
}

/**
 * 根构建脚本。
 * @returns {string} build.gradle 源码。
 */
export function rootBuildGradle() {
  return `plugins {
    id 'com.android.application' version '${AGP_VERSION}' apply false
}
`;
}

/**
 * app 模块构建脚本。
 *
 * 唯一的第三方依赖是 `androidx.core`(FileProvider)。不引 AndroidX 就没有
 * FileProvider 可用(平台没自带),阶段 2 拍照 + 分享必须要它,所以现在就引上,
 * 顺带把 `android.useAndroidX` 打开。除此之外全部只用平台 API。
 *
 * @returns {string} app/build.gradle 源码。
 */
export function appBuildGradle() {
  return `plugins {
    id 'com.android.application'
}

android {
    namespace '${APPLICATION_ID}'
    compileSdk ${COMPILE_SDK}
    buildToolsVersion '${BUILD_TOOLS}'

    defaultConfig {
        applicationId '${APPLICATION_ID}'
        minSdk ${MIN_SDK}
        targetSdk ${TARGET_SDK}
        versionCode ${VERSION_CODE}
        versionName '${VERSION_NAME}'
    }

    buildTypes {
        debug {
            // debug 用标准 debug keystore 签名,自己侧载够用
        }
        release {
            minifyEnabled false
            // 不给 release 签名:本工程只服务用户自己那一台手机
        }
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += ['META-INF/*']
    }

    lint {
        abortOnError false
    }
}

dependencies {
    implementation 'androidx.core:core:1.13.1'
}
`;
}

/**
 * Gradle 属性。
 *
 * 位置相关的东西故意一件都不写在这里:AGP 只要发现**两处**指向它的配置目录就会抛
 * `AndroidLocationsException`,所以缓存重定向只在 `tools/build-apk.js` 里设一个
 * `ANDROID_USER_HOME`。
 *
 * 那个重定向是这台机器上能构建的前提:Android 工具链从用户主目录推导 `.android/`,
 * 而 Windows 用户名含中文时该路径会被写成乱码,AGP 报
 * `IOException: 文件名、目录名或卷标语法不正确`。
 *
 * @returns {string} gradle.properties 源码。
 */
export function gradleProperties() {
  return `org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8
org.gradle.daemon=false
org.gradle.parallel=true
org.gradle.caching=true
# 受限进程令牌下原生文件监视器起不来
org.gradle.vfs.watch=false
android.useAndroidX=true
android.nonTransitiveRClass=true
`;
}

/**
 * 字符串资源。
 * @returns {string} strings.xml 源码。
 */
export function stringsXml() {
  return `<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">${APP_LABEL}</string>
</resources>
`;
}
