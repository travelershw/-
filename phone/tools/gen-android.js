#!/usr/bin/env node
/**
 * 生成 `android/` 工程目录。
 *
 * 工程是**生成物**:源码只有一份,放在 `lib/` 里(便于 review 和 diff),
 * 重新跑本脚本会覆盖已有文件,`android/` 整个删掉也能重建。
 * 阶段 2 加原生录音/拍照时,改的是 `lib/android-sources.js` 与
 * `lib/page/index.html`,不是 `android/` 里的文件。
 *
 * 用法: node tools/gen-android.js [--clean]
 *
 * @module qingyu-phone/tools/gen-android
 */
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import {
  BUILD_TOOLS,
  COMPILE_SDK,
  MIN_SDK,
  TARGET_SDK,
  VERSION_NAME,
  VERSION_CODE,
  APP_LABEL,
  APPLICATION_ID,
  GRADLE_VERSION,
  appBuildGradle,
  filePathsXml,
  gradleProperties,
  manifestXml,
  rootBuildGradle,
  settingsGradle,
  stringsXml,
} from '../lib/android-project.js';
import {
  PACKAGE_PATH,
  gradleWrapperProperties,
  iconResources,
  legacyMipmap,
  mainActivityJava,
  qingyuAudioJava,
  qingyuCameraJava,
  qingyuNativeJava,
  qingyuPlayerJava,
  qingyuServiceJava,
} from '../lib/android-sources.js';
import { ANDROID_DIR, ROOT } from '../lib/toolchain.js';

if (process.argv.includes('--clean') && existsSync(ANDROID_DIR)) {
  rmSync(ANDROID_DIR, { recursive: true, force: true });
  console.log(`已删除 ${ANDROID_DIR}`);
}

/**
 * 写一个文件,顺带建父目录。
 * @param relativePath 相对 `android/` 的路径。
 * @param content 文件内容。
 * @returns {string} 写入的绝对路径。
 */
function emit(relativePath, content) {
  const target = `${ANDROID_DIR}/${relativePath}`;
  mkdirSync(target.slice(0, target.lastIndexOf('/')), { recursive: true });
  writeFileSync(target, content, 'utf8');
  return target;
}

/** 生成清单,用来统计并让人核对。 */
const written = [];

written.push(emit('settings.gradle', settingsGradle()));
written.push(emit('build.gradle', rootBuildGradle()));
written.push(emit('gradle.properties', gradleProperties()));
written.push(emit('gradle/wrapper/gradle-wrapper.properties', gradleWrapperProperties(GRADLE_VERSION)));
written.push(emit('app/build.gradle', appBuildGradle()));
written.push(emit('app/src/main/AndroidManifest.xml', manifestXml()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/MainActivity.java', mainActivityJava()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/QingyuNative.java', qingyuNativeJava()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/QingyuAudio.java', qingyuAudioJava()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/QingyuPlayer.java', qingyuPlayerJava()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/QingyuCamera.java', qingyuCameraJava()));
written.push(emit('app/src/main/java/' + PACKAGE_PATH + '/QingyuService.java', qingyuServiceJava()));
written.push(emit('app/src/main/res/xml/file_paths.xml', filePathsXml()));
written.push(emit('app/src/main/res/values/strings.xml', stringsXml()));
for (const [relativePath, content] of Object.entries(iconResources())) {
  written.push(emit('app/src/main/res/' + relativePath, content));
}
written.push(emit('app/src/main/res/mipmap/ic_launcher.xml', legacyMipmap()));

// 页面原样搬进 assets,成为 APK 的一部分(不是远程页面)。
// 必须以 UTF-8 读、以 UTF-8 写:页面里有大量中文,编码错了会变成乱码。
// pure.js 是页面的纯逻辑(WAV 封装/组帧/base64),同一份还被 Node 测试直接 require,
// 所以这里只是搬运,不是复制 —— 测试和真机跑的永远是同一份代码。
const assets = [
  ['lib/page/index.html', 'app/src/main/assets/index.html'],
  ['lib/page/pure.js', 'app/src/main/assets/pure.js'],
];
for (const [source, target] of assets) {
  const sourcePath = `${ROOT}/${source}`;
  if (!existsSync(sourcePath)) {
    console.error(`缺少页面源码: ${sourcePath}`);
    process.exit(1);
  }
  const content = readFileSync(sourcePath, 'utf8');
  written.push(emit(target, content));
  console.log(`  打包 ${source} → ${target}(${(Buffer.byteLength(content, 'utf8') / 1024).toFixed(1)} KB)`);
}

console.log(`已生成 Android 工程: ${ANDROID_DIR}`);
console.log(`  ${APPLICATION_ID} · ${APP_LABEL} · v${VERSION_NAME}(${VERSION_CODE})`);
console.log(`  compileSdk ${COMPILE_SDK} · minSdk ${MIN_SDK} · targetSdk ${TARGET_SDK} · buildTools ${BUILD_TOOLS} · Gradle ${GRADLE_VERSION}`);
console.log('');
for (const path of written) console.log(`  ${path.slice(ANDROID_DIR.length + 1)}`);
console.log('');
console.log('下一步: node tools/build-apk.js');
