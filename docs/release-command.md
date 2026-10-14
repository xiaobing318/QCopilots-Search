# 自动准备发行材料与上传草稿

`tools/qcs-release.py` 提供 `prepare` 和 `upload-draft` 两个阶段。前者收集同一冻结候选已经通过验收的材料，后者只在显式调用时创建或续传 GitHub 草稿。两者都不会公开发布 Release。

统一开发入口也提供对应动作：Windows 使用 `qcs-dev.ps1 release-prepare` 和 `qcs-dev.ps1 release-upload-draft`，macOS/Linux 使用 `qcs-dev.sh` 的同名动作。参数与下文核心命令相同，统一入口自动选择仓库内工具。

本工具复用 [发行证据工具](../tools/qcs-release-evidence.py) 的完整 `preflight`，以及开发入口 `inspect_check` 的本地 full 检查，不以某份旧报告中的 `passed: true` 代替原始证据验证。轻量工具回归使用合成数据和模拟 GitHub API，不代表应用发行验收通过。

## 运行前提

- 当前 checkout 必须干净，HEAD 为准备发布的完整 40 位提交 SHA。源码树和依赖锁哈希必须与原始验收记录一致。
- 四个平台的原生 artifact 已下载并解压。父目录的每个直接子目录含 `reports/native-evidence.json`，平台集合必须恰好为 Windows x64、Linux x64、macOS arm64 和 macOS x64。
- 本机已按 [发行流程](releasing.md) 使用 `collect-local` 收集候选包、百万向量、十万物理文件和 Windows Computer Use 原始记录。传入包含 `manifest.json` 的目录。
- 同一候选已经运行 `check --test-profile full`，保留完整 `check-report.json` 及其绑定的日志、测试和安装包材料。fast 或存在未执行阶段的报告不能用于本阶段。
- 输出目录必须位于源码树之外，且不得与输入证据重叠。准备阶段使用新的输出目录，不覆盖旧候选。

`prepare` 会复制已绑定的证据和包，并生成单独的证据 ZIP。原始包、上传资产和归档会占用多份磁盘空间，请为四平台材料及本机 full 产物预留空间。该阶段不会下载模型或重新生成应用、符号和源码包。缺失的材料应先通过开发与验收流程生成。

## 准备材料

以下变量由调用者指定，所有路径均指向本次实际候选。`$source` 是源码目录，`$python` 是 Python 3，`$candidateHead` 是完整 SHA，`$nativeArtifacts` 是四平台解压父目录，`$collectedLocalRoot` 是 `collect-local` 输出，`$localFullReport` 是本地 full 报告，`$releaseOutput` 是新的外置输出目录。

```powershell
& $python "$source/tools/qcs-release.py" prepare `
  --candidate-commit $candidateHead `
  --native-artifacts-dir $nativeArtifacts `
  --local-evidence-dir $collectedLocalRoot `
  --local-full-report $localFullReport `
  --output-dir $releaseOutput
```

macOS 和 Linux 使用相同参数，换用 shell 的续行语法即可。源码目录默认取工具所属 checkout，可显式传入 `--source-dir`，但不得指向另一份与当前工具不同的 checkout。

成功后输出 `qcs-release-manifest.json` 的路径及 SHA-256，目录布局如下：

```text
release-output/
  qcs-release-manifest.json
  assets/
    qcs-<platformId>-<原始包名去掉 qcs- 前缀>
    qcs-<platformId>-licenses.zip
    qcs-local-windows-x64-<最终 Computer Use 候选包名>
    qcs-LICENSE
    qcs-THIRD_PARTY_NOTICES.md
    qcs-release-evidence-<提交前12位>-part001.zip
    SHA256SUMS
  evidence/
    native/<platformId>/reports/...
    native/<platformId>/pkg/...
    local/...
    local-full/...
    release-preflight.json
