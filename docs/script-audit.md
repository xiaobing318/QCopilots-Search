# 脚本审查与维护清单

本清单覆盖仓库自有 Python、PowerShell、Shell、CMake 模块及其注册入口。第三方源码、下载缓存、生成脚本和外置历史副本不计入。审查同时核对静态调用、动态导入、CMake、CI、测试聚合器和文档入口，存在调用方不等于实际执行通过。

## 清理结论

审查基线为 79 个 Python / PowerShell / Shell 脚本、21 个 `.cmake` 模块及全部 CMake 注册。本轮删除 18 个独立研究和专项验收脚本，再将候选身份、文件稳定性和材料完整性迁入统一 check 与发行准备，删除 3 个重复阶段脚本。保留的功能媒体迁入 `tests/acceptance/fixtures`，继续保留来源、许可和哈希。

被删除的范围包括独立的大规模向量与物理文件验收、专项延迟测试、相关性校准及策略证据，以及重复的阶段式打包。必要的小规模增删改、过滤、事务、快照、取消和恢复继续由普通功能测试覆盖。源码树之外的历史构建、模型、SDK 和用户配置不随脚本删除清理。

Qt / NSIS bootstrap、许可证、源码材料、部署、路径预算、临时根、进程管理和测试运行器保留实际职责。GPU SDK 与 ABI、变体配置、包身份和 Windows 安装器兜底只增加必要适配。普通打包只生成用户包，symbols 和 sources 为独立动作。

下表按当前源树逐文件登记，新增或删除脚本时应同步更新。数量由下面列出的实际文件计算，不把原定精简数量当作包含新增 GPU 工具后的最终数字。

当前登记 **67 个 Python / PowerShell / Shell 脚本**和 **24 个 `.cmake` 模块**。

## 开发、CI 与运行器脚本

