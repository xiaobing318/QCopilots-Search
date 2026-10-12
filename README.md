# QCopilots Search

QCopilots Search 是使用 C++20、Qt Widgets 和 CMake 开发的本地多模态搜索工具，应用场景和推理契约参考 [DigUp](https://github.com/ARahim3/DigUp)。它把文件名、正文、OCR 和语义结果放在同一个搜索入口中，面向 Windows、macOS 和 Linux。

项目正在完成首版集成。功能代码、测试是否存在与验收是否通过是不同的状态。请以 [实施状态](docs/implementation-status.md)、[需求与测试矩阵](docs/requirements-test-matrix.md) 和对应提交的 CI 报告判断可用范围。此仓库没有把未经执行的跨平台验收标记为通过。

## 应用与数据

GUI 为 `qcopilots-search`，本地服务为 `qcopilots-search-service`，提取和推理工作进程为 `qcopilots-search-worker`，命令行入口为 `qcopilots-search-cli`。所有组件使用相同的运行目录配置，服务统一管理索引写入和查询。

搜索只处理用户选择的目录。模型保存在本地，下载模型和检查更新需要网络，文件提取与搜索在本机执行。没有模型时仍可使用文件名和已提取文本的关键词搜索。删除索引配置不应删除源文件。

关键词检索默认要求输入的各个词共同出现，例如 `北京 budget` 会查找同时含有“北京”和 `budget` 的片段。加载模型后，自然语言查询还会进行语义检索，并与满足完整关键词条件的结果融合，支持没有文字说明的图片和音频。

首版范围包含 TXT、Markdown、HTML、RTF、DOCX、ODT、旧 DOC、PDF、图片、音频、视频，以及可选的源码和 Notebook 搜索。不同解码器覆盖的具体格式、损坏输入、扫描 PDF、HEIC 和 RAW 必须以 [格式与依赖说明](docs/dependencies.md) 中的测试证据为准。

## 获取与构建

运行程序不需要 Python。开发脚本使用 Python 3.10+，应用与测试代码仍为 C/C++。开发环境还需 CMake 3.28+、Ninja、C++20 编译器及平台 SDK。Windows 使用 MSVC 2022，macOS 使用 Xcode Command Line Tools，Linux 使用 GCC 或 Clang。

Qt 固定为 6.12.0，Qt PDF 为与该 Qt 版本配套的独立 6.140.0 模块。Bootstrap 从锁定的官方归档安装 SDK，并检查 SHA-256。依赖、构建、测试数据和安装暂存目录均放在源码目录之外。

Windows PowerShell 示例，路径可以替换为其他盘符或包含中文、空格的目录：

```powershell
git clone https://github.com/xiaobing318/QCopilots-search.git
Set-Location QCopilots-search
$buildRoot = Join-Path (Split-Path $PWD.Path -Parent) 'QCopilots-search-build'
$qtRoot = Join-Path (Split-Path $PWD.Path -Parent) 'Qt-SDK'
./qcopilots-search-dev.ps1 bootstrap --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 doctor --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 configure --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 build --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 run --build-root $buildRoot --qt-root $qtRoot
```

macOS / Linux 的入口参数相同：

```bash
git clone https://github.com/xiaobing318/QCopilots-search.git
cd QCopilots-search
build_root="$(dirname "$PWD")/QCopilots-search-build"
qt_root="$(dirname "$PWD")/Qt-SDK"
bash ./qcopilots-search-dev.sh bootstrap --build-root "$build_root" --qt-root "$qt_root"
bash ./qcopilots-search-dev.sh doctor --build-root "$build_root" --qt-root "$qt_root"
bash ./qcopilots-search-dev.sh configure --build-root "$build_root" --qt-root "$qt_root"
bash ./qcopilots-search-dev.sh build --build-root "$build_root" --qt-root "$qt_root"
bash ./qcopilots-search-dev.sh run --build-root "$build_root" --qt-root "$qt_root"
```

从修改代码到测试、安装和打包的完整流程见 [开发指南](docs/development.md)。脚本按自身位置定位源码，不依赖调用者当前工作目录。

## 测试与安装包

自动化测试使用独立临时配置，不连接正在使用的正式应用实例。完整测试需要已校验的真实模型缓存：

```powershell
$modelDir = Join-Path $buildRoot 'models'
./qcopilots-search-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcopilots-search-dev.ps1 install --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 package --build-root $buildRoot --qt-root $qtRoot --generator ZIP
./qcopilots-search-dev.ps1 verify-package --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
```

模型获取、测试分层和保留失败证据见 [测试指南](docs/testing.md)。模型缺失或校验失败会使必需的真实模型验收失败。黄金向量测试验证数值一致性，独立的语义质量集验证检索效果，合成百万向量测试验证容量，三者分别报告。

发行包目标为 Windows ZIP / NSIS、macOS 分架构 APP / DMG、Linux TGZ / DEB。实际包级测试、许可证材料、签名状态与发布门槛见 [发布指南](docs/releasing.md)。CI 生成测试包和报告，不自动公开发布。

## 文档

- [开发指南](docs/development.md)
- [架构与进程契约](docs/architecture.md)
- [依赖、模型与格式边界](docs/dependencies.md)
- [测试指南](docs/testing.md) 和 [需求与测试矩阵](docs/requirements-test-matrix.md)
- [Windows 用户操作验收](docs/windows-user-acceptance.md)
- [容量与性能验收](docs/capacity.md)
- [发布指南](docs/releasing.md)
- [对应源码材料](docs/source-materials.md)
- [贡献说明](CONTRIBUTING.md)

## 许可证与致谢

本项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 xbyang。DigUp 的应用设计、推理契约、图像预处理和参考向量为本项目提供了依据，相关改编保留原作者 Abdur Rahim 的 MIT 归属。第三方库、Qt 模块、模型及 OCR 数据保留各自许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。
