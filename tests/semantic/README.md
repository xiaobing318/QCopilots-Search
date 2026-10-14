# 真实推理语义验收

`qcs-tests-semantic` 将文件实际交给生产提取器，再调用锁定的 EmbeddingGemma 运行时生成 768 维向量，通过生产 `IndexRepository` 完成入库和搜索。没有随机向量或预先写好的搜索结果。临时语料、数据库、ANN 和媒体帧全部位于独立 `TestEnvironment` 中。模型目录只读。

旧语料包含 80 个独立编写的主题说明，其中 20 个主题的正文为中文。每个主题有显式指定的中英文查询和唯一相关文件，前四个主题生成真实 PDF。另有简单图形、英文和中文 OCR 告示、代码、合成语音、电子音及短视频。补充素材包括真实猫照片、卫星合成图、狗叫和风雨录音以及实拍雨景视频。既有正例 222 条全部保留，另增加 8 个原创 Python 算法文件及 16 条中英文查询，旧正例总计 238 条。

V1 首次独立留出验证失败后，全部旧 238 条正例和 96 条无答案查询，共 334 条，均作为 development。V2 增加 40 个新主题，对应 80 条中英正例和 80 条中英无答案查询，用于第二轮首次独立 holdout。完整语料包含 186 个实际文件、318 条正例和 176 条无答案查询。新主题清单是 `relevance_v2_text_code.json` 与 `relevance_v2_media.json`。

**V2 首次独立评估也未通过。** 虽然总体正例为 72/80，达到 90%，代码分组为 17/24，低于 80%，无答案误返回为 14/80，超过 10%。此次结果已经观察，40 个 V2 主题今后只能作为已观察回归数据，不能调参后再次声称其为未观察的独立验证。生产策略头文件保持未校准。

容量背景使用三本公版英文作品的完整文本，按生产分块逻辑产生不同片段。它们提供有实际内容的干扰片段，不通过复制主题段落来凑满一千个向量。所有片段都会真实编码，报告记录实际数量，不足 1,000 时发行验收失败。生产检索按文件聚合，因此这些背景片段最终只构成三个候选文件，不能把片段数解释为相同数量的独立文件。代码类别另有 24 个原创无关函数文件，避免仅有六个目标文件时 top-10 指标失去区分作用。

这是一套可重现的受控验收语料。主题与对应中英文查询在第一次观察模型结果前共同编写，未经独立人工盲标，不代表开放领域检索基准。简单插画不能代表自然照片，合成语音不能代表口音或多人对话。已经加入的真实媒体样本量仍很小，不能代表全部环境声、视频场景和拍摄条件。覆盖不足必须在发行报告中保留，不应由总查询量掩盖。

## 运行

通过 CTest 运行 `qcs-tests-semantic-manifest` 可校验固定资产的大小、SHA-256 和语料结构。运行 `qcs-tests-semantic` 执行完整模型验收。设置 `QCOPILOTS_SEARCH_MODEL_DIR` 和 `QCOPILOTS_SEARCH_TESSDATA_DIR` 后执行，或向程序传入 `--model-dir`、`--tessdata-dir`。

`--report <路径>` 指定 JSON 报告，优先于 `QCOPILOTS_SEARCH_SEMANTIC_REPORT` 环境变量。`--preserve` 保留当前运行的独立数据。未显式指定报告时，报告写入本次唯一测试目录并保留该目录，避免多个 checkout 或并行运行覆盖同一报告。失败也保留本次临时目录，报告给出实际位置。只有清单检查的运行不会覆盖环境变量指定的模型验收报告。`--smoke` 只验证小规模执行链，报告明确标记为 smoke，不满足发行门槛。所有路径都可以包含空格和中文。

报告同时给出生产混合检索的 Recall@10 和针对原始浮点向量精确计算的语义 Recall@10。每条正例只有一个相关文件，因此这里的 Recall@10 等于找到目标文件的查询比例。总门槛是 0.90，语言及模态分组门槛是 0.80。失败不会被改写为跳过。

`relevance_queries.json` 保留 V1 主题标识、历史分组、8 个新增代码文件及 96 条困难无答案查询，供重放原审核结果。V2 的有效开发分组由 `relevance_development_groups.json` 固定为 43 个语义家族、3 个折，同一主题的两种语言、明显近邻及无答案问题不跨折。无答案标签指明确请求的文档证据或代码操作不存在，不能据此推断所有主题相关文件均无价值。这些标签由作者编写，尚无独立人工盲标。

报告分别保留生产查询的返回结果、精确语义前十项、每个文件每种策略模态的最高 cosine、对应真实 segment ID 及关键词排名。原始分数来自生产数据库实际保存的 FP16 向量。普通文本、图片/视频帧、音频、代码文本使用四种独立门槛，在片段排序后、按文件聚合及 RRF 融合前过滤。关键词命中仍能独立保留文件，`SearchHit.score` 保持 RRF 分数。

## 阈值校准与独立验收

当前协议为 `grouped-cv-v2-quarter`，详细顺序和固定身份见 [V2 协议](calibration/v2-protocol.md)。先对完整 186 文件语料进行真实模型采样，再仅使用 development 查询执行三折交叉验证和全部开发数据拟合。新主题文件也会影响旧查询的候选排序，因此旧 146 文件索引上的诊断通过不能代替完整语料的重新校准。

