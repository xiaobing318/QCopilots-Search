# 小型功能夹具

`assets.json` 锁定六份媒体的相对路径、字节数、SHA-256、来源与许可。`media/NOTICE.md` 保留外部素材归属。素材字节没有改变，只迁移到本目录，供真实多模态功能测试和 Windows 界面验收复用。

多模态功能测试使用 `tests/semantic/semantic_acceptance.cpp` 生成文档、PDF、代码、OCR、图形、音频与视频的小型输入，再调用正式提取、编码、持久化与检索路径。Windows 界面验收通过 `tools/acceptance/qcs-copy-cu-fixtures.py --prepare-source` 在外置目录生成独立样本，并记录来源与文件哈希。

这些夹具验证应用功能与定位字段，不用于相关性校准、容量或性能验收。不得把清单中的文字说明和录音原文加入索引来替代媒体编码。
