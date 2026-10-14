# 候选验收运行器

这些工具与应用源码一起版本管理，记录当前冻结候选的实际执行。工具回归使用合成输入，只证明记录、隔离和拒绝规则，不能替代真实模型、安装包或 Windows Computer Use。

日常开发使用根 `qcs-dev` 的 `check`，它统一编排全部保留测试与打包。此目录保留候选身份、Windows 操作、只读源码、双 checkout 和进程隔离等必要专项职责，避免重复编排普通构建与打包。

## 候选身份与完整测试

先准备依赖与模型，将候选冻结为干净提交。构建根、Qt、模型、报告根和完整提交 SHA 均由调用者提供。程序路径按变体与配置选择，例如 Windows CPU 的 Release 程序位于 `<build-root>/windows-x64-cpu/Release/bin`。包解压、安装、搬移、验收 profile 和 Windows CU 运行根使用英文且无空格的路径，保留独立实例及实际搬移验证。样本内部的中文及空格文件名与中文查询继续用于功能检查。

`tools/qcs-release-evidence.py record-build` 执行并记录真实构建命令，绑定执行前后源码身份及四个应用程序。这是本机开发验证证据，不能证明同一提交的 CI 包具有相同字节。示例中的路径均替换为本次实际目录：

```powershell
$qcsSource = '<源码仓库绝对路径>'
$qcsBuildRoot = '<外置构建父根>'
$qcsQtRoot = '<Qt安装根>'
$qcsVariant = 'windows-x64-cpu'
$qcsBin = Join-Path $qcsBuildRoot "$qcsVariant/Release/bin"
$qcsEvidence = '<本次外置证据根>'
python "$qcsSource/tools/qcs-release-evidence.py" record-build `
  --source $qcsSource --output-root $qcsEvidence `
  --program "qcs-gui=$qcsBin/qcs-gui.exe" `
  --program "qcs-service=$qcsBin/qcs-service.exe" `
  --program "qcs-worker=$qcsBin/qcs-worker.exe" `
  --program "qcs-cli=$qcsBin/qcs-cli.exe" `
  -- pwsh -NoProfile -File "$qcsSource/qcs-dev.ps1" build `
       --variant $qcsVariant --build-root $qcsBuildRoot --qt-root $qcsQtRoot
```

不能仅散列已有程序而把它当作构建执行证明。命令输出的 `build-record.json` 用于后续候选及包记录。

`qcs-candidate-ctest-run.py` 接受 `--head`、`--source-tree`、`--build-record`、`--variant`、`--build-root`、`--qt-root`、`--qt-prefix`、`--launch-cwd`、`--report-parent` 和实际注册的 `--expected-tests`。`--model-dir` 可覆盖共享模型位置，`--execute` 才真正执行。它核对完整 CTest 清单、逐用例结果、原始输出、程序及来源稳定性，不从旧固定测试数量推定完整性。

## 包与 Windows 操作记录

普通安装与打包通过 `qcs-dev check`、`install`、`package`、`verify-package` 和 `installers` 完成。候选身份、文件稳定性与材料完整性在统一流程内验证，源码和符号使用独立动作。

正式交付先完成同一干净候选的本机完整 `check` 与全部 11 变体 CI，再按真实 run ID 和 attempt 执行 `release-prepare`。具体冻结命令见 [发布命令](../../docs/release-command.md)。以下步骤只使用冻结清单选中的 Windows CPU portable ZIP，不能以本地同提交重建包替代。

先从 manifest 唯一定位 CPU ZIP，再完整验证该文件。示例中的运行、报告和解包目录均须位于源码之外，采用英文且无空格的路径。`--build-root` 在此是专用验包工作根，不要求存在本机构建历史：

```powershell
$qcsReleaseManifest = '<冻结发行目录>/qcs-release-manifest.json'
$qcsRelease = Get-Content -LiteralPath $qcsReleaseManifest -Raw | ConvertFrom-Json
$qcsCpuAssets = @($qcsRelease.assets | Where-Object {
  $_.configuration -eq 'windows-x64-cpu' -and $_.role -eq 'user-package' -and
  $_.name.EndsWith('.zip') -and -not $_.name.EndsWith('-setup.zip')
})
if ($qcsCpuAssets.Count -ne 1) { throw '冻结清单必须恰好包含一个 Windows CPU portable ZIP' }
$qcsCpuZip = Join-Path (Split-Path -Parent $qcsReleaseManifest) $qcsCpuAssets[0].path
$qcsVerifyRoot = '<新的外置英文无空格验包根>'
$qcsVerifyReport = Join-Path $qcsVerifyRoot 'report'
$qcsModelDir = '<已校验的完整模型缓存目录>'
python -B "$qcsSource/tools/qcs-verify-package.py" `
  --build-root $qcsVerifyRoot --package-file $qcsCpuZip `
  --report-dir $qcsVerifyReport --model-dir $qcsModelDir --variant windows-x64-cpu
