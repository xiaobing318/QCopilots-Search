# 脚本审查与维护清单

本清单覆盖应用仓库自有的 Python、PowerShell、Shell 和 CMake 脚本，核对开发入口、CMake、CI、测试注册、动态导入与文档化专项入口。第三方源码、下载缓存、生成脚本和外置历史副本不计入。静态引用证明脚本有调用方，实际测试结果仍以对应候选的报告为准。

本轮审查基线为 **66 个 Python / PowerShell / Shell 脚本和 19 个 `.cmake` 脚本**。删除两个仅转发参数的安装包 PowerShell 包装，保留其 Python 实现。新增外置输出根、配置幂等、流程编排、测试策略、语义校准与发布工具共 9 个已接入脚本，以及 2 个 CMake 模块。另有 2 个已落盘但尚未注册验收的第二版语义校准脚本。当前实际源树为 **75 个 Python / PowerShell / Shell 脚本和 21 个 `.cmake` 脚本**，其中 73 个脚本已接入现有流程，2 个仍在开发。所有 `CMakeLists.txt`、`CMakePresets.json`、原生 Actions 工作流、桌面资源和 NSIS 模板也参与调用关系审查，但不重复计入这些数量。

| 脚本分组 | 审查基线 | 当前文件数 |
| --- | --- | --- |
| 根目录入口 | 2 | 2 |
| `tools/` 根目录 | 19 | 24 |
| `.github/scripts/` | 5 | 5 |
| `tests/` | 6 | 12 |
| `tools/acceptance/` | 34 | 32 |
| 合计，不含 `.cmake` | 66 | 75 |

## 审查结论

- 开发主入口为根目录的 `qcs-dev.ps1` 和 `qcs-dev.sh`，公共行为由 `tools/qcs-dev.py` 实现。Windows 包装负责 MSVC 环境与 Python 发现，POSIX 包装负责保留参数边界，两者有明确职责。
- 删除 `tools/acceptance/qcs-package-phase1.ps1` 和 `qcs-package-phase2.ps1`。它们没有独立业务逻辑，原调用方改用同目录 Python 实现。阶段一负责二进制包，阶段二负责对应源码材料，两阶段不能合并为一次未经核验的成功声明。
- `qcs-two-checkout-portability.py` 与 `qcs-service-latency.py` 共用 `qcs_owned_processes.py` 的 `WindowsProcesses`，消除前者内嵌的重复类。运行器继续保存 helper 原始文件、大小、SHA-256 和执行结束后的稳定性校验。
- 其余基线脚本都有实际入口、共享调用或动态测试注册，予以保留。脚本数量本身不是删减目标，不能仅凭找不到完整文件名的静态字符串就删除动态导入的测试。
- 所有开发、测试、依赖、安装暂存和打包产物继续位于源码树外。直接 CMake 使用同一输出边界检查，避免绕过开发脚本时在源码目录生成文件。

## 开发与发行工具

下表路径相对于仓库根。引用列使用调用文件和关键行为，避免后续编辑使行号失效。发布材料冻结与草稿上传的参数说明见 [发布命令](release-command.md)。

