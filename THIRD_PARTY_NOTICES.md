# 第三方声明

QCopilots Search 的自有代码使用根目录的 MIT 许可证，Copyright (c) 2026 xbyang。下列依赖和参考内容保留原许可证。此清单用于说明归属，最终发行材料必须包括与实际二进制对应的完整许可证、版权声明、源码和必要补丁。

## DigUp

项目参考 [DigUp](https://github.com/ARahim3/DigUp)，参考提交为 `6faa29e4c315a3a591de1b41bd78751bdf39588d`。推理契约、图像预处理、no-logits 补丁和参考向量的改编保留原作者归属。黄金夹具的原始许可证同时保存在 `tests/runtime/fixtures/LICENSE-DigUp`。

```text
MIT License

Copyright (c) 2026 Abdur Rahim

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 库、模型与运行数据

| 组件 | 上游许可或说明 | 集成方式 |
| --- | --- | --- |
| Qt Core、Gui、Widgets、Network、XML、SVG、Core5Compat、Multimedia 等 | 对应模块的 LGPLv3 / GPL / 商业许可选项，发行使用适用的 LGPL 模块，另含第三方组件 | 动态库与插件，保留 Qt 许可及 SBOM |
| Qt PDF / PDFium | Qt PDF 模块许可及 PDFium 所含第三方许可 | 动态库及实际随附资源 |
| llama.cpp / ggml | MIT | 固定版本，应用 no-logits 补丁 |
| EmbeddingGemma 2 GGUF | 固定模型卡标注 Apache-2.0 | 下载或离线导入，不嵌入安装包 |
| SQLite | Public domain，上游声明 | FTS5 构建 |
| hnswlib | Apache-2.0 | 头文件库 |
| cppjieba / limonp | MIT | 头文件与中文词典 |
| miniz | MIT | ZIP 读取 |
| Tesseract | Apache-2.0 | OCR 库 |
| Leptonica | BSD 2-Clause | 图像处理库 |
| tessdata_fast | Apache-2.0 | eng、chi_sim、chi_tra 数据 |
| FFmpeg | 对应构建使用的 LGPL 条款及第三方许可 | Qt 配套动态运行库，不启用 GPL / nonfree 扩展 |
| libheif | LGPL-3.0-or-later | 动态解码库 |
| libde265 | LGPL-3.0-or-later | 动态 HEVC 解码库 |
| LibRaw | LGPL-2.1 或 CDDL 1.0 双许可，本项目采用 LGPL 路径 | 动态库，不启用 GPL demosaic pack |
| libgit2 | GPLv2 加链接例外 | 只使用本地仓库和 ignore 能力 |
| wv2 | 上游 COPYING.LIB 及逐文件 LGPL 声明 | 动态库，保留适配补丁 |
| POLE | BSD 3-Clause | Calligra 的 OLE 读取实现，保留 pole.h 声明 |
| zlib | zlib license | 压缩依赖 |
| libjpeg-turbo | IJG、BSD-style 及 zlib 声明，以上游 LICENSE.md 为准 | 图像依赖 |
| libpng | libpng 许可证及源包内逐项声明 | OCR PNG 读写 |
| libtiff | 上游 LICENSE.md 的宽松许可和版权声明 | OCR TIFF 读写 |
| Microsoft Visual C++ CRT / OpenMP runtime | Microsoft Visual Studio / Visual C++ Redistributable 的适用再分发条款 | Windows 使用获准再分发的 app-local 运行库，非开源对应源码交付对象 |

许可证文件由依赖安装规则收集到 `share/qcs/licenses`，macOS 位于应用包的 `Contents/Resources/qcs/licenses`。其中 `qcs-runtime` 自动保留实际 Qt SDK 的八份模块 SPDX、所含版权声明、自定义许可全文和锁定 Qt 源码中的许可、NOTICE 与 attribution 引用原文。它同时保留 llama.cpp 的完整 MIT 声明，该上游声明覆盖其内置 ggml。生成和安装过程校验源归档及逐项文件哈希，包级验证再次检查必需材料。

这些 Qt SPDX 是 SDK 模块清单，可能包含未部署的可选功能或外部引用，不宣称它们是应用完整的运行依赖关系图。保留上游多种许可文本不会改变本项目采用的发行条款。发行审查应检查实际安装包和对应源码，而不仅检查本表。

Windows 包中的 `msvcp140`、`vcruntime140` 和 `vcomp140` 等编译器运行库来自构建所用 Visual Studio 的可再分发目录，按 Microsoft 适用许可分发，保留未经修改的发行版本并排除 `debug_nonredist` 文件。它们以应用目录中的动态库部署，无需为了运行该包单独启动管理员权限的 VC Redistributable 安装程序。对应源码工具只收集项目和开源依赖，不为 Microsoft 运行库宣称提供源码。参见 [Visual Studio 2022 再分发清单](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution) 和 [Visual C++ 文件再分发说明](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)。

参考来源：[Qt 许可与 SBOM](https://doc.qt.io/qt-6/licensing.html)、[LibRaw 许可说明](https://www.libraw.org/about)、[tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)、[固定模型卡](https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/blob/bfcd298762cc34d0357ece5ebdd31791a3a374d8/README.md)。

## 对应源码与修改

依赖锁记录上游版本、下载位置和 SHA-256。项目对第三方依赖的构建适配位于 `cmake`，llama.cpp 补丁位于 `third_party/patches`。发行材料应包含锁定源码归档、这些适配、构建脚本和工具链说明，以便重建对应动态库。

Qt 使用官方二进制 SDK，需要提供与 SDK 对应的源码材料及适用许可证。FFmpeg 必须记录配套构建的配置与源码，不把任意系统 FFmpeg 当作对应版本。保留用户按许可证替换动态库的能力，发布签名状态不能改写组件原有许可。

新增夹具、数据集、图标或媒体样本时必须记录来源与可再分发条件。当前实际验收与发行材料是否齐全见 `docs/implementation-status.md`。
