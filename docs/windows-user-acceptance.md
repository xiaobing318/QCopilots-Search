# Windows 用户操作验收

本验收使用最终安装包中的应用，通过 Windows Computer Use 插件实际观察和操作窗口。QtTest 报告与本报告分别保存。CLI 冒烟或截图生成不能替代真实用户流程。

## 运行前准备

创建唯一的验收根、profile 和样本目录。根目录由系统临时目录或显式外置测试根派生，路径应包含中文或空格。把相同 profile 传给应用及其服务，只读取专用样本，不连接正式实例。记录安装包文件名、SHA-256、源码提交、模型指纹、Windows 版本、显示缩放和输入法。

Computer Use 应先读取当前安装版本的 Windows Computer Use 技能及其 guidance、api 和 confirmations 文档，通过实际可用的 `mcp__node_repl__js` 初始化 `@oai/sky`。调用 `sky.list_apps()` 实际枚举应用，选择唯一目标窗口，再调用 `sky.get_window_state` 获取最新状态。浏览器入口没有原生应用能力不代表 Windows 插件不可用。每次操作依据最新窗口状态，错误后按技能执行有界恢复，不复用失效控件索引。

## 场景记录

| ID | 场景 | 预期结果 | 必需证据 |
| --- | --- | --- | --- |
| CU-01 | 首次启动及引导 | 独立 profile 显示引导，可理解本地索引和模型状态 | 引导窗口、实际点击步骤 |
| CU-02 | 选择目录和排除规则 | 仅索引所选样本，排除目录不出现在结果中 | 配置截图、索引结果 |
| CU-03 | 模型导入或下载、进度、取消和重试 | 显示真实进度及校验结果，错误可恢复 | 模型页面、错误与恢复记录 |
| CU-04 | 中文输入法和中英文查询 | 组合输入期间状态正确，提交后有对应查询结果 | 输入法状态、组合与提交后的界面 |
| CU-05 | 分类过滤、键盘导航和结果操作 | 分类改变候选，方向键与 Enter 对应当前结果 | 筛选与焦点截图、打开结果 |
| CU-06 | 文档、图片和 PDF 页预览 | 显示正文或图片，PDF 跳到命中页 | 每类预览及页码 |
| CU-07 | 音视频命中时间播放和切换 | 播放从命中位置开始，切换结果停止旧播放 | 时间位置、切换操作 |
| CU-08 | 代码搜索与编辑器定位 | 代码开关有效，应用内显示正确行号和 Notebook 单元格，配置的编辑器定位源码行 | 应用内行号/单元格、实际编辑器源码行位置 |
| CU-09 | 暂停、恢复和文件变化 | 进度与状态一致，修改后更新结果，无旧版本返回 | 暂停状态、变化前后结果 |
| CU-10 | 托盘、全局快捷键、设置、退出与重启 | 可恢复主界面，能力状态准确，配置在重启后保留 | 托盘/浮层、设置及重启结果 |

实际报告逐项记录“通过、失败、未运行或平台能力不可用”，附预期、操作、实际结果和必要截图路径。失败后修复并重新运行相关场景，保留修复前后的证据。不要把未运行项改成通过。

所有场景使用同一最终安装包及其独立动态 profile，不能拼接不同候选的截图作为一次通过。工具无法操作、用户协助和工具随后核验的部分分别写入操作记录。必需场景没有实际完成时，本轮验收保持未通过。QtTest 自动化和 Computer Use 各自提供证据，并与本地完整自动化、四组合原生 Actions 及容量门槛一起判定交付。

外部 Notebook 单元格定位只在明确支持该能力的编辑器配置下单独验收。编辑器命令中的 `{cell}` 只是参数替换，通用的文件与行号打开方式不保证跳到 Notebook 单元格，不能用应用内定位截图代替外部编辑器证据。

## 报告字段

机器可读原始报告保存在本次运行根，例如 `windows-computer-use-<UTC>.json`。以下字段与 `tools/qcs-release-evidence.py` 的 `verify_manifest`、`validate_cu` 和最终 `preflight` 契约一致：

| 范围 | 必需内容 |
| --- | --- |
| 报告身份 | `schemaVersion: 1`、`kind: "windows-computer-use"`、完整 `identity`，以及与 `identity.checkoutCommit` 相同的 `commit` |
| 候选绑定 | `packageSha256`、四个程序的 `executables`、`modelFingerprint`、`extractionFingerprint`，均与本次 `package-candidate.json` 对应字段一致 |
| 运行信息 | `profileRoot`、`platform`、UTC `startedAt` / `finishedAt`、`passed`、`scenarios`、顶层内嵌的 `files` 数组 |
| 每个场景 | `id`、`status`、非空 `expected` / `actual`、非空 `actions` 数组、`packageSha256`、`executables`、UTC `startedAt` / `finishedAt`、非空 `screenshots` 数组、`operationLog` |
| 每个附件 | `files` 中的 `{path, bytes, sha256}`，对应实际文件的相对路径、字节数和 SHA-256 |

从实际构建产生的 `package-candidate.json` 原样保留 `identity`，其中包括 `candidateHead`、`checkoutCommit`、`sourceTree`、`workingTreeModified: false` 和完整 `lockHashes`。同样原样保留四个程序的 `executables` 对象及每个对象的字段，不能仅摘取程序名或事后用当前 HEAD 替换来源。每个场景的包哈希和 `executables` 必须与报告顶层一致。

`CU-01` 至 `CU-10` 各出现一次。每个场景的开始时间必须早于结束时间，且完整落在本次报告的执行区间内。只有十个场景实际通过且证据完整时才能写 `passed: true`。失败、未运行和能力不可用分别保留 `failed`、`not_run`、`unsupported`，不能改写为 `passed`。

所有 `screenshots`、`operationLog` 和 `files[].path` 均以原始报告所在目录为根，使用不含 `..` 的相对 POSIX 路径。每张实际 PNG/JPEG 截图和每份非空操作日志必须在顶层 `files` 中登记，记录内容哈希。不要将报告自身放入其 `files`，以免产生自引用哈希。附加的 `-files.json` 可以用于归档，但不能代替原始报告内嵌的清单。

执行 [发布指南](releasing.md#本机最终证据流程) 中的 `collect-local` 时，`--computer-use` 传入这份自包含的原始 JSON，不传入附加清单。导入器按该 JSON 所在目录查找并校验附件，再复制到新的证据根。最后运行 `preflight` 核对同一候选的全部发行门槛，仅有 CU 报告不能证明发布完成。

报告发布前将个人目录和无关桌面内容脱敏。清理时只停止本次创建的应用与服务，只删除经过边界验证的验收根。源文件删除、安装或其他需当场确认的 UI 行为按 Computer Use 技能要求执行，测试范围本身不扩大插件权限。

本文是验收方法与记录模板，实际执行结论由 [实施状态](implementation-status.md) 引用的报告给出。
