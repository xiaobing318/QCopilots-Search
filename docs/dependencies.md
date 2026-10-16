# 依赖、模型与格式边界

应用使用锁定版本的第三方源码，不修改上游文件。依赖来源由 `dependencies.lock.json`、Qt / NSIS / 源码材料锁、GPU SDK 锁及原生 CMake 声明共同确定。下载归档校验 SHA-256 后在仓库外展开，外部源码目录也必须匹配锁定来源。

## 主要依赖

| 依赖 | 锁定版本或来源 | 用途 |
| --- | --- | --- |
| Qt / Qt PDF | 6.12.0 / 6.140.0 官方 SDK | Widgets、网络、IPC、图像、媒体、PDF 与测试 |
| llama.cpp / ggml | v0.7.0，`620d2225cc436ab0b001e3200e590e3b286ef9ab` | 文本、图像和音频嵌入推理 |
| SQLite | 3.53.4 | 权威数据、FTS5 与 FP16 向量 |
| hnswlib | 0.8.0 | ANN 分片 |
| cppjieba / limonp | 5.6.7 / 固定 gitlink | 中文分词 |
| miniz | 3.1.0 | DOCX / ODT 的 ZIP 读取 |
| Tesseract / Leptonica | 5.5.3 / 1.87.0 | OCR 与图像处理 |
| tessdata_fast | `87416418657359cb625c412a48b6e1d6d41c29bd` | eng、chi_sim、chi_tra 数据 |
| FFmpeg | 9.0.1，与 Qt Multimedia 配套 | 音视频解码与预览 |
| libheif / libde265 | 1.23.6 / 1.0.17 | HEIF / HEIC 解码 |
| LibRaw | 0.22.2 | 相机 RAW 解码 |
| libgit2 | v1.9.7，`49e408b3208bc3093757a1c2db938d3590f3f412` | Git ignore 与仓库规则 |
| wv2 / POLE | Calligra v26.08.2，`72123cc643f6625351139e7df294544255a1056f` | 旧 DOC 读取 |
| GNU libiconv | 1.19 | wv2 字符编码转换 |
| zlib / libjpeg-turbo | 1.3.2 / 3.1.4.1 | 压缩与图像依赖 |
| libpng / libtiff | 1.6.59 / 4.7.2 | OCR 图像读写 |

Qt PDF 使用其配套独立模块版本，不能把模块版本直接替换为 Qt 主版本。FFmpeg 使用 Qt SDK 配套运行库，应用检查动态符号与 ABI，不从任意 PATH 位置加载其他版本。实际 codec 支持以部署的运行库为准。

## 平台与 GPU 变体

`config/build-variants.json` 是开发入口、CMake、CI、验包和发布的共同清单。`--variant` 对应 CMake 的 `QCOPILOTS_SEARCH_VARIANT`。11 个变体产生 21 个用户文件，全部保留 CPU 后备能力。

| 变体 | GPU SDK 或目标 | 系统基线 | 用户文件 |
| --- | --- | --- | --- |
| `windows-x64-cpu` | CPU ALL | Windows 10 1809 x64 | EXE、便携 ZIP |
| `windows-x64-vulkan` | Vulkan 1.4.357.0 | Windows 10 1809 x64 | EXE、便携 ZIP |
| `windows-x64-rocm` | ROCm 10.0.0，`gfx1200`、`gfx1201` | Windows 10 1809 x64 | EXE、便携 ZIP |
| `windows-x64-cuda-12.4` | CUDA 12.4 | Windows 10 1809 x64 | EXE、便携 ZIP |
| `windows-x64-cuda-13.4` | CUDA 13.4 | Windows 10 1809 x64 | EXE、便携 ZIP |
| `linux-x64-cpu` | CPU ALL | Ubuntu 22.04 | DEB、便携 TGZ |
| `linux-x64-vulkan` | Vulkan 1.4.313.0 | Ubuntu 22.04 | DEB、便携 TGZ |
| `linux-x64-rocm` | ROCm 10.0.0，`gfx1200`、`gfx1201` | Ubuntu 24.04 | DEB、便携 TGZ |
| `linux-x64-cuda-12.8` | CUDA 12.8.2 | Ubuntu 24.04 | DEB、便携 TGZ |
| `linux-x64-cuda-13.4` | CUDA 13.4.1 | Ubuntu 24.04 | DEB、便携 TGZ |
| `macos-arm64-metal` | Metal | macOS 14.4 | DMG |

表中 Windows 版本是应用的 Qt 基线，具体 GPU SDK、显卡与驱动还有各自要求。最低系统版本和硬件支持必须通过实际设备验证，不能由编译成功推断。

