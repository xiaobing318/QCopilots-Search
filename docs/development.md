# 开发指南

开发脚本把源码位置与生成物位置分开。脚本从自身文件路径找到仓库，`--build-root` 指定外置构建根，`--qt-root` 指定 Qt SDK 根。路径按参数传递，包含空格或中文时需要正常引用，不要拼接成另一个 shell 命令。

## 工具链

| 平台 | 编译器与系统 SDK | 发行目标 |
| --- | --- | --- |
| Windows x64 | Visual Studio 2022 C++ 桌面工作负载、Windows SDK、CMake 3.28+、Ninja | Windows 10 1809+ / Windows 11 |
| macOS arm64 | Xcode Command Line Tools、CMake 3.28+、Ninja | macOS 14.4+ |
| macOS x86_64 | Xcode Command Line Tools、CMake 3.28+、Ninja | macOS 14.4+ |
| Linux x86_64 | GCC / Clang C++20、CMake 3.28+、Ninja 和 Qt 运行依赖 | Ubuntu 22.04+ |

开发脚本需要 Python 3.10+，安装后的程序不需要 Python。CPU 是四个构建组合的必需推理后端。硬件加速只有获得对应硬件的测试证据后才声明支持。

Qt 6.12.0 和独立 Qt PDF 6.140.0 由 bootstrap 安装。官方 SDK 包按下载校验值锁定。其他依赖通过 CMake 下载到外置依赖目录，不修改系统包的源码，也不在仓库中展开第三方源码。

Linux 主机还需要 Qt 使用的图形、字体、XCB、DBus 和多媒体运行依赖。Qt 的 XCB 平台插件需要 `libxcb-shape0` 提供的 `libxcb-shape.so.0`。Qt Multimedia 的 PulseAudio 客户端动态库由 `libpulse0` 提供，构建和运行都需要该库，不要求安装开发头文件或启动音频服务。无显示服务器的 CI 在 Xvfb 中执行 GUI 用例。平台依赖安装命令以 `.github/workflows/qcs-native.yml` 为可执行参考，`doctor` 输出用于检查当前主机。

## 目录布局

```text
QCopilots-search/                 源码，可以位于任意可读目录
  src/                           C++ 模块
  tests/                         QtTest、进程与模型测试及只读夹具
  tools/                         开发与验收工具
  cmake/                         依赖、部署和打包配置
  docs/                          开发和验收说明

<build-root>/                    仓库外，运行参数指定
  release/                       Release 构建树
  debug/                         可选 Debug 构建树
  deps/                          锁定依赖和下载缓存
  models/                        可选的共享只读模型缓存
  runs/                          独立测试实例与失败产物
  stage/                         cmake --install 的输出
  pkg/                           CPack 工作目录和安装包
  reports/                       CTest 和验收报告

<build-root>/release/            每配置独立生成目录
  nd/<dep>/                      原生依赖构建
  sb/<dep>/                      FetchContent 子构建
  ep/                            ExternalProject 工作目录
  gen/licenses/                  生成许可证材料
```

本机选用 `C:\Dev\github-repos-compilations\qcs` 作为外置根。其他电脑通过 `--build-root` 或 `QCOPILOTS_SEARCH_BUILD_ROOT` 指定位置，脚本的默认短根也位于源码树外。`qcs` 是程序、脚本、真实 CMake target、安装资源和新产物的名称前缀。产品显示名称仍为 QCopilots Search，命名空间 `qcopilots_search` 和 `QCOPILOTS_SEARCH_*` 配置接口不变，不提供旧长命令别名。

`stage` 供部署和打包使用。程序的真实用户数据通过平台标准应用目录解析，开发构建目录不会成为默认用户数据目录。使用 `--profile-dir` 可以让 GUI、服务和 CLI 连接独立实例，`--model-dir` 可以选择模型目录。

