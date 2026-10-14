# 需求与测试矩阵

本文追踪需求到实现入口和验证入口。状态“已建立用例”表示源码中存在对应测试，不表示编译或执行通过。执行结果统一由对应提交的 CTest、CI 和验收报告提供，当前发布结论见 [实施状态](implementation-status.md)。

| 需求 | 实现入口 | 验证入口或关键用例 | 证据状态 |
| --- | --- | --- | --- |
| 自有组件统一前缀 | 根 CMake、各模块、开发脚本 | CMake 测试名称检查、安装布局清单 | 已建立检查，待集成结果 |
| 外置构建与任意源码路径 | 开发脚本、CMake presets | CI 检出到 `qcs-src`、独立中文空格路径回归、CMake 拒绝源码内构建 | 已建立流程，待原生 CI |
| 显式 profile 与 model 路径 | `core/runtime_paths.*` | `tests-core`: explicitRuntimePaths、environmentOverrides | 已建立用例 |
| 中文、空格、长路径 | core、提取、runtime | longAndUnicodePaths、longUnicodePaths 的动态 258/259/260/300 长度数据行、unicodeAndSpacedImagePathMatchesReference | 已建立用例 |
| 不依赖工作目录 | RuntimePaths、脚本自定位 | workingDirectoryIndependence、CI 独立工作目录 | 已建立用例 |
| 每次 CTest 调用使用独立 cwd | `tools/qcs-test-runner.py`、测试支持库 | `qcs-tests-runner`，同一构建树并发执行同一测试，核对 cwd 和可写配置不共享 | 已建立回归，以本轮逐用例报告为准 |
| 真实只读源码与双 checkout | `tools/acceptance/qcs-two-checkout-portability.py`、开发脚本 | 实际只读权限下 configure/build/test，源码哈希不变，两个独立 checkout 并行构建 | 必需验收，不能用只读夹具替代 |
| Windows 逐节点只读与恢复 | `tools/acceptance/qcs-test-portability-runners.py` | 显式允许、继承保护、父子删除权限、只读属性、共享冲突、节点丢失与替换、恢复差异及原始错误保留 | 原生 Windows 用例，恢复差异必须使专项验收失败 |
| Windows 权限测试进程上下文 | `tools/acceptance/qcs-token-context.py` 与受控验收入口 | `qcs-test-token-context.py`，仅在受控暂停子进程中移除 Backup/Restore 特权、构建后代继承、父令牌不变、已有线程令牌拒绝、真实五探针及失败进程收尾 | 原生 Windows 用例与跨平台错误回归，实际结果待同一冻结候选报告，诊断分支不能替代验收 |
| 正式应用同时运行时隔离 | RuntimePaths、服务锁和 IPC | 启动受控正式实例并运行独立测试，核对其 PID、状态和数据未变 | 必需回归，以本轮进程报告为准 |
| 唯一临时根与失败不回退 | 测试支持库、runtime 夹具 | temporaryRootsUnique、customTemporaryRoot、temporaryFailureDoesNotFallback、temporaryDirectoryUsesConfiguredRoot、temporaryDirectoryRejectsFileAsRoot | 已建立用例 |
| Python 临时根空值与失败边界 | `tests/support/temp_paths.py` | `qcs-tests-temp-paths`，未设置或空值不写 cwd，中文目录隔离，阻塞路径明确失败 | 已建立回归，以本轮逐用例报告为准 |
| 清理边界与句柄释放 | 测试支持库、子进程封装 | cleanupBoundaries、cleanupRejectsMovedRoot、cleanupRejectsReplacedRoot、cleanupRequiresClosedWindowsHandles 及进程退出测试 | 已建立用例 |
| 清理失败不可只警告 | 显式测试收尾、开发脚本 clean | temporaryWriteFailure、destructorCleanupFailuresAreObservable、qcs-tests-runner 的 cleanup error、replaced root 和 interruption 回归 | 已建立回归，以本轮逐用例报告为准 |
| 生成物清理保留用户资源 | clean 范围及归属检查 | dry-run 无修改，generated 清理排除模型、SDK、源码和正式配置，归属不符拒绝 | 必需回归，以本轮工具测试报告为准 |
| 实际生成路径预算 | `tools/qcs-path-budget.py` 与 CMake | `qcs-tests-path-budget`，Windows UTF-16 长度、对象 200 字符、其他受控路径 240 字符，超长根诊断及动态长路径压力用例 | 已建立回归，以本轮路径报告为准 |
| 配置读取与原子保存 | `core/app_settings.*` | settingsRoundTrip、settingsRejectCorruption、settingsAtomicFailure | 已建立用例 |
| 配置 v2 与普通/代码目录分离 | `core/app_settings.*`、`app/settings_dialog.*` | `qcs-tests-core` 的 `settingsV1Migration`、`settingsFingerprint`，`qcs-tests-gui` 的 `independentFoldersAndExternalApplicationArgumentsRoundTrip` | 已建立迁移与界面回归，关闭代码索引必须保留代码目录 |
| 同路径两个空间的数据隔离 | `storage/index_repository.*`、服务配置与扫描 | `samePathSpacesKeepIndependentKeywordsAndVectors`、`pruningIsIndependentForOrdinaryAndCodeSpaces`、服务 `independentSpacesRepairMetadataAndBrowseWithoutContent` | 已建立回归，分别验证关键词、向量、清理及目录资格 |
| schema 3 元数据补齐与来源核对 | SQLite 迁移、文件扫描、元数据快照 | `schemaTwoMigrationRequiresMetadataWithoutReextractingFiles`、`fileMetadataBackfillPreservesIndexedContentAndVersion`、`metadataSnapshotCanRepairChangedSourcesWithoutReextracting`、服务 `pausedLegacyMetadataRepairHidesChangedSourceWithoutBlockingHealthyFile` | 已建立迁移回归，未知时间不可伪造，等待核对必须显式报告 |
| 旧持久布局拒绝且不修改 | RuntimePaths、ServiceClient | legacyProfileRejected、legacyProfileIsRejectedBeforeConnectingToExistingEndpoint | 已建立回归，以本轮进程报告为准 |
| 模型固定版本与校验 | `core/model_files.*` | pinnedManifest、corruptedModelCannotPublish、CLI models verify | 已建立用例 |
| 下载续传、取消和重试 | 模型下载器 | resumesRanges、cancelKeepsPartialAndUnlocks、invalidCompletePartialRestarts | 已建立用例 |
| 文本、HTML、RTF、Office | `extraction/document_extractor.*` | extraction-document 全套 | 已建立用例 |
| HTML 外部资源禁止加载 | 文档提取 | htmlNeverReadsLocalResources | 已建立用例 |
| 长文件续读 | 提取游标、worker | longDocumentResumesWithoutLosingTail、persistentCursorAndIsolatedProfiles | 已建立用例 |
| 近期变动、批量首读和续读公平调度 | `service/service_server.cpp` 三队列与队列令牌 | `qcs-tests-service` 的 `scansMergeNewFilesBetweenLongDocumentBatches`、`changedQueuedFileIsPrioritizedAheadOfBulkBacklog`、`changingSourceDoesNotStarveStableQueuedFile`、`continuationSurvivesMissingJournal` | 实现为批次 4:1:1，已建立时序回归，不代表固定响应时间保证 |
| worker 空闲退出和需求恢复 | 服务 index/query idle 定时器与退出原因状态 | `idleWorkersReleaseAndRestartDespiteFrequentReconciliation`、`workArrivingWhileWorkerCloses` | 索引默认 60 秒、查询默认 600 秒，测试缩短等待验证状态机，真实资源与恢复延迟另记 |
| 旧 DOC | legacy_doc_extractor | extraction-legacy_doc | 已建立入口，格式结果待报告 |
| 图片、PDF、OCR | image_extractor | pdfPaginationAndPosition、scannedPdfRendersImage、realOcrWithUnicodeDataDirectory | 已建立用例 |
| HEIC、RAW 格式范围 | libheif、LibRaw | 真实格式探针与包级运行 | 待明确夹具与执行证据 |
| 音频窗口、环境声、视频时间 | media_extractor、runtime | extraction-media、reference-vectors、多模态功能集 | 数值和窗口用例已建立，真实链路结果待报告 |
| 可选代码、Git ignore、未跟踪源码 | file_catalog、document_extractor | gitIgnoreKeepsTrackedAndUntrackedSource、codeLocationsAndNotebookSources | 已建立用例 |
| 普通与代码扫描的独立资格 | `core/file_catalog.*`、服务扫描合并 | `independentScopesAllowSharedTextAndRejectCodeData`、`codeScanningWorksWithoutGitExecutable`、`codeScopeSkipsNestedWorktreeUnlessExplicitlySelected`、服务 `newProjectMarkerWithdrawsQueuedOrdinaryFile` | 已建立回归，目录资格变动需同步撤销过时排队任务 |
| 九条黄金向量 cosine ≥ 0.999 | runtime | tests-reference-vectors | 已建立用例，各变体结果待 CI |
| 中文、截断、损坏媒体 | runtime | chineseInputsAreNormalizedAndDistinct、truncationPreservesThePrefix、malformedAndMismatchedMediaAreRejected | 已建立用例 |
| 中文关键词与定位 | storage/text_index | tests-index_repository 的 keywordSearch、locationAndFileAggregation | 已建立用例 |
| 无模型关键词搜索 | worker、storage | unicodePathsAndKeywordOnlyWithoutModels | 已建立用例 |
| 文件增删改与模型版本 | IndexRepository | replacementAndRemovalRetireOldSegments、pipelineChangeInvalidatesVectorsButPreservesKeywordSearch | 已建立用例 |
| 空查询文件浏览与 keyset 分页 | `IndexRepository::listFiles`、服务 `files.list` | `fileListPagesEveryFileOnceWithStableModificationTies`、`fileListPageDoesNotPinReadTransactionBetweenRequests` | 已建立回归，按修改时间与 ID 排序，每页 1 至 100 项且页面间不持有读事务 |
| 浏览游标与真实可见版本 | `browseGeneration`、服务 `visibilityEpoch`、游标配置摘要 | `fileListCursorRejectsChangedVisibilityContext`、`fileListCursorBecomesStaleAfterMembershipOrMetadataChanges`、`fileListCursorSurvivesSegmentContinuationAndAnnPublication` | 已建立回归，无变化扫描、续读和 ANN 发布不可误使页面失效 |
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
| 两行元信息列表与按选择读取正文 | `app/main_window.*`、`app/preview_panel.*` | `emptyQueryBrowsesFilesAndLoadsOnlySelectedBody`、`keywordSearchAndTextPreview`、`browsingDocumentLoadsOneBatchWithoutInventingSearchLocation` | 已建立回归，列表不得显示正文或内容缩略图，最终包仍需目视确认 |
| 主窗口分页与快速窗口上限 | GUI `files.list` 请求和浏览控件 | `browsingAppendAndStaleCursorPreserveSelectionAndPreview`、`quickBrowserUsesTwelveAndRefreshesLanguage` | 主窗口每页 100，快速最多 12 且不分页，已建立状态回归 |
| 过期页、迟到响应与核对等待 | GUI 请求上下文和浏览状态 | `lateBrowseResponseCannotReplaceTypedSearchBeforeDebounce`、`metadataPendingSearchShowsProgressAndRetriesAfterIndexChange`、`quickBrowseFailureRemainsVisibleAcrossStatusRefresh` | 已建立回归，旧页保留并提示刷新，版本不兼容不能静默回退 |
| 状态与语言刷新保留正在操作的预览控件 | `app/main_window.*`、`app/preview_panel.*` | `qcs-tests-gui` 的 `playingMediaButtonSurvivesStatusAndLanguageRefresh`、`mediaClockTracksDurationBeforePlaybackAndClears`、`statusRefreshPreservesUncommittedKindNavigation`、`statusRefreshPreservesIntermediatePdfPageEdit`，验证实际播放状态、播放前时长、未提交分类导航与 PDF 页码输入 | 已建立独立回归，旧代码失败与修复后结果分别记录，最终包仍需 Computer Use 复验 |
| 暂停、恢复与文件变化 | service、app | CU-09、服务变化对账测试 | 待集成与用户操作结果 |
| 托盘、全局快捷键与重启 | 平台适配 | CU-10 和平台能力报告 | 待各平台执行结果 |
| Linux portal 与 X11 全局快捷键 | `app/linux_global_shortcut.*`、`app/platform_integration.*` | Linux 专用 `qcs-tests-linux-shortcut`，另需真实 X11/Wayland 桌面验证授权、绑定、触发、关闭与重试 | 协议状态用例不能替代实际 compositor 运行结果，拒绝授权不得绕过 |
| 登录启动与隔离配置限制 | `app/login_startup.*`、`PlatformIntegration::canSetStartAtLogin`、设置页显式变更意图 | `qcs-tests-login-startup` 的 desktop-entry 编码、禁用覆盖、测试隔离用例，`qcs-tests-platform` 的 `isolatedProfilesCannotChangeLoginStartup` | 测试不写真实系统启动项，Windows/macOS/Linux 实际登录行为需原生验收 |
| 指定外部 PDF 与媒体应用定位 | `app/platform_external_open.*`、外部应用设置 | `platformPdfPresetUsesPhysicalPage`、`mediaStartUsesExactMilliseconds`、`actualProcessReceivesLiteralFileAndLocationArguments`、`missingApplicationNeverSilentlyFallsBack` | 参数构造和进程传参已有回归，实际查看器/播放器定位需安装相应应用验证 |
| 系统默认打开的定位边界 | `PlatformIntegration::openHit` 与 GUI 通知 | `ordinaryOpenExplicitlyReportsLocationLimit`、外部打开 Computer Use 场景 | 不保证默认应用页码或时间定位，指定应用失败不得伪装成默认打开成功 |
| 磁盘不足、提交中断与失效版本零返回 | storage、service | 故障注入及恢复检查 | 部分存储用例已建立，全流程待报告 |
| 安装布局与依赖部署 | install、CPack、verify-package | 英文无空格的解包、安装、搬移和 profile 路径，原生 CI、搬移后启动及依赖隔离 | 已建立流程，包级结果待报告 |
| 包报告绑定实际源码 checkout | `tools/qcs-verify-package.py` | `qcs-tests-workflow`，真实 Git HEAD、linked worktree、候选声明失配、dirty 状态、无 Git 归档与错误提前失败 | 已建立回归，真实包级结果待新冻结候选报告 |
| 验收运行器入库后契约 | `tools/acceptance` | qcs-tests-acceptance-runners，候选身份、fixture 拷贝、来源清单和可移植性运行器检查 | 已建立回归，实际专项验收仍单独执行 |
| 用例结果与诊断输出完整性 | `tools/acceptance/qcs-test-diagnostics.py` | 独立和聚合入口的真实合流管道，大段 JSON、失败、中断、重复执行及输出错误，严格逐例解析不放宽 | 已建立回归，执行结果以同一候选的原始日志为准 |
| 许可证、源码材料与签名状态 | packaging、依赖锁、第三方声明 | 包清单、源码材料清单、unsigned/signed 字段 | 待最终包审查 |
| Linux Qt FFmpeg 源码元数据加载 | `tools/qcs-source-bundle.py` | `qcs-tests-acceptance-runners` 中的 `qcs-test-source-checksums.py`，同目录 avformat 先加载、局部符号范围、句柄生命周期、错误传播及许可拒绝的模拟回归 | 已建立回归，真实 Linux SDK 与源码材料结果待原生 CI |
| QtTest 内部跳过和平台不适用分类 | CI CTest 证据解析器 | qcs-tests-ci-evidence.py，注册程序集合、逐用例与汇总核对、精确平台分类及日志截断拦截 | 已建立回归，短名候选结果以本轮报告为准 |
| 本地完整自动化门槛 | CTest、QtTest、工具与包验证 | 全量注册清单、逐用例明细、真实模型及安装报告，缺失不放行 | 同一冻结候选的独立门槛 |
| Windows Computer Use 门槛 | 最终 Windows 包、@oai/sky | CU-01 至 CU-10，英文无空格运行根、独立 profile、真实窗口操作，保留中文搜索样本、包及程序哈希和截图 | 同一最终包的独立门槛 |
| 正式语义候选与多模态链 | storage、runtime、`tests/semantic` | 小型固定输入实际提取、编码、持久化、重载，隔离单文件范围关闭关键词兜底检索 | 检查实际来源与定位，不设置排名或相关度接受线 |
| 扫描与提取格式一致 | core 共享格式定义、extraction 路由 | 音频、RAW、QML、Dockerfile 命名规则和扫描路由一致性 | 按本轮功能测试判定 |
| 查询上限统一为 500 | core 共享约束、CLI、服务 | 500 接受、501 拒绝、帮助及协议一致 | 按本轮 CLI/服务测试判定 |
| 快捷窗保留完整查询上下文 | quick search 与主窗口 | 转移 query、代码空间、类型和目录，覆盖旧主窗口过滤条件 | 按本轮 GUI 测试判定 |
| GUI 按规范化 profile 单实例 | GUI 启动协调 | 相同 profile 激活已有窗口，不同 profile 并行，重复启动不停止服务 | 按本轮双进程测试判定 |
| 默认全部保留测试 | `qcs-dev`、CTest preset、原生 CI | 完整注册清单、标签筛选、空选择失败、局部结果不可冒充完整 | 以实际选择记录和逐用例结果为准 |
| 11 变体远端 Actions | 共同变体清单、`qcs-native.yml` | 全部变体构建、测试、模型、安装、打包、验包及汇总成功 | 记录真实 run URL、attempt 和原始 job |
| 真实后端与 GPU 回退 | runtime、worker、服务诊断与 GUI | 自动选择、CPU 回退原因、严格 GPU 模式不能静默回退 | 无设备时明确记录硬件未实测 |
| CI 用户包白名单 | CI、artifact 选择器、release 工具 | 11 artifacts、21 用户文件，包类型与身份一致，无额外文件 | 以实际 artifacts 及 digest 为准 |
| 发布来源与冻结字节 | `qcs-release.py` | 部分重跑、失败不能借旧成功、错误候选、digest 失配、缺包和冻结后变化 | 工具回归与真实准备分别记录 |

完整交付要求本机全部保留自动化、11 变体原生 CI、最终 Windows CPU 包的 Computer Use 和用户包验证。源码中存在入口、触发 Actions 或执行相近用例不能替代实际证据。所有记录绑定执行时的同一不可变候选，分别报告失败、未运行和平台不适用。

模型与向量的数值完整性继续验证。语义检索不设置相关度接受线，低相似度结果可能出现。GPU 构建、依赖闭包和 CPU 回退通过不等于 GPU 实机通过，没有设备的变体明确记录硬件未实测。
