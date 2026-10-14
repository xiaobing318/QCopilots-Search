# 自动准备发行材料与上传草稿

`tools/qcs-release.py` 提供 `prepare` 和 `upload-draft` 两个阶段。准备阶段自动获取同一候选的 CI 用户包，核验来源与字节并冻结材料。上传阶段仅在显式调用时创建或续传 GitHub 草稿，两者均不公开发布。

Windows 可使用 `qcs-dev.ps1 release-prepare` 和 `release-upload-draft`，macOS / Linux 使用 `qcs-dev.sh` 的同名动作。统一入口会选择仓库中的核心工具。

## 前置条件

当前 checkout 必须干净，HEAD 与完整 40 位候选 SHA 相同。所选 CI run 属于 `xiaobing318/QCopilots-Search` 的规定 workflow，选定 attempt 中全部 11 个变体已经完成并具有可验证的用户包。输出目录必须位于源码树之外，使用新的目录，不覆盖旧候选。

使用 `GH_TOKEN`、`GITHUB_TOKEN` 或已经登录的 GitHub CLI 提供访问权限。工具不读取浏览器凭据，不输出令牌，也不把令牌放入命令行。准备需要读取 Actions 运行和 artifacts，草稿上传另需创建 Release 与上传附件的权限。

正式交付应先完成同一干净候选的本机完整 `check` 与 11 变体 CI，再运行 `prepare` 冻结 CI 成品。冻结后，对其中准确的 Windows CPU ZIP 完成本机正式验包、CI 来源候选记录和 CU-01 至 CU-10。相同提交的本地重建包不能代替这一 ZIP，下载和冻结成功也不表示这些本机场景已经执行。

## 准备命令

以下路径和身份均替换为本次真实候选。`--attempt` 是明确选择的运行次数，不以最新文件名猜测。

```powershell
$qcsSource = '<干净候选源码目录>'
$qcsCandidate = '<完整40位提交SHA>'
$qcsOutput = '<新的外置发行目录>'
$qcsRun = '<GitHub Actions run ID>'
python "$qcsSource/tools/qcs-release.py" prepare `
  --run-id $qcsRun --attempt 1 --candidate-commit $qcsCandidate `
  --output-dir $qcsOutput
```

同一操作也可用统一入口：

```powershell
& "$qcsSource/qcs-dev.ps1" release-prepare `
  --run-id $qcsRun --attempt 1 --candidate-commit $qcsCandidate `
  --output-dir $qcsOutput
```

核心工具可用 `--source-dir` 明确当前工具所属的候选 checkout，不能用一份 checkout 的工具操作另一份源码。`--source-cache-dir` 指定外置源码下载缓存。默认 repository 为 `xiaobing318/QCopilots-Search`，不得用其他仓库的相似包替代。

准备阶段读取原始 run、attempt、workflow、jobs 和 artifacts API 信息。选择器核对候选提交、原始 job 与 attempt 的关联，支持部分重跑中未重新执行的成功 job。最新失败不能退回旧成功产物，不存在或含糊的来源明确失败。

每个 artifact 下载后先校验 GitHub 提供的 digest，再校验 ZIP 内用户文件白名单、名称、包类型和数量。正常结果必须恰好为 11 个变体、21 个用户文件。拒绝报告、源码、符号、校验和或其他额外文件混入 CI 用户包 artifact。

工具对每个实际用户包计算 SHA-256，从相同干净提交与依赖锁生成一份对应源码材料，不重新构建应用。冻结结果布局如下：

```text
release-output/
  qcs-release-manifest.json
  assets/
    <21个已验证的用户文件>
    qcs-source-materials-<commit12>.zip
    SHA256SUMS
  provenance/
    github.json
    artifacts/<artifact-id>.zip
```

manifest 绑定实际文件名、大小、SHA-256、变体和来源。校验和不包含自身，manifest 自身的摘要由命令输出。来源快照与下载的 artifact ZIP 保存在本地，供离线重验字节来源，不作为 Release 附件上传。准备目录需要同时容纳原始下载和最终资产，应预留相应空间。