Windows 的数据库打开使用扩展 NT 路径和 SQLite `win32-longpath` VFS，锁文件在 `QLockFile` 构造处统一转换绝对盘符或 UNC 路径，使创建和删除使用同一文件名。公开路径、持久化配置和 IPC 实例标识仍采用原有表示。这些适配不取消文件系统、单个路径段或第三方工具自身的长度限制。启动子进程时，Windows 工作目录仍受 `MAX_PATH` 限制。发布证据工具仅在确认指向同一目录后使用现有 8.3 短名称，也允许显式指定较短的 `--working-directory`，详见[发布指南](releasing.md)。

## 统一脚本入口

Windows 使用 `qcs-dev.ps1`，macOS / Linux 使用 `bash qcs-dev.sh`。二者接受相同的动作与参数。

| 动作 | 用途 |
| --- | --- |
| `bootstrap` | 获取并校验锁定 Qt SDK，准备外置依赖环境 |
| `doctor` | 检查编译器、SDK、构建工具、路径和必要环境 |
| `configure` | 生成外置 CMake 构建树 |
| `build` | 编译程序、工具和启用的测试 |
| `test` | 默认运行快速 CTest 范围，显式 `--test-profile full` 运行全套，并保存报告 |
| `run` | 从构建结果启动应用 |
| `install` | 使用 CMake install 生成 staging 布局 |
| `package` | 从安装布局生成选定平台包 |
| `verify-package` | 检查部署文件、许可材料及包内真实多模态推理 |
| `clean` | 默认仅清理指定构建配置，显式生成物清理须验证归属和路径边界 |

共同参数：

| 参数 | 含义 |
| --- | --- |
| `--build-root PATH` | 外置构建根 |
| `--qt-root PATH` | Qt SDK 安装根 |
| `--config Release\|Debug` | 构建配置，默认 Release |
| `--jobs N` | 并行编译任务数 |
| `--model-dir PATH` | 实际模型目录 |
| `--test-profile fast\|full` | 测试范围，默认 `fast` 排除 `slow` 标签，`full` 不排除 |
| `--test-label LABEL` | 在所选测试范围内继续按 CTest 标签筛选 |
| `--generator NAME` | CPack 生成器，如 ZIP、NSIS、TGZ、DEB、DragNDrop |
| `--cmake-arg=-DNAME=VALUE` | 向 configure 传递额外的 CMake 配置 |
| `--report-dir PATH` | 当前测试或包验证的独立外置报告目录 |
| `--keep-failed-tests` | 保留失败用例的产物 |
| `--scope generated` | `clean` 的显式生成物清理范围，排除模型、SDK、源码和正式用户配置 |
| `--dry-run` | `clean` 只列出处理范围，不删除文件 |

使用较少的并行任务可以降低编译峰值内存和磁盘压力。修改构建选项后重新运行 `configure`，修改源文件后运行 `build` 和相关测试。

## 日常修改与完整验收

日常修改后先运行 `build` 和默认 `test`。`fast` 仍包含 GUI、真实服务、进程、存储、路径和提取回归，但不执行真实模型推理、语义质量、物理容量及完整验收运行器测试。只运行某类测试时可加 `--test-label`，筛选与 `fast` 的排除条件取交集。例如真实模型用例必须同时指定 `--test-profile full --test-label real-model`，空选择会明确失败。

下面的完整验收示例显式选择 `full`。日常快速通过不能替代发行所需的完整本地、四组合原生 CI、Computer Use 及容量证据。

```powershell
$buildRoot = 'D:\Builds\qcs 示例'
$qtRoot = 'D:\SDKs\Qt'
$modelDir = Join-Path $buildRoot 'models'
./qcs-dev.ps1 bootstrap --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 doctor --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 configure --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcs-dev.ps1 build --build-root $buildRoot --qt-root $qtRoot --jobs 4
./qcs-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --test-profile full --model-dir $modelDir
./qcs-dev.ps1 install --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 package --build-root $buildRoot --qt-root $qtRoot --generator ZIP
./qcs-dev.ps1 verify-package --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
```