| 文件 | 保留职责与接入方式 |
| --- | --- |
| `.github/scripts/qcs-artifact-selection.py` | 按 run、原始 job、attempt 和 artifact 身份选择成品 |
| `.github/scripts/qcs-ctest-evidence.py` | 核验 CTest、QtTest 与 Python unittest 逐用例记录 |
| `.github/scripts/qcs-deb-ci.py` | Linux DEB 的容器安装、包内运行及卸载验证 |
| `.github/scripts/qcs-native-ci.py` | 原生 runner 适配、完整构建验包及最终产物白名单 |
| `.github/scripts/qcs-tests-artifact-selection.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `.github/scripts/qcs-tests-ci-evidence.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `.github/scripts/qcs-tests-deb-ci.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `.github/scripts/qcs-tests-release-evidence.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `qcs-dev.ps1` | Windows MSVC 环境与根开发入口，调用公共 Python 驱动 |
| `qcs-dev.sh` | POSIX 根开发入口，调用公共 Python 驱动 |
| `tests/runtime/configure_idempotency_test.py` | 重复配置、补丁及生成内容的稳定性回归 |
| `tests/runtime/gpu_configuration_test.py` | GPU 变体配置、后端选择和构建参数回归 |
| `tests/runtime/install_layout_test.py` | 跨平台最终推理安装布局回归入口 |
| `tests/runtime/linux_deploy_test.py` | Linux 部署依赖与布局回归，由安装布局测试调用 |
| `tests/runtime/macos_deploy_test.py` | macOS bundle 依赖与布局回归，由安装布局测试调用 |
| `tests/runtime/output_paths_test.py` | 直接 CMake 生成位置必须在源码树外的边界回归 |
| `tests/runtime/source_integrity_test.py` | 锁定源码归档、补丁、版本和完整性回归 |
| `tests/support/temp_paths.py` | Python 测试共用临时根、归属及清理实现 |
| `tests/support/temp_paths_test.py` | 临时根创建、空值、路径与清理失败回归 |
| `tools/acceptance/qcs-candidate-ctest-run.py` | 冻结候选的完整 CTest、程序字节及输入稳定性 |
| `tools/acceptance/qcs-copy-cu-fixtures.py` | 生成、计划和复制独立 CU 夹具，核对来源与哈希 |
| `tools/acceptance/qcs-cu-recorder-tests.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-cu-recorder.py` | 记录实际 Windows 操作、截图、时间与候选包身份 |
| `tools/acceptance/qcs-cu-release-integration-test.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-launch-portability-sentinel.py` | 启动受控普通应用哨兵并记录实例身份 |
| `tools/acceptance/qcs-portability-sentinel.py` | 验证并行测试没有修改或终止受控普通实例 |
| `tools/acceptance/qcs-test-candidate-runner.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-test-copy-cu-fixtures.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-test-diagnostics.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-test-portability-runners.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-test-runners.py` | 聚合保留验收运行器用例，完整 CTest 执行全部能力分组 |
| `tools/acceptance/qcs-test-source-checksums.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-test-token-context.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/acceptance/qcs-token-context.py` | 受控 Windows 权限测试子进程上下文 |
| `tools/acceptance/qcs-two-checkout-portability.py` | 真实只读源码、双 checkout 并行及隔离验证 |
| `tools/acceptance/qcs_owned_processes.py` | 限定本次创建的 Windows 子进程生命周期与清理 |
| `tools/qcs-bootstrap-nsis.py` | 准备 Windows 安装器工具链，供打包与安装验证使用 |
| `tools/qcs-bootstrap-qt.py` | 下载并校验锁定 Qt SDK，供 bootstrap 使用 |
| `tools/qcs-dev.py` | 统一变体、外置目录、完整 check、独立构建测试及发行命令 |
| `tools/qcs-gpu-abi.py` | 核验独立 GPU 模块与主构建的 ABI、导入导出及依赖 |
| `tools/qcs-gpu-runtime.py` | 从 SDK 构造 GPU 运行依赖闭包与 RDNA4 数据布局，缺依赖、许可或资源明确失败 |
| `tools/qcs-gpu-sdk.py` | 按锁定上游配方准备 GPU SDK、后端构建和运行资源 |
| `tools/qcs-gpu-tests.py` | 无 GPU、网络、模型或真实 SDK 的部署契约回归，验证依赖闭包、资源和 ABI 失败边界 |
| `tools/qcs-license-materials.py` | 收集并校验实际部署依赖许可和 notices |
| `tools/qcs-msvc-response.py` | MSVC response file 编码与长路径适配 |
| `tools/qcs-package-symbols.py` | 独立生成符号材料，不由普通 package 自动调用 |
| `tools/qcs-package-windows-tests.py` | 安装器兜底选择、载荷归属、归档身份及真实离线安装卸载回归 |
| `tools/qcs-package-windows.py` | Windows 安装包编排及 ROCm 超限载荷兜底 |
| `tools/qcs-path-budget.py` | 生成路径、对象和打包位置的长度预算诊断 |
| `tools/qcs-release-evidence.py` | 真实构建、最终 Windows 包与 Computer Use 的候选身份和附件验证 |
| `tools/qcs-release.py` | 按 CI run/attempt 下载、验证与冻结成品，显式上传草稿 |
| `tools/qcs-sign-macos.py` | 最终 Mach-O 与 bundle 从内到外签名和验证 |
| `tools/qcs-source-bundle.py` | 从同一干净提交和依赖锁生成平台无关对应源码材料 |
| `tools/qcs-test-check.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-license-materials.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-macos-links.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-macos-signing.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-package-variants.py` | 11 变体的包身份、包类型及后端契约回归，不加载 GPU SDK |
| `tools/qcs-test-path-budget.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-policy-tests.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-policy.py` | 核验注册清单、证据类型、完整执行和局部标签筛选 |
| `tools/qcs-test-release.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-runner-tests.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-test-runner.py` | 为每次 CTest 调用分配独立 cwd，收集结果并严格清理 |
| `tools/qcs-test-workflow.py` | 保留对应工具的契约、失败边界与回归，通过 CTest 或验收测试聚合器接入 |
| `tools/qcs-verify-macos-links.py` | 核验最终 APP 动态库引用和部署来源 |
| `tools/qcs-verify-package.py` | 最终包重定位、真实模型、服务、依赖与许可证验证 |