```

各原生平台的包、源码、符号和源码 manifest 保留独立来源。尤其是原生 Windows CI 包与本机最终 Computer Use 包可能具有相同文件名和不同字节，工具会分别命名，不会自动认定它们是同一个包。`computer-use-candidate` 角色明确标记实际进行 Windows 界面验收的本机候选。

准备阶段除了执行公共门禁，还验证对应源码 ZIP 的内部清单与 sidecar 一致、上游来源符合锁定记录、项目文件对应当前提交。源码内容按候选 `.gitattributes` 的 Git clean 规则比较提交 blob，接受合法的文本 CRLF/LF 差异，`-text` 夹具仍逐字节区分。符号 ZIP 必须含四个应用程序的调试信息，程序哈希必须对应该平台构建记录。许可 ZIP 来自包验收已经绑定并验证的运行时许可材料。

`freezeManifest` 绑定原始证据、资产和本地 full 报告的相对路径、大小及 SHA-256。`SHA256SUMS` 使用实际上传文件名，包含其他全部资产，不包含自身。冻结 manifest 自己的 SHA-256由准备命令输出，并在草稿正文中记录，避免自引用校验和。

原始证据 ZIP 按大小自动分成多个可独立解压的 `partNNN` 文件，同一目录下解压所有分块即可恢复 `evidence` 布局。每个发行资产必须小于 2 GiB，超过限制的单独输入包会明确失败，不会创建一个必定上传失败的发行包。该限制来自 [GitHub Releases 文档](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

## 显式上传草稿

上传仅面向 `xiaobing318/QCopilots-Search`。使用 `GH_TOKEN` 或 `GITHUB_TOKEN` 环境变量提供 GitHub 凭据，也可以使用已经登录 `github.com` 的 GitHub CLI。工具不会读取浏览器凭据、输出令牌或把令牌传到命令行参数中。认证需要访问该仓库并创建草稿与上传发行资产的权限。

```powershell
& $python "$source/tools/qcs-release.py" upload-draft `
  --manifest "$releaseOutput/qcs-release-manifest.json" `
  --tag v0.1.0 `
  --title 'QCopilots Search v0.1.0'
```

这个命令会写入远端，应由发布负责人显式调用。工具在任何远端写入前重新验证本地候选来源、冻结文件、原始总门禁和本地 full 报告。它随后核实远端仓库规范名称和候选提交存在，并核对已有 tag 的最终 commit。

已有记录必须仍为草稿，tag 和 `target_commitish` 必须固定到同一候选 SHA。指向分支的旧草稿不会自动重定向，已公开 Release 也不会被修改。创建请求明确包含 `draft: true`。相关字段见 [GitHub Releases REST API](https://docs.github.com/en/rest/releases/releases)。

同名远端资产只有在状态、字节数和 SHA-256 一致时才会跳过。GitHub 返回可用的 SHA-256 digest 时直接核验，否则工具流式读取远端资产并计算 SHA-256。不会只比较名称和大小，也不会通过删除或覆盖文件解决冲突。下载跳转仅允许 GitHub 的发行资产存储域，API 令牌不会转发到签名下载地址。相关接口见 [GitHub Release Assets REST API](https://docs.github.com/en/rest/releases/assets)。

上传结束后再次枚举资产并验证完整性，输出实际仓库 ID、Release ID、资产 ID、已上传项和复用项。结果始终标明 `draft: true` 和 `published: false`。发布负责人可以在 GitHub 中检查草稿，然后独立决定是否公开发布。

## 失败与恢复

准备缺少必需证据、来源不一致或内容变化时返回非零，不生成可上传的冻结目录。已有输入报告保留原状，修复后重新使用新输出目录准备。

上传中断后保留草稿和已经成功上传的资产。再次运行相同 `upload-draft` 命令会复验全部证据和远端资产，仅补齐缺失项。遇到同名异内容、未知旧资产或 GitHub 留下的未完成 `starter` 资产时明确失败，工具不自动删除任何远端内容。发布负责人应先查明冲突来源，再决定如何处理。

冻结后修改代码、包或证据需要重新形成候选和验收记录。不要修改旧 manifest 的来源、哈希或通过状态来继续上传。

## 工具回归

`tools/qcs-test-release.py` 已注册为轻量 CTest `qcs-tests-release-tool`。也可以直接执行：

```powershell
& $python "$source/tools/qcs-test-release.py" -v
```

测试在外置临时目录中运行，遵守 `QCOPILOTS_SEARCH_TEST_ROOT`，覆盖缺失门禁、冻结后变更、完整资产来源、源码与符号契约、远端同名异哈希、断点续传、已公开 Release、错误 tag、认证输出及下载跳转。测试全部使用模拟 API，不创建真实 GitHub Release。
