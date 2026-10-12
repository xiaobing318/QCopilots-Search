# 发布指南

发布流程从通过测试的不可变提交生成安装布局，再自动生成平台包、校验和、依赖清单和测试报告。开发分支和草稿 PR 可以推送，合并、签名、公证及公开发布由维护者决定。仓库 CI 不自动创建公开 release。

## 发行门槛

| 门槛 | 必需证据 |
| --- | --- |
| Windows x64、Linux x64、macOS arm64 / x86_64 | 四个原生 CI job 的构建、CTest、真实模型和包级结果 |
| 推理兼容性 | 九条固定黄金向量 cosine ≥ 0.999，CPU 基线与当前分发路径 |
| 语义质量 | 真实片段与查询集来源、逐查询及分组 Recall@10、无答案结果 |
| 容量和恢复 | 当前 Windows 百万向量、十万物理文件、性能与故障结果 |
| 用户操作 | 最终 Windows 包的 Computer Use 十个场景及截图 |
| 运行依赖 | 脱离开发路径的程序、服务、worker、Qt 插件、OCR 数据和字典验证 |
| 许可与源码材料 | 实际部署依赖的许可证、版本、源归档、补丁及重建说明 |
| 签名状态 | macOS 对实际 staging APP 执行 `codesign --verify --deep --strict` 并读取身份，区分 ad-hoc、Developer ID、其他证书、未签名、无效与未知状态，公证单独记录 |

任一必需证据缺失时，保持草稿或测试包状态。操作系统 runner 版本比产品最低版本新，因此原生 CI 通过不自动证明最低操作系统版本经过实机验收。

发布检查同时读取 CTest 进程结果和 QtTest 用例明细。测试程序退出码为零不能替代内部用例执行证据。`reports/tests/qt-test-cases.json` 必须没有未执行的必需用例或缺失明细。唯一经过明确分类的平台不适用用例及其精确匹配规则见 [测试指南](testing.md)，这类用例保留单独计数，不计为通过。

## 构建与安装布局

先执行开发脚本的 `configure`、`build` 和 `test`。`install` 调用 `cmake --install`，将程序及依赖放入 `<build-root>/stage`。安装布局是后续部署和打包的依据，不手动复制开发目录里的 DLL 来补包。

```powershell
./qcopilots-search-dev.ps1 install --build-root $buildRoot --qt-root $qtRoot
./qcopilots-search-dev.ps1 package --build-root $buildRoot --qt-root $qtRoot --generator ZIP
./qcopilots-search-dev.ps1 package --build-root $buildRoot --qt-root $qtRoot --generator NSIS
./qcopilots-search-dev.ps1 verify-package --build-root $buildRoot --qt-root $qtRoot
```

Windows NSIS 生成器需要安装 NSIS。macOS 使用 `DragNDrop`，分架构生成 `.app` 和 `.dmg`。Linux 分别使用 `TGZ` 和 `DEB`。包位于 `<build-root>/packages`，文件统一采用 `qcopilots-search` 前缀，校验和写入 `SHA256SUMS`。

## 包级验收

使用新的目录解压或安装最终包。测试路径应与源码、SDK、构建树和 staging 不同。运行时清除开发 Qt 插件环境和工具链 PATH，仅保留系统及安装目录。使用新的 profile 启动 GUI、服务、worker 和 CLI，验证索引与查询、PDF 和媒体资源定位、OCR 数据、字典、重启以及卸载后数据保留策略。

自动部署检查和用户操作验收分别报告。只运行 `--version` 可以证明一部分动态库加载，不能替代 GUI 插件加载、后台索引或多媒体预览。`verify-package` 的实际执行范围需要在报告中注明，缺少的完整流程由包级测试补齐。

Windows CI 分别验证 ZIP 和最终 NSIS。NSIS 验证真实执行单用户安装、GUI / 服务 / worker / 提取及搜索测试，再执行卸载并检查独立用户配置保留。若检查到已有产品安装，验证器会停止，不修改已有安装。

Linux CI 在固定 Ubuntu 22.04 容器中使用 `dpkg --install` 安装最终 DEB，随后以普通用户从 `/usr` 运行共同包级验证器。验证器根据 `dpkg-query --listfiles` 的清单核对实际加载的库，避免系统中已有 Qt 掩盖漏包。完成后执行 `dpkg --remove`，确认包文件已移除且独立 profile 的文件哈希全部保留。源码和 DEB 以只读方式挂载，开发机和 CI 宿主不安装该 DEB。

