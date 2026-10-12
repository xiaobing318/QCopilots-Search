# 候选验收运行器

这些工具与应用源码一起版本管理，不依赖外置 `reports` 目录中的代码。所有运行记录必须由当前候选重新产生。脚本的快速单元测试使用合成输入，不构成真实模型、容量、安装包或 Computer Use 通过证据。

## 输入与执行顺序

源码默认从脚本位置定位。构建根、Qt kit、模型、报告根及候选完整提交 SHA 由参数传入。`qcs-dev.ps1 build` 只执行构建，完整构建记录必须由 `tools/qcs-release-evidence.py record-build` 捕获该真实构建命令产生。每个入口的完整参数由 `--help` 或 PowerShell `Get-Help` 查看。

先完成依赖准备及 Release 配置，将候选提交冻结为干净的工作树。以下 Windows PowerShell 7 示例从任意 cwd 均可执行。只需将前三个目录改为本机位置，并确认 `python` 与 `pwsh` 在 PATH 中。如果已设置 `QCOPILOTS_SEARCH_PYTHON`，继续使用该 Python。

```powershell
$qcsSource = (Resolve-Path '<源码仓库绝对路径>').Path
$qcsBuildRoot = (Resolve-Path '<外置构建根绝对路径>').Path
$qcsQtRoot = (Resolve-Path '<Qt安装根绝对路径>').Path
$qcsPython = $env:QCOPILOTS_SEARCH_PYTHON
if (-not $qcsPython) { $qcsPython = (Get-Command python -ErrorAction Stop).Source }
$env:QCOPILOTS_SEARCH_PYTHON = $qcsPython
$qcsPowerShell = (Get-Command pwsh -ErrorAction Stop).Source
$qcsBin = Join-Path $qcsBuildRoot 'release/bin'
$qcsRecordArgs = @(
    '-B', (Join-Path $qcsSource 'tools/qcs-release-evidence.py'), 'record-build',
    '--source', $qcsSource,
    '--output-root', (Join-Path $qcsBuildRoot 'reports'),
    '--working-directory', $qcsBuildRoot,
    '--program', ('qcs=' + (Join-Path $qcsBin 'qcs.exe')),
    '--program', ('qcs-service=' + (Join-Path $qcsBin 'qcs-service.exe')),
    '--program', ('qcs-worker=' + (Join-Path $qcsBin 'qcs-worker.exe')),
    '--program', ('qcs-cli=' + (Join-Path $qcsBin 'qcs-cli.exe')),
    '--program', ('qcs-benchmark=' + (Join-Path $qcsBin 'qcs-benchmark.exe')),
    '--program', ('qcs-tests-file-capacity=' + (Join-Path $qcsBin 'qcs-tests-file-capacity.exe')),
    '--', $qcsPowerShell, '-NoProfile', '-File', (Join-Path $qcsSource 'qcs-dev.ps1'),
    'build', '--build-root', $qcsBuildRoot, '--qt-root', $qcsQtRoot,
    '--config', 'Release', '--jobs', '2'
)
$qcsRecorded = & $qcsPython @qcsRecordArgs
if ($LASTEXITCODE -ne 0) { throw '候选构建或构建记录校验失败' }
$qcsReceipt = $qcsRecorded | ConvertFrom-Json
if ($qcsReceipt.passed -ne $true) { throw '没有成功的候选构建记录' }
$qcsBuildRecord = $qcsReceipt.record
Write-Output $qcsBuildRecord
```

`$qcsBuildRecord` 是新产生的 `build-record.json`，后续传给 `--build-record` 或 `-BuildRecord`。六个 producer 必须全部提供。记录器保存实际构建命令和日志，比较构建前后源码身份，校验并复制六个程序，不能把既有二进制文件单独散列后当作构建执行证明。

