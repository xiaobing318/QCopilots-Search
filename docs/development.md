# 开发指南

应用使用 C++20、Qt Widgets 和 CMake。Windows 通过 `qcs-dev.ps1` 开发，macOS / Linux 使用 `bash qcs-dev.sh`。入口按脚本位置定位源码，可以从任意工作目录调用。Python 只用于开发、测试和打包，最终应用不依赖 Python。

## 1. 准备环境

| 工具 | 要求 |
| --- | --- |
| 编译器 | Windows 使用 MSVC 2022 和 Windows SDK，macOS 使用 Xcode Command Line Tools，Linux 使用 GCC 或 Clang |
| Qt SDK | Qt 6.12.0，配套 Qt PDF 6.140.0，安装与目标架构匹配的 kit |
| 构建工具 | CMake 3.28+、Ninja、Git |
| 脚本运行环境 | Python 3.10+，Windows 需要 PowerShell 5.1+ |
| 平台打包 | Windows 使用 NSIS，Linux 使用 `dpkg-dev` 和 `file`，macOS 使用系统 DMG 与签名工具 |

Windows 包装脚本自动查找 Visual Studio 2022、初始化 x64 编译环境，并查找其自带 Ninja。如果 `python` 只是 Microsoft Store 占位入口，请安装真实 Python，或将 `QCOPILOTS_SEARCH_PYTHON` 设置为已有解释器的绝对路径。不要将某台机器的私有 Python 路径写进仓库。

以下示例使用已安装的 Qt。`--qt-root` 接受 SDK 根或具体 kit 目录。指向 SDK 根时，脚本从中选择当前平台的 kit，例如 Windows 的 `6.12.0/msvc2022_64`。指向包含 `lib/cmake/Qt6/Qt6Config.cmake` 的 kit 时直接使用该目录。

```powershell
Set-Location C:\Dev\github-repos\QCopilots-Search
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
./qcs-dev.ps1 doctor --variant $variant --build-root $buildRoot --qt-root $qtRoot
```

如果尚未安装 Qt，使用相同参数执行 `bootstrap`，下载并校验锁定 SDK。已有 SDK 不需要重复安装。

`doctor` 记录当前实际 Python 解释器、CMake、CTest、CPack、Ninja、Git 和编译器的路径与版本，检查 Python 3.10+、CMake 3.28+ 及 CMake 工具版本一致性。Qt 检查涵盖必需组件，并核对 Qt Core 6.12.0 和独立 Qt PDF 6.140.0。GPU 变体还会按照依赖锁只读检查 SDK 与编译器，不安装或下载依赖，也不要求本机有可执行推理的 GPU。缺少工具或版本不符时会列出具体错误，完整记录保存在外置报告目录的 `doctor.json`。

```bash
build_root="$(dirname "$PWD")/qcs-build"
qt_root="$(dirname "$PWD")/Qt-SDK"
variant=linux-x64-cpu
bash ./qcs-dev.sh bootstrap --variant "$variant" --build-root "$build_root" --qt-root "$qt_root"
bash ./qcs-dev.sh doctor --variant "$variant" --build-root "$build_root" --qt-root "$qt_root"
```

macOS 使用 `macos-arm64-metal`。全部 11 个变体、系统基线与 GPU SDK 见[依赖说明](dependencies.md)。Linux 无显示服务器测试需要 Xvfb，DEB 安装验证需要 Docker。GPU 变体按锁定配方准备 SDK，主程序仍使用平台原生 Qt 工具链。

## 2. 配置、编译和调试

```powershell
./qcs-dev.ps1 configure --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 build --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
./qcs-dev.ps1 run --variant $variant --build-root $buildRoot --qt-root $qtRoot
```

修改 C++ 后重复 `build` 即可增量编译。修改 CMake、依赖锁或构建选项后先重新 `configure`。调试时给三个动作都添加 `--config Debug`，然后将调试器连接到该配置的 `qcs-gui`、`qcs-service` 或 `qcs-worker`。`RelWithDebInfo` 用于带符号的优化构建。

`run` 使用外置的开发基目录和显式模型目录，不读写正常应用的数据。应用在该基目录下使用 `profiles/v4`，GUI、服务和 worker 之间始终传递基目录。GPU 变体先安装到 stage 再启动，以便使用完整部署布局。需要检查真实用户目录时，直接启动已安装程序，目录规则见[配置与数据目录](configuration.md)。

所有生成内容必须位于源码树外，且变体与配置相互隔离：

```text
<build-root>/
  sdk/                        未指定 Qt 根时的 SDK
  models/                     校验后的共享模型缓存
  gpu-sdk/                    GPU SDK 默认根
  <variant>/
    Release/                  Release 构建树
    Debug/                    Debug 构建树
    RelWithDebInfo/            优化且含符号的构建树
    deps/<Config>/            依赖缓存与子构建
    stage/<Config>/           完整安装布局
    pkg/<Config>/             安装器与便携包
    reports/<Config>/         测试、检查与包验证记录
    runs/<Config>/            测试临时实例
    sources/<Config>/         独立生成的源码材料
    symbols/<Config>/         独立生成的符号材料
```

