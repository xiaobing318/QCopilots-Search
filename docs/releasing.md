# 安装包与发布

普通开发生成当前变体的安装布局和用户包。正式发布从指定成功 CI run、attempt 与提交获取全部成品，校验后生成源码材料与发布清单。`release-upload-draft` 创建或续传草稿，`release-publish` 在重新校验后公开正式 Release。这些命令不自动合并分支或触发构建。

## 1. 准备版本与 CI

在准备发布的提交中完成版本、依赖锁和说明更新，运行相关测试及本机完整 `check`。开发检查允许未提交修改，正式 `release-prepare` 则要求工作区干净、HEAD 与指定的完整提交 SHA 相同。

原生 CI 读取 `config/build-variants.json`，对 11 个变体执行构建、功能测试、安装、打包和最终包验证。矩阵详情见[依赖说明](dependencies.md)。每次成功选择包含 21 个单文件 artifacts：Windows 五个变体各提供安装器和便携 ZIP，Linux 五个变体各提供 DEB 和便携 TGZ，macOS 提供一个 DMG。Windows ROCm 仅在 NSIS 数据块触及大小限制时使用带完整 payload 的 `-setup.zip` 安装包。

每个 artifact 直接保存一个已验证用户包的原始字节，不额外套一层 ZIP。文件名统一为 `qcs-<版本>-<变体>.<扩展名>`，例如 `qcs-0.2.0-windows-x64-rocm.exe` 和 `qcs-0.2.0-windows-x64-rocm.zip`。完整提交、工作区状态、attempt 与依赖锁摘要仍保留在包内。诊断保留在 GitHub 原生日志，源码与可选符号独立生成。无 GPU runner 的 CPU 回退验证不代表 GPU 硬件执行已验证，GUI 与桌面操作检查见[测试指南](testing.md)。

推送 `main` 后由原生 CI 自动构建，无需为同一提交再次手动触发。仅在需要补跑时使用 `workflow_dispatch`。核对 run 的 `head_sha` 与候选提交一致，等待全部 11 个变体和汇总检查成功。正常 PR 也会自动运行 CI，tag 和 Release 发布不会重复触发全量构建。若验收失败，先修复并重新验收，再准备发布。

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
  --tag v0.2.0 --title 'QCopilots Search v0.2.0'
```

将示例 tag 和标题替换为本次实际版本。上传前重新读取远端 CI 身份、原始 artifact 摘要和本地文件字节，不再次完整下载全部包。对应 artifacts 必须仍可用且与准备时一致。已存在的 tag 必须指向同一提交。此命令仅接受草稿，已公开发布或指向其他提交时拒绝修改。

同名远端资产仅在状态、大小和 SHA-256 一致时复用，缺失项继续上传，同名异内容或未完成资产明确报错，不自动覆盖。中断后重复相同命令即可重新校验并补齐缺项。附件共 24 个，包括 21 个用户包、一份源码包、校验和及发布清单。

发布说明从冻结资产生成，按 Windows、Linux 和 macOS 分类列出各后端的安装器和便携包链接。“其他下载”仅列出 `SHA256SUMS`、对应源码材料和发布清单链接。正文只保留下载分类与附件链接，GitHub 自动展示完整 Assets 列表。安装、模型配置、平台要求及验证范围保留在仓库文档和测试记录中。

## 5. 公开正式版本

在准确且干净的 `main` 候选提交上，确认草稿所有附件上传完成，再显式公开：

```powershell
& "$qcsSource/qcs-dev.ps1" release-publish `
  --manifest "$qcsOutput/qcs-release-manifest.json" --tag v0.2.0
```

命令重新验证候选、远端 `main`、成功 CI、tag 和全部 24 个附件。缺失资产应先通过 `release-upload-draft` 续传，同名异内容、tag 冲突或 CI 身份变化都会阻止发布。验证通过后设为正式版和最新 Release，不额外触发构建。

发布重试具有幂等性。若上次公开已成功但响应丢失，命令会核对同一提交、tag 和完整资产集后返回成功，不改写已公开内容。

草稿或 Release 存在不代表全部人工场景、GPU 硬件或最低系统版本已经测试。测试记录必须准确区分 CPU 回退、受控 worker 与真实 GPU 推理验收，并标明尚未实测的硬件和场景。工具回归位于 `tests/tooling`，模拟 API 测试不代替真实 CI 和上传结果。
