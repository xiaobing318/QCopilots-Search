# 安装包与发布

普通开发生成当前变体的安装布局和用户包。正式发布从指定成功 CI run、attempt 与提交获取全部成品，校验后生成源码材料与发布清单。维护者可使用本地 `qcs-dev` 命令或 GitHub Actions 网页发布入口，两者调用同一套工具。草稿上传与公开发布分开执行，Release 正文由脚本固定生成，不需要手工编写。这些命令不自动合并分支或触发原生构建。

## 1. 准备版本与 CI

在准备发布的提交中完成版本、依赖锁和说明更新，运行相关测试及本机完整 `check`。开发检查允许未提交修改，正式 `release-prepare` 则要求工作区干净、HEAD 与指定的完整提交 SHA 相同。

原生 CI 读取 `config/build-variants.json`，对 11 个变体执行构建、功能测试、安装、打包和最终包验证。矩阵详情见[依赖说明](dependencies.md)。每次成功选择包含 21 个单文件 artifacts：Windows 五个变体各提供安装器和便携 ZIP，Linux 五个变体各提供 DEB 和便携 TGZ，macOS 提供一个 DMG。Windows ROCm 仅在 NSIS 数据块触及大小限制时使用带完整 payload 的 `-setup.zip` 安装包。

每个 artifact 直接保存一个已验证用户包的原始字节，不额外套一层 ZIP。文件名统一为 `qcs-<版本>-<packageVariant>.<扩展名>`，`packageVariant` 来自候选提交的变体清单。内部变体 ID、`--variant` 和构建目录不变。ROCm/Vulkan 包名包含锁定 SDK 版本，其他变体保持原命名：

| 平台与后端 | Release 标签 | v0.3.0 文件名示例 |
| --- | --- | --- |
| Windows ROCm | ROCm SDK 10.0.0 | `qcs-0.3.0-windows-x64-rocm-10.0.0.exe` |
| Linux ROCm | ROCm SDK 10.0.0 | `qcs-0.3.0-linux-x64-rocm-10.0.0.deb` |
| Windows Vulkan | Vulkan SDK 1.4.357.0 | `qcs-0.3.0-windows-x64-vulkan-1.4.357.0.zip` |
| Linux Vulkan | Vulkan SDK 1.4.313.0 | `qcs-0.3.0-linux-x64-vulkan-1.4.313.0.tar.gz` |

这些版本表示构建 SDK，不表示显卡驱动。完整提交、工作区状态、attempt 与依赖锁摘要仍保留在包内。诊断保留在 GitHub 原生日志，源码与可选符号独立生成。无 GPU runner 的 CPU 回退验证不代表 GPU 硬件执行已验证，GUI 与桌面操作检查见[测试指南](testing.md)。

推送 `main` 后由原生 CI 自动构建，无需为同一提交再次手动触发。仅在需要补跑时使用 `workflow_dispatch`。核对 run 的 `head_sha` 与候选提交一致，等待全部 11 个变体和汇总检查成功。正常 PR 也会自动运行 CI，tag 和 Release 发布不会重复触发全量构建。若验收失败，先修复并重新验收，再准备发布。

原生 CI 保留 `fail-fast: false`。若某次交付要求发现一个失败便停止本轮，由操作者检查状态并取消本轮，修复后重新提交和推送。该操作不改变后续手动推送的工作流策略，旧提交或被取消轮次中的成功不替代新候选的完整验收。

## 2. 生成本机安装包

完成 `configure` 和 `build` 后：

```powershell
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qtRoot = 'C:\Softwares\Qt'
$variant = 'windows-x64-cpu'
./qcs-dev.ps1 install --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 package --variant $variant --build-root $buildRoot --qt-root $qtRoot
./qcs-dev.ps1 verify-package --variant $variant --build-root $buildRoot --qt-root $qtRoot --model-dir "$buildRoot/models"
```

`install` 自动形成完整 stage，`package` 部署依赖和资源后生成安装器与便携包，无需手工复制。默认输出为 `<build-root>/<variant>/stage/<Config>` 和 `pkg/<Config>`。`check` 会顺序调用这些阶段及适用平台的安装卸载验证。

Windows 使用 NSIS，macOS 默认使用 ad-hoc 签名，不把它描述为商业签名或公证。Linux DEB 保留实际系统依赖分析，CUDA DEB 使用锁定的无驱动容器，最终包不能带驱动 stub 或强制安装开发 SDK。

## 3. 准备发行材料

准备前确认所选 CI 的 11 个变体成功，指定准确的 run ID、attempt 和 40 位提交 SHA，使用新的外置输出目录。认证来自 `GH_TOKEN`、`GITHUB_TOKEN` 或已登录的 GitHub CLI，工具不输出令牌。首次使用 CLI 可以执行以下浏览器登录，完成后用 `gh auth status` 确认账号：

