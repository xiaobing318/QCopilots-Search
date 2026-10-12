# 容量与性能验收

容量验收分为物理文件遍历、合成向量检索、真实语义质量和故障恢复。四份结果各自说明测量边界。合成向量有高召回率不能推导实际用户文件的语义质量。

## 合成向量基准

`qcopilots-search-benchmark` 使用固定种子流式生成 768 维向量，实际写入 SQLite、构建 ANN、关闭并重新打开，然后与精确 top-20 对照。程序创建唯一的测试子目录，生成物不写入源码树。

CI 使用较小规模执行相同存储路径：

```bash
qcopilots-search-benchmark \
  --profile-dir "$build_root/ci-capacity" \
  --files 1000 --segments-per-file 10 \
  --queries 10 --timings 20 \
  --report "$build_root/reports/capacity-ci.json"
```

当前 Windows 的百万规模命令：

```powershell
$benchmark = Join-Path $buildRoot 'release/bin/qcopilots-search-benchmark.exe'
& $benchmark --profile-dir (Join-Path $buildRoot 'benchmark') `
  --files 100000 --segments-per-file 10 --queries 100 --timings 500 `
  --seed 20261011 --min-free-gib 3 --budget-gib 7 `
  --report (Join-Path $buildRoot 'reports/million.json')
```

`--keep` 保留本次唯一目录用于排查。默认清理由程序在关闭数据库、索引和句柄后执行。预算参数用于当前机器，可在其他机器调整。

要求 recall@20 ≥ 0.95。500 次预热后的检索计时以 P95 ≤ 500 ms 为目标，单分片后台重建时 P95 ≤ 1 s。查询编码、媒体提取和端到端延迟必须另报，不能混入或省略测量范围。基准还包含 10%、1%、0.1% 目录过滤的精确对照、增删改和后台单分片重建查询，报告分别保留其结果。

JSON 报告记录 `passed`、`error`、`recallAt20`、聚类/均匀分布召回、`latencyMilliseconds`、构建与重开状态、每条查询和磁盘采样。`sampledDirectoryPeakBytes` 是采样峰值，不是连续监测的严格上界。

## 物理文件遍历

`qcopilots-search-tests-file-capacity` 在动态创建的测试根生成真实小文件，验证遍历、修改对账、取消和排除。完整验收需实际生成十万个文件。CI 可运行较小规模，但必须在报告中写明文件数，不能沿用“十万”结论。

通过 `QCOPILOTS_SEARCH_PHYSICAL_FILE_COUNT=100000` 选择完整规模，`QCOPILOTS_SEARCH_DISK_RESERVE_BYTES` 指定磁盘空闲保留量。默认运行生成 1,000 个物理文件，不应把默认结果记为十万规模。

向量基准中的逻辑文件身份不等于真实磁盘文件，所以不能用其 `--files 100000` 参数替代物理遍历验收。

## 语义与故障验收

真实语义集要求至少 1,000 个真实片段、200 条中英有答案查询和独立无答案查询。覆盖文字、OCR、图片、语音、环境声、视频和代码。整体 Recall@10 ≥ 0.90，主要语言与模态分组 ≥ 0.80。数据来源、许可、标注、模型与提取指纹需要可追溯。

故障验收包括增删改、过滤、提交中断、worker 崩溃、磁盘不足和重启恢复。检查返回结果的文件版本，失效版本数量必须为零。普通存储重开用例不能替代中断时刻的故障注入。

## 磁盘预算与证据

本次 Windows 验收使用现有磁盘，开发 SDK、依赖、模型和编译产物峰值预算约 12 GiB，百万测试目录峰值预算 7 GiB，持续保留至少 3 GiB 可用空间。每阶段和批量写入前检查实际可用空间，空间不足应明确中止并保存错误，不冒险耗尽磁盘。

普通测试、十万文件、百万向量和最终打包分阶段执行，一次只重建一个分片。验收后保留报告、随机种子、指纹、源码提交和最终包，只有本任务创建的可再生产物可被边界校验后清理。

上述数值是本次验收参数，不是应用或测试框架对所有电脑的全局假设。最终结论以实际报告为准。
