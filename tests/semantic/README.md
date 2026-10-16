# 多模态功能测试

`qcs-tests-semantic` 使用 12 个固定输入验证正式提取、真实模型编码、索引持久化、关闭后重载和语义搜索。默认 CTest 运行包含此测试，必须提供锁定模型和 OCR 数据，缺失依赖会明确失败。

| 输入标识 | 内容 | 验证的提取通道与定位 |
| --- | --- | --- |
| `text` | 原创短文本 | 文本、来源文件 |
| `pdf` | 原创 PDF 通知 | 文本、物理页码 |
| `code` | 原创 Python 文件 | 文本、起止行号 |
| `illustration` | 原创红色圆形图片 | 图片、来源文件 |
| `photo` | NASA 地球合成照片 | 图片、来源文件 |
| `ocr-en` | 原创英文告示图片 | 实际 OCR 文字、图片、来源文件 |
| `ocr-zh` | 原创中文告示图片 | 实际汉字 OCR 文字、图片、来源文件 |
| `speech` | 固定语音录音 | 音频、起止时间 |
| `environment` | 固定狗叫录音 | 音频、起止时间 |
| `tone` | 原创纯音 WAV | 音频、起止时间 |
| `video-generated` | 原创运动圆形 AVI | 视频帧、起止时间 |
| `video-natural` | 固定大雨 WebM | 视频帧、音频存在时一并编码、起止时间 |

每个输入存放在独立子目录，全部数据写入同一临时索引后关闭并重开。每个输入执行英文和中文各一次查询，共 24 次。查询使用真实 Query 或 CodeQuery embedding，传给 `IndexRepository::search` 的关键词字符串保持为空，并通过正式目录过滤限定当前来源文件。断言正式语义路径返回该文件且 `matchSource` 为 `semantic`，返回的 segment ID、页码、行号、单元格及时间范围须与重载后的真实持久化片段一致。

功能测试不判断不同文件的相关性顺序，不设置最低 cosine、召回率或无答案误返回率。模型输出继续检查 768 维、有限值和单位长度，持久化 FP16 向量使用适当数值容差。锁定模型的黄金向量数值一致性由 `tests/runtime` 保留的测试负责。

二进制素材集中放在 `tests/acceptance/fixtures/media`。`tests/acceptance/fixtures/assets.json` 记录来源、许可证、字节数和 SHA-256，`media/NOTICE.md` 保留归属说明。GUI 手工操作场景共用其中部分素材。测试运行时在外置临时目录生成原创文档、PDF、OCR 图片和合成音视频，不向源代码目录写入生成物。

执行方式：

```powershell
$env:QCOPILOTS_SEARCH_MODEL_DIR = '<已校验的模型目录>'
$env:QCOPILOTS_SEARCH_TEST_ROOT = '<外置测试目录>'
$env:QCOPILOTS_SEARCH_SEMANTIC_REPORT = '<外置报告目录>/semantic-functional.json'
ctest --test-dir '<变体构建目录>' --output-on-failure
```

单独排查可直接执行 `qcs-tests-semantic --model-dir <路径> --tessdata-dir <路径> --report <路径>`。`--preserve` 保留该次独立输入与索引。`--validate-only` 只校验素材和生成输入，退出成功不代表已完成真实模型测试，其报告保持 `passed: false`、`realModel: false`。单项排查不作为完整候选验收。

完整功能报告使用 `schema: 2` 和 `test: "multimodal-functional"`。成功时 `mode` 为 `functional`，`passed`、`realModel`、`persisted`、`reloaded` 均为 `true`。`files` 记录 12 个输入的片段数量、实际提取通道和文件哈希，`queries` 记录 24 次查询的 `fixtureId`、语言、实际来源和定位校验。报告同时记录模型指纹、提取指纹、固定素材清单哈希与生成器源码哈希，供候选身份核对。报告只保留在本地或 CI 工作目录，不作为用户包 Artifact 上传。
