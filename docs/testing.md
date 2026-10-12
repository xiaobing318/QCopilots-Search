# 测试指南

测试按数据和运行边界分层。单元测试、真实模型、语义质量、物理文件遍历、合成向量容量、安装包和 Windows 用户操作分别保留证据。一个层级通过，不能替代另一个层级。

## 路径隔离

每个有可写状态的测试创建唯一的 `QTemporaryDir` 并检查 `isValid()`。默认临时根来自系统，`QCOPILOTS_SEARCH_TEST_ROOT` 可以覆盖到外置构建目录。创建失败必须失败退出，不能回退到源码或真实用户目录。

测试支持库集中管理配置、源文件样本、模型缓存引用和子进程生命周期。只读静态夹具由资源系统或 CMake 提供绝对路径。数据库、配置、日志和索引写入独立根，模型缓存只能在完整校验后共享。测试直接使用配置文件，不向 Windows 用户注册表写入正式设置。

CTest 为每个测试指定工作目录。测试不能把工作目录当作数据根，也不能在源码夹具旁写临时文件。程序路径来自 CMake target，子进程显式接收当前测试的 `--profile-dir` 和 `--model-dir`。

## 准备真实模型

编译和安装后，使用安装布局中的 CLI 下载模型。Windows / Linux 通常位于 `stage/bin`，macOS 位于 `stage/qcopilots-search.app/Contents/MacOS`。CLI 支持直接管理模型，不需要 GUI 或后台服务：

```powershell
$cli = Join-Path $buildRoot 'stage/bin/qcopilots-search-cli.exe'
$modelDir = Join-Path $buildRoot 'models'
$profileDir = Join-Path $buildRoot 'model-setup-profile'
& $cli models manifest
& $cli --model-dir $modelDir --profile-dir $profileDir --timeout 1800000 --progress models download
& $cli --model-dir $modelDir models verify
```

离线环境可使用 `models import FILE_OR_DIRECTORY`。导入与下载都校验锁定文件大小及 SHA-256，不能仅按文件名或文件是否存在判断完成。CLI 的成功与错误结果为 JSON，进度写入标准错误，便于脚本处理。

模型契约来自 `ModelFiles::manifest()`，固定 revision 和两个 GGUF 的校验值见 [依赖说明](dependencies.md)。发行验收中，模型、OCR 数据或必需夹具缺失是失败，不是跳过后通过。

## 运行测试

```powershell
./qcopilots-search-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcopilots-search-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --test-label unit
./qcopilots-search-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir --test-label real-model
```

开发时可以选择标签缩短反馈时间，完整验收必须运行全部必需层级。`--keep-failed-tests` 将保留失败用例的独立产物。脚本默认将 JUnit 写入 `<build-root>/reports/<run-id>/ctest.xml`，可用 `--report-dir` 指定独立报告目录。CTest 原始日志位于构建树的 `Testing/Temporary`，需要保留多次原始日志时复制到对应运行报告目录。

CTest 中列出实际注册的用例：

```bash
ctest --test-dir "$build_root/release" -N
ctest --test-dir "$build_root/release" --show-only=json-v1
```

## 已建立的测试入口

| 入口 | 主要验证 |
| --- | --- |
| `qcopilots-search-tests-core` | 路径、临时根、设置原子写入、指纹、序列化 |
| `qcopilots-search-tests-file-catalog` | 根目录去重、排除边界、Git ignore、文件身份、中文和长路径 |
| `qcopilots-search-tests-model-files` | 下载、续传、校验、取消、发布失败和离线导入 |
| `qcopilots-search-tests-image-sizing` | 图像预算、缩放及 Pillow 数值契约 |
| `qcopilots-search-tests-runtime` | 提示、未加载和损坏模型、运行目录 |
| `qcopilots-search-tests-reference-vectors` | 九条黄金向量、中文、截断、损坏媒体、取消恢复 |
| `qcopilots-search-tests-reference-vectors-baseline` | x64 强制基础 CPU 后端的九条黄金向量 |
| `qcopilots-search-tests-extraction-document` | 编码、分块、长文件续读、HTML、RTF、DOCX、ODT、代码和 Notebook |
| `qcopilots-search-tests-extraction-image` | 图片准备、PDF 页码、扫描页、OCR、临时目录失败 |
| `qcopilots-search-tests-extraction-media` | 音频窗口、重采样、视频分页、音轨、取消和损坏输入 |
| `qcopilots-search-tests-extraction-legacy_doc` | 旧 DOC 读取与夹具 |
| `qcopilots-search-tests-index_repository` | FTS、过滤、替换删除、定位、重开、schema、并发提交 |
| `qcopilots-search-tests-index_repository_regression` | 文件聚合上限、平台路径键、索引一致性与回归边界 |
| `qcopilots-search-tests-vector_index` | 分片、精确过滤、快照、重建、manifest、校验和及 FP16 |
| `qcopilots-search-tests-search_cancellation` | 检索取消与后续查询恢复 |
| `qcopilots-search-tests-recovery_process` | 独立进程中断后的存储恢复 |
| `qcopilots-search-tests-worker` | 进程协议、损坏帧、取消、续读、隔离和无模型行为 |
| `qcopilots-search-tests-cli` | 机器可读错误、离线 manifest、模型目录和只连接模式 |
| `qcopilots-search-tests-client` | IPC 分帧、请求超时、取消、认证 token、断线和重连 |
| `qcopilots-search-tests-service` | 真实服务和 worker 的索引与进程集成 |
| `qcopilots-search-tests-gui` | Qt Widgets 控件、交互状态和输入事件 |
| `qcopilots-search-tests-gui-service` | GUI 与真实服务的集成 |
| `qcopilots-search-tests-semantic-manifest` | 语义集 manifest 与夹具检查 |
| `qcopilots-search-tests-semantic` | 真实模型对独立语义集的检索评估 |
| `qcopilots-search-tests-file-capacity` | 动态生成物理文件的遍历、变化对账和排除 |
| `qcopilots-search-tests-workflow` | 开发与包级验证脚本的路径和安全边界 |
| `.github/scripts/qcopilots-search-tests-ci-evidence.py` | CI 独立运行，验证内部跳过、缺日志和平台分类不会误报通过 |