相应报告分别为 `reports/package-nsis/report.json` 和 `reports/package-deb/report.json`。后者同时保存容器日志及普通用户 smoke 报告。容器方案与脚本存在不代表验收已经通过，实际结果以对应原生 CI 为准。

## 材料清单

每个发行候选至少保留：

- 平台安装包、SHA-256 和源码提交。
- CTest JUnit、原生 CI 链接、模型与语义质量报告、容量报告、Computer Use 报告。
- 构建配置、工具链版本、依赖锁、运行库和插件清单。
- 调试符号，独立于普通用户包存放。
- 对应源码归档、上游源归档校验值、应用补丁、可复现的构建脚本和 `source-integrity.json`。
- 应用 MIT、DigUp MIT、各动态库及其随附第三方组件的许可证材料。
- 签名与公证状态及验证结果。

模型由下载或离线导入获得，不放进安装包。OCR 数据和中文分词字典是正常运行资源，需要随包部署并保留归属。

运行 `tools/qcopilots-search-source-bundle.py --build-root <root> --qt-root <sdk> --download` 生成实际对应源码包及校验清单，详见 [对应源码材料](source-materials.md)。生成最终材料时要求使用干净的发行提交，开发阶段的 `workingTreeModified` 标记不能被删除来伪装不可变来源。

## CI 和人工发布

`.github/workflows/qcopilots-search-native.yml` 在 PR 创建或同步、main 推送时运行四个原生组合，也支持手动触发。每个 job 使用独立外置根，执行校验过的模型下载、测试、小规模容量和平台打包，并上传报告与测试包。

CI 的 `required` job 下载当前运行的四份原生 artifact，重新核对全部文件 SHA-256、CTest 注册集合和用例明细、九条黄金向量、真实语义逐查询结果、模型校验、容量与包级报告，再生成 `qcopilots-search-native-aggregate.json`。必需报告缺失、重复平台、失败或来源不一致都会失败。这个结果只代表四个原生组合，最终 Windows 百万规模和 Computer Use 由下面的本机门禁单独核验。

PR 的候选提交使用事件中的 `pull_request.head.sha`。实际 CI checkout 通常是 GitHub 生成的 merge commit，两者分别记录。四个原生 job 必须执行同一 checkout，最终本机候选必须与其候选 head、Git tree 和依赖锁哈希一致。不能把 merge commit 与 head 字符串不同误判为构建失败，也不能将旧报告事后绑定到新的 HEAD。

## 本机最终证据流程

使用 `tools/qcopilots-search-release-evidence.py` 记录构建与验收。它使用 Python 标准库，所有运行创建外置唯一目录，保留执行前来源、实际命令、退出码、原始 stdout / stderr、执行时间和产物 SHA-256。聚合器不执行发布，也不根据报告中的单个 `passed` 字段放行。

先提交并冻结候选，然后在干净 checkout 下记录一次实际完整构建。路径均由调用者提供，下面的 `$source`、`$buildRoot`、`$qtRoot`、`$evidenceRoot` 和 `$python` 分别指向当前源码、外置构建根、SDK、证据根及 Python。`--program` 至少记录四个应用程序、百万容量程序和物理文件测试程序，程序的实际生成路径以当前构建为准。

```powershell
$tool = Join-Path $source 'tools/qcopilots-search-release-evidence.py'
& $python $tool record-build --source $source --output-root $evidenceRoot `
  --program "qcopilots-search=$buildRoot/release/bin/qcopilots-search.exe" `
  --program "qcopilots-search-service=$buildRoot/release/bin/qcopilots-search-service.exe" `
  --program "qcopilots-search-worker=$buildRoot/release/bin/qcopilots-search-worker.exe" `
  --program "qcopilots-search-cli=$buildRoot/release/bin/qcopilots-search-cli.exe" `
  --program "qcopilots-search-benchmark=$buildRoot/release/bin/qcopilots-search-benchmark.exe" `
  --program "qcopilots-search-tests-file-capacity=$physicalTest" `
  -- pwsh -NoProfile -File "$source/qcopilots-search-dev.ps1" build `
       --build-root $buildRoot --qt-root $qtRoot
```

命令输出新的 `build-record.json` 路径。将它保存为 `$buildRecord`，后续 `run-local` 在实际启动前核对 clean source 和 producer 的 SHA-256，在进程结束后再次核对。`{program}`、`{evidence}`、`{report}` 由工具展开，分别表示已经核验的程序、本次唯一目录和报告路径。不要将旧报告复制到新目录冒充新执行。

