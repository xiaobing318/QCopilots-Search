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
| 百万向量 | `qcs-run-million.ps1` | 默认 `-Mode Validate`。正式验收通过下方 `run-local` 命令传入 `-Mode Run` 和 `-ReportRoot {evidence}`，将原始监视器文件与构建证明一同封装，保留 7 GiB 产物预算和 3 GiB 空闲默认值 |
| 十万物理文件 | `qcs-run-physical.ps1` | `-ExpectedCommit`、`-BuildRecord`、`-BuildRoot`、`-QtPrefix`、`-Python`。真实执行添加 `-Execute` |
| 安装包阶段二 | `qcs-package-phase2.py` 或同名 `.ps1` | `--phase1-summary` 及其 `--phase1-summary-sha256`。重新核对候选身份和阶段一产物，再创建和验证源码材料，不能仅凭阶段一声明完整交付 |
| 服务端延迟 | `qcs-service-latency.py` | `--semantic-report`、`--source`、`--expected-commit`、`--package-candidate`、`--app-dir`、`--model-dir`、`--working-directory`、`--artifact-root`、`--report`。真实运行创建独立 profile 和 cwd |
| 普通窗口哨兵 | `qcs-launch-portability-sentinel.py` | `--head`、`--source-tree`、`--build-record`、`--app-bin`、`--qt-prefix`、`--run-parent`，使用最终包时选择 `--runtime-mode packaged` |
| 双 checkout 与只读源码 | `qcs-two-checkout-portability.py` | 完整候选身份、构建记录、Qt、`--calligra-source` 和外置 `--run-parent`，通过 `--sentinel-launch-record` 同时验证普通窗口隔离 |

Python 执行型入口默认只检查或展示计划，添加 `--execute` 才实际执行。计划阶段可能读取现有记录或校验身份，不能代替真正的执行记录。Windows 原生容量、Job Object、ACL 和 NSIS 运行器在其他平台明确不可用，其他平台的原生构建与包验证通过项目 CI 完成。

`qcs-physical-preflight.py`、`qcs-portability-sentinel.py` 和 `qcs_owned_processes.py` 为共享实现。它们分别验证候选及产物、观察普通应用身份、限定本次创建的 Windows 子进程。不得通过运行器的报告参数加载外部任意 Python 实现。

候选 CTest 记录还保存 `tests/support/temp_paths.py` 的原始副本和 SHA-256，并在执行结束后再次校验。该共享临时路径 helper 同时属于完整源码提交身份，不能在冻结候选测试期间修改。

语义用例成功后保留其独立运行目录。候选运行器按原始报告的 `preservedRoot` 检查 corpus，要求目录位于本次构建根的 `runs` 下、没有目录重定向，且每个原始文件与报告 SHA-256 一致。检查结果写入派生摘要，结束前再次验证，并作为完整 CTest 成功门槛。服务延迟和 Computer Use 准备继续使用这些原字节，不重新生成 PDF 等样本，也不改写原报告的路径。

## 百万验收的完整执行记录

沿用上面的 `$qcsSource`、`$qcsBuildRoot`、`$qcsPython`、`$qcsPowerShell` 和 `$qcsBuildRecord`。另将 `$qcsQtPrefix` 设置为具体的 Qt kit 目录，例如本机 Qt 安装根内的 `6.12.0/msvc2022_64`。必须先结束构建、CTest、其他容量运行和产品进程。默认需要至少 10.375 GiB 可用空间，包括监视器的缓冲预算。

```powershell
$qcsQtPrefix = (Resolve-Path '<具体Qt kit绝对路径>').Path
$qcsBuildProof = Get-Content -LiteralPath $qcsBuildRecord -Raw | ConvertFrom-Json
if ($qcsBuildProof.passed -ne $true) { throw '必须使用本轮成功构建的证明' }
$qcsMillionArgs = @(
    '-B', (Join-Path $qcsSource 'tools/qcs-release-evidence.py'), 'run-local',
    '--build-record', $qcsBuildRecord,
    '--output-root', (Join-Path $qcsBuildRoot 'reports'),
    '--working-directory', $qcsBuildRoot,
    '--kind', 'million', '--program-name', 'qcs-benchmark',
    '--report', '*.monitor.json',
    '--input', (Join-Path $qcsSource 'tools/acceptance/qcs-run-million.ps1'),
    '--input', (Join-Path $qcsSource 'tools/acceptance/qcs-physical-preflight.py'),
    '--', $qcsPowerShell, '-NoProfile', '-File',
    (Join-Path $qcsSource 'tools/acceptance/qcs-run-million.ps1'),
    '-Source', $qcsSource, '-ExternalRoot', $qcsBuildRoot,
    '-ReportRoot', '{evidence}', '-BenchmarkExe', '{program}',
    '-QtBin', (Join-Path $qcsQtPrefix 'bin'),
    '-BuildRecord', $qcsBuildRecord,
    '-ExpectedCommit', $qcsBuildProof.identity.checkoutCommit,
    '-Python', $qcsPython, '-BudgetGiB', '7', '-ReserveGiB', '3', '-Mode', 'Run'
)
$qcsMillionRecorded = & $qcsPython @qcsMillionArgs
if ($LASTEXITCODE -ne 0) { throw '百万验收失败，保留本次原始记录用于检查' }
$qcsMillionReceipt = $qcsMillionRecorded | ConvertFrom-Json
if ($qcsMillionReceipt.passed -ne $true) { throw '没有成功的百万执行记录' }
$qcsMillionRecord = $qcsMillionReceipt.record
Write-Output $qcsMillionRecord
```

`ExternalRoot` 始终是六个 producer 所属的真实构建根。`ReportRoot` 是 `run-local` 原子创建的证据目录，必须已经存在并位于 `<build-root>/reports` 内，目录及祖先不能包含重定向。五份原始文件从执行开始直接写入其中，包括 benchmark JSON、monitor JSON、独立采样 JSONL 和两份输出日志。执行证明同时绑定这些文件的原始路径和 SHA-256，不移动文件后重新解释来源。

`run-local` 最后返回的 `record` 用于最终 `collect-local --million`。监视器单独运行产生的 raw JSON 不能替代该执行证明。完整发布预检继续检查全部百万质量、延迟、恢复、磁盘、源文件和 producer 约束。

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

百万记录回归使用真实构建记录器、独立 Python 子进程、执行记录器和完整发布验证器，验证五份原始文件的路径、哈希、producer 与时间绑定。数值和容量数据明确标为合成，仅用于接口与拒绝分支验证。报告目录测试检查默认目录、中文空格、同前缀兄弟目录、父目录逃逸和真实目录联接，验证阶段不创建产品 profile。

快速测试中的合成容量和 CU 通过数据仅在临时目录中用于校验记录器与发布验证器的接口，测试退出时必须删除，永远不作为真实验收材料。
