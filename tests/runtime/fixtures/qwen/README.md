# Qwen 独立数值参考

本目录中的参考向量由 `tools/qcs-qwen-reference.py` 使用 Qwen 官方原始 BF16 权重和官方 `Qwen3VLForEmbedding` 实现生成。2B 使用 FP32 计算，8B 使用 BF16 计算，每个模型的全部 14 条输入使用一致的精度。生成过程不读取 GGUF，不调用本工程的 C++ 推理程序。JSON 内记录官方模型 revision、每个权重文件的 SHA-256、官方脚本 commit 与 SHA-256、Python 及依赖版本、实际 token ID、图片尺寸和图像 patch 网格。

2B Q8_0 的余弦门槛为 `0.995`，8B Q6_K 的门槛为 `0.97`。测试同时检查维度、有限数值、单位范数、中英文相关性排序及跨模态调用后的重复查询。门槛不能通过重写参考向量来规避。

## 重新生成

在仓库外创建 Python 3.12 虚拟环境，安装以下版本。CPU PyTorch 使用官方 CPU wheel 源，避免安装不必要的 GPU SDK。

```text
torch==2.8.0
torchvision==0.23.0
transformers==4.57.1
qwen-vl-utils==0.0.14
accelerate==1.11.0
huggingface-hub==0.36.0
safetensors==0.6.2
numpy==2.2.6
pillow==11.3.0
```

```powershell
python tools/qcs-qwen-reference.py --model qwen3-vl-embedding-2b --cache-root <外置缓存目录> --output-dir <外置输出目录> --threads 4
python tools/qcs-qwen-reference.py --model qwen3-vl-embedding-8b --cache-root <外置缓存目录> --output-dir <外置输出目录> --threads 4
```

脚本拒绝将下载缓存或生成结果写入仓库。`--metadata-only` 只获取固定的小文件，`--prepare-only` 下载并校验模型后停止。`--dtype float32` 或 `--dtype bfloat16` 可显式指定诊断精度，默认分别为 2B FP32 和 8B BF16。下载支持续传，固定权重的大小和摘要必须匹配。原权重约占 3.96 GiB 和 15.17 GiB，可以逐个生成并清理本轮下载的原权重。生成结果需要审查和独立对照后再更新本目录，普通测试没有覆盖参考向量的选项。

推理使用 CPU、SDPA、单输入、关闭 KV cache，并以 FP32 计算最后 token 的 L2 归一化。标题与正文使用一个换行连接，系统提示为 `Represent the user's input.`。官方 tokenizer 的 `TemplateProcessing` 在完整 assistant 尾部之后追加 `<|endoftext|>`，其 token ID 为 151643、attention mask 为 1，也是实际 LAST pooling 的位置。长文本只截断正文，保留完整 assistant 尾部和此终止 token。纯文本查询的上下文上限为 512，文档和图像的上限为 2048。

## 计算精度的独立验证

初次 2B 参考使用 BF16 计算。截图的 C++ 输出与该参考的余弦为 `0.99426141`，低于原定 `0.995`。检查确认 C++ 与官方实现的原图 RGB、缩放后 RGB 逐字节相同，独立首次加载的截图结果也相同，排除了图像预处理及前一次输入污染。

随后使用相同原权重、相同输入和未经修改的官方 `Qwen3VLEmbedder.process` 进行 FP32 对照，截图的 C++ 输出与 FP32 结果余弦为 `0.99631054`，而官方 FP32 与 BF16 计算自身只有 `0.99794791`。图像 MLP 仍为官方 GELUTanh，merger 仍为 `GELU(approximate='none')`，没有替换或近似官方算子。照片、竖图也执行了相同三方对照，数字和像素摘要记录在 `precision-comparison.json`。

因此 2B 的全部 14 条参考统一改为较高精度的官方 FP32 计算，保留 `0.995` 门槛。原始 2B BF16 全量输出和生成脚本快照保留在外置诊断目录，普通测试使用本目录已审核的数值参考。

8B 随后完成独立的逐层载入 FP32 对照。诊断使用固定的官方 BF16 原始权重，每次仅载入一层 FP32 decoder 权重，执行未经修改的官方 forward，从而降低完整 FP32 模型的常驻内存需求。视觉权重和 embedding 输出使用 FP32。该方法先在 2B 的全部 14 条输入上与官方完整 FP32 结果比较，输出逐位一致，最大绝对误差为 `0`，再用于 8B。此对照独立于 GGUF 和本工程的 C++ 推理输出。

8B 的同一组 14 条固定输入分别与官方 BF16 参考、上述独立 FP32 参考比较。Windows CPU 诊断中，Q4_K_M 对两组参考均只有 7 条达标，Q5_K_M 均有 13 条达标，Q6_K 均有 14 条达到原定 `0.97` 门槛。各组最小余弦如下，均出现在 `image-portrait-320`。

| 8B 量化 | 官方 BF16 参考最小余弦 | 独立 FP32 参考最小余弦 | 达标输入数，两组参考一致 |
| --- | ---: | ---: | ---: |
| Q4_K_M | 0.916885492 | 0.918190954 | 7/14 |
| Q5_K_M | 0.957060097 | 0.959922090 | 13/14 |
| Q6_K | 0.978735889 | 0.980599568 | 14/14 |

据此，8B 预置文件改用 Q6_K，正式 golden 仍保留官方 BF16 参考，`0.97` 门槛和全部输入不变。FP32 对照用于独立诊断，不由 Q6_K 输出倒推或改写参考。数据及原始证据摘要记录在外置构建根目录的 `oracle/diagnostics/qwen8b-independent-quantization-comparison.json`，其中明确标注 `diagnosticOnly: true`、`formalVerification: false`。选择 Q6_K 时，新锁文件下的正式完整验收、最终包验证与 CI 尚未执行，这组诊断结果不替代这些验证。

图像通过固定版本的 `qwen-vl-utils` 进行官方 `smart_resize`，patch 为 16，merge 为 2，最小面积为 4096。应用预算 140、280、560 分别对应最多 320、640、1280 个视觉 token。原图为 RGB，使用 Pillow bicubic，processor 不进行第二次 resize。

## 输入来源和 JSON 约定

`../photo.jpg` 和 `../screen.png` 复用上一层目录原有的 DigUp MIT 测试图片，来源及许可见上一层 `README.md` 和 `LICENSE-DigUp`。

`portrait.png` 是本工程的程序化 RGB 测试输入，尺寸为 641 × 1601，不包含外部素材。对坐标 `(x, y)`，像素为 `floor(x*255/640), floor(y*255/1600), ((floor(x/17)+floor(y/29)) mod 2)*255`。纵向尺寸故意不取 patch 的整倍数，以覆盖实际 resize 和非方形图像路径。

根字段 `modelId`、`dimensions` 和 `minimumCosine` 定义模型数值契约。`inputs` 中 `kind` 为 `query`、`document` 或 `image`，`textOnly` 表示对应运行时加载模式。`image` 相对于本目录，`budget` 是应用侧预算。图片 `sha256` 校验原文件字节，文本 `sha256` 校验未经标题和模板包装的 UTF-8 正文。`tokenIds` 记录送入官方模型的完整序列，`vector` 是独立输出。`rankings` 以输入 ID 指定查询、相关输入和无关输入。
