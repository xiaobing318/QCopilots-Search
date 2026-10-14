# 候选验收运行器

这些工具与应用源码一起版本管理，记录当前冻结候选的实际执行。工具回归使用合成输入，只证明记录、隔离和拒绝规则，不能替代真实模型、安装包或 Windows Computer Use。

日常开发使用根 `qcs-dev` 的 `check`，它统一编排全部保留测试与打包。此目录保留候选身份、Windows 操作、只读源码、双 checkout 和进程隔离等必要专项职责，避免重复编排普通构建与打包。

## 候选身份与完整测试

先准备依赖与模型，将候选冻结为干净提交。构建根、Qt、模型、报告根和完整提交 SHA 均由调用者提供。程序路径按变体与配置选择，例如 Windows CPU 的 Release 程序位于 `<build-root>/windows-x64-cpu/Release/bin`。包解压、安装、搬移、验收 profile 和 Windows CU 运行根使用英文且无空格的路径，保留独立实例及实际搬移验证。样本内部的中文及空格文件名与中文查询继续用于功能检查。

`tools/qcs-release-evidence.py record-build` 执行并记录真实构建命令，绑定执行前后源码身份及四个应用程序。示例中的路径均替换为本次实际目录：

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

`record-package` 绑定最终 CPU ZIP、包验证报告及构建记录，核对四个程序的实际字节。模型和提取指纹来自同一候选的真实执行：

```powershell
python "$qcsSource/tools/qcs-release-evidence.py" record-package `
  --build-record '<build-record.json>' --package '<最终CPU ZIP>' `
  --verification '<对应包验证JSON>' --output-root $qcsEvidence `
  --model-fingerprint '<实际模型指纹>' --extraction-fingerprint '<实际提取指纹>'
```

`qcs-cu-recorder.py` 的 `init`、`begin`、`observe`、`finish`、`summarize` 记录真实用户操作。初始化绑定候选、包和包内四个程序的 SHA-256，每个观察记录窗口、操作、时间与截图哈希。没有观察记录不能结束为通过，记录器自身不操作 UI，也不生成通过场景。

`qcs-copy-cu-fixtures.py --prepare-source` 读取 `tests/acceptance/fixtures/assets.json` 与 `media` 下的保留素材、来源及许可，生成独立 CU 样本。`--create-plan --source-root <样本目录> --plan <计划路径>` 创建哈希计划，再用 `--run-json <记录状态> --plan <计划路径> --execute` 复制到本次运行目录。源样本不删除或覆盖，已存在的目标明确失败。详细顺序见 [Windows 用户操作验收](../../docs/windows-user-acceptance.md)。

完成同一最终包的全部场景后，仅收集候选与 Computer Use 原始记录：

```powershell
python "$qcsSource/tools/qcs-release-evidence.py" collect-local `
  --candidate '<package-candidate.json>' --computer-use '<原始CU JSON>' `
  --output-root $qcsEvidence
python "$qcsSource/tools/qcs-release-evidence.py" validate-local `
  --candidate-commit '<完整40位提交SHA>' --local-evidence-dir '<collect-local输出根>' `
  --output '<外置本机验证JSON>'
```

`validate-local` 校验本机包与真实操作记录，不表示远端 11 个变体已经通过。远端来源与用户包由 `release-prepare` 按 run、attempt 和候选自动获取和验证，见 [发布命令](../../docs/release-command.md)。

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
