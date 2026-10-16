# 多模态推理模块

`qcs-runtime` 封装锁定版本的 llama.cpp 与 mtmd，使用真实 EmbeddingGemma 2 Q8_0，不提供占位向量。公开入口为 `EmbeddingRuntime.h`，依赖与模型来源见[依赖说明](../../docs/dependencies.md)。

## 调用与模型契约

每个实例由一个工作线程拥有。调用 `load(modelDir, textOnly, &error)` 前可以设置 CPU 线程数和取消回调，工作进程保持回调捕获对象的生命周期。加载检查模型准确大小与 SHA-256，失败保留错误信息。

`embedText` 支持 Query、CodeQuery、Document 和 Raw 提示类型。Document 使用标题，空标题为 `none`。纯文本查询上下文为 512 token，完整多模态上下文为 2048 token，过长文本保留开头与 EOS。成功返回 768 维 L2 归一化向量，失败返回空向量。

`embedImage` 支持 70、140、280、560、1120 五种视觉预算，解码后执行与参考 Pillow 实现一致的 22 位定点双三次缩放。`embedAudio` 读取准备好的窗口，最多使用前 30 秒。媒体文件通过 Qt 读取后传给解码器，视频由提取模块转换成帧和音频窗口。

文本推理可通过 llama.cpp abort callback 取消。mtmd 公开编码 API 没有计算中途取消回调，媒体编码在 chunk 边界检查取消，服务保留 worker 退出超时。

## 后端与部署

每份包包含 CPU 后备能力，GPU 变体额外提供 Vulkan、HIP、CUDA 或 Metal。`QCOPILOTS_SEARCH_BACKEND=auto` 自动选择，`cpu` 强制 CPU，`gpu` 要求 GPU 初始化成功。自动选择同时应用于文本模型和 projector，回退原因通过诊断报告。

x64 包包含通用 CPU 与按指令集区分的动态后端。`QCOPILOTS_SEARCH_CPU_BASELINE=1` 强制通用后端，`QCOPILOTS_SEARCH_THREADS` 设置默认 CPU 线程数。后端只从受控安装目录按已知文件名加载，不依赖 cwd 或 `GGML_BACKEND_PATH`。

Windows 加载上游窄字符路径 API 时，在应用调用边界使用线程局部 UTF-8 locale 并在结束后恢复，动态库与调用者使用共享 CRT。macOS 后端位于 `Contents/PlugIns/ggml`，基础推理库位于 `Contents/Frameworks`，插件通过相对 RPATH 查找依赖。安装布局、ABI 与签名由部署测试和平台包验证检查。

## 上游源码与验证

llama.cpp 直接使用 v0.7.0 的锁定原始源码，不应用补丁。归档无需 Git 元数据，可通过 `QCOPILOTS_SEARCH_LLAMA_SOURCE_DIR` 或 `FETCHCONTENT_SOURCE_DIR_QCS_LLAMA` 指定匹配源码目录。配置验证锁定清单和全部文件字节，拒绝缺失、额外文件、符号链接和内容修改。重复配置与构建不改变上游源码。

CTest 的 `qcs-tests-source-integrity` 使用已校验源码的独立副本检查完整性及拒绝边界，临时目录由通用测试运行器隔离。也可使用 `tests/runtime/source_integrity_test.py --archive <锁定归档> --report <外置报告>` 验证归档。它只证明源码准备规则，不代替完整应用构建与运行测试。

`tests/runtime` 的普通测试无需模型，`real-model` 用例必须提供准确模型，缺失时失败。黄金向量来自 DigUp 并锁定哈希，保留原作者 MIT 归属。模型 SHA-256、提示词和处理契约记录在 `core/pipeline_manifest.h`，不能通过改写黄金文件放宽回归。
