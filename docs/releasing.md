# 构建、安装包与发布

本项目在开发分支形成可审查的候选，完成测试后再由发布负责人决定合并和公开发布。普通构建只生成当前变体的用户包，发布准备自动获取 CI 成品并冻结来源与字节。

## 固定构建矩阵

平台、后端、SDK 和产物规则由 `config/build-variants.json` 统一提供，开发入口、CI、验包和发布工具共同读取。正常完整运行包含 11 个构建条目、11 个 package artifacts 和 21 个用户文件。

| 变体 | 后端与 SDK | 系统基线 | 用户文件 |
| --- | --- | --- | --- |
| `windows-x64-cpu` | CPU ALL | Windows x64 / MSVC 2022 | EXE + portable ZIP |
| `windows-x64-vulkan` | Vulkan + CPU，上游 Vulkan 配方 | Windows x64 / MSVC 2022 | EXE + portable ZIP |
| `windows-x64-rocm` | HIP + CPU，ROCm 10.0.0，gfx1200、gfx1201 | Windows x64 / MSVC 主程序 | EXE + portable ZIP |
| `windows-x64-cuda-12.4` | CUDA 12.4 + CPU | Windows x64 / MSVC 主程序 | EXE + portable ZIP |
| `windows-x64-cuda-13.4` | CUDA 13.4 + CPU | Windows x64 / MSVC 主程序 | EXE + portable ZIP |
| `linux-x64-cpu` | CPU ALL | Ubuntu 22.04 | DEB + portable TGZ |
| `linux-x64-vulkan` | Vulkan + CPU，上游 Vulkan 配方 | Ubuntu 22.04 | DEB + portable TGZ |
| `linux-x64-rocm` | HIP + CPU，ROCm 10.0.0，gfx1200、gfx1201 | Ubuntu 24.04 | DEB + portable TGZ |
| `linux-x64-cuda-12.8` | CUDA 12.8.2 + CPU | Ubuntu 24.04 | DEB + portable TGZ |
| `linux-x64-cuda-13.4` | CUDA 13.4.1 + CPU | Ubuntu 24.04 | DEB + portable TGZ |
| `macos-arm64-metal` | Metal + CPU，内嵌 Metal library | macOS 14.4 / Apple Silicon | DMG |

