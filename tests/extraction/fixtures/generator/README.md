# 固定中文 OCR 图像的生成

普通测试读取 `../images/qcs-ocr-*.png` 的已提交字节并校验 SHA-256，不需要安装字体、生成工具或联网。四张图分别使用简体、繁体的 40 px 与 80 px 正常字重，原句始终为「安全出口 禁止吸烟」和「社區會議 歡迎參加」。断言只去除空白，必须识别完整原句。

图像由本目录的 C++ Qt QPainter 工具生成。字体为 [Noto Sans CJK 2.004](https://github.com/notofonts/noto-cjk/releases/tag/Sans2.004)，固定提交 `523d033d6cb47f4a80c58a35753646f5c3608a78`，遵循相邻 `NotoSansCJK-LICENSE.txt` 中的 SIL Open Font License 1.1。仓库不分发字体文件，字体只作为开发者生成夹具时的外置输入。

| 字体 | 固定上游路径 | SHA-256 |
|---|---|---|
| NotoSansCJKsc-Regular.otf | `Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf` | `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b` |
| NotoSansCJKtc-Regular.otf | `Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf` | `dce08bd4fd91aa8aa76ed8fea4b694c2dfb8550f67871e326843212ddbeb88b4` |

下载地址以 `https://raw.githubusercontent.com/notofonts/noto-cjk/523d033d6cb47f4a80c58a35753646f5c3608a78/` 加上表中的路径构成。把两个文件放到自选外置字体缓存目录。生成器在加载之前强制校验上述 SHA-256，不使用系统字体回退。

下面的路径都是调用者提供的绝对路径，构建、字体缓存与输出目录须位于源码之外。适当选择本平台的 CMake generator，Windows 的 Visual Studio generator 需要 `--config Release`。

```text
cmake -S <source>/tests/extraction/fixtures/generator -B <external>/ocr-fixture-generator -DCMAKE_PREFIX_PATH=<qt-prefix>
cmake --build <external>/ocr-fixture-generator --config Release
<generator-executable> <font-cache> <external>/generated-ocr <external>/qcs-ocr-fixtures.json
```

生成器记录 Qt 版本、操作系统、字体版本及 SHA、像素尺寸、原句、边距和每张 PNG 的 SHA。已提交的基准来自 Qt 6.12.0 / Windows 11。字体栅格化和 PNG 编码可能随平台变化，因此不要在测试运行时重新生成输入，也不要无审查地更新图片及期待值。重新生成后的图片需要核对可见文字、所有识别断言与差异，再由开发者复制到夹具目录。没有将本机路径写入生成材料。

这个固定输入确实覆盖识别缺陷。80 px 简体在原流程中以 84 的置信度把「禁」识别为「茶」，半尺寸识别为原句，置信度 91。80 px 繁体的自动布局为空，同尺寸单块布局漏掉首字「社」，完整半尺寸结果与缺字结果的置信度均为 94。因此生产代码对明确单行大字额外检查半尺寸候选，在置信度相同且完整候选包含原有字母数字序列时才补齐。普通多行或多栏仍保留自动布局路径。

先前 macOS PingFang 生成的输入出现「蔡止」「蒜止」和文字错序，属于不同字形的真实 OCR 错误。固定图像让各系统测试完全相同的像素，不代表所有字体均能精确识别。四平台原生测试仍须实际执行，失败时保留的输入 PNG 和识别 JSON 会随诊断产物上传。