| 脚本 | 决定与职责 | 实际引用证据 |
| --- | --- | --- |
| `qcs-dev.ps1` | 保留，Windows 工具链初始化及参数入口 | `README.md`、`docs/development.md`，内部执行 `tools/qcs-dev.py` |
| `qcs-dev.sh` | 保留，POSIX 参数入口 | `README.md`、`docs/development.md`，内部执行 `tools/qcs-dev.py` |
| `tools/qcs-dev.py` | 保留，统一开发工作流与生成目录归属 | 两个根包装入口，`.github/scripts/qcs-native-ci.py` 的 `developer_workflow()` |
| `tools/qcs-bootstrap-qt.py` | 保留，锁定 Qt SDK 下载、校验与安装 | `tools/qcs-dev.py` 的 `bootstrap` 动作 |
| `tools/qcs-bootstrap-nsis.py` | 保留，按需准备 Windows 安装器工具链 | `tools/qcs-dev.py` 的 `package` 动作，仅默认生成器或显式包含 NSIS 时执行 |
| `tools/qcs-license-materials.py` | 保留，许可与 SBOM 材料生成和校验 | `cmake/qcs-licenses.cmake`，`tools/qcs-verify-package.py` |
| `tools/qcs-msvc-response.py` | 保留，MSVC response file 与路径适配 | `cmake/qcs-msvc-rsp.cmake` 的 linker launcher 与静态库命令模板 |
| `tools/qcs-package-symbols.py` | 保留，符号包与 manifest | `tools/qcs-dev.py` 的 `package` 动作，`tools/acceptance/qcs-package-phase1.py` |
| `tools/qcs-path-budget.py` | 保留，生成路径长度诊断 | `tools/qcs-dev.py` 的配置、doctor 与打包路径检查 |
| `tools/qcs-release-evidence.py` | 保留，候选来源与必需证据验证 | `.github/workflows/qcs-native.yml`、原生 CI 和候选验收运行器 |
| `tools/qcs-release.py` | 新增，校验并冻结完整候选材料，显式上传未公开的 GitHub 草稿 | `tools/qcs-dev.py` 的 `release-prepare`、`release-upload-draft` 动作分别调用 `prepare`、`upload-draft` |
| `tools/qcs-test-release.py` | 新增，发布材料、草稿上传与拒绝边界的合成回归 | 顶层 `CMakeLists.txt` 注册 `qcs-tests-release-tool`，测试动态加载 `qcs-release.py` |
| `tools/qcs-source-bundle.py` | 保留，对应源码材料生成与内容校验 | `.github/scripts/qcs-native-ci.py` 的 `sources` 阶段，`tools/acceptance/qcs-package-phase2.py` |
| `tools/qcs-sign-macos.py` | 保留，最终 APP 内 Mach-O 与 bundle 签名 | `cmake/qcs-package.cmake` 的安装部署脚本 |
| `tools/qcs-verify-macos-links.py` | 保留，最终 APP 链接布局及来源验证 | `cmake/qcs-package.cmake` 的安装部署脚本 |
| `tools/qcs-verify-package.py` | 保留，重定位后的程序、模型与安装验证 | `tools/qcs-dev.py` 的 `verify-package` 动作和原生 CI |
| `tools/qcs-test-runner.py` | 保留，CTest 独立工作目录与清理 | `tests/CMakeLists.txt` 的 `qcopilots_search_add_test()` |
| `tools/qcs-test-runner-tests.py` | 保留，测试运行器回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-runner` |
| `tools/qcs-test-path-budget.py` | 保留，路径预算回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-path-budget` |
| `tools/qcs-test-policy.py` | 新增，从实际 CTest 清单选择 fast/full 范围并验证证据类型 | `.github/scripts/qcs-ctest-evidence.py` 与 `qcs-native-ci.py` 动态加载，调用 `select_inventory()` 或 `describe_inventory()` |
| `tools/qcs-test-policy-tests.py` | 新增，测试选择、标签和清单完整性回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-test-policy` |
| `tools/qcs-test-license-materials.py` | 保留，许可材料与拒绝边界回归 | `cmake/qcs-licenses.cmake` 注册 `qcs-tests-license-materials` |
| `tools/qcs-test-macos-signing.py` | 保留，签名流程回归 | 顶层 `CMakeLists.txt` 注册 `qcs-tests-macos-signing` |
| `tools/qcs-test-macos-links.py` | 保留，链接部署回归 | `tools/qcs-test-macos-signing.py` 的 `load_tests()` 动态加载 |
| `tools/qcs-test-workflow.py` | 保留，开发、打包和安全清理回归 | 顶层 `CMakeLists.txt` 注册 `qcs-tests-workflow` |
| `tools/qcs-test-check.py` | 新增，fast/full `check` 编排、证据绑定和失败输出回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-check`，动态加载 `qcs-dev.py` 与 `qcs-release-evidence.py` |

## CI 与运行时测试工具

