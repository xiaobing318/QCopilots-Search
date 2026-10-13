# 测试指南

测试按数据和运行边界分层。单元测试、真实模型、语义质量、物理文件遍历、合成向量容量、安装包和 Windows 用户操作分别保留证据。一个层级通过，不能替代另一个层级。

## 路径隔离

每个有可写状态的测试创建唯一的 `QTemporaryDir` 并检查 `isValid()`。默认临时根来自系统，`QCOPILOTS_SEARCH_TEST_ROOT` 可以覆盖到外置构建目录。创建失败必须失败退出，不能回退到源码或真实用户目录。

测试支持库集中管理配置、源文件样本、模型缓存引用和子进程生命周期。只读静态夹具由资源系统或 CMake 提供绝对路径。数据库、配置、日志和索引写入独立根，模型缓存只能在完整校验后共享。测试直接使用配置文件，不向 Windows 用户注册表写入正式设置。

CTest 的每次程序调用使用原子创建的独立工作目录，不能只用固定测试名称区分。相同构建树的并发 CTest 运行也不能共享可写 cwd。测试不能把工作目录当作数据根，也不能在源码夹具旁写临时文件。程序路径来自 CMake target，子进程显式接收当前测试的 `--profile-dir` 和 `--model-dir`。

测试入口启用 `QStandardPaths::setTestModeEnabled(true)` 作为附加保护。运行上下文明确传递 profile、数据库、ANN、提取缓存和报告路径，网络模拟服务由系统分配端口。IPC 使用本次独立实例身份，正式应用同时运行时不连接、修改或终止正式实例。

可移植性验收在运行时生成中文、空格及所需长度的路径，覆盖显式临时根、任意 cwd、不同源码和安装位置、真实只读源码树与两个 checkout 并行构建。Windows 盘符和 UNC 字面量只作为路径解析的纯数据用例，实际文件操作不得依赖固定盘符或用户名。模型缺失及无法创建临时根必须失败，不能改用用户目录或源码树作为后备目录。

## 准备真实模型

编译和安装后，使用安装布局中的 CLI 下载模型。Windows / Linux 通常位于 `stage/bin`，macOS 位于 `stage/qcs.app/Contents/MacOS`。CLI 支持直接管理模型，不需要 GUI 或后台服务：