`QCOPILOTS_SEARCH_BUILD_ROOT` 可以设置默认外置根，命令行 `--build-root` 优先。下载和模型缓存可以在校验后共享，编译缓存不能跨变体、SDK 或配置复用。依赖源码保持上游原样，生成头和完整性标记存放在源码目录之外。

## 3. 测试、安装和打包

```powershell
./qcs-dev.ps1 models --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 test --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir "$buildRoot/models"
./qcs-dev.ps1 install --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 package --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 verify-package --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir "$buildRoot/models"
```

`models` 只准备 EmbeddingGemma 2，下载前比较已构建 CLI 的 manifest 与 `config/qcs-models.lock.json`，陈旧 CLI 会被拒绝并提示重新构建。主模型和投影文件都必须匹配固定 revision、大小与 SHA-256。`test` 保留 Gemma 的黄金向量及全部模态回归，`verify-package` 检查最终包中的真实推理和服务链路，不需要模型选择参数。

模型锁文件嵌入应用，修改文件契约后应重新 `build` 并重启应用。Gemma 的权重、提示或预处理发生实际变化时，必须同步更新管线 fingerprint，并验证受影响向量失效与重新索引。不能只增加 JSON 条目启用其他模型，也不能修改锁定的第三方源码。

新版运行配置仅接受 `version: 4`，数据位于基目录下的 `profiles/v4`。测试升级或旧数据隔离时，保留基目录中的旧文件作为哨兵，并检查运行后字节不变。默认模型缓存不会继承旧版文件，可用 `qcs-cli model import` 复制并校验旧 Gemma 文件，或显式传入 `--model-dir` 使用共享缓存。

`install` 形成完整 stage，`package` 自动部署 Qt、推理库、运行数据、许可证和图标，生成当前变体的用户包，无需手工复制。源码材料和符号分别通过 `sources`、`symbols` 生成，正式发布见[发布指南](releasing.md)。

一次执行完整流程：

```powershell
./qcs-dev.ps1 check --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
```

`check` 允许未提交修改，依次执行 `doctor → configure → build → models → test → install → package → verify-package → installers`，最后一项仅适用于有安装器验证的平台。报告记录阶段、命令、退出码、日志和工作区状态，失败后后续阶段不执行。报告位于 `<build-root>/<variant>/reports/<Config>/check-*/check-report.json`，`reports/<Config>/latest-check.json` 指向最近一次记录。开发检查通过与正式发布的干净提交、CI 来源要求分别判断。

`test --test-label unit` 等筛选用于局部排查，完整 `check` 不接受缩小测试范围。更多测试说明及 GUI 检查清单见[测试指南](testing.md)。

## 4. 直接使用 CMake

CMake presets 接受具体 Qt kit，不接受 SDK 父根。Windows 直接调用前需要进入 MSVC x64 开发环境。

```bash
export QCOPILOTS_SEARCH_BUILD_ROOT="/path/outside/source/build"
export QCOPILOTS_SEARCH_QT_PREFIX="/path/to/Qt/6.12.0/gcc_64"
export QCOPILOTS_SEARCH_VARIANT="linux-x64-cpu"
cmake --preset qcs-release
cmake --build --preset qcs-release
ctest --preset qcs-release
```

Debug 对应 `qcs-debug`。CTest 需要已经准备好的模型与测试依赖。Windows 程序与动态库使用共享 CRT，Release 为 `/MD`，Debug 为 `/MDd`。Linux CUDA CI 使用锁定的 CMake 3.31.10，并保证 `cmake`、`ctest`、`cpack` 来自同一套安装。

## 5. 排查与清理

| 现象 | 检查方式 |
| --- | --- |
| 找不到 Python、MSVC 或 Qt | 先执行 `doctor`，检查实际解释器、编译环境与 Qt kit |
| 模型不可用 | 执行模型校验，检查大小和 SHA-256，不能通过重命名其他权重替代 |
| 第三方源码校验失败 | 使用匹配锁定版本的原始源码和新的外置缓存，不修改源码或关闭校验 |
| 生成路径过长 | 改用较短的外置构建根，保留中文、空格与长路径功能测试 |
| 包内缺库 | 从最终包的独立解包目录复现，检查部署日志，排除开发机 PATH 补齐依赖 |
| Linux DEB 引入驱动依赖 | 使用锁定的无驱动容器，不能删除依赖字段绕过包验证 |
| 测试失败 | 查看报告目录和构建树 `Testing/Temporary`，必要时加 `--keep-failed-tests` |

清理前添加 `--dry-run` 查看准确范围。`clean --scope build` 只清理当前变体选定配置的构建树。`clean --scope generated` 清理当前变体的全部配置及其依赖、stage、包、报告和其他受控生成物，包含 Release、Debug 和 RelWithDebInfo，不能用于仅清理一个配置。确认范围后移除 `--dry-run`。源码、共享模型、Qt SDK、开发 profile 和正常用户数据保留。

完整参数以 `./qcs-dev.ps1 --help` 或 `bash ./qcs-dev.sh --help` 为准。脚本新增或删除时，同时核对 CMake、CI、动态导入、工具测试和相关文档。
