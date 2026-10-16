# 对应源码材料

`tools/qcs-source-bundle.py` 收集项目源码、锁定的原始上游归档、构建配置和许可证，生成可独立保存的源码材料。所有归档保持原样，项目的 CMake wrapper 与生成头模板随源码提供。普通打包与 CI artifact 只包含用户包，源码和符号通过独立动作生成。

## 生成与验证

日常开发可以使用统一入口的 `sources`。正式发布由 `release-prepare` 自动从指定干净提交生成材料，流程见[发布指南](releasing.md)。也可单独调用：

```powershell
$buildRoot = 'C:\Dev\github-repos-compilations\qcs'
$qcsCommit = '<完整40位提交SHA>'
python tools/qcs-source-bundle.py `
  --build-root $buildRoot --candidate-commit $qcsCommit --download
```

使用本机真实 Python，或将命令中的 `python` 替换为已配置解释器。默认输出位于 `<build-root>/pkg`：

- `qcs-source-materials-<commit12>.zip`
- 同名 `.zip.sha256`
- `qcs-source-manifest.json`
- `SHA256SUMS`

`--output-dir` 调整输出位置，`--cache-dir` 指定外置归档缓存。`--dependency-root` 指向 CMake 的 `QCOPILOTS_SEARCH_DEPENDENCY_ROOT`，用于复用已下载的依赖归档，默认是 `<build-root>/deps/Release`。统一 `sources` 动作按当前配置自动传入该目录，直接调用工具复用 Debug 或自定义依赖目录时需要显式指定。`--manifest-only` 列出待收集内容，`--download` 允许下载缺失归档，不允许下载时缺项会明确失败。归档进入材料包前校验大小与 SHA-256。

正式材料使用 `--candidate-commit` 绑定完整提交，生成前后确认工作区干净且 HEAD 相同，不接受 `--qt-root`。它不要求本机已安装 Qt，也不混入某一台机器的 SDK 元数据。

```powershell
python tools/qcs-source-bundle.py `
  --verify-bundle '<源码包的绝对路径>' `
  --candidate-commit $qcsCommit --expected-sha256 '<源码包SHA-256>'
```

验证会检查外部包哈希、提交身份和内部每项大小及哈希，不把源码展开到磁盘。包内 `source-manifest.json` 使用 schema 2，包含项目文件、依赖归档、源码树和工作区状态。开发材料可包含未提交修改，但不能作为干净发行提交的证明。

## 包含范围

源码材料以实际清单为准，主要包括：

- `project/` 中的应用源码、测试、图标、CMake wrapper、开发脚本、依赖锁和重建说明。
- Qt base、SVG、Core5Compat、Image Formats、Multimedia、translations 和配套 PDF 源码。PDF 与 WebEngine 共用源码仓库，因此归档名称包含 WebEngine。
- 锁定的 llama.cpp、原生提取与存储依赖、GNU libiconv，以及相关 OCR 数据归档。
- FFmpeg 9.0.1 源码。Qt 的 Windows FFmpeg 所用 zlib 1.3.1 与应用所用 zlib 1.3.2 分别保留。
- GPU 许可锁指定的开源组件与构建配方，保留归档内许可证和逐文件归属。

GPU 材料不构成完整离线 SDK 源码集。ROCm 的 TheRock 构建配方归档不包含 Git submodules，锁中部分精确提交没有完整源码归档，CUDA 的部分开发组件也没有独立归档条目。按 `source-manifest.json` 的实际内容判断覆盖范围，不能以构建配方代替其所有依赖源码。

开发者在独立材料任务中指定 `--qt-root` 时，可以附加实际 SDK 的公共构建元数据、原始 SPDX、FFmpeg 配置及运行库哈希。这类机器相关信息不混入正式平台无关源码包。FFmpeg 元数据只从所选 SDK 的匹配库读取，加载失败或 ABI 不匹配明确报错。

Microsoft Visual C++ CRT/OpenMP 依据适用再分发条款部署，不属于开源对应源码收集对象。模型权重由独立下载或离线导入提供，不包含在源码材料中。

## 运行包的许可材料

安装包通过构建规则自动收集实际部署组件的许可证和 notices，通常位于 `share/qcs/licenses`，macOS 位于应用 bundle 的 `Contents/Resources/qcs/licenses`。

Qt 材料包含所选 SDK 的模块 SPDX、版权声明、自定义许可全文和源码锁指定的 attribution 引用原文。SDK SPDX 可能包含未部署功能，不将其描述为整个应用的精确运行依赖图。GNU libiconv 包含 `COPYING.LIB`、`COPYING` 和 libcharset 的 `COPYING.LIB`。项目和 DigUp 的 MIT 归属同样保留。

`QCOPILOTS_SEARCH_DOWNLOAD_LICENSE_SOURCES=OFF` 可禁止许可源归档下载，配合 `QCOPILOTS_SEARCH_LICENSE_SOURCE_CACHE` 使用预先准备的锁定缓存。缓存和生成文件均在源码树外。安装和包级验证重新核对材料的文件集合、大小与哈希。完整组件归属见[第三方声明](../THIRD_PARTY_NOTICES.md)。

## 重建与替换动态库

先验证外部 SHA-256 和包内清单，在源码树外展开需要的上游归档。按[开发指南](development.md)准备匹配架构的编译器、Qt kit 和外置构建根。

原生依赖可使用已校验缓存，或通过 `FETCHCONTENT_SOURCE_DIR_<DEPENDENCY>` 指定匹配的原始源码目录。llama.cpp 也支持 `QCOPILOTS_SEARCH_LLAMA_SOURCE_DIR` 和 `FETCHCONTENT_SOURCE_DIR_QCS_LLAMA`，无需 `.git` 元数据。配置会检查完整源码清单并拒绝额外文件、缺失文件或内容变化，构建完成后再次验证。生成头和校验标记存放在源码目录之外。

wv2/POLE 与 GNU libiconv 由项目自有 CMake wrapper 直接编译上游原始文件。Windows 使用共享 CRT，不需要 Cygwin。第三方库的公开 API 调用适配位于应用源码中，不通过重写上游文件实现。

重建 Qt 时，先用对应 Qt base 配置动态库安装 prefix，再通过该 prefix 的 `qt-configure-module` 构建需要的模块。PDF 构建有额外依赖，按照归档内说明和[Qt 源码构建指南](https://doc.qt.io/qt-6/build-sources.html)准备工具。

FFmpeg 重建以 Qt 配套配置和实际 ABI 为准，保留动态链接与许可选项，不启用与发行声明不同的 GPL/nonfree 扩展。替换动态库后，在独立安装副本及 profile 中重新运行格式、真实模型和最终包验证。编译成功本身不证明运行兼容或数值一致。

材料包使用稳定排序与固定 ZIP 时间戳，发布清单绑定其最终 SHA-256。上传前再次验证全部字节，每个 Release 附件必须小于 2 GiB，超限明确失败。
