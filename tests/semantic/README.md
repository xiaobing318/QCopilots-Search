# 真实推理语义验收

`qcs-tests-semantic` 将文件实际交给生产提取器，再调用锁定的 EmbeddingGemma 运行时生成 768 维向量，通过生产 `IndexRepository` 完成入库和搜索。没有随机向量或预先写好的搜索结果。临时语料、数据库、ANN 和媒体帧全部位于独立 `TestEnvironment` 中。模型目录只读。

语料包含 80 个独立编写的主题说明，其中 20 个主题的正文为中文。每个主题有显式指定的中英文查询和唯一相关文件，前四个主题生成真实 PDF。另有简单图形、英文和中文 OCR 告示、代码、合成语音、电子音及短视频。补充素材包括真实猫照片、卫星合成图、狗叫和风雨录音以及实拍雨景视频。正例总计 222 条。

容量背景使用三本公版英文作品的完整文本，按生产分块逻辑产生不同片段。它们提供有实际内容的干扰片段，不通过复制主题段落来凑满一千个向量。所有片段都会真实编码，报告记录实际数量，不足 1,000 时发行验收失败。生产检索按文件聚合，因此这些背景片段最终只构成三个候选文件，不能把片段数解释为相同数量的独立文件。代码类别另有 24 个原创无关函数文件，避免仅有六个目标文件时 top-10 指标失去区分作用。

这是一套可重现的受控验收语料。主题与对应中英文查询在第一次观察模型结果前共同编写，未经独立人工盲标，不代表开放领域检索基准。简单插画不能代表自然照片，合成语音不能代表口音或多人对话。已经加入的真实媒体样本量仍很小，不能代表全部环境声、视频场景和拍摄条件。覆盖不足必须在发行报告中保留，不应由总查询量掩盖。

## 运行

通过 CTest 运行 `qcs-tests-semantic-manifest` 可校验固定资产的大小、SHA-256 和语料结构。运行 `qcs-tests-semantic` 执行完整模型验收。设置 `QCOPILOTS_SEARCH_MODEL_DIR` 和 `QCOPILOTS_SEARCH_TESSDATA_DIR` 后执行，或向程序传入 `--model-dir`、`--tessdata-dir`。

`--report <路径>` 指定 JSON 报告，优先于 `QCOPILOTS_SEARCH_SEMANTIC_REPORT` 环境变量。`--preserve` 保留当前运行的独立数据。未显式指定报告时，报告写入本次唯一测试目录并保留该目录，避免多个 checkout 或并行运行覆盖同一报告。失败也保留本次临时目录，报告给出实际位置。只有清单检查的运行不会覆盖环境变量指定的模型验收报告。`--smoke` 只验证小规模执行链，报告明确标记为 smoke，不满足发行门槛。所有路径都可以包含空格和中文。

报告同时给出生产混合检索的 Recall@10 和针对原始浮点向量精确计算的语义 Recall@10。每条正例只有一个相关文件，因此这里的 Recall@10 等于找到目标文件的查询比例。总门槛是 0.90，语言及模态分组门槛是 0.80。失败不会被改写为跳过。

20 条无答案查询与正例分开，前十条作为后续阈值校准材料，后十条为独立留出集。它们是明确不存在的虚构主题，不能代表与现有内容高度相似的困难无答案问题。报告保留返回数量和原始 cosine，并分别汇总两个分组的候选返回比例和最高 cosine 的范围。当前检索接口返回排序候选，没有承诺自动拒绝无答案查询，因此不会把返回候选误报为“已正确识别无答案”，也没有使用验收正例拟合阈值。

## 来源与许可

固定资产的下载地址、大小、校验值和许可说明见 `assets.json`。书籍原文件中的 Project Gutenberg 说明和分发条款完整保留。这些素材只用于测试，不随应用运行包分发。

- Jane Austen，*Pride and Prejudice*，1813 年作品。[Project Gutenberg 1342](https://www.gutenberg.org/ebooks/1342)，使用 `GITenberg/Pride-and-Prejudice_1342` 固定提交的镜像文本。
- Arthur Conan Doyle，*The Adventures of Sherlock Holmes*，1892 年作品。[Project Gutenberg 1661](https://www.gutenberg.org/ebooks/1661)，使用 `GITenberg/The-Adventures-of-Sherlock-Holmes_1661` 固定提交的镜像文本。
- Lewis Carroll，*Alice's Adventures in Wonderland*，1865 年作品。[Project Gutenberg 11](https://www.gutenberg.org/ebooks/11)，固定文件的 SHA-256 防止上游更新悄悄改变测试。
- 四段语音来自本项目原创文字，使用 Windows 系统语音合成为 16 kHz 单声道 PCM WAV。分发录音，不分发系统语音引擎或声音模型。录音文字只存于许可清单，不参与索引。
- 其余文字、绘图、声音信号和视频生成代码是本项目原创测试素材，遵循仓库 MIT 许可证。
- 真实照片、环境录音和雨景视频保留独立的 CC BY-SA、CC0 或政府公版条款，作者、来源和具体变化见 `assets/NOTICE.md`，不应把这些素材重新标为 MIT。
