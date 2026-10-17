# 本地服务协议

`qcs-service` 是运行配置目录的唯一数据库写入者。GUI 与 CLI 链接 `qcs-client`，通过 `ServiceClient` 异步发送请求。调用者使用同一个 `RuntimePaths`，无需依赖工作目录。`--profile-dir` 与 `QCS_HOME` 指向数据基目录，父进程传递 `profileBaseDir`，服务使用其下的 `profiles/v4` 作为最终 `profileDir`，不读取基目录中的旧版配置或索引。

服务先取得配置目录中的 `QLockFile`，再创建仅允许当前用户访问的本地 IPC。每次服务启动生成独立认证 token，保存在配置目录的 `qcs-ipc-token`。Unix token 文件权限为 `0600`，Windows 同时依靠 named pipe 的当前用户访问控制。客户端只在成功连接并验证对端用户后读取 token。

Windows 使用 `qcs-<用户和规范化配置目录摘要>` 管道名。Unix 使用 `/tmp/qcs-<有效UID>/qcs-<同一摘要>` 的短绝对 socket 路径，与 `TMPDIR`、配置目录长度和当前工作目录无关。路径按本机 `sockaddr_un` 的编码字节上限检查。私有 UID 目录以 `0700` 创建，已有目录必须属于当前用户且权限相同，符号链接和宽权限目录会被拒绝。macOS 系统 `/tmp` 指向 `/private/tmp` 的别名只在父目录解析时处理。

macOS 使用 `getpeereid`、Linux 使用 `SO_PEERCRED` 双向验证对端有效 UID，随后仍执行配置 token 认证。服务只有取得对应 profile 的锁后才清理同名旧 socket，且拒绝移除普通文件、符号链接或异用户节点。停止时不删除共享的 UID 父目录，也不触碰其他配置的 socket。

消息由四字节大端长度和 UTF-8 JSON 对象组成，单帧上限为 16 MiB。所有消息带 `protocol: 1`。请求包含字符串 `id`、`token`、`method` 和对象 `params`。成功响应包含 `ok: true` 与 `result`，失败响应包含 `ok: false` 与 `error.code`、`error.message`。进度广播使用 `event` 与 `data`。

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `status` | 空对象 | `progress`、`repository`、`settings`、模型状态、监控能力及失败文件 |
| `settings.get` | 空对象 | `version: 4` 的设置对象，不含模型选择字段 |
| `settings.set` | 要修改的设置字段 | 验证、原子保存后的完整设置 |
| `settings.reload` | 空对象 | 重新读取配置文件并通过统一验证入口应用，不回写文件 |
| `search` | `query`、`kind`、`root`、`code`、`limit`、`semantic` | `hits`、是否执行语义查询及降级原因 |
| `files.list` | `code`、`kind`、`root`、`limit`、可选 `cursor`，每页最多 100 项 | `files`、`nextCursor`、`hasMore`、`browseGeneration`、`metadataPending`、`visibilityEpoch` |
| `document.preview` | `path`、`code`、`start`、`maxSegments`，最多 100 个片段 | `segments`、`text`、`partial`、`next`、`state` |
| `index.start`、`index.reconcile` | 空对象 | 索引进度 |
| `index.pause`、`index.resume` | 空对象 | 持久化暂停状态及索引进度 |
| `index.retry` | 空对象 | 清除已记录失败后重新扫描 |
| `models.status` | 空对象 | Gemma 文件状态、能力、维度、索引目录及加载状态 |
| `models.reload` | 空对象 | 重新校验并加载 Gemma，返回模型状态 |
| `cancel` | `id` 为被取消请求的 ID | 是否找到并取消请求 |
| `shutdown` | 空对象 | 接受退出请求后关闭本配置的服务 |

搜索和文档预览只访问当前目录规则允许的源文件。代码搜索关闭时，服务拒绝代码查询。预览在独立提取进程中执行，同时最多运行两个任务。客户端取消、断线和超时会取消对应工作，已经取消但尚未释放的搜索仍计入排队上限，防止反复取消形成无界队列。

当前包含目录和排除目录在数据库召回时参与过滤，因此暂停索引后修改目录规则也不会让被排除的高排位结果占用结果数量。支持格式的文件提取失败时仍保留真实文件名和类型，用于关键词查找。正文和向量保持为空，已有且来源版本一致的正文前缀不会被文件名占位记录覆盖，失败原因继续通过进度和文件状态报告。

文件结果发布前重新校验平台文件身份、时间戳、设置代际和完整模型、提取指纹。配置变更与 SQLite 提交共享短期发布锁，ANN 重建在该锁之外执行。查询结果携带同一 SQLite 快照中的源版本，返回前与实际文件重新比较，避免新版本元数据使旧片段误判为有效。

