# 第三方许可证声明（THIRD PARTY NOTICES）

本文件列出本仓库直接包含或引用的第三方组件及其许可证。许可证文本以各组件自带的 `LICENSE` / `COPYING` 文件为准；下表为摘要，力求准确、保守。

> **总体说明**
> - 根目录采用 **AGPL-3.0**，但根目录 AGPL-3.0 **不覆盖**任何第三方组件自身的许可证。各第三方组件按其自身许可证单独授权。
> - 本仓库**不保证所有（闭源）依赖均可再分发**；再分发前请逐一核对各组件许可证及其适用条件。

## 一、代码中直接包含（bundled / vendored）的组件

| 组件 | 版本 | 许可证 | 位置 / 说明 |
| --- | --- | --- | --- |
| AstrBot（上游核心 + dashboard） | 4.28.x | **GNU AGPL-3.0** | `vendor/AstrBot/`；其自带 `vendor/AstrBot/LICENSE` 保留 |
| cpp-httplib（`httplib.h`） | 0.53.x | **MIT** | `legacy-cpp/`；文件头 MIT 声明保留 |
| nlohmann/json（`json.hpp`） | 3.11.x | **MIT** | `legacy-cpp/`；文件头 MIT 声明保留 |

## 二、构建时获取 / 运行时依赖的组件

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| Mbed TLS | v3.6.3 | **Apache-2.0** | `legacy-cpp/` 通过 CMake FetchContent 获取 |
| PySide6 | 6.11.2 | **LGPL-3.0 / GPL-3.0 / 商业许可** | 桌宠 GUI。**具体适用哪种许可取决于你的发行方式**（例如以动态链接 LGPL 库的方式分发；如需静态链接或闭源商用，需另选商业许可）。请查阅 Qt 官方许可说明 |
| httpx | 0.28.1 | **BSD-3-Clause** | 桌宠 HTTP 客户端 |
| websockets | 17.1 | **BSD-3-Clause** | 桌宠 WebSocket 客户端 |
| PyInstaller | 6.22.3 | **GPL-2.0（含 bootloader 例外）** | 仅作为**构建工具**使用，不随成品分发 |

## 三、不含在仓库内、需用户自行获取的组件

| 组件 | 许可证 | 说明 |
| --- | --- | --- |
| CA 证书（`cacert.pem`，Mozilla CA 证书束） | MPL-2.0 | **仓库不附带**；如需 HTTPS 校验，从 curl 官网下载：<https://curl.se/docs/caextract.html> |

## 四、许可证文本链接（供核对）

- AGPL-3.0：<https://www.gnu.org/licenses/agpl-3.0.html>
- Apache-2.0：<https://www.apache.org/licenses/LICENSE-2.0>
- MIT：<https://opensource.org/licenses/MIT>
- BSD-3-Clause：<https://opensource.org/licenses/BSD-3-Clause>
- LGPL-3.0 / GPL-3.0：<https://www.gnu.org/licenses/>
- MPL-2.0：<https://www.mozilla.org/en-US/MPL/2.0/>

## 五、再分发提示（保守）

- AGPL-3.0 组件的网络服务场景有源码提供义务，请按许可证履行。
- LGPL 组件通常要求允许用户替换/重新链接该库，并随附 LGPL 文本；商业闭源发行请核实是否需商业许可。
- 本仓库不保证任何未列出的闭源/商业依赖具备再分发权利；**发布前请自行完成许可证合规核查**。
