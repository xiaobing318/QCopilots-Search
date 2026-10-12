# 实施进度

当前阶段是最终候选冻结前的集成修复与发行验收。工程、推理和提取、服务和检索、Qt桌面应用、开发脚本及四组合CI已实现，完整交付门槛尚未全部通过。

本文件说明实现边界与已归档的历史基线，不追记每次运行的最新计数。最终结论以绑定不可变源码提交、实际程序和安装包哈希的外置报告、CI artifact及 [草稿PR #1](https://github.com/xiaobing318/QCopilots-search/pull/1) 中的证据为准。测试完成后不通过反复修改本文件来改变候选提交，也不把后来取得的HEAD追认成旧报告的执行来源。

下表路径相对于运行时选定的外置构建根，默认以 reports/ 保存报告。实际文件位置与SHA-256由各次证据清单记录，不要求其他电脑使用相同盘符或目录。以下均是已经执行的历史结果，不代表下一冻结候选已经重新通过。

| 已归档验证范围 | 实际结果与适用边界 | 主要证据路径 |
| --- | --- | --- |
| Windows普通集成，9b0e0d4阶段 | 23/23个CTest程序通过，46.83秒，368个QtTest PASS、0 FAIL、1个Windows不适用案例。所选标签为unit、storage、process、gui、portable，登记清单与输出一致。真实模型及完整语义测试单独执行 | reports/integration-cu-fixes/ctest.xml、case-evidence.json、inventory.json |
| Windows原生CI，9b0e0d4 PR head | 26/26 CTest通过，1126.10秒，398个QtTest PASS、0 FAIL、1个Windows不适用案例。实际PR合并checkout为ed9260840db6eaf782698ab64b7f5d50794b7e2b。随后NSIS打包失败，作业整体仍为failure，不能用测试通过代替完整CI通过 | reports/ci-38207590784/windows-native-test-evidence.json、windows-x64.log与windows-artifact-reports/reports/ |
| 默认CPU与强制baseline数值回归 | 9条参考向量分别执行，两组cosine均≥0.999。两个reference程序共30个QtTest PASS，包含初始化、清理与其他契约检查，不是30条黄金向量。仅属于已执行的Windows环境 | reports/runtime-current/ctest.xml与case-evidence.json中的两个reference程序 |
| OCR v3与真实语义质量 | 图片回归32项通过，包含简体、繁体中文的两种字号及有界低置信重试。semantic v3实际完成138文件、1,310个编码片段、222条有答案查询，生产检索与精确对照均为221/222，Recall@10为0.9954955。英文111/111、中文110/111，各主要语言和模态分组达到门槛，环境声音仍有1条漏召回 | reports/image-junit.xml、image-tests.txt、semantic-quality.json与semantic-v3/ctest.xml |
| 十万个物理文件 | 实际创建100,000个文件，验证遍历、排除、运行中取消、修改100、删除100、新增100。失效结果为0，清理确认成功，CTest案例耗时184.10秒。该项与向量容量验收分开计算 | reports/physical-files-100k.json与physical-files-100k/ctest.xml |
| 可移植路径与只读源码子集 | 中文空格路径、任意工作目录、独立外置构建根和真实NTFS拒绝写入的源码树下完成configure、所选目标build、4/4 CTest。198个源码文件前后哈希一致。该历史子集绑定fc813ef63fe2c06148fbe617e21dfd96b9deb7c9，不代表最终完整应用或其他系统已验证 | test-runs/portable-0c00ba22/reports/qcopilots-search-portability-report.json |
| Windows 9b0e0d4历史安装包 | ZIP的9项、NSIS的11项检查通过，覆盖迁移安装位置、四个程序、实际格式提取、服务索引查询、GUI启动及依赖来源。NSIS安装与卸载保留独立profile。此包未签名，对应源码材料仍须随最终候选重新生成 | reports/package-final-manifest-9b0e0d4.json、package-zip-9b0e0d4/report.json、package-nsis-9b0e0d4/report.json |
| Windows Computer Use历史完整覆盖 | CU01–CU10均取得场景证据，共74张截图，覆盖来自7615552与9b0e0d4两个包。CU08欢迎页关闭、CU10托盘暂停与打开由用户协助，工具随后核实实际状态。scenarioCoveragePassed=true，但混合版本历史的releaseEligible=false、顶层passed=false，不能声称单一最终包全流程通过 | reports/windows-computer-use-history-9b0e0d4.json与.md |
| 第一轮百万向量实测 | 实际写入、建图、关闭并重载1,000,000个768维向量，100条精确对照Recall@20为0.932，低于0.95。500次计时的未过滤P95为74.3349ms，1%与0.1%过滤P95分别为569.9402ms和525.4593ms，超过500ms。确认失败后停止，CRUD、并发单分片重建及4种进程丢失恢复尚未完成，不能记为通过 | reports/capacity-million-first-run-summary.json、capacity-million-first-run-stop.json及其引用的原始benchmark、monitor、采样和日志 |
| 百万失败后的检索修复 | ANN查询预算调整至efSearch=4096，目录范围改用可索引的字面范围条件，并修复最近文件按片段提前截断的问题。定向存储回归与独立探针已有证据，但没有以这些小规模或停止快照结果替代新的完整百万验收 | reports/storage-4096-*.xml、storage-filter-range-regressions.txt、qcopilots-search-ann-budget-20261012.json |
| 平台部署与工具链修复 | Linux PNG/ZLIB版本符号、macOS依赖与CPU模块部署路径、macOS模型目录容量查询及NSIS UTF-8编译参数已作修正。模块配置、布局或Windows回归已有证据，其中模型管理24项通过，NSIS小工程实际编译成功。布局夹具和Windows执行不能充当macOS签名、模型加载或Linux最终链接通过 | reports/runtime-install-layout.json、runtime-backend-layout.xml、model-files-apfs-fix-windows.txt、nsis-encoding-probe-path.txt及其指向的原始探针日志 |

普通集成与Windows CI中唯一的不适用案例是IndexRepositoryRegressionTest::rootFilterRespectsCaseSensitivePlatformKeys()，专门验证非Windows平台的大小写敏感路径规则。Windows采用明确的大小写折叠规则，该项没有计入368或398个PASS。其他缺能力、缺模型或缺材料的跳过不能沿用这一豁免。

语义语料的目标资料与标签在观察模型结果前共同编写，属于受控实施验收集，不是独立人工盲评。真实自然照片、声音和视频样本仍然较少，不能推广为所有环境、口音或题材的质量保证。另20条无答案查询分为calibration和holdout各10条，全部仍返回10个候选，当前没有校准的无答案拒绝承诺，不能写成“无答案检测通过”。第一轮失败语义证据继续保留。

四组合历史CI的完整流程尚未通过。[run 38207590784](https://github.com/xiaobing318/QCopilots-search/actions/runs/38207590784) 中Windows在测试通过后NSIS打包失败，Linux worker链接缺少PNG/ZLIB版本符号，macOS暴露依赖解析和嵌套CPU模块签名问题。修复后的原生执行结果必须另行取得。取消的旧运行不视为新提交的失败，也不能作为新提交成功的证据。

最终不可变候选仍需完成以下门槛：

- 同一候选的Windows x64、Linux x64、macOS arm64与x64原生CI，包括完整测试、真实模型、语义验收和对应平台包验证。
- 修复后的完整百万规模建图与重载、精确对照、过滤、500次预热计时、后台单分片重建、增删改和故障恢复，保留磁盘预算与监控证据。
- 对同一最终Windows包重新完整执行CU01–CU10，使用新的独立profile、实际截图和逐场景包及程序哈希。历史混合版本报告只能保留为历史记录。
- 将物理文件、可移植性、安装包、源码完整性等必需证据绑定到真实执行时的构建记录，不能从当前HEAD反推旧报告来源。包内程序、模型和提取指纹、依赖锁与报告附件须能够重新校验。
- 最终ZIP/NSIS、各架构DMG、TGZ/DEB及对应源码材料、许可文件、校验和、调试符号和签名状态记录。公开发布与合并仍由用户决定。

外置不可变报告、CI artifact和PR中的来源清单是最终判据。本文件的历史成功计数、模块修复说明或安装包启动记录均不能单独解除上述门槛。
