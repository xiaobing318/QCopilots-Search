# 推理参考夹具

`photo.jpg`、`screen.png`、`speech.wav` 和 `vectors.json` 原样复制自 [DigUp](https://github.com/ARahim3/DigUp/tree/6faa29e4c315a3a591de1b41bd78751bdf39588d/Tests/LlamaRuntimeTests/Reference)。原作者为 Abdur Rahim，MIT 许可证见 `LICENSE-DigUp`。

参考测试保留原来的 9 条输入及提示词。参考 JSON 的 `vector_version` 为 1，每个输出与原参考向量的 cosine 必须至少为 0.999。测试同时校验文件 SHA-256，防止修改夹具掩盖兼容性问题。此工程不提供覆盖黄金向量的开关。

测试运行需要显式设置 `QCOPILOTS_SEARCH_MODEL_DIR`。未提供或缺少实际模型时测试失败，不跳过。CMake 为参考测试提供 `QCOPILOTS_SEARCH_RUNTIME_FIXTURES` 绝对路径。测试只读取原夹具，生成的损坏媒体等样本放入唯一的 `QTemporaryDir`，可通过 `QCOPILOTS_SEARCH_TEST_ROOT` 更改临时根。

来源测试见 `Tests/LlamaRuntimeTests/ReferenceVectorTests.swift`。源工程的 `scripts/testbed/make_reference_inputs.py` 生成这些图片及 WAV 输入。测试程序适配提示词和输入，额外验证中文向量、输入差异、长文本截断、失败后的恢复和纯文本运行模式。
