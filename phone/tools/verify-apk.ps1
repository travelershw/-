# Verify the built APK without a phone.
#
# `adb install` needs a phone on the other end, but most of what makes an APK
# unusable is visible statically: alignment, signature scheme, declared entry
# point, permissions, the FileProvider, and the cleartext-traffic flag the LAN
# fallback depends on. This runs the SDK's own tooling against the artifact so a
# build that merely *succeeded* is not mistaken for one that can be installed.
#
# Written in PowerShell rather than Node on purpose: under the file sandbox a
# Node child process with piped stdio is denied (EPERM), so execFileSync around
# build-tools always fails. PowerShell's own pipelines are unaffected.
#
# Kept ASCII-only deliberately: Windows PowerShell 5.1 reads .ps1 files as ANSI,
# so non-ASCII literals in this file arrive as mojibake and can break parsing.
# The expected permission list is therefore *derived* from the generated
# manifest source (lib/android-project.js is its single source of truth) instead
# of being duplicated here as escaped Chinese-bearing XML.
#
# Usage:
#   pwsh -NoProfile -File tools/verify-apk.ps1
#   pwsh -NoProfile -File tools/verify-apk.ps1 -Apk path\to.apk

[CmdletBinding()]
param(
    [string]$Apk = ""
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

# Toolchain is reused in place from wherever this machine keeps it; the repository never
# hardcodes a local path. Precedence: QINGYU_TOOLCHAIN > QINGYU_SHARED_TOOLCHAIN >
# the one-line hint file `.toolchain.local` (git-ignored) > this project's own `.toolchain`.
if ($env:QINGYU_TOOLCHAIN) {
    $toolchain = $env:QINGYU_TOOLCHAIN
} elseif ($env:QINGYU_SHARED_TOOLCHAIN) {
    $toolchain = $env:QINGYU_SHARED_TOOLCHAIN
} elseif (Test-Path (Join-Path $root '.toolchain.local')) {
    $toolchain = (Get-Content (Join-Path $root '.toolchain.local') -Raw).Trim()
} else {
    $toolchain = Join-Path $root '.toolchain'
}
$buildTools = Join-Path $toolchain 'android-sdk\build-tools\35.0.0'

if ([string]::IsNullOrWhiteSpace($Apk)) {
    $Apk = Join-Path $root 'android\app\build\outputs\apk\debug\app-debug.apk'
}

if (-not (Test-Path $Apk)) {
    Write-Host "missing APK: $Apk"
    Write-Host 'run first: node tools/build-apk.js'
    exit 1
}
if (-not (Test-Path $buildTools)) {
    Write-Host "missing build-tools: $buildTools"
    Write-Host 'set QINGYU_TOOLCHAIN, or run node tools/setup-android-sdk.js'
    exit 1
}

$script:Failures = @()

function Test-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        if ($Detail) { Write-Host "ok   $Name  ($Detail)" } else { Write-Host "ok   $Name" }
    } else {
        if ($Detail) {
            Write-Host "FAIL $Name  ($Detail)"
            $script:Failures += "$Name - $Detail"
        } else {
            Write-Host "FAIL $Name"
            $script:Failures += $Name
        }
    }
}

# Run a build-tools executable, merging stderr into the captured text.
function Invoke-Tool {
    param([string]$Tool, [string[]]$Arguments)
    $exe = Join-Path $buildTools $Tool
    $output = & $exe @Arguments 2>&1 | Out-String
    return [pscustomobject]@{ Output = $output; Code = $LASTEXITCODE }
}

$item = Get-Item $Apk
Write-Host "APK  $Apk"
Write-Host ("size {0:N1} KB ({1} bytes)" -f ($item.Length / 1KB), $item.Length)
Write-Host ''

# 1. Alignment: unaligned APKs install but fail to load on some devices.
$align = Invoke-Tool 'zipalign.exe' @('-c', '4', $Apk)
Test-Check 'zipalign 4-byte aligned' ($align.Code -eq 0)

# 2. Signature: a debug-keystore signature is what makes it sideloadable.
$sign = Invoke-Tool 'apksigner.bat' @('verify', '--verbose', $Apk)
$scheme = ([regex]::Match($sign.Output, 'Verified using v2 scheme[^\r\n]*')).Value.Trim()
Test-Check 'apksigner verify' ($sign.Code -eq 0) $scheme
Test-Check 'v2 signature scheme present' ($sign.Output -match 'v2 scheme.*: true')