GPU 构建配方固定到与运行时相同的 [llama.cpp 提交](https://github.com/ggml-org/llama.cpp/blob/620d2225cc436ab0b001e3200e590e3b286ef9ab/.github/workflows/release.yml)。CUDA 架构采用锁定上游选择，ROCm 仅覆盖两个指定 RDNA4 目标。Windows 主程序使用 MSVC 和 Qt kit，CUDA/HIP 后端通过外置子构建和 ggml C ABI 接入，最终包只部署主构建的一份 `ggml-base`。

包内携带所需后端、运行库和资源，保持其查找布局，显卡驱动由用户系统提供。Windows ROCm 载荷超过单 EXE 限制时，安装器改为包含 `setup.exe` 与完整 `payload/` 的 `setup.zip`，便携 ZIP 仍独立生成，安装不依赖联网补齐。

默认 `QCOPILOTS_SEARCH_BACKEND=auto` 在 GPU 可用时启用，否则报告原因并回退 CPU。`cpu` 强制 CPU，`gpu` 要求 GPU 初始化成功。诊断显示构建变体、实际后端、设备和回退原因，无 GPU runner 不能证明 GPU 执行已经通过。

## 上游源码与应用适配

项目自有 CMake wrapper、配置头和公开 API 调用负责集成，所有生成文件放入构建树，不向上游源码目录写补丁、标记或改写文件。缓存按锁定归档区分，重新配置与构建后继续检查源码完整性。llama.cpp 已包含所需 no-logits 实现。

wv2/POLE 直接编译锁定 Calligra 中的原始文件，通过 GNU libiconv 转换编码，不需要 Cygwin。Windows DOC 路径在应用调用边界规范化为绝对扩展路径，并在线程局部 UTF-8 locale 下调用上游库，结束时恢复原 locale。推理后端加载使用同一适配。Windows 程序和相关动态库统一使用共享 CRT。

libgit2 通过公开文件配置 API 使用应用级 `core.longpaths`，不修改用户或源仓库配置。仓库元数据路径受上游能力限制，超限明确报错，不忽略 Git 排除规则，也不调用 Git CLI 绕过。GNU libiconv 的 CP932 映射与错误时指针推进遵循其上游契约。

升级依赖时同步更新锁定版本、归档 SHA-256、源码清单、GPU 配方、运行时来源、管线指纹、许可证和源码材料。保持模型权重与数值回归基线，执行相关格式、真实推理及最终包测试。需要的索引更新由管线指纹触发。

## 模型契约

模型来自[固定 revision 的 EmbeddingGemma 2 GGUF](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/tree/bfcd298762cc34d0357ece5ebdd31791a3a374d8)。运行时下载或离线导入，发行包不嵌入权重。

| 文件 | 大小（字节） | SHA-256 |
| --- | ---: | --- |
| `embeddinggemma-2-Q8_0.gguf` | 309855456 | `2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135` |
| `mmproj-embeddinggemma-2-Q8_0.gguf` | 554821024 | `c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f` |

`ModelFiles::manifest()` 为运行时清单，`qcs-cli models manifest` 输出相同内容。下载后检查大小与哈希，再发布最终文件。向量采用 768 维、mean pooling 和 L2 归一化。提示词、图像 token 预算与缩放、音频窗口保持固定，黄金夹具及原作者 MIT 归属位于 `tests/runtime/fixtures`。

## 格式范围

| 类别 | 处理方式 | 验证重点 |
| --- | --- | --- |
| TXT / Markdown | 原生解码与分块 | BOM、编码、长文件、中文与组合字符 |
| HTML | 本地文本解析 | 不加载外部资源，不执行脚本 |
| DOCX / ODT | ZIP/XML | 正文、表格、脚注、压缩展开限制与损坏包 |
| RTF | 控制组与编码解析 | Unicode、代码页、隐藏组和损坏输入 |
| DOC | worker 中的 wv2 / POLE | Word 版本、编码、表格、加密及损坏文件 |
| PDF | Qt PDF 按页提取与渲染 | OCR、密码、页码和分页续读 |
| 常见图片 | Qt Image Formats | 方向、颜色、尺寸与损坏输入 |
| HEIC / HEIF | libheif + libde265 | 具体 codec 与容器组合 |
| RAW | LibRaw | 相机型号和子格式 |
| 音频 | FFmpeg 16 kHz 单声道分窗 | 30 秒窗口、25 秒步长、静音和尾部 |
| 视频 | FFmpeg 解码、抽帧与音轨 | 真实 PTS、变帧率、重复画面和时间定位 |
| 代码 / Notebook | 独立代码空间 | Git ignore、未跟踪源码、整行分块和单元格，仅读取 Notebook 源码 |

普通文档和代码使用不同的提取限制。普通文档输入默认上限为 64 MiB，代码源文件为 1 MiB。Notebook 容器上限为 20 MiB，其中源码合计不超过 1 MiB。单个文件最多索引 5000 个片段，超过时明确报告部分索引，已有片段仍可查询。这些容量限制不决定文件夹是否被扫描，也不要求普通文本开启代码搜索。

HEIF 不隐式启用 x265、GPL 编码器或任意系统 codec，LibRaw 不隐式引入 GPL demosaic pack、RawSpeed 或未锁定依赖。具体格式的支持结论以实际样本测试为准。

发行包携带实际部署组件的完整许可材料。源码材料和符号分别生成，范围见[对应源码材料](source-materials.md)与[第三方声明](../THIRD_PARTY_NOTICES.md)。