| 脚本 | 决定与职责 | 实际引用证据 |
| --- | --- | --- |
| `.github/scripts/qcs-native-ci.py` | 保留，原生 runner 适配与阶段编排 | `.github/workflows/qcs-native.yml` 的平台 job |
| `.github/scripts/qcs-ctest-evidence.py` | 保留，CTest 及逐用例证据完整性 | `qcs-native-ci.py`、`tools/qcs-release-evidence.py`、候选 CTest 运行器 |
| `.github/scripts/qcs-deb-ci.py` | 保留，Ubuntu 容器安装与卸载验证 | `qcs-native-ci.py` 的 Linux 安装器阶段 |
| `.github/scripts/qcs-tests-ci-evidence.py` | 保留，CI 范围与日志验证回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-ci-evidence`，由本地和 CI 的 CTest 流程执行 |
| `.github/scripts/qcs-tests-release-evidence.py` | 保留，发行证据验证回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-release-evidence`，由本地和 CI 的 CTest 流程执行 |
| `tests/runtime/install_layout_test.py` | 保留，安装布局与平台部署测试入口 | `tests/runtime/CMakeLists.txt` 注册 `qcs-tests-inference-install-layout` |
| `tests/runtime/linux_deploy_test.py` | 保留，Linux 部署布局回归 | `install_layout_test.py` 按平台 `import linux_deploy_test` |
| `tests/runtime/macos_deploy_test.py` | 保留，macOS 部署布局回归 | `install_layout_test.py` 按平台 `import macos_deploy_test` |
| `tests/runtime/source_integrity_test.py` | 保留，锁定源码、归档及补丁完整性 | `qcs-native-ci.py` 的 `sources` 阶段 |
| `tests/support/temp_paths.py` | 保留，测试临时根共享实现 | Python 工具与运行时测试导入 `temp_paths` |
| `tests/support/temp_paths_test.py` | 保留，临时根生命周期回归 | `tests/CMakeLists.txt` 注册 `qcs-tests-temp-paths` |
| `tests/runtime/output_paths_test.py` | 新增，直接 CMake 外置输出边界回归 | `tests/runtime/CMakeLists.txt` 注册 `qcs-tests-output-paths` |
| `tests/runtime/configure_idempotency_test.py` | 新增，重复配置与 libgit2 补丁准备的内容、时间戳稳定性 | `tests/runtime/CMakeLists.txt` 注册 `qcs-tests-configure-idempotency`，传入实际 CMake 和 libgit2 源码目录 |
| `tests/semantic/calibrate_relevance.py` | 新增，从冻结的真实语义报告校准模态阈值，独立校验留出集 | `tests/semantic/README.md` 的显式 Python 校准命令，`test_calibrate_relevance.py` 动态导入 |
| `tests/semantic/test_calibrate_relevance.py` | 新增，校准输入、分组隔离、搜索及策略输出回归 | `tests/semantic/CMakeLists.txt` 注册 `qcs-tests-relevance-calibration` |
| `tests/semantic/calibrate_relevance_v2.py` | 开发中，分组交叉验证与新的独立留出校准，尚未验收 | 已有独立 `main()` 入口并复用 v1 helper，尚未接入 CTest 或冻结候选流程 |
| `tests/semantic/test_calibrate_relevance_v2.py` | 开发中，第二版校准协议、数据隔离及搜索回归，尚未注册 | 动态导入同目录 `calibrate_relevance_v2.py`，尚未接入 CTest |

## 专项验收运行器

这些脚本位于 `tools/acceptance/`。下表中的专项入口通过 [验收运行器说明](../tools/acceptance/README.md) 接入最终候选流程。运行器自身的合约测试不能代替真实模型、容量、双 checkout 或 Computer Use 验收。

| 脚本 | 决定与职责 | 实际引用证据 |
| --- | --- | --- |
| `qcs-candidate-ctest-run.py` | 保留，完整候选 CTest 与身份验证 | 验收 README 的“完整本机 CTest”，`qcs-test-candidate-runner.py` |
| `qcs-package-phase1.py` | 保留，构建证明、安装包及包级验证 | 验收 README 的“安装包阶段一”，`qcs-package-phase2.py` 导入共享验证 |
| `qcs-package-phase1.ps1` | 删除，仅转发 Python 参数 | 原唯一生产引用为验收 README，已改为 `.py` 入口 |
| `qcs-package-phase2.py` | 保留，对应源码及阶段一材料闭合 | 验收 README 的“安装包阶段二”，`qcs-test-package-runners.py` |
| `qcs-package-phase2.ps1` | 删除，仅转发 Python 参数 | 原唯一生产引用为验收 README，已改为 `.py` 入口 |
| `qcs-run-million.ps1` | 保留，百万向量监视、预算及收尾 | `docs/releasing.md` 的 `run-local`，验收 README 与容量回归 |
| `qcs-run-physical.ps1` | 保留，十万物理文件的候选验证与执行 | 验收 README 的“十万物理文件”，`qcs-test-capacity-runners.py` |
| `qcs-physical-preflight.py` | 保留，容量候选与构建材料共享验证 | `qcs-run-million.ps1`、`qcs-run-physical.ps1` |
| `qcs-service-latency.py` | 保留，服务延迟专项 | 验收 README 的“服务端延迟”，`qcs-test-latency-preparation.py` |
| `qcs-launch-portability-sentinel.py` | 保留，启动受控普通窗口哨兵 | 验收 README 的“普通窗口哨兵”，`qcs-test-portability-runners.py` |
| `qcs-portability-sentinel.py` | 保留，应用实例与隔离观察 | `qcs-launch-portability-sentinel.py`，双 checkout 运行器 |
| `qcs-two-checkout-portability.py` | 保留，合并重复进程管理实现 | 验收 README 的“双 checkout 与只读源码”，导入 `qcs_owned_processes.WindowsProcesses` |
| `qcs_owned_processes.py` | 保留，唯一的自有 Windows 进程管理实现 | 服务延迟、双 checkout 与 `qcs-token-context.py` |
| `qcs-token-context.py` | 保留，受控 Windows 令牌上下文 | 聚合测试、可移植性测试与双 checkout 的 `reexec_if_needed()` |
| `qcs-cu-recorder.py` | 保留，Computer Use 原始操作与材料记录 | 验收 README 的 `init/begin/observe/finish/summarize` 流程，CU 回归 |
| `qcs-copy-cu-fixtures.py` | 保留，按哈希计划复制独立 CU 样本 | 验收 README 的 `--create-plan` 和 `--execute` 流程，fixture 回归 |

