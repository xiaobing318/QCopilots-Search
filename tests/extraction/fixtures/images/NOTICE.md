# 原创图像测试夹具

本目录四个图像均为 QCopilots Search 测试生成的原创梯度，不含个人照片、用户文件或第三方媒体。它们遵循仓库根目录的 MIT 许可证。普通测试直接读取已提交的固定字节，不需要 Python、相机设备或网络。

| 文件 | 内容与编码 | SHA-256 |
|---|---|---|
| `generated-gradient.heic` | 256 × 128 RGB，像素 `(x, 2y, (x+y) mod 256)`，Pillow 12.3.0 与 pillow-heif 1.8.1，HEIF quality 95 | `32e0919c49a25e3896652a124c3c6f30a7cf60445e422e7c9d9730a35f20e29e` |
| `generated-uncompressed.dng` | 64 × 64，16-bit CFA，未压缩 | `7a9e5a5c4f369b85d8e18bb35337bea183dbbf7f372b700d18f06d2a85fc58a9` |
| `generated-deflate.dng` | 64 × 64，32-bit float CFA，Deflate 与浮点 Predictor 3 | `d6cd333bd6c96a5ed40b25d5a25cb76cefcf5829484cdf844f6c259fb98d2fe4` |
| `generated-jpeg.dng` | 64 × 64，8-bit LinearRaw，JPEG 压缩，JPEG 字节由 Windows System.Drawing 编码 | `65eb7a5050b3f87172b9a424cfa9b7652adc0c30a260b1d4b971e14c2a1fb28b` |

DNG 使用本项目构造的 TIFF IFD 和 DNG 1.4 元数据。未压缩梯度值为 `4096 + ((257x + 127y) mod 50000)`。浮点梯度为 `0.1 + ((257x + 127y) mod 50000) / 60000`。相机名称为 `QCopilot / SearchRAW`，不是对真实相机文件兼容性的声明。

测试要求真实解码、按像素尺寸验证结果、确认非均匀图像并验证中文文件名下的解码和预览。LibRaw 对 CFA 使用半尺寸处理。其当前后端不支持的整数 16-bit Deflate DNG 不在这些通过的样本范围内。
