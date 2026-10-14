# 依赖、模型与格式边界

依赖版本与下载校验值由 `dependencies.lock.json`、Qt / NSIS / 源码材料锁、`config/gpu-sdk.lock.json`、`config/gpu-license-sources.lock.json` 及 `cmake` 中的依赖声明共同锁定。下载包经过校验后在仓库外展开。禁止无提示地换成开发机已有但版本不明的库。

## 主要依赖

| 依赖 | 锁定版本或来源 | 用途 |
| --- | --- | --- |
| Qt | 6.12.0 官方 SDK | Widgets、网络、本地 IPC、图像、XML、多媒体和测试 |
| Qt PDF | 配套独立 6.140.0 | PDF 文本、页面渲染与预览 |
| llama.cpp / ggml | b11461，`4d756bc72bf00a4aacf410ae15a2d315f3db400d` | CPU 及按变体选择的 GPU 多模态嵌入推理 |
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

## 构建变体与 GPU 依赖

`config/build-variants.json` 是构建、CI、验包及发布共用的变体清单。开发入口的 `--variant` 对应 CMake 的 `QCOPILOTS_SEARCH_VARIANT`。11 个构建条目产生 21 份用户文件，所有变体均保留 CPU 后备能力。

候选的 `lockHashes` 绑定 8 个文件：`dependencies.lock.json`、`tools/qt-packages.lock.json`、`tools/nsis-package.lock.json`、`tools/qcs-source-packages.lock.json`、`cmake/qcs-native-deps.cmake`、`config/build-variants.json`、`config/gpu-sdk.lock.json` 和 `config/gpu-license-sources.lock.json`。包内构建身份另以 SHA-256 绑定随包的变体清单、依赖锁及两份 GPU 锁，验包时按实际字节复核。更换 SDK、运行资源或许可来源不能保留旧候选身份。

| 变体 | GPU SDK 或目标 | 系统基线 | 用户文件 |
| --- | --- | --- | --- |
| `windows-x64-cpu` | CPU ALL | Windows x64 | EXE、便携 ZIP |
| `windows-x64-vulkan` | Vulkan 1.4.357.0 | Windows x64 | EXE、便携 ZIP |
| `windows-x64-rocm` | ROCm 10.0.0，RDNA4 `gfx1200`、`gfx1201` | Windows x64 | EXE、便携 ZIP |
| `windows-x64-cuda-12.4` | CUDA 12.4 | Windows x64 | EXE、便携 ZIP |
| `windows-x64-cuda-13.4` | CUDA 13.4 | Windows x64 | EXE、便携 ZIP |
| `linux-x64-cpu` | CPU ALL | Ubuntu 22.04 | DEB、便携 TGZ |
| `linux-x64-vulkan` | Vulkan 上游配方 | Ubuntu 22.04 | DEB、便携 TGZ |
| `linux-x64-rocm` | ROCm 10.0.0，RDNA4 `gfx1200`、`gfx1201` | Ubuntu 24.04 | DEB、便携 TGZ |
| `linux-x64-cuda-12.8` | CUDA 12.8.2 | Ubuntu 24.04 | DEB、便携 TGZ |
| `linux-x64-cuda-13.4` | CUDA 13.4.1 | Ubuntu 24.04 | DEB、便携 TGZ |
| `macos-arm64-metal` | Metal，内嵌 Metal library | macOS 14.4 | DMG |

GPU 配方固定引用 [llama.cpp b11461 的发布流程](https://github.com/ggml-org/llama.cpp/blob/4d756bc72bf00a4aacf410ae15a2d315f3db400d/.github/workflows/release.yml)。Windows CUDA、ROCm 的 SDK setup actions 使用同一完整提交 SHA，CUDA 默认 GPU 架构随该版本上游选择，不另行维护缩减名单。ROCm 仅构建 `gfx1200` 和 `gfx1201` 是本项目明确的范围差异。升级时同时核对源码锁、补丁、SDK、配方和验证结果，不从浮动上游分支临时取构建配置。

Windows 主程序保留 MSVC 和 Qt kit。CUDA、HIP backend 在外置子构建编译，通过 ggml C ABI 接入主程序。最终安装目录只使用主构建的唯一一份 `ggml-base`，不能把独立子构建的同名库覆盖进去。部署需要检查插件的导入导出、ABI 与运行库一致性。

运行包携带所需 CUDA 运行库，以及 HIP、BLAS 库和两个 RDNA4 目标所需资源，保留资源查找要求的目录布局。Vulkan 仅部署运行所需组件，显卡驱动由用户系统提供。依赖收集和包验证以安装目录为边界，不能依赖开发机 SDK 的 PATH 或当前目录。若 Windows ROCm 载荷超过单 EXE 容量限制，安装器使用包含 `setup.exe` 和完整 `payload/` 的 `setup.zip`，便携 ZIP 仍独立生成，不要求安装时联网补齐运行库。

`QCOPILOTS_SEARCH_BACKEND=auto` 是默认运行方式。`cpu` 显式选 CPU，`gpu` 要求实际 GPU 成功初始化，否则明确失败。自动模式的 CPU 回退需要报告原因。诊断中的 `buildVariant`、`selectedBackend`、`deviceName`、`gpuSelected` 和 `fallbackReason` 描述当前运行事实，不以包名替代实际设备状态。没有 GPU 的托管 runner 可以验证编译、依赖部署和 CPU 回退，不能据此宣称 GPU 推理已经实测。

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

上游源码包保持不变，项目所需适配位于本仓库 CMake、补丁或生成代码中。用户包携带对应实际部署库的许可证及 notices。发布准备从同一干净候选及依赖锁生成一份平台无关的对应源码材料，包含原始源码校验值、补丁和构建说明。源码材料和调试符号保留独立生成入口，不由每次普通打包重复生成，也不作为 CI package artifacts 的附加内容。更新依赖后重新执行相关格式、真实模型和包级测试。

libgit2 1.9.1 的适配脚本为 `cmake/qcs-native-git-patch.cmake`。它校验四个上游文件的 SHA-256，在外置构建目录生成按补丁内容寻址的源码副本，保留原始下载缓存。Windows 仓库元数据路径检查使用库调用者实际支持的 4096 个 UTF-8 字节容量，包含结尾 NUL 和预留后缀，继续检查加法溢出。原生路径转换先使用独立缓冲，再校验完整 UTF-8 字节长度并复制，不能将这一边界解读为 4095 个任意 Unicode 字符。另补充已有内存配置 factory 的导出声明。扫描时的 `core.longpaths=true` 仅附加在当前仓库对象的只读配置快照上，不写源仓库或个人 Git 配置。对应源码材料包括原始 libgit2 归档、本脚本和应用源码，可重现这些适配。

第三方动态库保留上游惯用名称，例如 Windows 的 `zlib1.dll` 和 Unix 的 `libz`。`qcs-native-wv2` 是应用私有的适配库，将 Calligra 维护的 wv2 与所需 POLE 源码合并构建，并加入 Qt 6 Core5Compat 字符编码适配、Windows UTF-8 路径处理及生成的配置和导出声明。它不承诺与独立安装的上游 wv2 保持 ABI 兼容，因此使用产品前缀区分这份私有构建。上游命名空间、许可证和作者归属保持不变，适配内容随对应源码材料提供。

许可归属见 [第三方声明](../THIRD_PARTY_NOTICES.md)。Qt 官方 [许可说明](https://doc.qt.io/qt-6/licensing.html) 区分不同模块的许可，不能仅依据“使用 Qt”推断所有模块适用同一条款。发行材料需要与最终部署的实际二进制对应。