# 3. Manifest facts the app depends on.
$badging = (Invoke-Tool 'aapt2.exe' @('dump', 'badging', $Apk)).Output
Test-Check 'package dev.qingyu.phone' ($badging -match "package: name='dev\.qingyu\.phone'")
# Version expectations come from lib/android-project.js (单一来源), not hardcoded here:
# a hardcoded pair silently rots the moment the version is bumped (v0.2.1 就撞过一次).
$projectSource = Get-Content (Join-Path $root 'lib\android-project.js') -Raw
$expectedName = [regex]::Match($projectSource, "VERSION_NAME = '([^']+)'").Groups[1].Value
$expectedCode = [regex]::Match($projectSource, 'VERSION_CODE = (\d+)').Groups[1].Value
Test-Check "versionName $expectedName" ($badging -match "versionName='$([regex]::Escape($expectedName))'") ([regex]::Match($badging, "versionName='[^']*'")).Value
Test-Check "versionCode $expectedCode" ($badging -match "versionCode='$expectedCode'")
Test-Check 'minSdk 24' ($badging -match "sdkVersion:'24'") ([regex]::Match($badging, "sdkVersion:'\d+'")).Value
Test-Check 'targetSdk 35' ($badging -match "targetSdkVersion:'35'") ([regex]::Match($badging, "targetSdkVersion:'\d+'")).Value
Test-Check 'launcher activity dev.qingyu.phone.MainActivity' ($badging -match "launchable-activity: name='dev\.qingyu\.phone\.MainActivity'")
# The label is Chinese, so the check cannot spell it out in an ASCII-only script:
# assert that the label line exists and carries non-ASCII characters.
Test-Check 'app label is set and non-ASCII' ($badging -match "application-label:'.*[^\x00-\x7F]")

# Permissions: read the generated manifest (single source of truth) and require
# every declared one to survive into the built APK.
$manifestSource = Join-Path $root 'android\app\src\main\AndroidManifest.xml'
if (-not (Test-Path $manifestSource)) {
    Test-Check 'generated AndroidManifest.xml present' $false 'run node tools/gen-android.js'
} else {
    $manifestText = Get-Content -LiteralPath $manifestSource -Raw -Encoding UTF8
    $declared = [regex]::Matches($manifestText, 'uses-permission\s+android:name="([^"]+)"') |
        ForEach-Object { $_.Groups[1].Value }
    Test-Check 'manifest source declares the required permission set' ($declared.Count -ge 14) ("$($declared.Count) permissions")

    $required = @(
        'android.permission.INTERNET',
        'android.permission.ACCESS_NETWORK_STATE',
        'android.permission.RECORD_AUDIO',
        'android.permission.CAMERA',
        'android.permission.MODIFY_AUDIO_SETTINGS',
        'android.permission.BLUETOOTH',
        'android.permission.BLUETOOTH_CONNECT',
        'android.permission.BLUETOOTH_SCAN',
        'android.permission.FOREGROUND_SERVICE',
        # 对话模式熄屏也要在跑:CPU 唤醒锁 + 高性能 WiFi 锁
        'android.permission.WAKE_LOCK',
        'android.permission.CHANGE_WIFI_STATE',
        'android.permission.ACCESS_WIFI_STATE'
    )
    foreach ($permission in $required) {
        $inSource = $declared -contains $permission
        $inApk = $badging -match [regex]::Escape("name='$permission'")
        Test-Check "permission $permission" ($inSource -and $inApk)
    }

    # The legacy BLUETOOTH permission must stay capped at API 30, otherwise the
    # app asks for a permission that no longer exists on Android 12+.
    Test-Check 'legacy BLUETOOTH capped at maxSdkVersion 30' ($manifestText -match 'android\.permission\.BLUETOOTH"\s+android:maxSdkVersion="30"')

    # BLUETOOTH_SCAN carries usesPermissionFlags="neverForLocation"; without that
    # flag some OEM builds refuse the permission request outright, and HarmonyOS
    # needs SCAN to enumerate communication devices at all.
    Test-Check 'BLUETOOTH_SCAN declares neverForLocation' ($manifestText -match 'android\.permission\.BLUETOOTH_SCAN"\s*\r?\n?\s*android:usesPermissionFlags="neverForLocation"')
}