```powershell
& $python $tool run-local --build-record $buildRecord --output-root $evidenceRoot `
  --kind physical --program-name qcopilots-search-tests-file-capacity `
  --report physical.json --env 'QCOPILOTS_SEARCH_PHYSICAL_FILE_COUNT=100000' `
  --env 'QCOPILOTS_SEARCH_PHYSICAL_FILE_REPORT={report}' `
  --env 'QCOPILOTS_SEARCH_TEST_ROOT={evidence}/temporary' `
  --env 'QT_FORCE_STDERR_LOGGING=1' -- '{program}' -o -,txt
```

物理测试需要由运行环境提供构建依赖的位置，且 `QCOPILOTS_SEARCH_TEST_ROOT` 的父目录必须可写。百万向量测试使用独立监控器，通过 `--input` 保留监控脚本原文和执行前后哈希。监控器的 `ExternalRoot` 必须使用 `{evidence}`，让原始 benchmark JSON、采样 JSONL 和输出日志都保留在本次根内。下面的 `$millionRunner` 是维护者选择并审查的实际监控脚本路径，不是假定另一台电脑存在的固定路径。

```powershell
& $python $tool run-local --build-record $buildRecord --output-root $evidenceRoot `
  --kind million --program-name qcopilots-search-benchmark `
  --report 'reports/*.monitor.json' --input $millionRunner `
  -- pwsh -NoProfile -File $millionRunner -BenchmarkExe '{program}' `
       -ExternalRoot '{evidence}' -QtBin $qtBin -Mode Run
```

百万验收要求实际写入和重载至少 1,000,000 个 768 维向量、至少 100 条不同 oracle 查询和 500 次预热后计时，同时验证过滤、CRUD、后台重建、四个崩溃恢复边界及独立磁盘采样。原始报告和监控报告均保留，绝对路径只作为原始记录，由附件映射对应到可移植相对路径。采样峰值不冒充连续测量峰值。

执行 `package` 和 `verify-package` 后，将最终 ZIP、验证报告和上述 build record 交给 `record-package`。该命令核对 ZIP 内四个可执行文件与本次构建的字节一致，生成 `package-candidate.json`。`$modelFingerprint` 和 `$extractionFingerprint` 来自相同候选实际语义验收，不使用人工编造的版本名称。

```powershell
& $python $tool record-package --build-record $buildRecord --package $finalZip `
  --verification $packageReport --output-root $evidenceRoot `
  --model-fingerprint $modelFingerprint --extraction-fingerprint $extractionFingerprint
```

以新生成的 package candidate 初始化 Windows Computer Use 记录，再对这一个最终包完成十个场景。报告必须带相同候选身份、包哈希、四个程序哈希、模型和提取指纹，每个场景保留预期、实际操作、结果、时间、截图与操作日志。JSON 和其 `files` 清单位于同一个运行根，附件路径为相对 POSIX 路径。旧跨包报告保留历史价值，但缺少执行前来源链时属于 `unbound`，不能充当最终候选验收。

将 `record-package`、两个 `run-local` 和 Computer Use 的原始 JSON 路径分别赋给下列变量。`collect-local` 只复制已列入哈希清单的证据，不导入整个数据库目录。四份 GitHub 原生 artifact 分别解压到 `$nativeArtifacts` 的四个子目录，保持各自 `reports` 和 `packages` 布局。

```powershell
& $python $tool collect-local --candidate $candidateRecord --million $millionRecord `
  --physical $physicalRecord --computer-use $computerUseRecord --output-root $evidenceRoot
& $python $tool preflight --candidate-commit $candidateHead `
  --native-artifacts-dir $nativeArtifacts --local-evidence-dir $collectedLocalRoot `
  --output "$evidenceRoot/final-preflight.json"
```

最终命令只有在四组合原生证据及全部本机门槛满足时返回 0。其失败报告逐项保留缺失、规模不足、原始结果不一致或来源失配的原因。纯粹整理旧报告、合成验证器 fixture 通过、部分平台测试通过，都不会成为最终发布完成证明。

macOS 部署使用 ad-hoc 签名，验收报告以实际 `codesign` 命令及退出码为依据。ad-hoc 不等于 Developer ID 分发签名，签名验证也不证明已公证。报告中的签名范围是 staging APP，未生成 APP、命令不可用或身份无法识别时记为未知，其他平台尚未配置签名验证时同样记为未知。正式签名、公证凭据通过受控发布环境配置，不能写入仓库、日志或普通测试产物。签名后重新计算包校验和，并验证签名后的最终包。维护者确认全部验收和许可材料后再决定公开发布。
