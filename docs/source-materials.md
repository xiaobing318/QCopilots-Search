# 对应源码材料

安装包的许可材料需要对应实际部署的库。`tools/qcs-source-bundle.py` 收集锁定源码归档、项目源码、补丁、构建适配和公共 SDK 元数据，生成可独立保存的源码材料包。它不会只生成下载链接，也不会为未准备的源码作承诺。

```powershell
python tools/qcs-source-bundle.py `
  --build-root $buildRoot --qt-root $qtRoot --download
```

输出位于 `<build-root>/pkg`：

- `qcs-source-materials-<commit>-<platform>-<arch>.zip`
- `qcs-source-materials-<commit>-<platform>-<arch>.zip.sha256`
- `qcs-source-manifest.json`

下载文件优先复用已经校验过的 CMake 缓存。Qt 源码由 `tools/qcs-source-packages.lock.json` 锁定，其他原生库从实际 CMake 依赖声明读取 URL 和 SHA-256。文件大小和哈希校验通过后才能进入材料包。`--manifest-only` 可以查看待收集清单，`--download` 允许补齐缺失归档，不传该参数时缺失归档导致明确失败。

`--verify-bundle FILE --expected-sha256 HASH` 会检查整个材料包的外部哈希，并逐项读取包内源归档、项目源码和 SDK 元数据，核对 manifest 中的大小与哈希，不展开源码到磁盘。

## 包含范围

材料包保留上游归档的压缩形式，不展开体积较大的 Qt WebEngine / PDF 源码。它包含实际使用的 Qt base、SVG、Core5Compat、Image Formats、Multimedia、translations 和配套 PDF 源码。Qt PDF 的源归档名称为 Qt WebEngine，因为两个模块共用该源码仓库。

FFmpeg 使用固定 9.0.1 源码，清单记录当前 SDK 的真实 `avcodec_configuration()`、许可证及运行库 SHA-256。Windows 配套 FFmpeg 使用的 zlib 1.3.1 源码与应用自身使用的 zlib 1.3.2 分开保留。第三方源归档内部包含其原始许可证和版权声明。

Linux 读取 Qt SDK 的 FFmpeg 元数据时，先从选中的 `libavcodec.so.63` 所在目录以 `RTLD_LOCAL` 加载 `libavformat.so.63` 及其依赖组，再以相同方式加载 avcodec，并在元数据读取期间保留依赖组句柄。缺少同目录的 avformat、动态加载失败或元数据符号读取失败都会明确失败，不跨 SDK 寻找替代库。Windows 和 macOS 保留原有加载路径，FFmpeg ABI 及 GPL / nonfree 配置拒绝规则保持不变。

项目源码放入 `project/`，其中 `cmake/` 是原生依赖构建适配，`third_party/patches/` 保存 llama.cpp 补丁，`tools/` 保存 SDK、部署及材料生成流程。源文件清单逐项记录哈希。若打包期间文件发生变化，脚本拒绝继续发布该材料包。

指定 `--qt-root` 时，`sdk-metadata/` 同时保留该 SDK 的公共构建配置和原始 SPDX JSON，包含第三方版权及自定义许可全文。运行包中的 `qcs-runtime` 许可目录由 `tools/qcs-license-materials.py` 自动生成。它使用上述源码锁和外置许可证源码缓存，默认位于 `deps/licenses`，收集 Qt base、SVG、Image Formats、Multimedia 的原始许可和 attribution 引用文件，并按实际 SDK SPDX 检查标准许可全文是否齐全。对应源码材料工具的默认归档缓存位于 `deps/sources`。PDF / Chromium 的自定义许可来自实际 SDK SPDX 的原始 `extractedText`。任何平台新增未覆盖的许可 ID 都会明确阻止配置。

离线配置可设置 `QCOPILOTS_SEARCH_DOWNLOAD_LICENSE_SOURCES=OFF`，并通过 `QCOPILOTS_SEARCH_LICENSE_SOURCE_CACHE` 指定预先准备的锁定归档目录。这里的缓存和生成目录均位于源码树外，安装步骤和包级测试会重新验证生成材料的文件集合、大小与哈希。

材料包使用固定 ZIP 时间戳和稳定排序。清单记录 `commit` 与 `workingTreeModified`，有未提交修改的材料包用于开发验证，不能作为不可变发行提交的证明。最终发布前在干净的目标提交重新生成源码材料和运行包。

Windows 另外部署的 Microsoft Visual C++ CRT / OpenMP 是按适用许可再分发的运行库，不属于开源对应源码收集范围。它们的归属和部署方式见 [第三方声明](../THIRD_PARTY_NOTICES.md)。

## 重建与替换动态库

先验证外部 SHA-256，再检查包内 `source-manifest.json`。在源码树外展开所需上游归档，保留它们的许可证。应用的一般构建流程见 [开发指南](development.md)。

原生库由项目 CMake 配置控制。开发者可以将已校验的归档放入外置依赖缓存，或使用标准 `FETCHCONTENT_SOURCE_DIR_<DEPENDENCY>` 选项指向匹配的已展开源码。llama.cpp 也接受没有 `.git` 的官方归档展开目录，通过 `QCOPILOTS_SEARCH_LLAMA_SOURCE_DIR` 或 `FETCHCONTENT_SOURCE_DIR_QCS_LLAMA` 指定。配置阶段使用自身哈希已锁定的完整文件清单校验该目录，只允许经过审核的 no-logits 和 UTF-8 后端路径补丁对应的精确文件哈希。应用补丁仍需要 Git 程序，不需要网络或上游 Git 元数据。

原生 CI 收集源码后，运行 `tests/runtime/source_integrity_test.py --archive <locked-archive> --report <report-path>`。该测试在唯一临时目录中实际展开归档，验证无 Git 元数据的源码、位于无关 Git 仓库中的源码、两份补丁、重复应用以及文件、清单和补丁被修改后的拒绝行为。结果写入 `reports/source-integrity.json`，逐项保留跳过原因，发布验收要求全部必需用例实际执行成功。这个检查证明源码准备与完整性规则，不代替完整应用构建及运行测试。

重新构建 Qt 时，使用材料包中对应版本的 Qt base 作为基础，使用平台原生编译器及与应用匹配的架构，以动态库模式配置并安装到独立 prefix。随后使用该 prefix 的 `qt-configure-module` 构建 SVG、Core5Compat、Image Formats、Multimedia 和 PDF 等模块。具体编译依赖以源归档内的构建说明及 [Qt 源码构建文档](https://doc.qt.io/qt-6/build-sources.html) 为准，PDF 的构建依赖多于普通 Widgets 模块。

FFmpeg 重建使用清单中的真实配置作为依据，将上游构建机绝对路径替换为本机依赖路径，保留 ABI、动态链接方式和许可选项。不要开启 GPL / nonfree codec 来生成与原发行声明不同的运行库。项目通过版本与符号检查拒绝不匹配的 FFmpeg ABI。

替换库后，在单独的安装副本和 profile 中重新运行包级验证。应用采用动态库部署，不阻止符合许可证的库替换。不同编译选项、补丁或系统库可能改变行为，重建成功不自动代表黄金向量和格式回归通过。

## 发布检查

源码材料的最终 SHA-256 与对应运行包一起保存。检查依赖锁、Qt / FFmpeg 配置、上游归档、完整许可证、项目补丁和重建说明均已包含。模型权重按独立下载或离线导入提供，源码材料包不包含模型。
