# 安装包与发布

普通开发生成当前变体的安装布局和用户包。正式发布从指定成功 CI run、attempt 与提交获取全部成品，校验后生成源码材料与发布清单。上传动作只创建或续传 GitHub Release 草稿，不自动合并分支或公开发布。

## 1. 准备版本与 CI

在准备发布的提交中完成版本、依赖锁和说明更新，运行相关测试及本机完整 `check`。开发检查允许未提交修改，正式 `release-prepare` 则要求工作区干净、HEAD 与指定的完整提交 SHA 相同。

原生 CI 读取 `config/build-variants.json`，对 11 个变体执行构建、功能测试、安装、打包和最终包验证。矩阵详情见[依赖说明](dependencies.md)。正常结果是 11 个 package artifacts 与 21 个用户文件：Windows 五个变体各提供安装器和便携 ZIP，Linux 五个变体各提供 DEB 和便携 TGZ，macOS 提供一个 DMG。Windows ROCm 超大载荷可以使用带完整 payload 的 setup ZIP 作为安装器。

每个 artifact 只包含对应变体的已验证用户包，诊断保留在 GitHub 原生日志。源码与可选符号独立生成。无 GPU runner 的 CPU 回退验证不代表 GPU 硬件执行已验证，GUI 与桌面操作检查见[测试指南](testing.md)。

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

准备前确认所选 CI 的 11 个变体成功，指定准确的 run ID、attempt 和 40 位提交 SHA。使用新的外置输出目录。认证来自 `GH_TOKEN`、`GITHUB_TOKEN` 或已登录的 GitHub CLI，工具不输出令牌。

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

准备阶段验证仓库、workflow、提交、run、attempt、job、artifact 与下载摘要，再检查包白名单、变体、类型、数量和实际文件 SHA-256。部分重跑可以保留未重跑的成功 job，最新失败不能借用旧成功产物。缺项、来源不明、额外文件或摘要不匹配都会失败。

同一干净提交及依赖锁生成一份平台无关源码材料，不重新构建应用。输出为：

```text
<release-output>/
  qcs-release-manifest.json
  assets/
    <21 个用户包>
    qcs-source-materials-<commit12>.zip
    SHA256SUMS
```

发布清单记录提交、CI 来源、资产名称、大小和 SHA-256，使用 schema 3。下载的原始 artifact 仅用于临时校验和抽取，不作为永久发布材料。校验和不包含自身。

每个 Release 附件必须小于 2 GiB，超限明确失败，不自动分卷或裁剪 GPU 目标。失败后保留控制台诊断，修复后使用新的输出目录重新准备，不手工修改清单绕过校验。

## 4. 上传草稿与恢复

对准备好的实际包完成所需的人工检查后，显式上传草稿：

```powershell
& "$qcsSource/qcs-dev.ps1" release-upload-draft `
  --manifest "$qcsOutput/qcs-release-manifest.json" `
  --tag v0.1.0 --title 'QCopilots Search v0.1.0'
```

将示例 tag 和标题替换为本次实际版本。上传前再次临时下载所选 CI artifacts，校验其摘要与成员文件是否匹配本地资产，同时核对远端提交。因此准备和上传阶段都需要能够下载对应 CI artifacts。已存在的 tag 必须指向同一提交。已有 Release 必须仍为草稿，已公开发布或指向其他提交时拒绝修改。

同名远端资产仅在状态、大小和 SHA-256 一致时复用，缺失项继续上传，同名异内容或未完成资产明确报错，不自动覆盖。中断后重复相同命令即可重新校验并补齐缺项。附件共 24 个，包括 21 个用户包、一份源码包、校验和及发布清单。

草稿存在不代表人工场景、GPU 硬件或最低系统版本已经测试。公开发布与合并由维护者决定。工具回归位于 `tests/tooling`，模拟 API 测试不代替真实 CI 和上传结果。
