# 需求与测试矩阵

本文追踪需求到实现入口和验证入口。状态“已建立用例”表示源码中存在对应测试，不表示编译或执行通过。执行结果统一由对应提交的 CTest、CI 和验收报告提供，当前发布结论见 [实施状态](implementation-status.md)。

| 需求 | 实现入口 | 验证入口或关键用例 | 证据状态 |
| --- | --- | --- | --- |
| 自有组件统一前缀 | 根 CMake、各模块、开发脚本 | CMake 测试名称检查、安装布局清单 | 已建立检查，待集成结果 |
| 外置构建与任意源码路径 | 开发脚本、CMake presets | CI 中文空格 checkout、CMake 拒绝源码内构建 | 已建立流程，待原生 CI |
| 显式 profile 与 model 路径 | `core/runtime_paths.*` | `tests-core`: explicitRuntimePaths、environmentOverrides | 已建立用例 |
| 中文、空格、长路径 | core、提取、runtime | longAndUnicodePaths、unicodeAndSpacedImagePathMatchesReference | 已建立用例 |
| 不依赖工作目录 | RuntimePaths、脚本自定位 | workingDirectoryIndependence、CI 独立工作目录 | 已建立用例 |
| 唯一临时根与失败不回退 | 测试支持库 | temporaryRootsUnique、customTemporaryRoot、temporaryFailureDoesNotFallback | 已建立用例 |
| 清理边界与句柄释放 | 测试支持库、子进程封装 | cleanupBoundaries、cleanupRejectsMovedRoot、进程退出测试 | 已建立用例 |
| 配置读取与原子保存 | `core/app_settings.*` | settingsRoundTrip、settingsRejectCorruption、settingsAtomicFailure | 已建立用例 |
| 模型固定版本与校验 | `core/model_files.*` | pinnedManifest、corruptedModelCannotPublish、CLI models verify | 已建立用例 |
| 下载续传、取消和重试 | 模型下载器 | resumesRanges、cancelKeepsPartialAndUnlocks、invalidCompletePartialRestarts | 已建立用例 |
| 文本、HTML、RTF、Office | `extraction/document_extractor.*` | extraction-document 全套 | 已建立用例 |
| HTML 外部资源禁止加载 | 文档提取 | htmlNeverReadsLocalResources | 已建立用例 |
| 长文件续读 | 提取游标、worker | longDocumentResumesWithoutLosingTail、persistentCursorAndIsolatedProfiles | 已建立用例 |
| 旧 DOC | legacy_doc_extractor | extraction-legacy_doc | 已建立入口，格式结果待报告 |
| 图片、PDF、OCR | image_extractor | pdfPaginationAndPosition、scannedPdfRendersImage、realOcrWithUnicodeDataDirectory | 已建立用例 |
| HEIC、RAW 格式范围 | libheif、LibRaw | 真实格式探针与包级运行 | 待明确夹具与执行证据 |
| 音频窗口、环境声、视频时间 | media_extractor、runtime | extraction-media、reference-vectors、独立语义集 | 数值和窗口用例已建立，真实语义待报告 |
| 可选代码、Git ignore、未跟踪源码 | file_catalog、document_extractor | gitIgnoreKeepsTrackedAndUntrackedSource、codeLocationsAndNotebookSources | 已建立用例 |
| 九条黄金向量 cosine ≥ 0.999 | runtime | tests-reference-vectors | 已建立用例，四组合结果待 CI |
| 中文、截断、损坏媒体 | runtime | chineseInputsAreNormalizedAndDistinct、truncationPreservesThePrefix、malformedAndMismatchedMediaAreRejected | 已建立用例 |
| 中文关键词与定位 | storage/text_index | tests-index_repository 的 keywordSearch、locationAndFileAggregation | 已建立用例 |
| 无模型关键词搜索 | worker、storage | unicodePathsAndKeywordOnlyWithoutModels | 已建立用例 |
| 文件增删改与模型版本 | IndexRepository | replacementAndRemovalRetireOldSegments、pipelineChangeInvalidatesVectorsButPreservesKeywordSearch | 已建立用例 |
| 过滤参与召回 | IndexRepository、VectorIndex | semanticSearchAppliesFiltersBeforeLimit、allowedIdsRestrictCandidates | 已建立用例 |
| ANN 快照、损坏恢复 | VectorIndex | oldSnapshotSurvivesRebuild、corruptManifestPreservesLiveSnapshot、corruptShardChecksumPreservesLiveSnapshot | 已建立用例 |
| 并发读取与分片重建 | storage | concurrentReadersSeeCommittedReplacements、concurrentReadersKeepOldSnapshotDuringRebuild | 已建立用例 |
| IPC 分帧、版本、超时和取消 | service、client、worker | service client 用例、worker invalidFramesDoNotPoisonNextRequest、oversizedFrameIsTerminal | 已建立用例，服务整体集成待报告 |
| 同配置目录唯一服务与身份访问 | 服务锁、本地协议身份 | 服务集成及多实例测试 | 待集成测试证据 |
| Worker 崩溃及迟到结果 | 服务任务调度 | 进程故障注入与版本检查 | 待集成测试证据 |
| 首次引导、设置与模型界面 | Qt Widgets app | GUI QtTest、CU-01 至 CU-03 | 待 GUI 与 Computer Use 结果 |
| 搜索、过滤、导航和预览 | Qt Widgets app | GUI QtTest、CU-04 至 CU-08 | 待 GUI 与 Computer Use 结果 |
| 暂停、恢复与文件变化 | service、app | CU-09、服务变化对账测试 | 待集成与用户操作结果 |
| 托盘、全局快捷键与重启 | 平台适配 | CU-10 和平台能力报告 | 待各平台执行结果 |
| 十万个物理文件 | file_catalog 容量入口 | tests-file-capacity，独立全规模运行 | 已建立入口，规模结果待报告 |
| 百万 768 维向量 | qcopilots-search-benchmark | 写入、ANN、重开、精确 top-20 对照及 500 次计时 | 已建立入口，本机实测待报告 |
| 真实语义质量 | 独立语义数据集与评估器 | ≥ 1,000 真实片段、≥ 200 中英查询和无答案集 | 待数据集与执行报告 |
| 磁盘不足、提交中断与失效版本零返回 | storage、service | 故障注入及恢复检查 | 部分存储用例已建立，全流程待报告 |
| 安装布局与依赖部署 | install、CPack、verify-package | 原生 CI 与重定位包级启动 | 已建立流程，包级结果待报告 |
| 许可证、源码材料与签名状态 | packaging、依赖锁、第三方声明 | 包清单、源码材料清单、unsigned/signed 字段 | 待最终包审查 |
| QtTest 内部跳过和平台不适用分类 | CI CTest 证据解析器 | qcopilots-search-tests-ci-evidence.py，注册程序集合、逐用例与汇总核对、精确平台分类及日志截断拦截 | 18 个解析器回归测试已在本机通过，实际全量明细以运行报告为准 |

完整验收还需要只读源码树、并发测试实例、不同安装目录以及正式应用同时运行的隔离证据。没有对应报告时，这些要求保持待验证，不由相近的单元测试替代。