`qcs-service-latency.py` 的输入校验可跨平台执行，当前真实运行依赖 Windows 原生进程管理。不能因脚本可在其他平台导入，就把实际延迟验收记为已支持或已完成。

## 专项验收测试

下列 18 个脚本同样位于 `tools/acceptance/`。`qcs-test-runners.py` 使用同一 `SCRIPTS` 注册表和 `RecorderIntegration`，通过 `build_suite()` 将用例分为 `pure` 与 `native`。CTest 的 `qcs-tests-acceptance-runners` 传入 `--suite pure`，带 `slow` 标签的 `qcs-tests-acceptance-native` 传入 `--suite native`。后者包含跨平台超时回归及按实际平台能力执行的原生用例。独立执行默认 `--suite all`。实际 fast/full 范围以 [测试指南](testing.md) 和 CTest 注册为准。

| 脚本 | 决定与职责 | 实际引用证据 |
| --- | --- | --- |
| `qcs-test-runners.py` | 保留，聚合注册、pure/native 分组与逐例输出 | `tests/CMakeLists.txt` 的 `qcs-tests-acceptance-runners` 与 `qcs-tests-acceptance-native` |
| `qcs-test-candidate-runner.py` | 保留，候选身份及完整范围拒绝回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-source-checksums.py` | 保留，源码材料与 FFmpeg 元数据回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-capacity-runners.py` | 保留，容量前置验证与原生守卫回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-million-evidence.py` | 保留，百万向量证据完整性 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-package-runners.py` | 保留，两个包阶段的候选与材料契约 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-portability-runners.py` | 保留，路径、ACL、恢复与隔离回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-diagnostics.py` | 保留，聚合日志、失败与中断传播 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-token-context.py` | 保留，令牌隔离及共享进程 helper 回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-latency-preparation.py` | 保留，延迟运行器输入与输出边界 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-cu-recorder-tests.py` | 保留，CU 记录与附件绑定回归 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-test-copy-cu-fixtures.py` | 保留，样本计划、哈希与目标边界 | `qcs-test-runners.py` 的 `SCRIPTS` 注册 |
| `qcs-cu-release-integration-test.py` | 保留，合成 CU 记录导入的集成回归 | `qcs-test-runners.py` 的 `RecorderIntegration` 动态加载 |
| `qcs-test-million-guard-array.ps1` | 保留，PowerShell 参数数组回归 | `qcs-test-capacity-runners.py` 动态生成 `guard-array` 文件名 |
| `qcs-test-million-guard-contract.ps1` | 保留，监视器契约回归 | `qcs-test-capacity-runners.py` 动态生成 `guard-contract` 文件名 |
| `qcs-test-million-guard-evidence.ps1` | 保留，监视证据回归 | `qcs-test-capacity-runners.py` 动态生成 `guard-evidence` 文件名 |
| `qcs-test-million-guard-process.ps1` | 保留，自有进程清理回归 | `qcs-test-capacity-runners.py` 动态生成 `guard-process` 文件名 |
| `qcs-test-million-report-root.ps1` | 保留，报告根归属与边界回归 | `qcs-test-capacity-runners.py` 的 `report-root` 分支 |

## CMake 脚本

