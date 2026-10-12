# 依赖、模型与格式边界

依赖版本与下载校验值由 `dependencies.lock.json`、`tools/qt-packages.lock.json` 及 `cmake` 中的依赖声明共同锁定。下载包经过校验后在仓库外展开。禁止无提示地换成开发机已有但版本不明的库。

## 主要依赖

| 依赖 | 锁定版本或来源 | 用途 |
| --- | --- | --- |
| Qt | 6.12.0 官方 SDK | Widgets、网络、本地 IPC、图像、XML、多媒体和测试 |
| Qt PDF | 配套独立 6.140.0 | PDF 文本、页面渲染与预览 |
| llama.cpp / ggml | b11461，`4d756bc72bf00a4aacf410ae15a2d315f3db400d` | CPU 多模态嵌入推理 |
| SQLite | 3.53.4 | 权威数据、FTS5、FP16 向量 |
| hnswlib | 0.8.0 | ANN 分片 |
| cppjieba / limonp | 5.6.7 / 固定 gitlink | 中文分词 |
| miniz | 3.1.0 | DOCX / ODT 的 ZIP 读取 |
| Tesseract / Leptonica | 5.5.3 / 1.87.0 | OCR |
| tessdata_fast | `87416418657359cb625c412a48b6e1d6d41c29bd` | eng、chi_sim、chi_tra LSTM 数据 |
| FFmpeg | 9.0.1，与 Qt Multimedia 配套 | 音视频解码与预览 |
| libheif / libde265 | 1.23.6 / 1.0.17 | HEIF / HEIC 解码 |
| LibRaw | 0.22.2 | 相机 RAW 解码 |
| libgit2 | 1.9.1 | Git ignore 和仓库路径规则 |
| wv2 / POLE | Calligra `88bde0ce71a5c12fb50786017b2e2e07c5ce13e4` | 旧 DOC 读取 |
| zlib / libjpeg-turbo | 1.3.2 / 3.1.4.1 | 原生图像依赖 |
| libpng / libtiff | 1.6.59 / 4.7.2 | OCR 图像读写依赖 |

Qt PDF 在 Qt 6.12 对应单独版本的模块发布，不能把旧仓库中的同名 beta 包当作正式 SDK。Bootstrap 使用锁定的官方归档和 kit 路径。参见 [Qt 的模块发布说明](https://www.qt.io/blog/qt-webengine-will-release-separate) 和 [Qt 支持平台](https://doc.qt.io/qt-6.12/supported-platforms.html)。

FFmpeg 运行库复用官方 Qt Multimedia 配套构建，源码头文件按固定 9.0.1 版本使用。调用时检查动态符号和版本，部署必须包含相同 ABI 的运行库。不能通过机器 PATH 中的任意 FFmpeg 替代。解码库的实际 codec 支持以被部署构建为准。

## 模型契约

模型来自 [固定 revision 的 EmbeddingGemma 2 GGUF](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/tree/bfcd298762cc34d0357ece5ebdd31791a3a374d8)。运行时下载或离线导入，发行包不嵌入权重。

| 文件 | 大小（字节） | SHA-256 |
| --- | ---: | --- |
| `embeddinggemma-2-Q8_0.gguf` | 309855456 | `2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135` |
| `mmproj-embeddinggemma-2-Q8_0.gguf` | 554821024 | `c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f` |

`ModelFiles::manifest()` 是运行时权威清单，CLI 的 `models manifest` 输出该清单。模型 revision 为 `bfcd298762cc34d0357ece5ebdd31791a3a374d8`。下载后同时检查大小与哈希，再发布最终文件。

向量契约为 768 维、mean pooling、L2 归一化。保持 DigUp 的任务提示、图像 token 预算与缩放、音频窗口和 llama.cpp no-logits 补丁。不能混用不同模型或预处理版本的向量。数字回归输入和期望保存在 `tests/runtime/fixtures`，原作者 MIT 许可证随夹具保留。

## 格式范围

| 类别 | 处理方式 | 需要单独验证的边界 |
| --- | --- | --- |
| TXT / Markdown | 原生解码、分块 | BOM、编码、长文件、中文与组合字符 |
| HTML | 本地文本解析 | 外部资源不加载，脚本不执行 |
| DOCX / ODT | ZIP/XML 读取正文、表格等 | 损坏包、压缩展开限制、脚注、编码 |
| RTF | C++ 控制组与编码解析 | Unicode、代码页、隐藏组、损坏输入 |
| DOC | 独立 worker 中的 wv2 / POLE | Word 版本、编码、表格、加密与损坏文件 |
| PDF | Qt PDF 按页提取与渲染 | 扫描页 OCR、密码、页码、分页续读 |
| 常见图片 | Qt Image Formats | 方向、颜色、尺寸、损坏文件 |
| HEIC / HEIF | libheif + libde265 解码 | 只声明真实样本通过的 codec / 容器组合 |
| RAW | LibRaw 解码 | 相机型号与子格式，未启用的可选后端不在声明范围 |
| 音频 | FFmpeg 16 kHz 单声道分窗 | 30 秒窗口、25 秒步长、静音与尾部、格式损坏 |
| 视频 | FFmpeg 解码、抽帧、音轨提取 | 真实 PTS、变帧率、重复画面、时间定位 |
| 代码 / Notebook | 独立代码模式 | Git ignore、未跟踪源码、整行分块、单元格，只读取 Notebook 源码 |

当前依赖配置仅启用需要的解码能力。HEIF 不隐式启用 x265、GPL 编码器或任意系统 codec。LibRaw 不隐式引入 GPL demosaic pack、RawSpeed 或未锁定的可选依赖。实际验证过的 HEIC / RAW 格式需要在验收报告中逐项列明，不以扩展名列表代替成功解码证据。

## 依赖修改与发行材料

上游源码包保持不变，项目所需适配位于本仓库 CMake、补丁或生成代码中。发布时同时提供原始源码校验值、补丁、构建说明及动态库许可证材料。更新依赖后重新执行相关格式、真实模型和包级测试。

第三方动态库保留上游惯用名称，例如 Windows 的 `zlib1.dll` 和 Unix 的 `libz`。`qcopilots-search-native-wv2` 是应用私有的适配库，将 Calligra 维护的 wv2 与所需 POLE 源码合并构建，并加入 Qt 6 Core5Compat 字符编码适配、Windows UTF-8 路径处理及生成的配置和导出声明。它不承诺与独立安装的上游 wv2 保持 ABI 兼容，因此使用产品前缀区分这份私有构建。上游命名空间、许可证和作者归属保持不变，适配内容随对应源码材料提供。

许可归属见 [第三方声明](../THIRD_PARTY_NOTICES.md)。Qt 官方 [许可说明](https://doc.qt.io/qt-6/licensing.html) 区分不同模块的许可，不能仅依据“使用 Qt”推断所有模块适用同一条款。发行材料需要与最终部署的实际二进制对应。
