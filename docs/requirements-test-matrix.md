# 需求与测试矩阵

本文追踪需求到实现入口和验证入口。状态“已建立用例”表示源码中存在对应测试，不表示编译或执行通过。执行结果统一由对应提交的 CTest、CI 和验收报告提供，当前发布结论见 [实施状态](implementation-status.md)。

| 需求 | 实现入口 | 验证入口或关键用例 | 证据状态 |
| --- | --- | --- | --- |
| 自有组件统一前缀 | 根 CMake、各模块、开发脚本 | CMake 测试名称检查、安装布局清单 | 已建立检查，待集成结果 |
| 外置构建与任意源码路径 | 开发脚本、CMake presets | CI 中文空格 checkout、CMake 拒绝源码内构建 | 已建立流程，待原生 CI |
| 显式 profile 与 model 路径 | `core/runtime_paths.*` | `tests-core`: explicitRuntimePaths、environmentOverrides | 已建立用例 |
| 中文、空格、长路径 | core、提取、runtime | longAndUnicodePaths、longUnicodePaths 的动态 258/259/260/300 长度数据行、unicodeAndSpacedImagePathMatchesReference | 已建立用例 |
| 不依赖工作目录 | RuntimePaths、脚本自定位 | workingDirectoryIndependence、CI 独立工作目录 | 已建立用例 |
| 每次 CTest 调用使用独立 cwd | `tools/qcs-test-runner.py`、测试支持库 | `qcs-tests-runner`，同一构建树并发执行同一测试，核对 cwd 和可写配置不共享 | 已建立回归，以本轮逐用例报告为准 |
| 真实只读源码与双 checkout | `tools/acceptance/qcs-two-checkout-portability.py`、开发脚本 | 实际只读权限下 configure/build/test，源码哈希不变，两个独立 checkout 并行构建 | 必需验收，不能用只读夹具替代 |
| Windows 逐节点只读与恢复 | `tools/acceptance/qcs-test-portability-runners.py` | 显式允许、继承保护、父子删除权限、只读属性、共享冲突、节点丢失与替换、恢复差异及原始错误保留 | 原生 Windows 用例，恢复差异必须使专项验收失败 |
| 正式应用同时运行时隔离 | RuntimePaths、服务锁和 IPC | 启动受控正式实例并运行独立测试，核对其 PID、状态和数据未变 | 必需回归，以本轮进程报告为准 |
| 唯一临时根与失败不回退 | 测试支持库、runtime 夹具 | temporaryRootsUnique、customTemporaryRoot、temporaryFailureDoesNotFallback、temporaryDirectoryUsesConfiguredRoot、temporaryDirectoryRejectsFileAsRoot | 已建立用例 |
| Python 临时根空值与失败边界 | `tests/support/temp_paths.py` | `qcs-tests-temp-paths`，未设置或空值不写 cwd，中文目录隔离，阻塞路径明确失败 | 已建立回归，以本轮逐用例报告为准 |
| 清理边界与句柄释放 | 测试支持库、子进程封装 | cleanupBoundaries、cleanupRejectsMovedRoot、cleanupRejectsReplacedRoot、cleanupRequiresClosedWindowsHandles 及进程退出测试 | 已建立用例 |
| 清理失败不可只警告 | 显式测试收尾、开发脚本 clean | temporaryWriteFailure、destructorCleanupFailuresAreObservable、qcs-tests-runner 的 cleanup error、replaced root 和 interruption 回归 | 已建立回归，以本轮逐用例报告为准 |
| 生成物清理保留用户资源 | clean 范围及归属检查 | dry-run 无修改，generated 清理排除模型、SDK、源码和正式配置，归属不符拒绝 | 必需回归，以本轮工具测试报告为准 |
| 实际生成路径预算 | `tools/qcs-path-budget.py` 与 CMake | `qcs-tests-path-budget`，Windows UTF-16 长度、对象 200 字符、其他受控路径 240 字符，超长根诊断及动态长路径压力用例 | 已建立回归，以本轮路径报告为准 |
| 配置读取与原子保存 | `core/app_settings.*` | settingsRoundTrip、settingsRejectCorruption、settingsAtomicFailure | 已建立用例 |
| 旧持久布局拒绝且不修改 | RuntimePaths、ServiceClient | legacyProfileRejected、legacyProfileIsRejectedBeforeConnectingToExistingEndpoint | 已建立回归，以本轮进程报告为准 |
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
| 过滤分片策略与取消恢复 | VectorIndex | `qcs-tests-vector_index`，真实允许集合的选择边界、缺失 ID 与墓碑、混合分片、实际打分途中取消、异常、旧快照及全局 k 并列排序 | 已建立回归，正确性与性能结果待新冻结候选报告 |
| 并发发布时的 SQL/ANN 配对快照 | IndexRepository | `qcs-tests-index_repository_regression` 的 `annFileAggregationAndPendingTombstones`，待发布增删改、目录规则变化、旧读者与单分片发布交错 | 已建立回归，原生执行结果待新冻结候选报告 |
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
| 百万 768 维向量 | qcs-benchmark | 写入、ANN、重开、精确 top-20 对照及 500 次计时 | 已建立入口，本机实测待报告 |
| 真实语义质量 | 独立语义数据集与评估器 | ≥ 1,000 真实片段、≥ 200 中英查询和无答案集 | 待数据集与执行报告 |
| 磁盘不足、提交中断与失效版本零返回 | storage、service | 故障注入及恢复检查 | 部分存储用例已建立，全流程待报告 |
| 安装布局与依赖部署 | install、CPack、verify-package | 原生 CI 与重定位包级启动 | 已建立流程，包级结果待报告 |
| 包报告绑定实际源码 checkout | `tools/qcs-verify-package.py` | `qcs-tests-workflow`，真实 Git HEAD、linked worktree、候选声明失配、dirty 状态、无 Git 归档与错误提前失败 | 已建立回归，真实包级结果待新冻结候选报告 |
| 验收运行器入库后契约 | `tools/acceptance` | qcs-tests-acceptance-runners，候选身份、fixture 拷贝、容量守卫、来源清单和可移植性运行器检查 | 已建立回归，实际专项验收仍单独执行 |
| 用例结果与诊断输出完整性 | `tools/acceptance/qcs-test-diagnostics.py` | 独立和聚合入口的真实合流管道，大段 JSON、失败、中断、重复执行及输出错误，严格逐例解析不放宽 | 已建立回归，执行结果以同一候选的原始日志为准 |
| 许可证、源码材料与签名状态 | packaging、依赖锁、第三方声明 | 包清单、源码材料清单、unsigned/signed 字段 | 待最终包审查 |
| Linux Qt FFmpeg 源码元数据加载 | `tools/qcs-source-bundle.py` | `qcs-tests-acceptance-runners` 中的 `qcs-test-source-checksums.py`，同目录 avformat 先加载、局部符号范围、句柄生命周期、错误传播及许可拒绝的模拟回归 | 已建立回归，真实 Linux SDK 与源码材料结果待原生 CI |
| QtTest 内部跳过和平台不适用分类 | CI CTest 证据解析器 | qcs-tests-ci-evidence.py，注册程序集合、逐用例与汇总核对、精确平台分类及日志截断拦截 | 已建立回归，短名候选结果以本轮报告为准 |
| 本地完整自动化门槛 | CTest、QtTest、工具与包验证 | 全量注册清单、逐用例明细、真实模型及安装报告，缺失不放行 | 同一冻结候选的独立门槛 |
| 四组合远端 Actions 门槛 | qcs-native.yml、required 汇总 | 四组合 configure/build/test/真实模型/package/verify 全部 success，记录实际 run URL | 同一冻结候选的独立门槛 |
| Windows Computer Use 门槛 | 最终 Windows 包、@oai/sky | CU-01 至 CU-10，真实枚举与窗口操作，包及程序哈希、截图和结果 | 同一最终包的独立门槛 |

完整验收需要本地自动化、四组合原生 CI、当前 Windows Computer Use 和必需容量测试分别完成。源码中存在入口、触发 Actions 或执行相近用例均不能替代实际证据，失败、未运行和平台不适用必须分别记录。所有报告绑定执行时的同一候选，不复用旧长命名候选的报告。