| 阶段 | 入口 | 关键输入和行为 |
|---|---|---|
| 完整本机 CTest | `qcs-candidate-ctest-run.py` | `--head`、`--source-tree`、`--build-record`、`--expected-tests`、`--extraction-prefix`、`--build-root`、`--qt-root`、`--qt-prefix`、`--launch-cwd`、`--report-parent`。校验全部用例、18 条 Windows 双后端参考向量和完整语义语料 |
| 安装包阶段一 | `qcs-package-phase1.py` 或同名 `.ps1` | 完整构建及 CTest 记录、语义报告和 SHA-256、Qt、模型及外置输出根。生成 ZIP、NSIS 和包级验证，必须传 `--defer-source-bundle`，整体 `passed` 保持 false |
| 百万向量 | `qcs-run-million.ps1` | `-BenchmarkExe`、`-ExternalRoot`、`-QtBin`、`-ExpectedCommit`、`-BuildRecord`、`-Python`。默认 `-Mode Validate`，真实执行使用 `-Mode Run`，保留 7 GiB 产物预算和 3 GiB 空闲默认值 |
| 十万物理文件 | `qcs-run-physical.ps1` | `-ExpectedCommit`、`-BuildRecord`、`-BuildRoot`、`-QtPrefix`、`-Python`。真实执行添加 `-Execute` |
| 安装包阶段二 | `qcs-package-phase2.py` 或同名 `.ps1` | `--phase1-summary` 及其 `--phase1-summary-sha256`。重新核对候选身份和阶段一产物，再创建和验证源码材料，不能仅凭阶段一声明完整交付 |
| 服务端延迟 | `qcs-service-latency.py` | `--semantic-report`、`--source`、`--expected-commit`、`--package-candidate`、`--app-dir`、`--model-dir`、`--working-directory`、`--artifact-root`、`--report`。真实运行创建独立 profile 和 cwd |
| 普通窗口哨兵 | `qcs-launch-portability-sentinel.py` | `--head`、`--source-tree`、`--build-record`、`--app-bin`、`--qt-prefix`、`--run-parent`，使用最终包时选择 `--runtime-mode packaged` |
| 双 checkout 与只读源码 | `qcs-two-checkout-portability.py` | 完整候选身份、构建记录、Qt、`--calligra-source` 和外置 `--run-parent`，通过 `--sentinel-launch-record` 同时验证普通窗口隔离 |

Python 执行型入口默认只检查或展示计划，添加 `--execute` 才实际执行。计划阶段可能读取现有记录或校验身份，不能代替真正的执行记录。Windows 原生容量、Job Object、ACL 和 NSIS 运行器在其他平台明确不可用，其他平台的原生构建与包验证通过项目 CI 完成。

`qcs-physical-preflight.py`、`qcs-portability-sentinel.py` 和 `qcs_owned_processes.py` 为共享实现。它们分别验证候选及产物、观察普通应用身份、限定本次创建的 Windows 子进程。不得通过运行器的报告参数加载外部任意 Python 实现。

候选 CTest 记录还保存 `tests/support/temp_paths.py` 的原始副本和 SHA-256，并在执行结束后再次校验。该共享临时路径 helper 同时属于完整源码提交身份，不能在冻结候选测试期间修改。

## Computer Use 记录

`qcs-cu-recorder.py` 的 `init`、`begin`、`observe`、`finish`、`summarize` 子命令记录真实操作。初始化绑定候选、安装包和包内四个程序的 SHA-256。每个观察记录目标窗口、操作、时间及截图哈希，缺少观察记录不能结束为通过。

`qcs-copy-cu-fixtures.py --create-plan --source-root <样本目录> --plan <计划路径>` 从显式样本产生带哈希的复制计划。随后使用 `--run-json <记录状态> --plan <计划路径> --execute` 将样本复制到本次运行目录。原始样本从不被删除或覆盖，已有目标会失败。测试样本应由候选中的夹具或生成器提供，不能复用被清理的历史样本路径。

记录器不操作 Windows UI，也不生成通过场景。真实用户流程必须由 Windows Computer Use 插件执行并观察。普通窗口哨兵也不会代替 UI 验收。

## 轻量测试

```text
python -B tools/acceptance/qcs-test-runners.py
```

统一入口产生一份完整 unittest 逐例报告。测试临时根默认来自系统，支持 `QCOPILOTS_SEARCH_TEST_ROOT`。可从任意 cwd 执行，只需给出脚本实际路径。临时目录创建失败会直接失败，不回退到源码或用户配置。

测试包含候选身份与文件哈希篡改、越界路径、短名产物清单、CTest 隔离 wrapper、阶段一与最终门槛区别、源包校验、容量监视器负例、真实目录链接边界和自有进程清理。Windows 用真实 NTFS junction 验证目录重定向，POSIX 用真实符号链接。文件符号链接用例在 POSIX 注册，Windows 独立报告该权限能力不适用，不把模拟分支声明为真实文件符号链接通过。平台能力清单以 `QCS_ACCEPTANCE_TEST_CAPABILITIES` 行输出。

快速测试中的合成 CU 通过数据仅在临时目录中用于校验记录器与发布验证器的接口，测试退出时必须删除，永远不作为真实验收材料。
