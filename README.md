# QCopilots Search

QCopilots Search 是基于 C++20/Qt6 的本地多模态搜索工具，面向 Windows/Linux/macOS 三种平台，该仓库参考 [DigUp](https://github.com/ARahim3/DigUp) 仓库实现的，区别在于当前仓库可以实现跨平台。搜索只处理用户选定的目录，提取和推理在本机执行。模型下载和更新检查需要网络，没有模型时仍可使用文件名与已提取正文的关键词搜索，删除索引配置不会删除源文件。

## 使用与数据

交互界面为 `qcs-gui` 可执行文件，本地服务为 `qcs-service` 可执行文件，提取与推理进程为 `qcs-worker` 可执行文件，命令行入口为 `qcs-cli` 可执行文件，同一数据目录重复启动会激活已有交互界面，不同目录可并行运行。默认数据基目录是 Windows 的 `%LOCALAPPDATA%\qcs` 目录、macOS 的 `~/Library/Application Support/qcs` 目录和 Linux 的 `$XDG_DATA_HOME/qcs` 目录，在 Linux 未设置该变量时使用 `~/.local/share/qcs` 目录。可以通过环境变量设置绝对路径 `QCS_HOME` 可以更换基目录，例如：

```powershell
$env:QCS_HOME = 'D:\QCS'
```

新版不自动读取或迁移旧版数据，首次启动需重新设置搜索目录和建立索引。应用只支持 EmbeddingGemma 2，可在独立模型管理对话框中下载或离线导入，下载或导入成功后自动重新加载。旧 Gemma 文件可导入新版，源文件保持不变。登录自动启动仅在明确启用后接管产品入口，更换环境变量不会静默改变登录目标，详细说明见[配置与数据目录](docs/configuration.md)。

支持文档、PDF、图片与 OCR、音频、视频，以及独立的代码与 Notebook 搜索。关键词要求输入词共同出现，语义结果按 cosine 排序、按文件聚合后与关键词结果执行 RRF 融合，不设置最低相关度接受线。具体格式与后端边界见[依赖说明](docs/dependencies.md)。

## 开发与构建

应用运行不需要 Python 解释器，开发需要 Python 3.10+、CMake 3.28+、Ninja、Git、C++20 Compiler 与 Qt 6.12.0 框架，配套 Qt PDF 为 6.140.0 版本，平台 Windows 使用 MSVC 2022 工具链，平台 macOS 使用 Xcode Command Line Tools 工具链，平台 Linux 使用 GCC 或 Clang 工具链。所有构建、依赖、模型、测试和打包产物位于源码树外，以下 Windows 示例使用已有 Qt 框架，路径可替换：

```powershell
Set-Location C:\Dev\github-repos\QCopilots-Search
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
./qcs-dev.ps1 doctor --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 configure --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 build --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
./qcs-dev.ps1 run --variant $variant --build-root $buildRoot --qt-root $qtRoot
```

平台 macOS / Linux 使用 `bash ./qcs-dev.sh` 和相同参数，变体分别为 `macos-arm64-metal` 或 `linux-x64-cpu` 配置，未安装 Qt 时先执行 `bootstrap` 脚本，脚本按自身路径定位源码，不依赖调用者的工作目录。

## 测试、安装与发布

阶段 `check` 允许未提交修改，自动执行环境检查、配置、编译、模型准备、全部测试、安装、打包、最终包验证与适用的安装器验证：

```powershell
./qcs-dev.ps1 check --variant $variant --build-root $buildRoot --qt-root $qtRoot --jobs 4
```

日常可单独执行 `test`、`install`、`package` 和 `verify-package` 阶段，安装与打包自动部署依赖、运行资源、许可证和应用图标，无需手工拷贝，真实模型缺失或校验失败会明确报错，标签筛选只用于局部排查。

多 GPU 索引按文件并行，每张当前后端支持的兼容 GPU 使用独立 worker 进程，自动模式在全部 GPU 不可用时回退 CPU 硬件，严格 GPU 模式报告失败。缺少设备的 runner 不代表 GPU 实机验证通过，双 AMD 或双 NVIDIA GPU 的实际加速尚未实测。正式发布使用 `release-prepare` 收集准确的成功 CI run 与尝试，验证摘要并生成源码材料、校验和与发布清单。`release-upload-draft` 上传或续传草稿，维护者显式运行 `release-publish` 才会公开发布。也可在 GitHub Actions 手动运行发布工作流，先创建草稿，再使用原 `freeze_run_id` 恢复同一份材料并公开发布。Release 正文由脚本从候选配置和冻结清单确定性生成，历史版本正文使用 `release-update-notes` 单独更新。完整流程见[安装包与发布](docs/releasing.md)。

## 文档

- [开发指南](docs/development.md)：环境、增量编译、调试、安装和常见错误
- [架构说明](docs/architecture.md)：模块、进程、数据流和平台能力
- [配置与数据目录](docs/configuration.md)：`QCS_HOME`、模型、备份和登录启动
- [依赖说明](docs/dependencies.md)：锁定版本、变体、模型与格式边界
- [测试指南](docs/testing.md)：自动测试、CI、包验证和 GUI 清单
- [发布指南](docs/releasing.md)：发行材料、校验和、草稿上传与恢复
- [对应源码材料](docs/source-materials.md)：收集范围、许可证与重建
- [贡献说明](CONTRIBUTING.md)

## 许可证与致谢

本项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 xbyang。DigUp 的应用设计、推理契约、图像预处理和参考向量为本项目提供依据，相关改编保留原作者 Abdur Rahim 的 MIT 归属，第三方源码保持上游原样，Qt 模块、运行库、模型与 OCR 数据保留各自许可，详见[第三方声明](THIRD_PARTY_NOTICES.md)。
