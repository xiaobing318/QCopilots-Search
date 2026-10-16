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
| 服务与进程 | IPC、实例隔离、多 GPU 文件调度、worker 生命周期与故障恢复 | `tests/service`、`tests/processes` |
| GUI | 搜索、预览、快速窗口、各 GPU 状态、单实例、登录目标切换与失败处理 | `tests/gui` |
| 推理 | 黄金向量、768 维、有限值、归一化、取消、CPU 后备和受控后端加载 | `tests/runtime` |
| 多模态链 | 真实提取、编码、存储、重载与正式检索 | `tests/semantic` |
| 开发与发布工具 | 外置构建、完整 check、矩阵、包白名单、CI attempt、摘要、源码包、草稿恢复和幂等公开发布 | `tests/tooling` |
| 最终包 | 重定位、依赖、许可证、图标、签名和安装卸载 | 包验证器与安装器测试 |

黄金向量验证锁定推理契约，不作为检索相关度接受线。真实多模态测试对小型素材执行生产链路，检查来源文件和有效定位字段，不通过关键词兜底或伪造 embedding 达成成功。媒体位于 `tests/acceptance/fixtures`，其清单保留来源、许可、大小和哈希。

第三方完整性测试对锁定的原始源码做逐文件校验，覆盖重复配置与构建后源码不变。外部源码覆盖也必须符合锁定来源，不能关闭校验或放宽数值阈值来消除回归。

## CI、GPU 与最终包

原生 CI 读取共同变体清单，完成 11 个变体的构建、全部适用测试、安装、打包和最终包验证。安装器与便携包分别作为单文件 artifact 上传，直接下载原始文件，每个成功选择结果共 21 个用户包。包名采用 `qcs-<版本>-<变体>.<扩展名>`，完整提交和 attempt 保留在包内身份资料中。ROCm 安装器触及 NSIS 限制时提供独立 `-setup.zip`，同一 run 的历史格式备选不能混入当前发布集合。失败诊断保留在 GitHub 原生日志，源码材料与符号独立生成。

GPU 包在无设备的 runner 上验证模块、依赖、完整功能链和 CPU 回退。报告写明“GPU 硬件未实测”。有设备时使用严格 GPU 模式验证实际执行，不能把加载库成功或 CPU 回退当作 GPU 推理通过。

最终包在独立工作目录和 profile 中运行，验证搬移后启动，排除开发 SDK 与 PATH 补齐缺库。Windows 检查安装卸载与便携 ZIP，Linux 检查 DEB 和 TGZ，macOS 检查 DMG、bundle 和签名。卸载应保留用户数据。包内 worker 执行真实文本、图片和音频编码，包内服务执行语义搜索，关键词限定检查仅用于排查。

## 真实多 GPU 检查

`tools/qcs-check-gpus.py` 复用已安装的 `qcs-worker`、`qcs-service` 和 `qcs-cli`，需要至少两张当前包后端支持的 GPU 以及完整校验过的模型。Windows 示例使用可用 Python 解释器，替换实际安装目录和外置工作根：

```powershell
& $env:QCOPILOTS_SEARCH_PYTHON -B ./tools/qcs-check-gpus.py `
  --bin-dir 'D:\qcs-installed\bin' `
  --model-dir 'D:\qcs-models' `
  --work-root 'D:\qcs-hardware-checks' `
  --files 64 --paragraphs 32
```

macOS/Linux 用 `python3 -B` 调用同一脚本。macOS 的 `--bin-dir` 指向应用包内含三个程序的 `Contents/MacOS` 目录。脚本拒绝把输出写入源码树，自动在外置根创建唯一工作目录，使用独立 profile 和新建文本语料，只读共享模型。日志、逐次状态和 `report.json` 保留供复核，结束时停止本次服务及其工作进程。不要把正式用户 profile 传入测试。

脚本先枚举设备，再用严格 GPU 模式对全部兼容卡运行索引，随后对每一张卡运行相同语料。每张选择的 GPU 都必须完成索引任务，其诊断必须确认设备身份、显式绑定、GPU 计算自检、文本预热及模型加载。还检查所有文件可检索、真实语义查询成功，以及单卡和多卡的文件与片段数一致。设备不可用、某张卡没有完成任务、CPU 回退、索引失败或语料未完成都会失败，不把这些情况标记为硬件通过。较慢的卡来不及领取工作时，可增加 `--files` 或 `--paragraphs`。

报告记录各卡完成批次数、观察到的并行忙碌槽位、逐文件状态、程序摘要及端到端耗时。每次使用新进程与空索引，计时包含模型初始化，操作系统文件缓存不清空，因此单卡与多卡耗时比例只是本次观察，不能当作稳态吞吐或线性加速保证。此入口使用文本语料，真实图片和音频数值回归继续由正常测试集执行。`--skip-single` 只执行多卡正确性验收，不产生单卡比较。

`qcs-tests-gpu-check` 仅验证验收脚本的拒绝条件和目录隔离，合成诊断不代表硬件执行。当前开发机仅有 VMware 虚拟显示设备，本轮未实测双 AMD 或双 NVIDIA GPU 的实际吞吐，正式发布说明应保留这一限制。

## GUI 检查清单

对准备交付的实际包记录版本、提交、包 SHA-256、平台和结果。使用独立测试目录及可公开的小型文件，不需要专用操作记录器。

- 启动主窗口，检查标题、语言、窗口缩放、托盘、退出与再次启动。同一 profile 激活原窗口，不同 profile 相互隔离。
- 添加普通和代码目录，修改排除规则，暂停与恢复索引，确认删除配置不删除源文件。
- 执行文件名、正文、OCR 和真实语义查询，检查类型和目录筛选、空查询浏览、分页、快速窗口及预览定位。
- 检查模型下载或离线导入、校验失败、取消与恢复。验证外部查看器缺失时的明确错误。
- 有真实 GPU 时检查索引设备数量、忙碌状态、各卡任务计数和故障原因。服务断开后不应继续显示旧的活动任务，CPU 后备与严格 GPU 失败必须明确区分。
- 使用自定义 `QCS_HOME` 检查目录创建、子进程路径与登录启动目标。切换目录后，普通启动使用新目录，登录目标仅在明确操作后改变。
- 启用、关闭和切换登录启动，观察系统真实状态。macOS 还需检查等待批准和实际重新登录，Linux 检查所用桌面的授权和启动行为。
- Windows 检查资源管理器、四个 EXE 的任务管理器图标、窗口、托盘、安装器，以及固定到任务栏后退出并重新启动。CLI 在 Windows Terminal 中的任务栏图标由终端宿主管理。
- macOS 检查 Finder 与 Dock 图标，Linux 检查应用菜单与 desktop entry 图标。实际显示不能只由资源文件存在推断。

本机 Windows 测试不能代替 macOS/Linux 桌面登录、最低系统版本、全部 GPU 或所有 RAW 相机型号验证。未执行项在交付结果中明确列出。
