# QCopilots Search

QCopilots Search 是使用 C++20、Qt Widgets 和 CMake 开发的本地多模态搜索工具，应用场景和推理契约参考 [DigUp](https://github.com/ARahim3/DigUp)。它把文件名、正文、OCR 和语义结果放在同一个搜索入口中，面向 Windows x64、Linux x64 和 macOS Apple Silicon。

项目正在完成首版跨平台集成。功能实现与实际验收分别记录，请以 [实施状态](docs/implementation-status.md)、[需求与测试矩阵](docs/requirements-test-matrix.md) 和同一候选的执行记录判断已验证范围。尚未执行的设备或平台验证不能记为通过。

## 应用与数据

GUI 为 `qcs-gui`，Windows 对应 `qcs-gui.exe`，macOS 应用包为 `qcs-gui.app`。本地服务为 `qcs-service`，提取和推理工作进程为 `qcs-worker`，命令行入口为 `qcs-cli`。服务统一管理索引写入和查询，组件通过规范化 profile 定位实例。同一 profile 的重复 GUI 启动激活已有窗口，不同 profile 可并行运行。

搜索只处理用户选择的目录。模型保存在本地，模型下载和更新检查需要网络，文件提取与搜索在本机执行。没有模型时仍可使用文件名和已提取文本的关键词搜索。删除索引配置不删除源文件。

关键词检索要求输入的各个词共同出现，例如 `北京 budget` 查找同时含有“北京”和 `budget` 的片段。语义检索使用真实模型向量，按 cosine 排序、按文件聚合，再与关键词结果执行 RRF 融合。结果不设置最低相关度接受线，低相似度文件也可能出现。模型校验、768 维向量与数值回归继续保留，恢复语义候选不改变模型指纹、数据库 schema 或现有用户索引。

首版范围包含文档、PDF、图片与 OCR、音频、视频，以及可选的源码和 Notebook 搜索。扫描与提取使用统一格式规则，CLI 和服务搜索上限均为 500。具体解码器和格式边界见 [依赖与格式说明](docs/dependencies.md)。

## 获取与构建

应用运行不需要 Python。开发脚本需要 Python 3.10+、CMake 3.28+、Ninja、C++20 编译器及平台 SDK。Windows 使用 MSVC 2022，macOS 使用 Xcode Command Line Tools，Linux 使用 GCC 或 Clang。Qt 固定为 6.12.0，配套 Qt PDF 模块固定为 6.140.0。模型、Qt、llama.cpp 与其他依赖通过锁定来源和哈希验证。

使用统一 `--variant` 选择构建，构建、stage、包和报告均在源码树外，且按变体与配置隔离。本机示例使用已安装的 Qt，路径可自行替换：

```powershell
Set-Location C:\Dev\github-repos\QCopilots-Search
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
./qcs-dev.ps1 doctor --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 check --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
./qcs-dev.ps1 run --variant $variant --build-root $buildRoot --qt-root $qtRoot
```

macOS / Linux 使用相同参数：

```bash
build_root="$(dirname "$PWD")/qcs-build"
qt_root="$(dirname "$PWD")/Qt-SDK"
variant=linux-x64-cpu
bash ./qcs-dev.sh bootstrap --variant "$variant" --build-root "$build_root" --qt-root "$qt_root"
bash ./qcs-dev.sh check --variant "$variant" --build-root "$build_root" --qt-root "$qt_root"
```

macOS 选择 `macos-arm64-metal`。`bootstrap` 用于未安装 SDK 的环境。脚本按自身位置定位源码，不依赖调用者当前目录，完整参数见 [开发指南](docs/development.md)。

## 测试、安装包与发布

`test` 默认执行当前平台全部保留的功能测试，真实模型缺失或校验失败会明确失败。`check` 顺序完成环境检查、配置、构建、模型准备、完整测试、安装、打包和最终包验证。局部排查可以使用 `test --test-label`，筛选结果不作为完整验收。

CI 使用一套流程构建 11 个变体，产生 21 个用户文件。Windows 每个变体提供 EXE 与便携 ZIP，Linux 每个变体提供 DEB 与便携 TGZ，macOS 提供 DMG。GPU 变体默认自动使用可用后端，不可用时回退 CPU 并报告原因。没有 GPU 的托管 runner 验证 CPU 回退及部署完整性，不能声称已经完成 GPU 实机验证。

CI 源码检出目录为 `qcs-src`。包验证的解包、安装、搬移、profile 及 Windows CU 运行根使用英文且无空格的路径，中文查询和样本文件继续验证搜索能力。每个 CI artifact 仅包含对应变体已验证的用户安装包和便携包。构建、测试与失败诊断保留在 GitHub 原生日志中。发布准备可按 run ID、attempt 和候选提交自动下载 CI 成品，验证来源与字节后生成本地冻结清单。源码材料独立生成，符号保留独立生成能力，草稿上传必须显式执行。详细矩阵和协议见 [发布指南](docs/releasing.md) 与 [发布命令](docs/release-command.md)。

## 文档

- [开发指南](docs/development.md) 与 [脚本审查清单](docs/script-audit.md)
- [架构与进程契约](docs/architecture.md)
- [依赖、模型与格式边界](docs/dependencies.md)
- [测试指南](docs/testing.md) 与 [需求与测试矩阵](docs/requirements-test-matrix.md)
- [Windows 用户操作验收](docs/windows-user-acceptance.md)
- [发布指南](docs/releasing.md) 与 [对应源码材料](docs/source-materials.md)
- [贡献说明](CONTRIBUTING.md)

## 许可证与致谢

本项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 xbyang。DigUp 的应用设计、推理契约、图像预处理和参考向量为本项目提供依据，相关改编保留原作者 Abdur Rahim 的 MIT 归属。第三方库、Qt 模块、模型及 OCR 数据保留各自许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。