每个 Release 附件必须小于 2 GiB，超限明确失败，不分卷、不裁剪 GPU 目标。来源与限制分别见 [GitHub Actions artifacts API](https://docs.github.com/en/rest/actions/artifacts) 和 [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

## 从冻结 CI 包进入 Windows 验收

从 `qcs-release-manifest.json` 的 `assets` 中选取 `configuration: windows-x64-cpu`、`role: user-package` 的唯一 portable ZIP，实际文件位于 manifest 的相对 `path`。不要从本机 `pkg` 目录按最新时间另选一个包，也不要只凭候选提交或文件名判断字节相同。

对这个准确 ZIP 运行完整 `tools/qcs-verify-package.py --package-file ... --model-dir ... --variant windows-x64-cpu`，随后执行 `tools/qcs-release-evidence.py record-ci-package --release-manifest ... --verification ...`。该入口从冻结清单定位原包并绑定本机验包结果，不接受任意包替换来源。用其输出的 `package-candidate.json` 和相同 ZIP 初始化 CU 记录器，实际完成全部十个场景，再执行 `collect-local` 和 `validate-local --release-manifest <同一冻结清单>`。

完整必填参数和可执行示例见 [候选验收运行器](../tools/acceptance/README.md#包与-windows-操作记录)，场景要求见 [Windows 用户操作验收](windows-user-acceptance.md)。最终验证会交叉核对 CI 包、四个程序和 CU 身份，未传 `--release-manifest` 的 local-only 结果不能替代此步骤。保持冻结目录与候选记录目录可用，受控解包、运行及 profile 路径使用英文且无空格，中文功能样本继续保留。

`prepare` 不依赖 CU 已完成，显式 `upload-draft` 也不自动执行或证明 CU 验收。项目最终完成结论仍要求本机完整检查、全部 11 变体 CI、十个 CU 场景，以及 `validate-local` 的 `scope: final-release-package` 和 `localGates.finalReleasePackage` 全部通过，不能由草稿存在反推验收成功。

## 显式上传草稿

准备成功后，显式指定冻结 manifest 与 tag：

```powershell
python "$qcsSource/tools/qcs-release.py" upload-draft `
  --manifest "$qcsOutput/qcs-release-manifest.json" `
  --tag v0.1.0 --title 'QCopilots Search v0.1.0'
```

统一入口对应命令为：

```powershell
& "$qcsSource/qcs-dev.ps1" release-upload-draft `
  --manifest "$qcsOutput/qcs-release-manifest.json" `
  --tag v0.1.0 --title 'QCopilots Search v0.1.0'
```

上传前重新验证冻结文件字节和来源，并查询同一 run 的最新 attempt 与选择结果。后续失败或 artifact 已过期会明确拒绝，不凭旧 `passed` 字段放行。远端候选提交和已有 tag 必须指向同一候选。已有 Release 必须仍是草稿，已公开 Release 不修改，指向其他提交的草稿不自动重定向。

创建请求明确设置 `draft: true`。同名远端资产只有在状态、大小和 SHA-256 一致时才复用，未完成资产或同名异内容会失败，不自动删除或覆盖。上传集合包含 21 个用户文件、一份源码包、`SHA256SUMS` 和发布 manifest，共 24 个附件。上传完成后重新枚举并验证完整资产集合，输出实际 Release 和资产身份，结果保持 `published: false`。

## 失败与恢复

准备中发现错误候选、失败 job、digest 不匹配、包缺项、额外文件、附件超限或生成期间文件变化时返回非零。错误写入命令输出，失败的临时准备目录会清理。保留控制台输出用于排查，修复后使用新输出目录重新准备，不手工补改 manifest 中的来源与哈希。

上传中断后保留草稿及已上传资产。重复同一上传命令会重新验证本地和远端字节，只补齐缺失项。冲突需先查明原因，不自动破坏已有远端内容。

`tools/qcs-test-release.py` 与 artifact 选择器回归使用外置临时数据及模拟 GitHub API，覆盖部分重跑、错误候选、摘要不匹配、包白名单、准备后变更和上传恢复。工具回归通过只证明这些契约，不等于真实 11 个 CI 变体或发布完成。