原始测量、运行日志和策略 JSON 输出必须位于源码树外。`--header` 是全部质量门禁通过后有意更新的受控源码，允许明确指向仓库中的策略头文件。经过审查的逐查询策略和开发诊断 JSON 可作为固定审核快照保存在 `tests/semantic/calibration/`，临时 profile 和构建产物不进入该目录。

```text
qcs-tests-semantic --model-dir <models> --tessdata-dir <tessdata> --calibration-export --preserve --report <outside-source>/raw-relevance.json
python tests/semantic/calibrate_relevance_v2.py <outside-source>/raw-relevance.json --output <outside-source>/relevance-policy.json --header src/core/relevance_policy_data.h
```

`--calibration-export` 是程序内部专用原始候选模式，不通过 IPC 暴露。它生成 `calibration-exported` 报告且 `passed` 为 false，只表示测量完整，不满足发行门禁。合成容量工具也使用内部原始候选模式，其报告明确不评估语义质量。

校准脚本确定性地搜索与训练观测分数等价的门槛区间，采用 `cosine >= threshold` 边界。V2 对四个策略通道统一取区间内从拒绝边界到接受边界的 25% 位置，并验证训练接受集合及训练指标完全不变，保留实际浮点余量。规则仅依据 development 比较后固定，不针对某一失败查询单独设置门槛。

每折训练及全部开发数据拟合的无答案误返回率上限为 0.05。三个折各自未参与训练的结果合并为 OOF，仍须满足正例 Recall@10 总体 0.90、各语言及内容模态 0.80、无答案误返回率不超过 0.10。只有 OOF 与全部开发拟合通过并冻结最终门槛后，脚本才验证、重放和评估新 holdout 的分数。新 holdout 使用相同的 0.90 / 0.80 / 0.10 门槛。搜索预算用尽、数据不完整和无可行解分别报告，均不能伪装成通过。

已审核的旧 146 文件索引 quarter 诊断为 OOF 229/238、PDF 7/8、无答案误返回 5/96，全部旧数据拟合为 233/238 与 4/96。它们只是 development 上的方法选择证据，不是完整语料的最终验收结果。

随后已在完整 186 文件、1,364 个实际编码片段上重新采样。OOF 为 229/238 与 5/96，全部 development 拟合为 231/238 与 3/96，均通过开发门禁。冻结参数后首次独立 holdout 的总体 72/80 达标，但代码 17/24 和无答案误返回 14/80 失败，最终状态为 `holdout-failed`、`accepted: false`。全部 494 条查询的 raw RRF 重放一致只能证明实现契约，不能覆盖相关性失败。没有生成可发布的已校准生产头文件。历史 midpoint、V1 及 V2 首次失败均保留，详见 [审核快照说明](calibration/README.md)。

头文件绑定模型指纹、语料身份和策略版本。未校准或模型指纹不一致时语义分支不接受候选，关键词分支照常工作，发行验收必定失败。不能用随意选取的常数解除此门禁。生成策略后重新编译，再不带 `--calibration-export` 执行完整 `qcs-tests-semantic`，由真正的生产检索验证过滤、关键词保留和 RRF 结果。模拟计算通过不能代替这个步骤。

每个分组的样本数和未覆盖场景必须保留。新媒体补充了独立主题，但单个自然媒体分组的样本量仍很少，整体门槛通过也不代表全部环境声、视频或拍摄条件获得了独立泛化验证。

## 来源与许可

旧固定资产的下载地址、大小、校验值和许可说明见 `assets.json`，V2 媒体见 `relevance_v2_media.json`。新媒体的标题、出处、转录、标签和新颖性说明仅用于审核，不参与索引。书籍原文件中的 Project Gutenberg 说明和分发条款完整保留。这些素材只用于测试，不随应用运行包分发。

- Jane Austen，*Pride and Prejudice*，1813 年作品。[Project Gutenberg 1342](https://www.gutenberg.org/ebooks/1342)，使用 `GITenberg/Pride-and-Prejudice_1342` 固定提交的镜像文本。
- Arthur Conan Doyle，*The Adventures of Sherlock Holmes*，1892 年作品。[Project Gutenberg 1661](https://www.gutenberg.org/ebooks/1661)，使用 `GITenberg/The-Adventures-of-Sherlock-Holmes_1661` 固定提交的镜像文本。
- Lewis Carroll，*Alice's Adventures in Wonderland*，1865 年作品。[Project Gutenberg 11](https://www.gutenberg.org/ebooks/11)，固定文件的 SHA-256 防止上游更新悄悄改变测试。
- 四段语音来自本项目原创文字，使用 Windows 系统语音合成为 16 kHz 单声道 PCM WAV。分发录音，不分发系统语音引擎或声音模型。录音文字只存于许可清单，不参与索引。
- 其余文字、绘图、声音信号和视频生成代码是本项目原创测试素材，遵循仓库 MIT 许可证。
- 真实照片、环境录音和雨景视频保留独立的 CC BY-SA、CC0 或政府公版条款，作者、来源和具体变化见 `assets/NOTICE.md`，不应把这些素材重新标为 MIT。
