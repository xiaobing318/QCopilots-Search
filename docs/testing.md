# 测试指南

测试验证真实行为，结果区分通过、失败、未运行和平台能力不适用。存在测试源码、编译成功或已触发 CI 都不等于执行通过。

## 运行测试

先按[开发指南](development.md)配置并编译。以下命令使用已有 Qt 和外置构建根：

```powershell
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
$modelDir = Join-Path $buildRoot 'models'
./qcs-dev.ps1 models --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcs-dev.ps1 test --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
```

macOS / Linux 使用 `bash ./qcs-dev.sh` 和对应变体。`test` 默认执行全部注册用例。缺少必需模型、OCR 数据或夹具会失败，不能标记为跳过后通过。离线环境可通过 `qcs-cli models import` 导入准确模型，导入仍验证大小与 SHA-256。

`test --test-label unit` 或 `test --test-label real-model` 用于局部排查，报告记录实际选择和未运行项。空选择失败，筛选结果不能代表完整测试。`qcs-release` 与 `qcs-debug` CTest preset 默认执行全部注册用例，`ctest --test-dir <构建目录> -N` 可列出清单。

完整开发与包检查使用 `check`，允许未提交修改，自动准备模型并执行测试、安装、打包和验证。`--keep-failed-tests` 保留失败用例产物。JUnit、逐用例结果、选择清单和日志在当前变体与配置的外置报告目录，CTest 原始输出在构建树 `Testing/Temporary`。

## 隔离要求

有可写状态的测试使用唯一 `QTemporaryDir` 或等价临时根。`QCOPILOTS_SEARCH_TEST_ROOT` 可覆盖系统临时位置，创建失败不能回退到源码树或用户 profile。每次调用使用独立 cwd、profile、数据库和日志，子进程显式接收相同目录。只读夹具和校验后的模型可以共享。

正式应用同时运行时，测试不能连接、修改或终止它的服务，也不能读取或修改真实登录启动设置。结束前停止本次进程、关闭文件与数据库，再核对归属和路径边界后清理。创建或清理失败必须影响退出码。

功能用例继续覆盖中文、空格、emoji、长路径及任意 cwd。CI 检出目录和受控安装、解包、搬移及 profile 根使用英文且无空格的路径，中文与空格样本文件保留在这些目录中。

## 验证范围

| 范围 | 核心行为 | 位置 |
| --- | --- | --- |
| 核心与路径 | `QCS_HOME` 优先级、无效路径、默认目录、模型校验和目录规则 | `tests/core` |
| 提取 | Office/PDF/OCR/图片/媒体/代码、定位、编码、损坏输入及资源释放 | `tests/extraction` |
| 存储 | 过滤、分页、快照、事务、取消、索引更新与故障恢复 | `tests/storage` |
| 服务与进程 | IPC、实例隔离、调度、worker 生命周期与恢复 | `tests/service`、`tests/processes` |
| GUI | 搜索、预览、快速窗口、单实例、登录目标切换与失败处理 | `tests/gui` |
| 推理 | 黄金向量、768 维、有限值、归一化、取消、CPU 后备和受控后端加载 | `tests/runtime` |
| 多模态链 | 真实提取、编码、存储、重载与正式检索 | `tests/semantic` |
| 开发与发布工具 | 外置构建、完整 check、矩阵、包白名单、CI attempt、摘要、源码包和草稿恢复 | `tests/tooling` |
| 最终包 | 重定位、依赖、许可证、图标、签名和安装卸载 | 包验证器与安装器测试 |

黄金向量验证锁定推理契约，不作为检索相关度接受线。真实多模态测试对小型素材执行生产链路，检查来源文件和有效定位字段，不通过关键词兜底或伪造 embedding 达成成功。媒体位于 `tests/acceptance/fixtures`，其清单保留来源、许可、大小和哈希。

第三方完整性测试对锁定的原始源码做逐文件校验，覆盖重复配置与构建后源码不变。外部源码覆盖也必须符合锁定来源，不能关闭校验或放宽数值阈值来消除回归。

## CI、GPU 与最终包

原生 CI 读取共同变体清单，完成 11 个变体的构建、全部适用测试、安装、打包和最终包验证。每个变体 artifact 只包含已验证的用户包，共 21 个用户文件。失败诊断保留在 GitHub 原生日志，源码材料与符号独立生成。

GPU 包在无设备的 runner 上验证模块、依赖、完整功能链和 CPU 回退。报告写明“GPU 硬件未实测”。有设备时使用严格 GPU 模式验证实际执行，不能把加载库成功或 CPU 回退当作 GPU 推理通过。

最终包在独立工作目录和 profile 中运行，验证搬移后启动，排除开发 SDK 与 PATH 补齐缺库。Windows 检查安装卸载与便携 ZIP，Linux 检查 DEB 和 TGZ，macOS 检查 DMG、bundle 和签名。卸载应保留用户数据。包内 worker 执行真实文本、图片和音频编码，包内服务执行语义搜索，关键词限定检查仅用于排查。

## GUI 检查清单

对准备交付的实际包记录版本、提交、包 SHA-256、平台和结果。使用独立测试目录及可公开的小型文件，不需要专用操作记录器。

- 启动主窗口，检查标题、语言、窗口缩放、托盘、退出与再次启动。同一 profile 激活原窗口，不同 profile 相互隔离。
- 添加普通和代码目录，修改排除规则，暂停与恢复索引，确认删除配置不删除源文件。
- 执行文件名、正文、OCR 和真实语义查询，检查类型和目录筛选、空查询浏览、分页、快速窗口及预览定位。
- 检查模型下载或离线导入、校验失败、取消与恢复。验证外部查看器缺失时的明确错误。
- 使用自定义 `QCS_HOME` 检查目录创建、子进程路径与登录启动目标。切换目录后，普通启动使用新目录，登录目标仅在明确操作后改变。
- 启用、关闭和切换登录启动，观察系统真实状态。macOS 还需检查等待批准和实际重新登录，Linux 检查所用桌面的授权和启动行为。
- Windows 检查资源管理器、四个 EXE 的任务管理器图标、窗口、托盘、安装器，以及固定到任务栏后退出并重新启动。CLI 在 Windows Terminal 中的任务栏图标由终端宿主管理。
- macOS 检查 Finder 与 Dock 图标，Linux 检查应用菜单与 desktop entry 图标。实际显示不能只由资源文件存在推断。

本机 Windows 测试不能代替 macOS/Linux 桌面登录、最低系统版本、全部 GPU 或所有 RAW 相机型号验证。未执行项在交付结果中明确列出。