```powershell
gh auth login --hostname github.com --git-protocol https --web --scopes workflow
gh auth status
```

如果使用便携 GitHub CLI，将其目录加入当前进程的 `PATH`。PowerShell 入口允许通过 `QCOPILOTS_SEARCH_PYTHON` 选择实际 Python 3.10+ 解释器，避免 Windows Store 占位入口。

```powershell
$qcsSource = 'C:\Dev\github-repos\QCopilots-Search'
$qcsCommit = '<完整40位提交SHA>'
$qcsRun = '<GitHub Actions run ID>'
$qcsOutput = '<新的外置发行目录>'
& "$qcsSource/qcs-dev.ps1" release-prepare `
  --run-id $qcsRun --attempt 1 --candidate-commit $qcsCommit `
  --output-dir $qcsOutput
```

macOS / Linux 使用 `bash qcs-dev.sh release-prepare` 和相同参数。核心工具为 `tools/qcs-release.py prepare`，`--source-cache-dir` 可指定外置源码归档缓存。完整选项以工具帮助为准。

准备阶段验证仓库、workflow、提交、run、attempt、原始成功 job、artifact ID、大小与摘要，再逐包下载一次并检查实际文件 SHA-256。部分重跑可以保留未重跑的成功 job，最新失败不能借用旧成功产物。ROCm 重跑发生安装器格式切换时，旧格式 artifact 可留在运行历史中，但不进入当前 21 包选择集合。缺项、来源不明、额外文件或摘要不匹配都会失败。

同一干净提交及依赖锁生成一份平台无关源码材料，不重新构建应用。输出为：

```text
<release-output>/
  qcs-release-manifest.json
  assets/
    <21 个用户包>
    qcs-source-materials-<commit12>.zip
    SHA256SUMS
```

发布清单记录提交、CI 来源、资产名称、大小和 SHA-256，使用 schema 4，CI 选择结果使用 schema 3。原始 artifact 摘要直接对应用户包摘要。旧 schema 冻结目录不能混用，需要从新格式成功 CI 重新准备。校验和不包含自身。

每个 Release 附件必须小于 2 GiB，超限明确失败，不自动分卷或裁剪 GPU 目标。失败后保留控制台诊断，修复后使用新的输出目录重新准备，不手工修改清单绕过校验。

## 4. 上传草稿与恢复

对准备好的实际包完成所需的人工检查后，显式上传草稿：

```powershell
& "$qcsSource/qcs-dev.ps1" release-upload-draft `
  --manifest "$qcsOutput/qcs-release-manifest.json" `
  --tag v0.3.0 --title 'QCopilots Search v0.3.0'
```

将示例 tag 和标题替换为本次实际版本。上传前重新读取远端 CI 身份、原始 artifact 摘要和本地文件字节，不再次完整下载全部包。对应 artifacts 必须仍可用且与准备时一致。已存在的 tag 必须指向同一提交。此命令仅接受草稿，已公开发布或指向其他提交时拒绝修改。

同名远端资产仅在状态、大小和 SHA-256 一致时复用，缺失项继续上传，同名异内容或未完成资产明确报错，不自动覆盖。中断后重复相同命令即可重新校验并补齐缺项。附件共 24 个，包括 21 个用户包、一份源码包、校验和及发布清单。

发布说明由 `tools/qcs-release.py` 从候选配置与冻结资产确定性生成。后端标签读取候选提交的 SDK 配置，下载链接读取冻结清单中的准确资产名，不根据显示文字拼凑文件名。正文按 Windows、Linux 和 macOS 分类列出各后端的安装器和便携包，“其他下载”仅列出 `SHA256SUMS`、对应源码材料和发布清单。GitHub 自动展示完整 Assets 列表。安装、模型配置、平台要求及验证范围保留在仓库文档和测试记录中。

创建草稿和续传时都会同步脚本正文。可以在上传前生成 UTF-8 正文供查看，输出位置必须在冻结目录之外，避免额外文件使冻结校验失败：

```powershell
& "$qcsSource/qcs-dev.ps1" release-notes `
  --manifest "$qcsOutput/qcs-release-manifest.json" --tag v0.3.0 `
  --output "${qcsOutput}-notes.md"
```

## 5. 公开正式版本

在准确且干净的 `main` 候选提交上，确认草稿所有附件上传完成，再显式公开：

```powershell
& "$qcsSource/qcs-dev.ps1" release-publish `
  --manifest "$qcsOutput/qcs-release-manifest.json" --tag v0.3.0