完整目录扫描与系统目录监控共同工作。监控覆盖已遍历的目录，包括空目录。超过 8192 个目录或系统监控资源不足时，通过 `watcherWarning` 明确报告，并继续每 30 秒对账。暂停发生在当前提取批次结束处，已提交的长文档片段和续读位置保存在隔离配置目录中。

模型可用时，服务先通过隔离工作进程的 `devices` 操作发现当前安装包支持的 GPU。每张兼容 GPU 分配一个独立索引进程，文本模型和多模态 projector 绑定同一设备。不同文件可并行推理，同一文件的批次保持顺序，结果仍由单个数据库写入队列发布。目录扫描完成后停止派发新批次，等待所有活动批次和数据库写入结束，再合并目录可见性。模型缺失时只启动一个关键词提取进程，不探测或加载 GPU。

`status` 响应和 `runtime` 事件增加 `indexDevices` 数组，设备项包含 `backend`、`deviceId`、`name`、`description`、显存信息、`state`、`activePath`、`completedTasks`、`error` 和 `runtime`。`state` 为 `discovered`、`ready`、`busy`、`idle` 或 `failed`，发现成功不代表初始化、自检或预热已经通过。`completedTasks` 统计成功发布的文件批次数。`indexDeviceCounts` 中的 `discovered`、`ready`、`busy`、`failed` 只统计 GPU，CPU 后备在设备数组中单独显示。

GPU 初始化或推理故障会隔离对应设备，将未完成文件交给健康设备。普通文件解码错误和共享模型文件错误不会隔离设备。`auto` 模式在所有 GPU 不可用时只建立一个 CPU 后备，严格 `gpu` 模式保留待处理队列并报告错误。模型重载或服务重启后重新探测故障设备。查询仍由一个独立进程处理。

默认使用全部兼容 GPU。开发和性能验证可重复传入 `--index-device '{"backend":"HIP","deviceId":"设备 ID","name":"设备名称"}'` 限定参与索引的设备，选择器应来自 `devices` 探测结果。有 ID 时按后端和 ID 匹配，没有 ID 时只接受唯一名称，找不到设备或名称歧义会明确报告。进度日志版本 2 保存多个活动任务，仍可读取版本 1 的失败记录，恢复游标和已提交片段以数据库为准。

单个文件达到 5000 个片段上限时，已有前缀保持可搜索，状态明确报告部分索引。持久化终态阻止周期性对账从头覆盖这些片段，只有文件版本变化、管线变化或显式 `index.retry` 才重新处理。

应用只支持 `embeddinggemma-2`，索引固定为 768 维。SQLite、关键词索引、ANN 和进度位于新版 profile 内，服务不打开其他模型目录。配置、`models.status` 和 `models.reload` 均拒绝模型选择参数。来源元数据核对使变化文件的过期内容失效，复用未变化文件的索引。这个核对在暂停提取时仍会运行。暂时离线目录的磁盘缓存保留，但未经本轮核对的结果不可见。

`settings.set` 与 `settings.reload` 共用验证和应用入口。旧配置格式、未来格式、非法 JSON 或废弃模型选择字段会明确失败，活动设置保持不变。在线重载不会覆盖手工编辑的磁盘文件，需要修正后再次重载。模型管理对话框在成功下载或导入并校验后调用 `models.reload`，加载过程使用隔离 worker，失败原因保持可见。

`status` 的 `modelId` 固定为 `embeddinggemma-2`，`dimensions`、`embeddingFingerprint` 和 `capabilities` 描述 Gemma 索引契约，能力字段为 `text`、`image`、`videoFrames` 和 `audio`。文件完整性、实际加载状态和错误分别报告，不应将文件存在解释为可加载。worker 请求与结果携带固定模型身份、维度和契约指纹，服务在入库和查询前核验。

`models.status.indexState` 区分正在核对的 `reconciling`、当前目录没有文件的 `empty`、暂停且有待处理文件的 `pending`、正在建立的 `indexing`、存在失败文件的 `partial` 及已完成的 `ready`。这些状态使用当前服务维护的目录与任务信息，服务可响应时当前数据库已打开。

模型暂缺或重载失败时，服务保留 Gemma 的索引身份和已有向量，以关键词模式处理新内容。新生成的关键词版本单独标记，恢复模型后只补做这些文件。异步数据库任务持有索引的共享引用，任务代次、文件源版本和管线指纹检查阻止迟到结果覆盖当前数据。

测试入口为 `qcs-tests-client` 和 `qcs-tests-service`。前者使用模拟本地服务验证传输与客户端契约，后者启动实际服务和实际工作进程，验证隔离、恢复、索引、预览及分类切换。故障测试覆盖存储提交失败、配置重载失败、模型暂缺后的关键词降级与恢复。每个用例使用独立 `TestEnvironment`，测试不会连接正常应用的运行配置。
