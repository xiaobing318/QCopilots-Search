# 二进制 DOC 回归夹具

本目录保留 Apache POI 的原始测试文件，仅供离线自动化测试。文件未经修改。

来源为 [Apache POI](https://github.com/apache/poi/tree/f3688f45b0de8934fb32736ba14efc5bbca4a580/test-data/document)，固定提交 `f3688f45b0de8934fb32736ba14efc5bbca4a580`。原项目的 `legal/LICENSE` 和 `legal/NOTICE` 完整保留在本目录，适用 Apache License 2.0。

| 文件 | 验证内容 | SHA-256 |
| --- | --- | --- |
| `footnote.doc` | 正文、脚注、尾注、批注 | `81a18ac63010789ca08c28de107658a308c758a3cfc667a6edf11ffe888d3e13` |
| `simple-table.doc` | Word 97 正文、六个表格单元格、表格后正文 | `67d7fe8e0555c16516354498a5935f24eeef90beb5efbf526f93a1d6c734da43` |
| `HeaderFooterUnicode.doc` | 页眉、页脚、Unicode 欧元符号和重音字符 | `8bd7fc9552bccb7b71a0dc34ffd801e7d3547d1317fd75d0c35fca3572f6fa4b` |

测试通过 Qt 资源系统读取，再复制到每次独立创建的临时目录中的中文、空格路径。测试不依赖当前工作目录，不修改本目录，不读取用户文档。