if ($LASTEXITCODE -ne 0) { throw '冻结 CI CPU ZIP 本机验证失败' }
```

该入口运行包内程序的真实推理、服务检索、依赖和许可证检查，不能使用 `--keyword-only` 报告进入最终验收。读取这次实际报告的指纹，以 `record-ci-package` 建立来源候选：

```powershell
$qcsVerification = Get-Content -LiteralPath "$qcsVerifyReport/report.json" -Raw | ConvertFrom-Json
if (-not $qcsVerification.releaseComplete) { throw '必须使用完整包验证报告' }
$qcsModelFingerprint = $qcsVerification.modelSmoke.vectors[0].modelFingerprint
$qcsExtractionFingerprint = $qcsVerification.modelSmoke.vectors[0].extractionFingerprint
python -B "$qcsSource/tools/qcs-release-evidence.py" record-ci-package `
  --release-manifest $qcsReleaseManifest --verification "$qcsVerifyReport/report.json" `
  --output-root '<新的外置候选记录父目录>' `
  --model-fingerprint $qcsModelFingerprint --extraction-fingerprint $qcsExtractionFingerprint
if ($LASTEXITCODE -ne 0) { throw 'CI 包来源候选记录失败' }
```

`record-ci-package` 自动定位并验证清单中的 CPU ZIP，不接受任意 `--package`。输出为新建 `qcs-candidate-*/package-candidate.json`，包记录为 `ci/package/<文件名>`，CI 来源记录为 `ci/ci-origin.json`。它绑定真实 CI 来源、本机验包及四个程序的字节，不创建虚构的本地构建历史。保留该候选目录及原冻结发行目录。

开发阶段仍可用 `record-package` 绑定本机 CPU ZIP、验证报告及真实构建记录。以下命令只产生 local-build 开发证据，不代替上述 CI 来源候选：

```powershell
python "$qcsSource/tools/qcs-release-evidence.py" record-package `
  --build-record '<build-record.json>' --package '<本机构建CPU ZIP>' `
  --verification '<对应包验证JSON>' --output-root $qcsEvidence `
  --model-fingerprint '<实际模型指纹>' --extraction-fingerprint '<实际提取指纹>'
```

`qcs-cu-recorder.py` 的 `init`、`begin`、`observe`、`finish`、`summarize` 记录真实用户操作。正式验收的 `init --candidate-record` 使用 `record-ci-package` 实际输出的 `package-candidate.json`，`--package` 使用上面的 `$qcsCpuZip`，`--app-dir` 指向该 ZIP 新解包副本内含四个程序的 `bin` 目录。初始化绑定候选、包和包内四个程序的 SHA-256，每个观察记录窗口、操作、时间与截图哈希。保留原冻结发行和候选目录，不能靠替换路径中的同名包延续旧记录。没有观察记录不能结束为通过，记录器自身不操作 UI，也不生成通过场景。

`qcs-copy-cu-fixtures.py --prepare-source` 读取 `tests/acceptance/fixtures/assets.json` 与 `media` 下的保留素材、来源及许可，生成独立 CU 样本。`--create-plan --source-root <样本目录> --plan <计划路径>` 创建哈希计划，再用 `--run-json <记录状态> --plan <计划路径> --execute` 复制到本次运行目录。源样本不删除或覆盖，已存在的目标明确失败。详细顺序见 [Windows 用户操作验收](../../docs/windows-user-acceptance.md)。

完成同一冻结 CI ZIP 的 CU-01 至 CU-10 后，收集 CI 来源候选与 Computer Use 原始记录，再与同一发行 manifest 交叉核对：

```powershell
python "$qcsSource/tools/qcs-release-evidence.py" collect-local `
  --candidate '<record-ci-package输出的package-candidate.json>' --computer-use '<原始CU JSON>' `
  --output-root '<新的外置本机证据父目录>'
