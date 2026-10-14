# relevance-v2 独立媒体素材归属

本目录只存放固定测试素材。16 个主题、32 条正查询和 32 条无答案查询见上级 `relevance_v2_media.json`。素材和标签在首次模型检索前确定，不能根据评估结果替换。`a01` 等文件名不携带答案，清单中的标题、转写、许可和查询不得加入索引。

素材不随应用安装包分发。以下作者及机构没有为 QCopilots Search 提供背书。所有文件的字节数和 SHA-256 都记录在清单中。

| 文件 | 内容与作者 | 来源及许可 | 本项目变化 |
|---|---|---|---|
| `a01.png`、`a02.png` | 原创叉车和手提箱绘图，QCopilots Search contributors | 仓库 MIT 许可证 | 原创固定 PNG，没有第三方图片 |
| `a03.png`、`a04.png` | 原创洗衣设备维护和失物招领告示 | 仓库 MIT 许可证 | 原创文字渲染为固定 PNG |
| `a05.jpg` | Jellyfish from Corpus Christi，Miguel Angel Omaña Rojas | [作者在 Commons 发布的原作](https://commons.wikimedia.org/wiki/File:Jellyfish_from_Corpus_Christi.jpg)，[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) | 下载原文件后未改动，来源页记录作者先前的处理 |
| `a06.jpg` | Great Sand Dunes National Park and Preserve P1012960，National Park Service Digital Image Archives | [来源及公版证据](https://commons.wikimedia.org/wiki/File:Great_Sand_Dunes_National_Park_and_Preserve_P1012960.jpg)，美国联邦政府公版作品 | 下载原文件后未改动 |
| `a07.wav` 至 `a09.wav` | 原创包裹柜、电影取消、护照预约通知 | 原创文字与生成录音按仓库 MIT 许可证提供 | Windows System.Speech，Microsoft Zira Desktop，rate -1，16 kHz 单声道 PCM16。没有分发语音引擎或模型 |
| `a10.mp3` | Yosemite National Park 直升机录音，NPS | [录音来源](https://www.nps.gov/subjects/sound/sounds-helicopter.htm)，[NPS 图库公版声明](https://www.nps.gov/subjects/sound/gallery.htm) | 原录音未改动。来源页的配图不代表录音地点 |
| `a11.mp3` | Sequoia and Kings Canyon National Parks 链锯录音，NPS | [录音来源](https://www.nps.gov/subjects/sound/sounds-chainsaw.htm)，[NPS 图库公版声明](https://www.nps.gov/subjects/sound/gallery.htm) | 原录音未改动 |
| `a12.mp3` | Mount Rainier National Park 摩托车录音，NPS | [录音来源](https://www.nps.gov/subjects/sound/sounds-motorcycle.htm)，[NPS 图库公版声明](https://www.nps.gov/subjects/sound/gallery.htm) | 原录音未改动 |
| `a13.avi`、`a14.avi` | 原创电梯开合及篮球入篮动画，QCopilots Search contributors | 仓库 MIT 许可证 | 8 秒，480×360，8 fps，MJPEG，没有音轨 |
| `a15.mp4` | Halemaʻumaʻu Lava Lake，NPS / Janice Wei | [官方视频和公版声明](https://www.nps.gov/media/video/view.htm?id=AE8532CB-EB00-415A-AF93-5989ECDC2817) | 取官方 360p 文件前 12 秒，缩放至 480×270，去除音轨，H.264 yuv420p CRF 23，速度不变 |
| `a16.webm` | Loggerhead sea turtle (Caretta caretta)，Robert Davenport | [Commons 许可与来源](https://commons.wikimedia.org/wiki/File:Loggerhead_sea_turtle_(Caretta_caretta).webm)，[作者原视频](https://vimeo.com/179656680)，[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) | Commons 版本已删除原作部分片段和音轨。本项目进一步取其前 12 秒，缩放至 480×270，VP9 CRF 32，速度不变 |

全部外部来源于 2026-10-14 获取。两个裁剪视频的下载原文件字节数、SHA-256 和固定时间范围也保留在清单中。裁剪统一使用前 12 秒，与模型输出无关。真实视频的逐半秒画面核验没有发现人脸或个人信息，全部固定音视频通过 FFmpeg 解码检查。这些检查只验证素材内容与可读性，不代表检索验收通过。

三个环境声是实地录制的机械环境声，并非合成音。它们扩展了原来雨声、狗叫和纯电子音之外的场景。全部原创 PNG、WAV、AVI 均是有意纳入版本控制的测试输入，运行时不得在本目录生成缓存或重写源文件。