| 脚本 | 决定与职责 | 实际引用证据 |
| --- | --- | --- |
| `cmake/qcs-msvc-rsp.cmake` | 保留，MSVC 链接与静态库 response file 适配 | 顶层 `CMakeLists.txt` 的 `include(qcs-msvc-rsp)` |
| `cmake/qcs-native-deps.cmake` | 保留，锁定依赖与外置源码构建 | 顶层 `CMakeLists.txt` 的 `include(qcs-native-deps)` |
| `cmake/qcs-native-git-patch.cmake` | 保留，确定性上游补丁 | `qcs-native-deps.cmake` 的补丁函数 |
| `cmake/qcs-native-ocr.cmake` | 保留，OCR 依赖适配 | `qcs-native-deps.cmake` 的 `include` |
| `cmake/qcs-native-images.cmake` | 保留，图像依赖适配 | `qcs-native-deps.cmake` 的 `include` |
| `cmake/qcs-native-data.cmake` | 保留，OCR 数据锁定与安装 | `qcs-native-deps.cmake` 的 `include` |
| `cmake/qcs-native-doc.cmake` | 保留，旧 DOC 依赖适配 | `qcs-native-deps.cmake` 的 `include` |
| `cmake/qcs-ffmpeg.cmake` | 保留，Qt 配套 FFmpeg 验证与导入 | 顶层 `CMakeLists.txt` 的 `include(qcs-ffmpeg)` |
| `cmake/qcs-symbols.cmake` | 保留，程序与符号 manifest | 顶层 `CMakeLists.txt` 的 `include(qcs-symbols)` |
| `cmake/qcs-dev-deploy.cmake` | 保留，开发运行库布局 | 顶层 `CMakeLists.txt` 的 `include(qcs-dev-deploy)` |
| `cmake/qcs-package.cmake` | 保留，安装布局与各平台包 | 顶层 `CMakeLists.txt` 的 `include(qcs-package)` |
| `cmake/qcs-licenses.cmake` | 保留，许可材料与安装规则 | `qcs-package.cmake` 的 `include(qcs-licenses)` |
| `cmake/qcs-macos-deploy.cmake` | 保留，macOS 推理运行库布局 | `qcs-package.cmake` 的平台 `include` |
| `cmake/qcs-linux-deploy.cmake` | 保留，Linux 运行库布局 | `qcs-package.cmake` 的平台 `include` |
| `cmake/qcs-checked-deploy.cmake` | 保留，部署失败检测及前置校验 | `qcs-package.cmake` 写入安装期脚本的 `include` |
| `cmake/qcs-cpack.cmake` | 保留，生成器相关包配置 | `qcs-package.cmake` 的 `CPACK_PROJECT_CONFIG_FILE` |
| `src/runtime/verify_llama_source.cmake` | 保留，llama.cpp 锁定源码验证 | `src/runtime/CMakeLists.txt` 的 `include` |
| `src/runtime/install_inference_runtime.cmake` | 保留，推理库及 CPU 模块安装 | `src/runtime/CMakeLists.txt` 与 `qcs-macos-deploy.cmake` 的 `include` |
| `tests/service/client-tests.cmake` | 保留，服务客户端用例注册 | `tests/service/CMakeLists.txt` 的 `include` |
| `cmake/qcs-output-paths.cmake` | 新增，直接 CMake 的外置输出边界 | 顶层、原生依赖、llama 和许可配置复用边界函数，安装期和 `qcs-cpack.cmake` 运行期再次 `include` 并校验有效输出路径 |
| `cmake/qcs-write-if-different.cmake` | 新增，仅内容变化时写入配置生成文件，避免无改动重编译 | `qcs-native-deps.cmake`、`qcs-native-git-patch.cmake`、`qcs-native-ocr.cmake`、`qcs-native-doc.cmake` 与 `qcs-ffmpeg.cmake` 的 `include` 及 `qcopilots_search_write_if_different()` 调用 |

## 后续维护规则

第二版语义校准仍在开发。`calibrate_relevance_v2.py` 和配套 `test_calibrate_relevance_v2.py` 已落盘并计入当前数量，尚待注册与候选验收。相关第二版语料 JSON、资产和许可清单是数据材料，不计作脚本。新增或接入脚本后须同步调用关系和计数，注册与验收前不能标记为已完成。

增加脚本前先确认已有入口是否可以复用。删除时核对动态 `import`、CMake `include`、CPack 配置、工作流、专项验收和文档入口。共享 helper 变更后同步来源捕获与哈希验证，不能让报告继续绑定已经移除的重复实现。

GUI target、程序清单和包内发现逻辑使用 `qcs-gui`。包名前缀、其他程序、profile、测试名称和配置变量继续保留 `qcs`，禁止全仓替换产品常量来完成 GUI 改名。任何入口收敛都必须保留完整候选、实际执行范围、失败状态和原始证据验证。
