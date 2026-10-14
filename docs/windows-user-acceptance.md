# Windows 用户操作验收

正式验收使用 `release-prepare` 冻结清单中的 Windows CPU portable ZIP，通过 Windows Computer Use 插件实际观察和操作其中的应用。先完成同一干净候选的本机完整 `check` 和 11 变体 CI，再冻结 CI 包、在本机验证这一 ZIP，并通过 `record-ci-package` 建立来源候选。QtTest 报告与本报告分别保存，CLI 冒烟或截图生成不能替代真实用户流程。

## 运行前准备

创建唯一的验收根、profile 和样本目录。受控验收根、应用解包目录、安装及搬移目录、profile 均使用英文且无空格的路径，放在显式外置测试根下。中文查询、中文及空格样本文件继续保留，用于验证内容搜索和文件处理。把相同 profile 传给应用及其服务，只读取专用样本，不连接正式实例。记录安装包文件名、SHA-256、源码提交、模型指纹、Windows 版本、显示缩放和输入法。

先固定 CI ZIP 与 `record-ci-package` 输出的 `package-candidate.json` 身份，再从该 ZIP 的新解包副本启动 `qcs-gui`。本次应用、服务、worker、模型目录和外部工具的位置必须明确记录，不能混用构建树中的旧程序。相同源码提交不保证本地重建包与 CI 包字节相同。源码修复产生新候选，同一提交重新打包或更换 CI attempt 也可能改变包身份，必须按新的来源重新记录和执行相关验收，不能只替换旧报告哈希。样本准备、外部工具和报告均位于源码树与正式用户 profile 之外，不修改已冻结的包来补装依赖。完整验包与 `record-ci-package` 命令见 [候选验收运行器](../tools/acceptance/README.md#包与-windows-操作记录)。

准备至少 205 个文件作为三页浏览样本，保存文件清单、相对路径、类型、修改时间及内容哈希。修改时间应有可核对的先后顺序，也包含相同修改时间的文件，用于验证默认倒序和重复时间下的稳定分页。另准备中文及空格文件名、长路径、多页 PDF、可继续读取的长文档、图片、带明确命中时刻的音视频、代码和 Notebook。普通文件与代码分别设置根目录及排除目录，包含仅属于其中一个空间的样本和允许在两空间独立出现的文本。样本总量、关键词定位用例与空范围查询应在操作前记录。语义结果记录实际返回文件和定位字段，不要求自然语言查询命中特定片段。

使用同一冻结源码中的 `tools/acceptance/qcs-copy-cu-fixtures.py --prepare-source` 准备专用夹具，不新增独立生成脚本，也不依赖历史语义测试目录。下面的路径占位符均须替换为绝对路径，准备根应位于源码树外，`source` 必须尚不存在。生成器不启动应用、不加载或下载模型，也不修改用户 profile。

```powershell
$qcsSource = '<冻结候选的源码目录>'
$qcsPreparation = '<外置构建根>/cu-fixtures/<本次唯一目录>'
$qcsTools = Join-Path $qcsSource 'tools/acceptance'
$qcsFixtureSource = Join-Path $qcsPreparation 'source'
$qcsFixturePlan = Join-Path $qcsPreparation 'copy-plan.json'

python -B "$qcsTools/qcs-copy-cu-fixtures.py" --prepare-source --source-root $qcsFixtureSource
if ($LASTEXITCODE -ne 0) { throw 'CU 夹具准备失败，保留现场并使用新目录重试' }
python -B "$qcsTools/qcs-copy-cu-fixtures.py" --create-plan --source-root $qcsFixtureSource --plan $qcsFixturePlan
if ($LASTEXITCODE -ne 0) { throw 'CU 夹具计划创建失败' }
```

准备结果由 `negative-models/qcs-fixture-source.json` 逐文件记录用途、查询、字节数、SHA-256 和毫秒修改时间。生成器只读取源码中的 `tests/acceptance/fixtures/assets.json`、`media/NOTICE.md`、`media/photo-002.jpg`、`media/speech-001.wav`、`media/speech-002.wav`，其中 `media/` 均相对 `tests/acceptance/fixtures/`。照片和语音先校验锁定资产哈希，许可说明原样保留在不索引的 `negative-models/fixture-licenses/`。其他文件由当前脚本确定性生成，清单绑定脚本本身及上述输入的哈希。不会自动寻找或复制旧 corpus、模型、索引、报告。后续创建计划和复制时会复核这些来源与实际文件，脚本或资产变化后必须重新准备，不能只修改旧清单中的哈希。

准备中途失败时保留已写文件及 `.qcs-fixture-preparing.json` 标记，便于调查，不自动清理目录。带标记的目录不能创建计划或复制，不能当作手工样本继续使用，应选择一个新输出根重新准备。遍历权限不足、链接或重定向会明确失败，不静默省略目录。复制中途失败也保留本次已写副本与可写入的失败报告，不删除或改写源夹具。

| 相对准备根的目录 | 内容与用途 |
| --- | --- |
| `samples/分页 普通文件` | 恰好 205 个普通 UTF-8 文本，包含中文、空格和多层长路径。只将该目录设为普通 root 时，空查询应产生 100/100/5 三页 |
| `samples/内容定位` | 原创三页 PDF、DOCX 文档、固定猫照片、75 秒组合语音及 12 秒原创 AVI。PDF 的唯一目标在物理第 3 页，语音目标在第 55 秒后，视频蓝色方块在第 6 秒后 |
| `samples/内容定位/图片缩放` | 三张原创 BMP，横图 1600×900、竖图 900×1600、小图 160×120。四角分别为红、绿、蓝、金色，带 100 像素网格，供 CU-06 观察缩放、双向滚动、切图适配和滚动范围清除 |
| `samples/代码 单元格` | Python 源码和四单元格 Notebook，含 `{line}` 字面文件名。清单记录目标源码行和 Notebook 第 4 单元格，外部编辑器须核对实际命中片段起始行 |
| `samples/共享 文本` | 可分别加入普通与代码 root 的同一个文本，用于独立清理与范围验证 |
| `samples/普通 排除`、`samples/代码 排除` | 两空间独立排除的 canary，先记录范围，再验证所属空间的可见性 |
| `samples/变更与续读` | 修改、删除 canary 和 240 段原创长文本。长文包含头、中、尾独立标记，供续读、暂停和公平调度观察 |
| `samples/故障输入` | 明确损坏的 DOCX 和 WAV，只在故障场景中加入，不影响最初 205 个普通文件 |
| `negative-models` | 故意损坏的模型、来源与许可清单、待新增短文件及修改后的内容模板，整个目录不作为索引 root |

分页文件使用固定整秒修改时间，第 99–102 项、第 199–202 项分别形成相同时间组，跨越两处分页边界。预期首先按修改时间倒序分组，同时间组的正式次序由本次索引产生的 `file_id DESC` 决定，不能用文件名次序替代。记录首次浏览的实际 ID/次序，再核对刷新稳定、无重复和漏项。CU-05 开始前把普通 roots 恢复为仅含 `samples/分页 普通文件`，清空查询与筛选，其他普通场景目录随后按需要加入。代码目录不计入这 205 个普通文件。

定位检查以最终包实际返回结果的时间字段为准。夹具中的 `targetRegionMs`、`expectedWindowStartMs`、`expectedFrameStartMs` 保留用于提取与定位一致性检查，不要求自然语言查询一定选中这些预设片段。记录结果自身的非零时间或非首页字段，再核对内置预览及外部应用是否到达相同位置。只打开文件或看到进程启动不能标记定位通过。

取得 `record-ci-package` 输出的 `package-candidate.json` 与冻结 CI ZIP 后，先初始化记录器，再复制夹具。`--fixture-manifest` 传准备结果中的来源清单，将它保留为候选输入。示例中的包和解包目录均来自同一冻结资产，解包目录和运行根使用英文且无空格的路径。原冻结发行目录与候选记录目录在运行期间持续保留，记录器每次核对实际来源与包字节。

```powershell
python -B "$qcsTools/qcs-cu-recorder.py" init `
  --candidate-record '<record-ci-package输出的package-candidate.json绝对路径>' `
  --package '<冻结manifest所选CPU ZIP绝对路径>' `
  --app-dir '<该ZIP新解包副本的bin目录>' --source $qcsSource `
  --run-parent '<外置CU运行父目录>' `
  --fixture-manifest "$qcsFixtureSource/negative-models/qcs-fixture-source.json"
if ($LASTEXITCODE -ne 0) { throw 'CU 记录器初始化失败' }

# 使用 init 实际输出的 runRoot，不猜测它生成的唯一目录名。
$qcsRun = '<init输出的runRoot>'
python -B "$qcsTools/qcs-copy-cu-fixtures.py" --run-json "$qcsRun/run.json" --plan $qcsFixturePlan
if ($LASTEXITCODE -ne 0) { throw 'CU 夹具复制预检失败' }
python -B "$qcsTools/qcs-copy-cu-fixtures.py" --run-json "$qcsRun/run.json" --plan $qcsFixturePlan --execute
if ($LASTEXITCODE -ne 0) { throw 'CU 夹具复制失败，保留本次失败记录' }
```

复制计划使用 schema 2，逐项绑定 `mtimeMs`，写入目标后恢复并核验实际修改时间，不修改源文件的字节或修改时间。旧 schema 1 计划需重新创建。复制必须在启动应用、模型导入及 `begin` 场景之前完成，因为此时 `samples`、`profile`、`models`、`evidence` 和 `exports` 必须为空。完成记录写入 `negative-models/qcs-fixture-copy.json`，`acceptancePassed` 始终为 false。CU-09 只操作本次 run 内的 canary，将 `negative-models/actions/待新增 short.txt` 复制到本次已索引的变更目录作为新增事件，用对应模板改写修改 canary，删除 canary 也仅针对本次副本，保留原始准备集及时间线证据。

执行前需在外部构建根准备可用的 SumatraPDF 和 mpv，并记录实际工具版本、路径和文件哈希。显式配置为这些程序，不通过 PATH 偶然命中其他安装。准备完成与实际运行、定位通过分别记录。只用本次样本验证首次启动、应用运行中以及同一文件已经打开三种状态，记录工具自身显示的页码或时间，不用启动进程成功代替定位成功。

Computer Use 应先读取当前安装版本的 Windows Computer Use 技能及其 guidance、api 和 confirmations 文档，通过实际可用的 `mcp__node_repl__js` 初始化 `@oai/sky`。调用 `sky.list_apps()` 实际枚举应用，选择唯一目标窗口，再调用 `sky.get_window_state` 获取最新状态。浏览器入口没有原生应用能力不代表 Windows 插件不可用。每次操作依据最新窗口状态，错误后按技能执行有界恢复，不复用失效控件索引。

## 场景记录

| ID | 场景 | 预期结果 | 必需证据 |
| --- | --- | --- | --- |
| CU-01 | 最终包首次启动及引导 | 使用记录的最终包、独立 profile 和专用模型目录，显示引导，可理解本地索引和模型状态，不连接或修改正式实例 | 包及四程序哈希、实际启动路径、profile、引导窗口和点击步骤 |
| CU-02 | 普通与代码目录、排除规则和重启保存 | 两空间的根目录及排除规则独立生效，切换代码搜索时目录筛选随之变化。关闭再开启代码索引保留代码目录，保存及重启不扩大读取范围 | 两套配置、空间切换及过滤结果、排除样本核对、重启后的设置 |
| CU-03 | 模型导入或下载、进度、取消和重试 | 显示真实进度及校验结果，错误可恢复 | 模型页面、错误与恢复记录 |
| CU-04 | 中文输入法、中英文、空白、纯标点及空范围查询 | 组合输入期间不提交半成品，提交后展示当前查询。清空或只输入空白进入文件浏览，纯标点查询不能误变成最近文件列表，空范围不保留上次结果冒充命中 | 组合与提交后的界面、各类查询输入和实际结果、模型及后端状态 |
| CU-05 | 三页浏览、紧凑列表、筛选与键盘操作 | 主窗口按默认修改时间顺序加载 100、100、至少 5 项，无重复或漏项。追加不改变选中项、列表滚动和右侧已加载内容，响应不会抢走当前焦点。主窗口与快速窗口均只有两行元信息，快速窗口最多 12 项且无分页。类型及目录过滤、方向键、Enter 对应当前项，快速窗口转完整搜索保留其查询范围并清除主窗口旧过滤，长路径和窄窗仍可使用 | 样本清单及排序对照、三页数量和边界项、追加前后选择/滚动/焦点、主与快速列表、长路径窄窗截图 |
| CU-06 | 正文续读、图片缩放、PDF 与外部 SumatraPDF | 自动选中首项后只在右侧显示内容。长文档可继续读取，切换文件取消旧读取且不显示迟到正文。图片默认适配，可放大、缩小、按预览像素显示 100% 或恢复适配，超出区域后可滚动。普通刷新与窗口调整保留同图的手动缩放，切换图片恢复适配，离开图片释放旧资源。内置 PDF 定位命中页，配置的 SumatraPDF 在首次启动、运行中、同文件已开三种状态均实际验证目标页 | 选择与续读前后、图片适配/缩放/滚动及刷新前后状态、切图重置与快速切换后的预览、内置与外部 PDF 实际页码、三种外部应用状态及参数配置 |
| CU-07 | 音视频命中、播放切换与外部 mpv | 内置播放使用命中时间，切换结果停止旧播放，状态/语言刷新不重置正在操作的播放控件。配置的 mpv 在首次启动、运行中、同文件已开三种状态均实际验证命中时刻，不把进程启动当作定位完成 | 播放前后时间、状态/语言刷新、旧播放停止、mpv 三种状态的实际时间位置和设置 |
| CU-08 | 代码搜索与编辑器定位 | 代码开关有效，应用内显示正确行号和 Notebook 单元格，配置的编辑器定位源码行 | 应用内行号/单元格、实际编辑器源码行位置 |
| CU-09 | 暂停恢复、长文件期间的新文件、浏览页失效 | 长文件和批量任务运行时加入短文件，短文件能够在全部长任务结束前进入搜索，旧文件续读也有进展。修改/移除样本或调整目录资格后，已加载浏览页保留选择、滚动及预览并提示刷新，不继续追加旧游标。手动刷新后显示新目录状态，搜索无旧版本返回 | 新短文件创建至可查的时间线、长文件前后进度、暂停/恢复、失效提示前后同一页面、刷新后的排序和内容 |
| CU-10 | 托盘、全局快捷键、设置、故障退出和重启 | 主窗口和快速浮层可恢复，同一 profile 再次启动激活原窗口且不停止服务，不同 profile 可并行，快捷键状态真实，无关设置保存不丢选择或预览，配置重启后保留。对本次 worker/service 做受控故障注入，失败和断连明确可见，按实际恢复路径可继续使用，旧响应不被发布。退出和收尾不残留本次子进程、锁或文件句柄 | 托盘/浮层及语言更新、故障前后 PID 与状态、错误提示、恢复操作、退出后进程和句柄清理、重启结果 |

实际报告逐项记录“通过、失败、未运行或平台能力不可用”，附预期、操作、实际结果和必要截图路径。失败后修复并重新运行相关场景，保留修复前后的证据。不要把未运行项改成通过。

表中复合场景的子步骤写入原有场景的 `actions` 和操作日志，不新增 CU 门禁 ID。CU-05 的三页浏览应先在没有查询、没有额外类型筛选的状态完成，再改变过滤条件。记录追加请求发出后用户实际停留的焦点，核对响应抵达没有额外抢焦点，不能把用户点击“加载更多”造成的正常按钮焦点变化判为错误。窄窗使用应用允许的最小宽度附近，记录实际窗口大小和显示缩放，检查长文件名、目录提示、按钮及预览是否仍可辨认与操作。

CU-09 先在已有至少两页结果并选中某项时制造文件或资格变化。看到过期提示后先核对旧列表和预览仍保留，再显式刷新，不能只验证刷新后的列表而漏掉失效期间的行为。仅追加长文件片段或无变化扫描不应误让文件页过期。公平性记录同时包含新短文件和已有长文件的进展，不能仅用总体进度增加证明没有饥饿，也不把批次 4:1:1 比例解释成固定毫秒响应保证。

CU-06 的图片步骤使用 `图片缩放` 目录中的 1600×900 横图和 900×1600 竖图，调整窗口使当前预览区域小于图像尺寸，先观察默认适配，再依次操作放大、缩小、100% 和适配。放大后实际移动水平与垂直滚动条或拖动图片，记录可见区域变化。保持手动缩放时调整窗口并触发同一选中图片的普通刷新，核对缩放比例与刷新前后的阅读位置，左侧列表始终只有元信息。随后切换 160×120 小图，确认恢复适配且没有旧滚动范围，再切换正文或 PDF，确认旧图片不再显示。100% 按当前预览像素显示，不作为源图片完整分辨率的证明。预览清空时缩放按钮应禁用。

CU-06 和 CU-07 的每种外部应用状态均使用实际返回结果中记录的非首页或非零时间字段，实际观察外部窗口后填写结果。应用运行中可用另一个文件保持该程序活动，同文件已开则先停留在不同页码或时刻，再从搜索结果重新发起定位。设置为系统默认时只验证普通打开和明确的定位限制提示。指定工具缺失或启动失败必须显示错误，不能静默换成默认应用后记为定位成功。

CU-10 只对本次独立 profile 的进程注入故障，先核对 PID、可执行路径和所属实例。worker 失败、service 断连与正常退出分别记录，不假定服务断连后一定自动重连，允许按界面提示重新启动并验证恢复。最终关闭内置预览和本次外部查看器/播放器，再检查本次进程退出、源样本句柄释放及 profile 锁可重新获取，避免把外部播放器仍持有文件误判为应用清理通过。

空结果界面使用明确为空的目录、类型或关键词条件验证。语义查询可能返回低相似度结果，不能要求无答案自然语言一定返回空列表。模型不可用、后端回退及搜索失败按实际状态记录。

所有场景使用同一冻结 CI CPU ZIP 及其独立动态 profile，不能拼接不同候选或不同包字节的截图作为一次通过。工具无法操作、用户协助和工具随后核验的部分分别写入操作记录。必需场景没有实际完成时，本轮验收保持未通过。QtTest 自动化和 Computer Use 各自提供证据，并与本地完整自动化、11 变体原生 Actions 一起判定交付。

外部 Notebook 单元格定位只在明确支持该能力的编辑器配置下单独验收。编辑器命令中的 `{cell}` 只是参数替换，通用的文件与行号打开方式不保证跳到 Notebook 单元格，不能用应用内定位截图代替外部编辑器证据。

## 报告字段

机器可读原始报告保存在本次运行根，例如 `windows-computer-use-<UTC>.json`。以下字段与 `tools/qcs-release-evidence.py` 的 `verify_manifest`、`validate_cu` 和 `validate-local` 契约一致：

| 范围 | 必需内容 |
| --- | --- |
| 报告身份 | `schemaVersion: 1`、`kind: "windows-computer-use"`、完整 `identity`，以及与 `identity.checkoutCommit` 相同的 `commit` |
| 候选绑定 | `packageSha256`、四个程序的 `executables`、`modelFingerprint`、`extractionFingerprint`，均与本次 `package-candidate.json` 对应字段一致 |
| 运行信息 | `profileRoot`、`platform`、UTC `startedAt` / `finishedAt`、`passed`、`scenarios`、顶层内嵌的 `files` 数组 |
| 每个场景 | `id`、`status`、非空 `expected` / `actual`、非空 `actions` 数组、`packageSha256`、`executables`、UTC `startedAt` / `finishedAt`、非空 `screenshots` 数组、`operationLog` |
| 每个附件 | `files` 中的 `{path, bytes, sha256}`，对应实际文件的相对路径、字节数和 SHA-256 |

从 `record-ci-package` 产生的 `package-candidate.json` 原样保留 `identity`，其中包括 `candidateHead`、`checkoutCommit`、`sourceTree`、`workingTreeModified: false` 和完整 `lockHashes`，并保留其 CI 来源记录。同样原样保留四个程序的 `executables` 对象及每个对象的字段，不能仅摘取程序名、补造本地构建历史或事后用当前 HEAD 替换来源。每个场景的包哈希和 `executables` 必须与报告顶层一致。本地 `record-build` / `record-package` 记录可用于开发验证，不能仅因 commit 相同就作为本流程的 CI 来源。

`CU-01` 至 `CU-10` 各出现一次。每个场景的开始时间必须早于结束时间，且完整落在本次报告的执行区间内。只有十个场景实际通过且证据完整时才能写 `passed: true`。失败、未运行和能力不可用分别保留 `failed`、`not_run`、`unsupported`，不能改写为 `passed`。

所有 `screenshots`、`operationLog` 和 `files[].path` 均以原始报告所在目录为根，使用不含 `..` 的相对 POSIX 路径。每张实际 PNG/JPEG 截图和每份非空操作日志必须在顶层 `files` 中登记，记录内容哈希。不要将报告自身放入其 `files`，以免产生自引用哈希。附加的 `-files.json` 可以用于归档，但不能代替原始报告内嵌的清单。

执行 [发布指南](releasing.md#本机最终证据流程) 中的 `collect-local` 时，`--computer-use` 传入这份自包含的原始 JSON，不传入附加清单。导入器按该 JSON 所在目录查找并校验附件，再复制到新的证据根。最后运行 `validate-local --release-manifest <同一冻结发行清单>`，交叉核对 CI 来源、实际 ZIP、四个程序、候选与 CU 附件，要求结果的 `scope` 为 `final-release-package`、`passed` 为 true，且 `localGates.finalReleasePackage` 通过。这一步验证已有来源与实际操作的绑定，不重新运行 CI。省略 `--release-manifest` 时 `scope` 为 `local-package`，不满足正式发行包验收。

报告发布前将个人目录和无关桌面内容脱敏。清理时只停止本次创建的应用与服务，只删除经过边界验证的验收根。源文件删除、安装或其他需当场确认的 UI 行为按 Computer Use 技能要求执行，测试范围本身不扩大插件权限。

本文是验收方法与记录模板，实际执行结论由 [实施状态](implementation-status.md) 引用的报告给出。