# 4. The LAN fallback serves plain HTTP, so cleartext must be permitted or every
#    request fails on API 28+ with no obvious cause.
$tree = (Invoke-Tool 'aapt2.exe' @('dump', 'xmltree', '--file', 'AndroidManifest.xml', $Apk)).Output
Test-Check 'cleartext HTTP allowed (LAN fallback)' ($tree -match 'usesCleartextTraffic\(0x010104ec\)=true')

# 5. Stage 2 stores captured photos via FileProvider, so the provider must be in
#    the packaged manifest with the right authority and a paths file.
Test-Check 'FileProvider declared' ($tree -match 'androidx\.core\.content\.FileProvider')
Test-Check 'FileProvider authority dev.qingyu.phone.fileprovider' ($tree -match 'dev\.qingyu\.phone\.fileprovider')
Test-Check 'FileProvider paths metadata' ($tree -match 'android\.support\.FILE_PROVIDER_PATHS')
Test-Check 'FileProvider not exported' ($tree -match 'exported\(0x01010010\)=false')
Test-Check 'camera feature declared (optional installs allowed)' ($tree -match 'android\.hardware\.camera"')

# 6. Stage 2 specifics.
# The legacy BLUETOOTH permission must be capped (section 3), but
# BLUETOOTH_CONNECT must NOT be: on Android 12+ it is the only way to reach a
# Bluetooth audio device, so a maxSdkVersion on it would silently break SCO.
Test-Check 'BLUETOOTH_CONNECT not capped' (-not ($badging -match "name='android\.permission\.BLUETOOTH_CONNECT' maxSdkVersion"))

# BLUETOOTH_SCAN must reach the packaged manifest WITH the neverForLocation flag
# (bit 0x00010000): HarmonyOS needs SCAN to enumerate communication devices, and
# without the flag some builds refuse the request entirely.
Test-Check 'BLUETOOTH_SCAN packaged with neverForLocation' ($tree -match 'usesPermissionFlags\(0x01010644\)=0x00010000')

# Photos are written to cacheDir/images and handed out through the provider, so
# the packaged paths file must expose exactly that directory. A missing or wrong
# entry only fails at runtime, on the phone, after a photo has been taken.
$paths = (Invoke-Tool 'aapt2.exe' @('dump', 'xmltree', '--file', 'res/xml/file_paths.xml', $Apk)).Output
Test-Check 'FileProvider paths file packaged' ($paths -match 'cache-path')
Test-Check 'FileProvider exposes images directory' ($paths -match 'images/')

# 7. Keep-alive foreground service: without it the OS kills the WebSocket a few
#    seconds after the app goes to the background, which is the whole reason the
#    service exists. The type bits must survive into the packaged manifest or
#    Android 14+ refuses startForeground.
$serviceTree = $tree
Test-Check 'foreground Service declared' ($serviceTree -match 'E: service')
Test-Check 'foreground Service is QingyuService' ($serviceTree -match 'dev\.qingyu\.phone\.QingyuService')
Test-Check 'foreground Service declares a type' ($serviceTree -match 'foregroundServiceType')
# (The service code itself - notification channel id, title, stop action - is
#  asserted at dex level by tools/verify-dex.js; aapt2 dump strings only covers
#  the resource table, not DEX string constants.)

# 8. Native entry points and the injected bridge are checked at dex level by
#    tools/verify-dex.js (this script cannot read a dex inside a zip).
if ($script:Failures.Count -gt 0) {
    Write-Host ''
    Write-Host 'FAILURES:'
    foreach ($line in $script:Failures) { Write-Host "  $line" }
    exit 1
}

Write-Host ''
Write-Host 'PASS: APK verified statically and is ready to sideload'
Write-Host ''
Write-Host 'next: node tools/verify-dex.js   (classes and the bundled page)'
Write-Host 'install onto a phone (USB debugging enabled):'
$adb = Join-Path $toolchain 'android-sdk\platform-tools\adb.exe'
Write-Host "  $adb install -r `"$Apk`""
exit 0
