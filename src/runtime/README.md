# 多模态推理模块

`qcopilots-search-runtime` 封装锁定版本的 llama.cpp 和 mtmd。它使用真实的 EmbeddingGemma 2 Q8_0 模型，不提供占位向量。公开入口为 `EmbeddingRuntime.h`。

每个实例由一个工作线程拥有。调用 `load(modelDir, textOnly, &error)` 前可设置 CPU 线程数和读取原子取消标志的回调。工作进程保持回调捕获对象的生命周期，运行期间不替换回调。加载会检查模型的准确大小与 SHA-256，失败时保留错误信息。

`embedText` 接受 Query、CodeQuery、Document 和 Raw 四种提示类型。Document 使用标题，空标题按参考实现写为 `none`。纯文本查询上下文为 512 token，完整多模态上下文为 2048 token。过长文本保留开头和 EOS。每次调用返回 768 维、L2 归一化向量，空向量表示失败。

`embedImage` 接受 70、140、280、560、1120 五种视觉预算。图像经 mtmd 解码为 RGB 后执行与 Pillow 一致的 22 位定点双三次缩放。`embedAudio` 读取已经准备好的音频窗口，并将解码样本限制到前 30 秒。文件由 Qt 读取后传入解码器，中文、空格和其他 Unicode 路径不依赖系统窄字符文件 API。视频由提取模块先转换为帧和音频窗口。

推理始终关闭 GPU 层、KQV 和操作卸载，mtmd 也关闭 GPU。x64 发行构建包含通用 CPU 后端与按处理器指令集区分的 CPU 动态库。应用只从安装布局中的已知文件名加载兼容后端，拒绝隐式依赖工作目录或 `GGML_BACKEND_PATH`。`QCOPILOTS_SEARCH_CPU_BASELINE=1` 强制使用通用 x64 后端。`QCOPILOTS_SEARCH_THREADS` 设置默认线程数，实例设置可覆盖它。

macOS 应用包把 CPU 模块放在 `Contents/PlugIns/ggml`，以便 Qt 的部署工具包含这些通过 `dlopen` 加载的模块并完成签名。基础推理动态库保留在 `Contents/Frameworks`，模块通过 `@loader_path/../../Frameworks` 查找依赖。包内进程只搜索新的插件目录，独立开发程序及 Windows、Linux 的搜索目录保持各自布局。`qcopilots-search-tests-backend-layout` 检查加载路径，`qcopilots-search-tests-inference-install-layout` 用小型原生库执行真实的 CMake 安装规则。后者可在任意平台检查安装目录与 RPATH 配置，macOS 二进制签名仍由原生发布验收检查。

文本计算可通过 llama.cpp 的 CPU abort callback 取消。mtmd 的公开编码 API 没有计算中途取消回调，所以媒体编码在 chunk 边界检查取消。服务对工作进程实施的退出超时仍须保留。

测试位于 `tests/runtime`。普通测试不需要模型，`real-model` 标签的两个测试要求 `QCOPILOTS_SEARCH_MODEL_DIR` 指向准确模型，缺少模型会失败。黄金向量文件来自 DigUp 原始测试夹具并锁定哈希。不能通过重写黄金文件让回归通过。两个模型 SHA-256、提示词和处理契约统一记录在 `core/pipeline_manifest.h`。

离线源码材料中的 llama.cpp 归档不需要 `.git` 元数据。解压后可使用 `QCOPILOTS_SEARCH_LLAMA_SOURCE_DIR` 或标准的 `FETCHCONTENT_SOURCE_DIR_QCOPILOTS_SEARCH_LLAMA` 指定源码目录。配置会校验锁定归档的全部 3,699 个文件及清单本身的 SHA-256，拒绝额外文件、缺失文件、未批准修改和符号链接。两份已审核补丁继续独立校验 SHA-256，归档模式按确定性的 LF 格式应用补丁，后续配置接受准确的补丁后文件哈希。Git checkout 路径仍校验根目录与固定 commit。

`tests/runtime/source_integrity_test.py --archive <llama-b11461.tar.gz> --report <外置报告.json>` 使用系统临时目录或 `QCOPILOTS_SEARCH_TEST_ROOT` 创建独立目录，验证完整归档、重复打补丁、不同 Git 换行设置、位于无关 Git 仓库中的解压目录以及文件、清单和补丁被修改时的拒绝行为。测试仅依赖 Python 标准库、CMake 和 Git，不向源码树写入。