```powershell
$cli = Join-Path $buildRoot 'stage/bin/qcs-cli.exe'
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
./qcs-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir
./qcs-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --test-label unit
./qcs-dev.ps1 test --build-root $buildRoot --qt-root $qtRoot --model-dir $modelDir --test-label real-model
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
| `qcs-tests-core` | 路径、临时根、设置原子写入、指纹、序列化 |
| `qcs-tests-file-catalog` | 根目录去重、排除边界、Git ignore、文件身份、中文和长路径 |
| `qcs-tests-model-files` | 下载、续传、校验、取消、发布失败和离线导入 |
| `qcs-tests-image-sizing` | 图像预算、缩放及 Pillow 数值契约 |
| `qcs-tests-runtime` | 提示、未加载和损坏模型、运行目录 |
| `qcs-tests-reference-vectors` | 九条黄金向量、中文、截断、损坏媒体、取消恢复 |
| `qcs-tests-reference-vectors-baseline` | x64 强制基础 CPU 后端的九条黄金向量 |
| `qcs-tests-extraction-document` | 编码、分块、长文件续读、HTML、RTF、DOCX、ODT、代码和 Notebook |
| `qcs-tests-extraction-image` | 图片准备、PDF 页码、扫描页、OCR、临时目录失败 |
| `qcs-tests-extraction-media` | 音频窗口、重采样、视频分页、音轨、取消和损坏输入 |
| `qcs-tests-extraction-legacy_doc` | 旧 DOC 读取与夹具 |
| `qcs-tests-index_repository` | FTS、过滤、替换删除、定位、重开、schema、并发提交 |
| `qcs-tests-index_repository_regression` | 文件聚合上限、平台路径键、索引一致性与回归边界 |
| `qcs-tests-vector_index` | 分片、精确过滤、快照、重建、manifest、校验和及 FP16 |
| `qcs-tests-search_cancellation` | 检索取消与后续查询恢复 |
| `qcs-tests-recovery_process` | 独立进程中断后的存储恢复 |
| `qcs-tests-worker` | 进程协议、损坏帧、取消、续读、隔离和无模型行为 |
| `qcs-tests-cli` | 机器可读错误、离线 manifest、模型目录和只连接模式 |
| `qcs-tests-client` | IPC 分帧、请求超时、取消、认证 token、断线和重连 |
| `qcs-tests-service` | 真实服务和 worker 的索引与进程集成 |
| `qcs-tests-gui` | Qt Widgets 控件、交互状态和输入事件 |
| `qcs-tests-gui-service` | GUI 与真实服务的集成 |
| `qcs-tests-semantic-manifest` | 语义集 manifest 与夹具检查 |
| `qcs-tests-semantic` | 真实模型对独立语义集的检索评估 |
| `qcs-tests-file-capacity` | 动态生成物理文件的遍历、变化对账和排除 |
| `qcs-tests-workflow` | 开发与包级验证脚本的路径和安全边界 |
| `qcs-tests-runner` | 每次调用的独立 cwd、并发隔离、子进程结束及显式清理失败 |
| `qcs-tests-temp-paths` | Python 夹具临时根解析，空值使用系统临时根，中文目录创建、实例唯一及创建失败不回退 |
| `qcs-tests-path-budget` | 实际生成路径解析、UTF-16 长度、对象与生成物预算 |
| `qcs-tests-acceptance-runners` | 入库验收运行器的参数、来源和报告契约 |
| `.github/scripts/qcs-tests-ci-evidence.py` | CI 独立运行，验证内部跳过、缺日志和平台分类不会误报通过 |

以 CTest 清单及 [需求矩阵](requirements-test-matrix.md) 维护完整映射。测试文件存在不代表已经在四个平台执行，通过记录必须包含提交与平台。

`tools/qcs-test-runner.py` 是 CTest 每次调用的隔离入口，其回归位于 `tools/qcs-test-runner-tests.py`。`tools/qcs-path-budget.py` 检查真实构建图，回归位于 `tools/qcs-test-path-budget.py`。运行器不会把成功退出但清理失败的用例当作通过。

完整候选和本机专项运行器位于 `tools/acceptance`，包括 `qcs-candidate-ctest-run.py`、`qcs-two-checkout-portability.py`、`qcs-run-physical.ps1`、`qcs-run-million.ps1` 和 `qcs-service-latency.py`。使用各工具的帮助查看显式源码、外置根、Qt、模型及来源参数。真实只读源码与双 checkout 验收必须运行专项入口，不能仅用运行器自身的单元测试替代。

Windows 只读验收先保存独立 checkout 的全部节点身份和 ACL，再逐节点拒绝写入、删除及重命名。原 checkout 的权限始终只读取。权限探针必须获得实际访问拒绝，文件占用不能作为通过依据。收尾时逐项恢复原 ACL，完成后重新比较所有节点，并执行实际读写和清理探针。

无法精确恢复时，运行器保留原始和实际描述符、节点差异及首个错误，并将整个验收判为失败。回归测试同时覆盖继承保护目录、显式允许权限、只读文件属性和恢复失败路径，不通过忽略差异或减少探针满足门槛。权限快照及恢复比较必须保留实际描述符的条目顺序，不能用经过排序的显示文本代替原始状态。

## 黄金向量和语义质量

九条黄金向量验证 DigUp 推理兼容性，要求 cosine ≥ 0.999。输入及期望向量的文件校验值固定，不能用本次实现重新生成期望值。图片示例和合成音频是数值回归夹具，不能证明真实 OCR、语音或环境声语义检索质量。

语义质量验收使用独立标注集，要求至少 1,000 个真实片段和 200 条有答案中英查询，另有无答案查询。整体 Recall@10 ≥ 0.90，主要语言与模态分组 ≥ 0.80。报告必须包含样本来源、去重规则、标签、模型与提取指纹、每条查询的结果及分组统计。不得用重复同一片段、查询答案泄漏或合成标签凑足真实样本数量。

## 原生 CI 与包级测试

CI 在 Windows x64、Linux x64、macOS arm64 和 macOS x86_64 分别原生编译并运行测试。构建和测试根来自 runner 临时目录与运行 ID，checkout 路径包含空格和中文。模型缓存命中后仍由 CLI 校验内容。

macOS 原始 GUI 测试失败时，CI 最多用 LLDB 重放两次图片预览场景，每次 45 秒，分别使用独立测试目录。原始 JUnit 和失败结论保持不变，调试日志只用于定位问题。超时先中断调试器并请求终止其测试进程，无法确认进程退出时记录 `cleanupUnknown` 并停止后续重放。日志位于 `reports/mac-gui-debug`。CI 另有界收集本次构建之后生成的产品 `.ips` / `.crash` 文件，并上传失败图片提取用例保留的 `qcs-image-*` 目录。没有崩溃文件不代表没有发生崩溃。

GUI 自动化在可用显示环境中执行，Linux 使用 Xvfb。QtTest 可以验证输入事件和界面状态，Windows 额外 Computer Use 验收记录真实窗口和系统输入法，见 [用户操作验收](windows-user-acceptance.md)。

包级验证从安装或解压后的布局启动程序，清除开发 Qt 插件路径，使用独立 profile。服务与 worker 需要从安装布局正确找到资源，运行时不能从开发机 SDK 或源码目录补齐缺失文件。

## 失败与报告

每条报告标明实际执行结果、失败原因、被跳过或未运行的场景。平台能力不可用不能记为通过。临时目录清理失败、残留子进程、磁盘空间不足和模型缺失都要保留明确错误。

QtTest 的 `QSKIP` 不会使测试程序退出失败，CTest JUnit 也不会自动将它转换成 `<skipped>`。CI 从 JUnit 的 `system-out` / `system-err` 中解析各条 `SKIP` 和 `Totals`，将完整明细写入 `reports/tests/qt-test-cases.json`，并在 `native-acceptance.json` 汇总。缺少 QtTest 输出、CTest 截断标志、汇总与明细不一致、失败、blacklisted 或必需用例未执行，都会使原生 CI 测试阶段失败，也会阻断发布验收。测试运行器将捕获上限设为 16 MiB，超过限制时仍必须重新取得完整证据。非 QtTest 的语义程序和 Python workflow 测试单独按其报告处理。

目前唯一的 QtTest 平台不适用分类是 Windows 上 `IndexRepositoryRegressionTest::rootFilterRespectsCaseSensitivePlatformKeys()`，该用例验证非 Windows 的大小写敏感路径契约。分类同时匹配平台、CTest 名称、完整用例名和固定原因，仍记录为 `notApplicable`，不计入通过数。Windows 账户无法创建符号链接导致 `FileCatalogTest::symbolicLinksCannotEscapeOrLoop()` 跳过时，记录为 `requiredNotRun` 并阻断发布。需要在具备创建符号链接能力的测试环境执行该用例，不能把权限限制改记为通过。

`qcs-tests-workflow` 的三个 POSIX 布局清理用例有单独的精确规则：`test_clean_unlinks_internal_framework_and_versioned_library_links`、`test_clean_rejects_external_symlink_without_deleting_any_target` 和 `test_clean_rejects_linked_top_level_target`。仅在 Windows 且原始原因完全匹配 `Windows symbolic-link creation privilege unavailable (WinError 1314), native macOS/Linux execution required` 时，记录为 `notApplicable` 并保留原始 skip，不能计为 PASS。Linux 和两种 macOS 架构必须实际通过这三个用例，缺失、跳过或失败均阻断各自原生门槛。

Windows 必须实际通过 `test_clean_rejects_windows_junction_inside_generated_tree`，验证当前平台的真实重定向边界。该单一用例只有在 Linux / macOS 且原因完全匹配 `Windows junction semantics require Windows` 时才能记为不适用。以上规则不扩大到其他符号链接、清理测试或权限错误，完整覆盖仍需四组合原生证据共同闭合。

每个 QtTest 日志还必须有匹配的开始与结束记录、非零实际执行数，并且逐条 PASS / FAIL / SKIP 等记录与汇总一致。只保留 `Totals` 行或截断后的日志不能作为完整通过证据。

CI 在全量测试开始前保存 `ctest --show-only=json-v1` 的注册清单，写入 `reports/tests/ctest-inventory.json`。报告中的程序名称必须与该清单完全一致，缺失、重复或额外程序都会使验收失败。开发者使用 `--test-label` 或 `ctest -R` 得到的子集报告只能说明所选范围，不得称为整套测试完成。

测试清理只允许作用于本次创建且边界校验通过的根。先结束本次启动的子进程，再释放文件句柄并清理数据。不能终止正在运行的正式应用来让测试通过。

显式收尾检查必须将清理失败转为测试失败，不能只在析构中输出警告。创建失败、写入失败、文件被占用、目录替换、路径越界、重复清理和异常进程退出均属于必需回归范围。保留失败产物时，报告给出实际根、失败原因和保留策略，不把主动保留误报为清理成功。

## 最终候选门槛

本地完整自动化、四组合 GitHub Actions 和 Windows Computer Use 分别验收。四组合的构建、测试、真实模型、打包、包验证及汇总 job 必须实际结束为 success，启动工作流不代表完成。本机执行同一候选最终 Windows 包的十个用户操作场景，并继续十万物理文件和百万向量验收。

所有结果绑定执行前记录的源码身份、依赖锁和实际程序或包哈希。旧长命名候选的报告已退出本轮验收，不通过重命名复用。只有新候选的全部必需门槛通过才能标记交付完成。
