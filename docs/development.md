# 开发指南

Windows 使用 `qcs-dev.ps1`，macOS / Linux 使用 `bash qcs-dev.sh`。两者把参数交给公共 Python 入口，按脚本自身位置定位源码，不依赖当前工作目录。应用运行不依赖 Python。

## 工具链与变体

开发环境需要 Python 3.10+、CMake 3.28+、Ninja 和 C++20 编译器。Windows 主程序使用 MSVC 2022 与 Windows SDK，macOS 使用 Xcode Command Line Tools，Linux 使用 GCC 或 Clang。Qt 固定为 6.12.0，配套独立 Qt PDF 模块为 6.140.0。

`config/build-variants.json` 是平台、后端、SDK 和包类型的共同清单。`--variant` 对应 CMake 的 `QCOPILOTS_SEARCH_VARIANT`，完整 11 项矩阵见 [发布指南](releasing.md)。默认变体为 Windows / Linux 的 CPU 或 macOS arm64 Metal。其他主机架构不能被默认值伪装成支持的平台。

Windows CUDA 和 HIP 后端使用外置子构建，主程序及 Qt 仍由 MSVC 构建。最终部署使用主构建的唯一 `ggml-base`，后端 ABI、导入导出和运行库必须一致。SDK 和编译配方按锁定的 llama.cpp 提交引用，升级必须同时更新锁、配方及验证结果。

Linux CPU / Vulkan 使用 Ubuntu 22.04 基线，CUDA / ROCm 使用 Ubuntu 24.04 基线。Qt 的图形、字体、XCB、DBus 和多媒体依赖需要由操作系统提供，包含 `libxcb-shape0` 和 `libpulse0`。无显示服务器的 CI 使用 Xvfb。DEB 安装验证需要 Docker，Windows 安装验证需要 NSIS。Windows 完整测试包含安装器回归时，入口会先准备 NSIS，PowerShell 包装也会查找 Visual Studio 自带 Ninja。macOS 最低版本为 14.4，默认 ad-hoc 签名。

## 外置目录

`--build-root` 指定父根，`--qt-root` 指定 Qt SDK 根。所有生成目录必须位于源码树之外，包含空格或中文的路径应作为单独参数正常引用。

```text
<build-root>/
  sdk/                            未指定 Qt 根时的 SDK 位置
  models/                         经大小和 SHA-256 校验的共享模型
  gpu-sdk/                        GPU SDK 默认根，可显式覆盖
  <variant>/
    Release/                      CMake 构建树
    Debug/                        独立的调试构建树
    RelWithDebInfo/                可选带符号配置
    stage/<Config>/               安装布局
    pkg/<Config>/                 用户安装包及便携包
    reports/<Config>/             本机测试与打包记录
    runs/<Config>/                独立测试实例
    deps/<Config>/                本配置依赖及子构建
    symbols/<Config>/             独立生成的符号材料
    sources/<Config>/             独立生成的开发源码材料
```

本机可以使用 `C:\Dev\github-repos-compilations\qcs` 和 `C:\Softwares\Qt`。这些只是可替换的本地参数，不写入可复用代码。默认构建父根根据仓库上级推导为外置的 `<仓库父目录名>-compilations/qcs`，也可用 `QCOPILOTS_SEARCH_BUILD_ROOT` 覆盖。

编译缓存不能跨不同变体、SDK 或配置复用。已验证的下载与模型缓存可共享，模型不复制进每个包。旧的不含变体身份的构建缓存不原地改名，重新配置新的构建树。已有历史构建、模型、SDK 和用户配置不属于本轮清理范围。

程序真实用户数据使用平台标准目录。`--profile-dir` 为 GUI、服务、CLI 选择独立实例，`--model-dir` 选择模型缓存，stage 或当前工作目录不会成为默认用户数据目录。

## 动作与参数

| 动作 | 用途 |
| --- | --- |
| `bootstrap` | 下载、验证并安装锁定 Qt SDK |
| `doctor` | 检查工具链、SDK、变体、路径和必要环境 |
| `configure`、`build` | 配置外置 CMake 树并编译应用与测试 |
| `models` | 下载或复用模型并校验大小和 SHA-256 |
| `test` | 执行全部注册测试，缺少必需模型明确失败 |
| `check` | 执行完整开发与包验证流程 |
| `run` | 启动当前变体与配置的 GUI，GPU 变体先安装到 stage 再运行 |
| `install`、`package` | 生成 stage，再生成该变体的用户包 |
| `verify-package`、`installers` | 验证最终包及平台安装卸载生命周期 |
| `symbols`、`sources` | 独立生成调试符号或对应源码材料 |
| `release-prepare` | 按 CI run、attempt 和候选下载验证包并冻结发行材料 |
| `release-upload-draft` | 显式创建或续传草稿，不公开发布 |
| `clean` | 核验归属后清理当前变体与配置的受控生成物 |