## CMake 模块

| 文件 | 保留职责与接入方式 |
| --- | --- |
| `cmake/qcs-checked-deploy.cmake` | 部署前置条件及失败传播 |
| `cmake/qcs-cpack.cmake` | 生成器专属打包配置 |
| `cmake/qcs-dev-deploy.cmake` | 开发构建的运行依赖部署 |
| `cmake/qcs-ffmpeg.cmake` | 锁定 Qt 配套 FFmpeg 的导入与 ABI 校验 |
| `cmake/qcs-gpu-backend.cmake` | 独立 GPU backend 子构建与主程序集成 |
| `cmake/qcs-gpu-variants.cmake` | 读取共同变体配置并绑定 CMake 后端参数 |
| `cmake/qcs-licenses.cmake` | 许可生成、安装和对应测试注册 |
| `cmake/qcs-linux-deploy.cmake` | Linux 运行库部署与依赖来源 |
| `cmake/qcs-macos-deploy.cmake` | macOS 推理与 Qt 依赖布局 |
| `cmake/qcs-msvc-rsp.cmake` | 接入 MSVC response file 适配器 |
| `cmake/qcs-native-data.cmake` | OCR 数据下载、校验与安装 |
| `cmake/qcs-native-deps.cmake` | 锁定原生依赖与外置源码构建 |
| `cmake/qcs-native-doc.cmake` | 旧 DOC 提取依赖适配 |
| `cmake/qcs-native-git-patch.cmake` | 确定性上游补丁与重复配置稳定性 |
| `cmake/qcs-native-images.cmake` | 图像解码依赖与选项 |
| `cmake/qcs-native-ocr.cmake` | OCR 编译依赖与选项 |
| `cmake/qcs-output-paths.cmake` | 配置、安装与打包的外置边界检查 |
| `cmake/qcs-package-identity.cmake` | 生成变体、版本和候选包身份 |
| `cmake/qcs-package.cmake` | 各平台 install、部署与 CPack 注册 |
| `cmake/qcs-symbols.cmake` | 程序符号与 manifest 生成注册 |
| `cmake/qcs-write-if-different.cmake` | 仅内容变化时写生成文件，避免无修改重编译 |
| `src/runtime/install_inference_runtime.cmake` | 安装规范推理库与 CPU/GPU 模块 |
| `src/runtime/verify_llama_source.cmake` | 校验 llama.cpp 提交、归档和项目补丁 |
| `tests/service/client-tests.cmake` | 服务客户端测试注册 |

## 注册与后续维护

所有 `CMakeLists.txt`、`CMakePresets.json`、工作流、NSIS 模板和共同变体清单参与审查，不重复计入脚本数量。CTest 默认执行全部保留程序，纯协议与原生能力分组均纳入完整流程。局部标签筛选、工具合成数据与未执行的设备测试分别记录，不能冒充完整验收。

增加脚本前先检查现有统一入口是否可复用。删除时同步核对动态 import、CMake include、CPack、CI、聚合测试和文档，不能留下失效注册。共享 helper 变更必须更新来源与哈希验证，不让候选报告绑定已删除的副本。

CI artifact 仅上传已验证用户包，汇总检查 11 个变体和 21 个文件。发行工具保留原始 API 来源与选择结果，重新核验 run、attempt、job、artifact ID 和 digest。最新失败不能借用旧成功产物，冻结后字节变化必须失败。

此清单属于代码审查记录，实际构建、测试、GPU 设备与最终包状态另见同一候选的执行报告。