以上路径是可替换的命令示例。代码、CI 与测试不会依赖这些盘符。模型准备方式及完整测试的门槛见 [测试指南](testing.md)。

`verify-package` 默认执行发布验收，需要通过 `--model-dir` 或 `QCOPILOTS_SEARCH_MODEL_DIR` 提供两份完整、校验通过的锁定模型。验收使用最终包内的 worker 执行文字、图片和音频黄金向量，并通过安装后的服务执行语义检索。它保留原始 worker 回复、实际加载库的来源与哈希，以及对应许可材料。仅做开发阶段关键词检查时可显式使用 `--keyword-only`，该报告的 `releaseComplete` 为 false，不能用于发布门禁。

## 直接使用 CMake

熟悉 CMake 的开发者可以设置 `QCOPILOTS_SEARCH_BUILD_ROOT` 和 `QCOPILOTS_SEARCH_QT_PREFIX` 后使用仓库的 `qcs-release` preset。`QCOPILOTS_SEARCH_QT_PREFIX` 指向具体 Qt kit，区别于脚本使用的 SDK 根目录。

```bash
export QCOPILOTS_SEARCH_BUILD_ROOT="/path/outside/source/build"
export QCOPILOTS_SEARCH_QT_PREFIX="/path/to/Qt/6.12.0/gcc_64"
cmake --preset qcs-release
cmake --build --preset qcs-release
ctest --preset qcs-release
cmake --install "$QCOPILOTS_SEARCH_BUILD_ROOT/release"
```

`ctest --preset qcs-release` 与 `qcs-debug` 默认排除 `slow`。需要全套测试时使用 `ctest --preset qcs-release-full` 或 `qcs-debug-full`，完整 configure → build → test 可使用 `cmake --workflow --preset qcs-release-full`。这些是运行时筛选，所有用例仍注册在构建树中。直接执行无过滤的 `ctest --test-dir <build>/release` 仍运行全套测试，并需要真实模型。

不同操作系统的 Qt kit 名称不同。Windows 在 MSVC 环境中运行直接 CMake 命令，或使用会初始化工具链的开发脚本。CMake 会拒绝源码树内构建。

## 排查失败

Windows 配置使用 200 字符的默认对象路径预算，普通构建的其他受控生成路径预算为 240 字符。路径检查覆盖对象、MOC、RCC、response file、依赖辅助文件和打包目录，Windows 按 UTF-16 长度统计。输入文件的长路径压力用例不套用普通构建预算。根目录本身过长时应选择更短的外置位置，不能通过关闭测试或修改系统全局设置隐藏生成路径问题。

先保留控制台错误、CMake 配置日志、CTest 输出与独立测试根。模型问题同时记录文件大小、SHA-256 和模型 revision。IPC 问题记录实例名及脱敏后的配置目录，不发送用户文件内容。包级问题从解压后的安装目录复现，避免使用源码树或开发 SDK 补齐缺失的运行库。

不要通过关闭必需测试、替换黄金向量或把格式失败标记为成功来完成验收。依赖下载失败时先检查锁定 URL 和校验值，不能无提示地回退到任意系统版本。

## 清理与重新构建

运行 `clean --dry-run` 先检查指定配置的删除范围，完整清理本项目生成目录需显式选择 `clean --scope generated`。清理必须核对归属标记、规范化目录边界和链接，先结束本次创建的子进程并释放句柄。清理失败返回失败并列出未删除项，不按进程通用名称终止正式应用，不因为文件被 Git 忽略就视为可删除。

旧长命名构建树不能原地改名后继续使用。首次切换时，在必要外置运行器整理入仓库、模型校验搬移完成后，删除旧构建、日志、历史报告、测试数据和缓存，再在新根配置。Qt、MSVC 等外部 SDK、模型内容、源码、Git 历史、未提交修改和正式用户配置不属于这次生成物清理。每次清理保留简短结果，具体实际完成情况以该记录为准。