| 参数 | 含义 |
| --- | --- |
| `--variant ID` | 共同清单中的构建变体 |
| `--build-root PATH`、`--qt-root PATH` | 外置父根与 Qt 安装根 |
| `--config Release\|Debug\|RelWithDebInfo` | 独立构建配置，默认 Release |
| `--gpu-sdk-root PATH` | 已准备的 GPU SDK 根，支持对应环境变量覆盖 |
| `--jobs N` | 编译并发数 |
| `--model-dir PATH` | 已校验或待准备的模型缓存 |
| `--test-label LABEL` | 仅用于排查的 CTest 标签筛选，不能代表完整验收 |
| `--generator NAME` | 独立打包时指定 ZIP、NSIS、TGZ、DEB 或 DragNDrop |
| `--cmake-arg=-DNAME=VALUE` | 额外 CMake 配置参数 |
| `--report-dir PATH` | 本次独立报告目录 |
| `--keep-failed-tests` | 保留失败测试的独立产物 |
| `--scope generated`、`--dry-run` | 显式清理范围与只列出不删除 |

## 日常修改与完整检查

已有 Qt 时直接运行 `doctor`。未安装 SDK 时先运行 `bootstrap`。`check` 顺序执行环境诊断、配置、构建、模型准备、全部保留测试、安装、平台打包、最终包验证，以及 Windows / Linux 安装器验证。源码和符号使用独立动作，不在普通打包中重复生成。`check` 的本地记录包含 `semantic-functional.json`，并核对执行前后源码、四个程序、运行库和用户包的字节稳定性。Windows 完整检查按 NSIS → ZIP 顺序打包。每个生成器使用本次调用的独立 CPack 工作目录，生成后只清理本次受控 stage，保留用户指定的安装 stage 和已有历史产物。

```powershell
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
./qcs-dev.ps1 check --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
./qcs-dev.ps1 build --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 test --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir "$buildRoot/models"
./qcs-dev.ps1 test --variant $variant --build-root $buildRoot --qt-root $qtRoot --test-label unit
```

完整 `check` 不接受缩小范围的标签或只测关键词选项。局部 `test` 记录实际选择与排除项，空选择失败。较少编译任务可以降低峰值内存和磁盘需求，不能通过减少 GPU 架构或测试范围掩盖资源不足。

`check-report.json` 绑定执行时的源码、依赖锁、阶段状态和产物哈希。候选身份绑定 8 项依赖与构建配置文件，包含共同变体清单、GPU SDK 锁和 GPU 许可源码锁。候选来源、程序及包的字节在关键阶段前后重新核对，失败后后续阶段保留为未运行。本机检查成功只说明该主机对应的完整流程通过，跨平台 CI 与 Windows 实际操作需要另行完成。

`verify-package` 使用最终包内的 worker 执行真实文字、图片与音频编码，并由包内服务执行语义搜索。它需要完整、校验通过的模型。独立 `--keyword-only` 仅用于排查，没有完整模型验证的报告不能成为最终包验收。

## 直接使用 CMake

可设置 `QCOPILOTS_SEARCH_BUILD_ROOT`、`QCOPILOTS_SEARCH_QT_PREFIX` 和 `QCOPILOTS_SEARCH_VARIANT` 后使用 `qcs-release` 或 `qcs-debug` preset。Qt prefix 指向具体 kit，与开发入口的 Qt SDK 根不同。两个测试 preset 都执行完整注册清单，直接 CTest 也需要准备模型和测试依赖。

```bash
export QCOPILOTS_SEARCH_BUILD_ROOT="/path/outside/source/build"
export QCOPILOTS_SEARCH_QT_PREFIX="/path/to/Qt/6.12.0/gcc_64"
export QCOPILOTS_SEARCH_VARIANT="linux-x64-cpu"
cmake --preset qcs-release
cmake --build --preset qcs-release
ctest --preset qcs-release
```

Windows 直接 CMake 必须处于 MSVC 环境，也可以使用负责初始化工具链的 PowerShell 包装。CMake 拒绝源码树内构建。

## 排查与清理

先保留原始控制台错误、CMake 配置日志、CTest 输出及独立运行根。模型问题核对大小、SHA-256 和 revision。IPC 问题记录脱敏后的 profile 和实例，不发送用户内容。包级问题从重定位后的最终包复现，排除开发 SDK 的 PATH 或当前目录对依赖加载的影响。

Windows 对象路径默认预算为 200 个字符，其他受控生成路径为 240 个 UTF-16 单元。数据库和锁使用长路径适配，但第三方工具和 `CreateProcessW` 工作目录仍有自身限制。外置根过长时选择更短位置，不关闭路径测试或修改全局设置隐藏错误。

先用 `clean --dry-run` 查看范围，再清理明确归属的当前变体与配置。校验规范化边界、归属标记和目录链接，结束本次子进程后释放句柄。模型、Qt、源码、Git 历史、未提交修改及正式用户数据始终保留。清理失败必须返回失败，不按进程通用名称终止其他实例，也不因为文件被 Git 忽略就视为可删除。
