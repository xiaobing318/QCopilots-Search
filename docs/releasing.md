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
| 签名状态 | 明确 unsigned、signed 或 notarized，不以文件存在推断已签名 |

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

手动 `release_preflight` 只检查验收材料是否完整且成功，不执行公开发布。独立 Windows Computer Use、语义和百万规模证据必须提供后才能通过这一门槛。没有证据的工作流应明确失败，不能自动制造通过摘要。

测试包默认未签名。正式签名、公证凭据通过受控发布环境配置，不能写入仓库、日志或普通测试产物。签名后重新计算包校验和，并验证签名后的最终包。维护者确认全部验收和许可材料后再决定公开发布。