if ($LASTEXITCODE -ne 0) { throw '本机 CI 包与 CU 记录收集失败' }
python "$qcsSource/tools/qcs-release-evidence.py" validate-local `
  --candidate-commit '<完整40位提交SHA>' --local-evidence-dir '<collect-local输出根>' `
  --release-manifest $qcsReleaseManifest `
  --output '<外置本机验证JSON>'
if ($LASTEXITCODE -ne 0) { throw '冻结 CI 包与本机操作记录绑定失败' }
```

`validate-local --release-manifest` 增加 `localGates.finalReleasePackage` 验证，要求本机实际操作的 ZIP、四个程序和 CI 来源与冻结发行包完全对应。正式结果需满足 `scope: final-release-package`、`passed: true` 及该项通过。它核对已有证据，不重新执行远端测试。未提供该参数时仍可验证开发记录，但 `scope` 为 `local-package`，不能作为正式 CI 包的最终验收。仅有相同 commit、文件名或版本号不足以满足字节绑定。

## 路径与进程隔离

| 入口 | 保留职责 |
| --- | --- |
| `qcs-launch-portability-sentinel.py` | 启动受控普通窗口，绑定候选程序、实例和运行目录 |
| `qcs-portability-sentinel.py` | 观察同一普通实例，验证测试没有修改或终止它 |
| `qcs-two-checkout-portability.py` | 两份 checkout 并行构建、真实只读源码及输出隔离 |
| `qcs-token-context.py` | 为受控 Windows 子进程准备权限测试上下文，不改变父进程令牌 |
| `qcs_owned_processes.py` | 仅管理当前运行创建的 Windows 子进程及收尾 |

执行型入口支持计划与身份检查，必须使用其 `--execute` 才产生实际执行证据。具体参数以当前入口 `--help` 为准。Windows Job Object、ACL 或令牌能力在其他平台明确记录不适用，不能通过可导入脚本声称已执行原生检查。

共享 helper 的原文、哈希和执行结束后的稳定性属于来源记录。不得通过报告参数加载任意外部 Python 实现。临时路径 helper 同样绑定冻结候选，测试期间不可修改。

## 运行器自身回归

```text
python -B tools/acceptance/qcs-test-runners.py --suite all
python -B tools/acceptance/qcs-test-runners.py --suite pure
python -B tools/acceptance/qcs-test-runners.py --suite native
```

默认执行 `all`。`pure` 与 `native` 表示能力分组，完整 CTest 同时执行两组，单独选择只是排查。原生组包含真实 ACL、进程令牌、超时输出和本次进程收尾。Windows 专用能力在其他平台按精确用例与原因记为不适用，跨平台超时回归仍实际执行。

临时根遵循 `QCOPILOTS_SEARCH_TEST_ROOT`，创建失败不回退到源码或用户配置。测试覆盖候选身份与哈希篡改、路径越界、CTest 独立 cwd、源码材料、fixture 复制、真实目录链接、进程收尾和错误传播。平台能力按实际运行输出，模拟分支不冒充原生能力通过。

合成 CU 通过记录只存在于工具回归的临时目录，不能作为真实界面验收材料。清理失败、残留进程、输入或程序变化都必须导致验证失败。