llama.cpp 固定为 b11461，提交 `4d756bc72bf00a4aacf410ae15a2d315f3db400d`。CUDA 双版本、默认 GPU 架构和 Windows 独立后端编译参考该提交的 [上游发布流程](https://github.com/ggml-org/llama.cpp/blob/4d756bc72bf00a4aacf410ae15a2d315f3db400d/.github/workflows/release.yml)。Windows SDK setup actions 按完整 SHA 引用，其他步骤只作项目必要适配。ROCm 明确保留 RDNA4 两个目标，不因包体积悄悄减少。

## 部署与安装规则

保留 no-logits 和 Windows UTF-8 后端路径补丁。Windows 主程序使用 MSVC/Qt，CUDA/HIP 后端位于外置子构建，最终只部署主构建的一份 `ggml-base`。后端必须匹配同一上游版本、ABI 与运行库，不能从任意开发 SDK 或当前目录动态补齐。

CUDA 部署对应运行库，ROCm 部署 HIP、BLAS 和两个 RDNA4 目标实际需要的资源，Vulkan 只携带运行必需组件。模型经首次使用下载或离线导入后校验，不内置到各个安装包。实际许可证和 notices 随用户包部署。

CUDA DEB 保留 CPack 的共享库依赖分析。SDK 驱动 stub 仅以独立副本放入外置分析目录，供打包工具解析可选驱动，不能进入安装器或便携包。最终 DEB 检查安装内容和依赖字段，拒绝驱动 stub、驱动库及强制安装 NVIDIA 驱动或 CUDA SDK 的声明。正常系统依赖仍须保留，无驱动环境应能安装并使用 CPU，真实 GPU 能力另行验证。

Windows 默认单 EXE 安装器。若 ROCm 包实际触及 NSIS 单 EXE 容量限制，仅该安装器改为 `setup.zip`，内含小型 `setup.exe` 和完整 `payload/`。用户解压后离线安装，独立 portable ZIP 仍保留。安装器核对载荷清单、磁盘空间和路径，按清单安装与卸载，不能在线补齐缺失运行库。容量边界见 [NSIS 文档](https://nsis.sourceforge.io/Docs/Chapter1.html)。

ROCm 编译与打包分阶段进行，只提取目标资源。Windows 完整检查先生成 NSIS 安装器，再生成便携 ZIP。每个 CPack 生成器使用本次调用的独立工作目录，完成后只清理本次生成的受控 stage，避免多份完整载荷同时驻留，保留已有历史产物与用户安装 stage。所有平台卸载均保留用户模型、索引和配置。

Windows 沿用开发包签名条件。macOS 使用 ad-hoc，部署修改、strip 和链接校验完成后从内到外签名，最终执行严格验证。签名后不再修改 APP。ad-hoc 不等于 Developer ID 签名或公证，商业签名不属于本轮范围。

## CI 产物协议

CI 将源码检出到 `qcs-src`。每个变体顺序执行工具链准备、构建、全部保留测试、stage 安装、打包和最终包验证。受控解包、安装、搬移及验收 profile 路径使用英文且无空格的命名，仍需实际完成搬移后启动、安装卸载和隔离检查。GPU 开发运行也先生成完整 stage，避免裸构建目录依赖 SDK 环境补齐运行库。PR、主分支推送及手动运行使用同一流程。汇总 job 核对所有变体状态及用户文件，不下载大型测试报告重复聚合。

仅上传通过验证的安装器和便携包。artifact 名为 `qcs-packages-<variant>-<候选SHA>-attempt<N>`，用户文件包含变体、版本及候选身份。artifact 不包含日志、报告、截图、源码、符号、测试数据或独立校验和。失败诊断保留在 GitHub 原生日志，需要时通过 API 读取。

现有托管 runner 无 GPU 时完成所有适用测试、CPU 回退及依赖验证，明确记录硬件未实测。有设备时执行严格 GPU 模式，不能用 CPU 回退冒充设备通过。

## 发行准备与候选身份

`release-prepare` 接受 CI run ID、attempt 和完整候选 SHA，自动选择并下载 11 个 artifacts。它校验 workflow、run、原始 job 执行、attempt、artifact ID 和 digest，下载后验证包白名单及每个用户文件的 SHA-256。部分重跑可保留未重跑的原成功 job，最新失败不能借用历史成功产物。

工具从同一干净候选和依赖锁独立生成一份平台无关的对应源码材料。冻结输出包含用户包、源码包、校验和、发布 manifest，以及本地来源快照。CI 不承担源码材料上传。符号通过独立动作按需生成，不作为每次打包的附带产物。

正式交付顺序为同一干净候选的本机完整 `check` 和全部 11 变体 CI 通过，再执行 `release-prepare`。随后在本机验证冻结 manifest 中的 Windows CPU ZIP，以 `record-ci-package` 建立 CI 来源候选，使用该 ZIP 完成 CU-01 至 CU-10，最后执行 `validate-local --release-manifest`。发布准备成功仅证明 CI 成品获取和冻结通过，不能替代后续本机包验证与实际操作。相同提交的本地重建包不能替代 CI 包字节，也不能复用另一包的截图或报告。

显式 `release-upload-draft` 上传前再次核对冻结字节，只创建或续传草稿，不合并分支、不公开发布。每个 Release 附件必须小于 2 GiB，超限明确失败，本轮不提供附件分卷，也不裁剪 GPU 目标绕过限制。限制见 [GitHub Releases 文档](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

完整命令、输出布局和恢复规则见 [发布命令](release-command.md)。

## 本机最终证据流程

`record-build` 与 `record-package` 保留用于本机构建及开发包验证，绑定真实构建命令、源码身份和四个应用程序。正式交付使用 `record-ci-package --release-manifest <冻结清单> --verification <该CI包的本机验包报告>`。该入口自动选择冻结清单中的 Windows CPU portable ZIP，记录真实 run、attempt、job、artifact 与包来源，核对包内程序、完整验证报告及实际模型和提取指纹，不编造本地 build history。

Windows Computer Use 使用英文且无空格的独立运行根，从同一冻结 CI ZIP 的解包副本启动独立 profile，完成 CU-01 至 CU-10 并保留实际操作、截图、时间和包哈希。中文查询与中文、空格样本文件继续保留。`collect-local` 收集 CI 来源候选与 Computer Use 原始记录，按附件清单校验字节，不导入整个数据库目录。随后 `validate-local --release-manifest <同一冻结清单>` 精确交叉绑定 ZIP、四个程序、CI 来源和 CU 记录。正式通过要求 `scope: final-release-package`、`passed: true` 且 `localGates.finalReleasePackage` 通过。未提供该参数时 `scope` 为 `local-package`，只形成独立本机结论。详细命令见 [候选验收运行器](../tools/acceptance/README.md)。

冻结发行目录和 `record-ci-package` 输出的候选目录在验收期间必须持续存在。记录器按来源引用并校验原始包，不靠另存同名 ZIP 或改写历史报告维持来源。候选源码、CI 包或所选 attempt 改变后，重新建立对应的验证与操作证据。

Windows 工作目录仍受 `CreateProcessW` 限制。记录器可使用明确指定的短工作目录，或在验证指向相同目录后使用已有 8.3 名称，不通过改写来源或手工补报告掩盖失败。