```

命令重新验证候选、远端 `main`、成功 CI、tag、脚本正文和全部 24 个附件。缺失资产应先通过 `release-upload-draft` 续传，正文偏离模板时也应先重新上传草稿同步。同名异内容、tag 冲突或 CI 身份变化都会阻止发布。验证通过后设为正式版和最新 Release，不额外触发构建。

发布重试具有幂等性。若上次公开已成功但响应丢失，命令会核对同一提交、tag 和完整资产集后返回成功，不改写已公开内容。

草稿或 Release 存在不代表全部人工场景、GPU 硬件或最低系统版本已经测试。测试记录必须准确区分 CPU 回退、受控 worker 与真实 GPU 推理验收，并标明尚未实测的硬件和场景。工具回归位于 `tests/tooling`，模拟 API 测试不代替真实 CI 和上传结果。

## 6. 从 GitHub Actions 网页发布

在仓库 Actions 中选择 **qcs release**（`.github/workflows/qcs-release.yml`），点击 **Run workflow**，分支选择 `main`。工作流只提供手动入口，使用 `GITHUB_TOKEN`，不需要新增 PAT。先运行只读预检，写操作 job 使用 `contents: write` 与 `actions: read`。同一仓库的发布串行执行，新请求不会自动取消正在上传的发布。

| 输入 | 填写规则 |
| --- | --- |
| `action` | `draft` 创建或恢复草稿，`publish` 公开发布，默认 `draft` |
| `candidate_sha` | 已通过原生 CI 的完整 40 位提交 SHA，必须与本次发布工作流提交和当前远端 main 一致 |
| `ci_run_id` | 该候选对应的成功原生 CI run ID |
| `ci_attempt` | 要使用的准确 CI attempt，默认 `1` |
| `tag` | 与候选项目版本一致的标签，例如 `v0.3.0` |
| `freeze_run_id` | 新草稿留空，恢复草稿或公开发布时填写首次准备冻结材料的发布工作流 run ID |

新草稿的流程是预检、准备冻结材料、上传冻结 artifact，最后上传 Release 草稿。冻结 artifact 名为 `qcs-release-freeze-<完整候选SHA>-run<原生CI run ID>-a<原生attempt>`，保留 14 天，只包含原发布清单、`SHA256SUMS` 和原源码 ZIP，不重复保存 21 个用户包。工作流按实际资产大小检查磁盘空间，源码包校验后移动到冻结目录，避免保留重复副本。

草稿准备完成后，再次运行同一工作流，选择 `publish` 并填写原 `freeze_run_id`，其余候选、CI 和 tag 输入保持一致。恢复动作验证来源 workflow、提交、原冻结上传步骤、artifact 身份及摘要，下载原始三份冻结文件，再按清单中的原生 CI artifact ID 恢复 21 个用户包。清单时间与源码包不会重新生成。所有检查通过后才公开发布。

若草稿上传中断，只要冻结 artifact 完整且原冻结上传步骤成功，可以选择 `draft` 并填写原 `freeze_run_id` 续传。使用原始冻结 run ID，不能把只执行恢复的 run 当作新的冻结来源。冻结 artifact 或原生包 artifact 过期、来源不匹配、同名资产异内容都会明确失败。

本地也可恢复相同材料，输出目录必须为新的外置目录：

```powershell
& "$qcsSource/qcs-dev.ps1" release-restore `
  --freeze-run-id '<原发布工作流 run ID>' `
  --candidate-commit $qcsCommit --run-id $qcsRun --attempt 1 `
  --tag v0.3.0 --output-dir '<新的外置恢复目录>'
```

恢复完成后，使用恢复目录中的发布清单执行 `release-upload-draft` 或 `release-publish`。本地命令与网页入口都要求候选身份、冻结字节及远端状态一致，不能手工改清单或重新打包源码来恢复。

## 7. 只更新历史 Release 正文

历史正文维护独立于新版本发布，不要求历史提交等于当前 main，也不会替换历史二进制。脚本读取该标签对应提交的配置与原发布清单，保留原资产名，生成带准确 SDK 标签的下载链接。以下命令先查看 v0.2.0 正文，再明确执行更新：

```powershell
& "$qcsSource/qcs-dev.ps1" release-notes --tag v0.2.0 `
  --output '<外置目录>/v0.2.0-release-notes.md'
& "$qcsSource/qcs-dev.ps1" release-update-notes --tag v0.2.0 `
  --expected-commit e09c5d59d30bf04f0c7da5eada2fe339e844b9f9
```

更新请求只包含 `body`。脚本在更新前后核对 Release 身份、标签、提交和全部原资产，任何变化均报错。正文已经符合模板时不重复写入。v0.2.0 的 ROCm/Vulkan 标签会显示其原构建 SDK 版本，下载链接继续指向原先不含 SDK 版本的资产名，不重命名、不重新上传。此维护入口不会放宽新发布对干净 main、成功 CI 或完整资产集合的要求。
