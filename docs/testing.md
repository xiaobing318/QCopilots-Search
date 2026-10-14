# 测试指南

默认测试执行当前平台全部保留的功能用例。测试程序存在、manifest 校验成功、筛选用例通过与真实应用链通过是不同结论，报告必须记录实际执行范围。

## 独立运行目录

有可写状态的测试创建唯一 `QTemporaryDir` 或等价的原子临时根，并检查创建与清理结果。`QCOPILOTS_SEARCH_TEST_ROOT` 可覆盖系统临时根，创建失败不能回退到源码或用户 profile。

每次 CTest 调用拥有独立 cwd、profile、数据库、日志和工作文件，程序路径来自实际 CMake target。只读夹具从源码读取，模型只能在大小和 SHA-256 验证后共享。正式应用同时运行时，测试不得连接、修改或终止其服务。

路径回归覆盖中文、空格、长路径、任意 cwd、不同 checkout 和安装位置。只读源码与双 checkout 检查使用实际权限和独立输出目录。清理先停止本次子进程、关闭文件，再核对目录边界和归属，失败必须影响退出状态。

## 模型与执行入口

`check` 自动准备模型。单独 `test` 前可运行 `models`，随后执行完整测试：

```powershell
$variant = 'windows-x64-cpu'
$modelDir = Join-Path $buildRoot 'models'
./qcs-dev.ps1 models --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcs-dev.ps1 test --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcs-dev.ps1 check --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
```

离线环境可用 `qcs-cli models import FILE_OR_DIRECTORY`，导入与下载均核对锁定字节数及 SHA-256。缺模型、OCR 数据或必需夹具是失败，不能记为跳过后通过。完整模型契约见 [依赖说明](dependencies.md)。

`test --test-label unit` 或 `test --test-label real-model` 用于局部排查。标签筛选记录实际选择和未运行项，空选择失败，筛选结果不能成为完整验收。`qcs-release` 与 `qcs-debug` CTest preset 默认执行全部已注册用例。需要列出清单时执行 `ctest --test-dir <当前变体构建目录> -N`。

`--keep-failed-tests` 保留失败用例的独立数据。JUnit、逐用例明细、选择清单及日志位于当前变体与配置的外置报告目录，CTest 原始日志位于构建树 `Testing/Temporary`。`check` 另外绑定阶段命令、来源、退出状态和实际用户包哈希，后续未执行阶段不能被补写为成功。

## 保留的验证范围

| 范围 | 实际行为 | 主要入口 |
| --- | --- | --- |
| 核心与提取 | 配置、模型下载校验、格式扫描路由、Office/PDF/OCR/媒体解码、定位与损坏输入 | `tests/core`、`tests/extraction` |
| 存储 | 小规模增删改、过滤、分页、取消、SQL/ANN 快照、事务和故障恢复 | `tests/storage` |
| 服务与进程 | IPC、超时、取消、独立 profile、任务调度、worker 生命周期、500/501 边界 | `tests/service`、`tests/processes` |
| GUI | 浏览、搜索、预览、快捷窗完整上下文、同/不同 profile 单实例 | `tests/gui` |
| 真实推理数值 | 锁定参考向量、768 维、有限值、归一化、非恒定输出、损坏输入及取消恢复 | `tests/runtime` |
| 真实多模态链 | 文档、PDF、代码、图片、OCR、语音、环境音和视频的提取、编码、存储、重载、正式检索 | `tests/semantic` |
| 工具与发布 | 外置路径、测试清单、完整 check、11 变体矩阵、包白名单、attempt 选择、摘要与字节稳定性 | `tools`、`.github/scripts` |
| 部署与安装 | 最终包重定位、依赖闭包、许可证、签名、安装卸载与用户数据保留 | 包验证器及平台安装器测试 |

黄金向量的 cosine 一致性检查用于验证数值实现，并非检索相关度接受线。多模态功能测试使用固定小集合，每个输入在隔离的单文件范围内关闭关键词兜底，以真实 query embedding 调用正式搜索。测试要求返回来源文件及有效页码、行号或时间字段，不要求特定排名、召回率或无答案比例。

静态媒体集中在 `tests/acceptance/fixtures`，清单包含来源、许可和哈希。功能测试与 Windows 场景共同使用保留媒体，生成输入仍通过生产提取和真实推理，不使用伪造 embedding。`--validate-only` 只能验证夹具完整性，不能代替真实模型执行。

## GPU 与最终包

默认后端策略为自动选择，GPU 可用时启用文本模型和多模态 projector，不可用时明确回退 CPU。诊断报告构建变体、实际后端、设备与回退原因。严格 GPU 模式要求真实设备执行，CPU 回退不能冒充成功。

GPU 包在无设备的托管 runner 上执行全部适用功能测试和 CPU 回退验包，同时验证模块、依赖和受控加载目录。记录“GPU 硬件未实测”，不能把编译成功或库存在写成 GPU 运行通过。有设备时再执行严格 GPU 测试。

最终包从中文和空格路径、独立 cwd、独立 profile 启动，排除开发 SDK 对依赖加载的补齐。Windows 验证安装卸载和便携包移动，Linux 验证 DEB 安装卸载及便携 TGZ，macOS 验证 DMG、bundle 布局与 ad-hoc 签名。卸载保留用户模型、索引和配置。

## 原生 CI 与用户操作

PR、主分支推送与手动触发使用相同完整流程。每个变体依次准备工具链、构建、执行全部保留测试、安装、打包、验证最终包。汇总核对全部 11 个变体的状态与 21 个用户文件，不能以其他变体的成功覆盖失败。

每个成功变体上传一个仅含用户包的 artifact，文件名包含版本、变体和候选身份，artifact 区分重跑 attempt。失败诊断保留在 GitHub 原生日志，按需使用 API 获取。报告、截图、符号、源码和校验和不上传到 CI Artifacts。

Windows 最终 CPU 包另执行 [用户操作场景](windows-user-acceptance.md)。QtTest、CLI 或生成截图不能替代真实窗口操作。最终结果区分通过、失败、未运行和平台能力不适用，要求同一不可变源码与包身份。