以 CTest 清单及 [需求矩阵](requirements-test-matrix.md) 维护完整映射。测试文件存在不代表已经在四个平台执行，通过记录必须包含提交与平台。

## 黄金向量和语义质量

九条黄金向量验证 DigUp 推理兼容性，要求 cosine ≥ 0.999。输入及期望向量的文件校验值固定，不能用本次实现重新生成期望值。图片示例和合成音频是数值回归夹具，不能证明真实 OCR、语音或环境声语义检索质量。

语义质量验收使用独立标注集，要求至少 1,000 个真实片段和 200 条有答案中英查询，另有无答案查询。整体 Recall@10 ≥ 0.90，主要语言与模态分组 ≥ 0.80。报告必须包含样本来源、去重规则、标签、模型与提取指纹、每条查询的结果及分组统计。不得用重复同一片段、查询答案泄漏或合成标签凑足真实样本数量。

## 原生 CI 与包级测试

CI 在 Windows x64、Linux x64、macOS arm64 和 macOS x86_64 分别原生编译并运行测试。构建和测试根来自 runner 临时目录与运行 ID，checkout 路径包含空格和中文。模型缓存命中后仍由 CLI 校验内容。

macOS 原始 GUI 测试失败时，CI 最多用 LLDB 重放两次图片预览场景，每次 45 秒，分别使用独立测试目录。原始 JUnit 和失败结论保持不变，调试日志只用于定位问题。超时先中断调试器并请求终止其测试进程，无法确认进程退出时记录 `cleanupUnknown` 并停止后续重放。日志位于 `reports/mac-gui-debug`。CI 另有界收集本次构建之后生成的产品 `.ips` / `.crash` 文件，并上传失败图片提取用例保留的 `qcopilots-search-image-*` 目录。没有崩溃文件不代表没有发生崩溃。

GUI 自动化在可用显示环境中执行，Linux 使用 Xvfb。QtTest 可以验证输入事件和界面状态，Windows 额外 Computer Use 验收记录真实窗口和系统输入法，见 [用户操作验收](windows-user-acceptance.md)。

包级验证从安装或解压后的布局启动程序，清除开发 Qt 插件路径，使用独立 profile。服务与 worker 需要从安装布局正确找到资源，运行时不能从开发机 SDK 或源码目录补齐缺失文件。

## 失败与报告

每条报告标明实际执行结果、失败原因、被跳过或未运行的场景。平台能力不可用不能记为通过。临时目录清理失败、残留子进程、磁盘空间不足和模型缺失都要保留明确错误。

QtTest 的 `QSKIP` 不会使测试程序退出失败，CTest JUnit 也不会自动将它转换成 `<skipped>`。CI 从 JUnit 的 `system-out` / `system-err` 中解析各条 `SKIP` 和 `Totals`，将完整明细写入 `reports/tests/qt-test-cases.json`，并在 `native-acceptance.json` 汇总。缺少 QtTest 输出、CTest 截断标志、汇总与明细不一致、失败、blacklisted 或必需用例未执行，都会使原生 CI 测试阶段失败，也会阻断发布验收。测试运行器将捕获上限设为 16 MiB，超过限制时仍必须重新取得完整证据。非 QtTest 的语义程序和 Python workflow 测试单独按其报告处理。

目前唯一的平台不适用分类是 Windows 上 `IndexRepositoryRegressionTest::rootFilterRespectsCaseSensitivePlatformKeys()`，该用例验证非 Windows 的大小写敏感路径契约。分类同时匹配平台、CTest 名称、完整用例名和固定原因，仍记录为 `notApplicable`，不计入通过数。Windows 账户无法创建符号链接导致 `FileCatalogTest::symbolicLinksCannotEscapeOrLoop()` 跳过时，记录为 `requiredNotRun` 并阻断发布。需要在具备创建符号链接能力的测试环境执行该用例，不能把权限限制改记为通过。

每个 QtTest 日志还必须有匹配的开始与结束记录、非零实际执行数，并且逐条 PASS / FAIL / SKIP 等记录与汇总一致。只保留 `Totals` 行或截断后的日志不能作为完整通过证据。

CI 在全量测试开始前保存 `ctest --show-only=json-v1` 的注册清单，写入 `reports/tests/ctest-inventory.json`。报告中的程序名称必须与该清单完全一致，缺失、重复或额外程序都会使验收失败。开发者使用 `--test-label` 或 `ctest -R` 得到的子集报告只能说明所选范围，不得称为整套测试完成。

测试清理只允许作用于本次创建且边界校验通过的根。先结束本次启动的子进程，再释放文件句柄并清理数据。不能终止正在运行的正式应用来让测试通过。
