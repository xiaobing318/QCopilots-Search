# 本地服务协议

`qcs-service` 是运行配置目录的唯一数据库写入者。GUI 与 CLI 链接 `qcs-client`，通过 `ServiceClient` 异步发送请求。调用者使用同一个 `RuntimePaths`，无需依赖工作目录。

服务先取得配置目录中的 `QLockFile`，再创建仅允许当前用户访问的本地 IPC。每次服务启动生成独立认证 token，保存在配置目录的 `qcs-ipc-token`。Unix token 文件权限为 `0600`，Windows 同时依靠 named pipe 的当前用户访问控制。客户端只在成功连接并验证对端用户后读取 token。

Windows 使用 `qcs-<用户和规范化配置目录摘要>` 管道名。Unix 使用 `/tmp/qcs-<有效UID>/qcs-<同一摘要>` 的短绝对 socket 路径，与 `TMPDIR`、配置目录长度和当前工作目录无关。路径按本机 `sockaddr_un` 的编码字节上限检查。私有 UID 目录以 `0700` 创建，已有目录必须属于当前用户且权限相同，符号链接和宽权限目录会被拒绝。macOS 系统 `/tmp` 指向 `/private/tmp` 的别名只在父目录解析时处理。

macOS 使用 `getpeereid`、Linux 使用 `SO_PEERCRED` 双向验证对端有效 UID，随后仍执行配置 token 认证。服务只有取得对应 profile 的锁后才清理同名旧 socket，且拒绝移除普通文件、符号链接或异用户节点。停止时不删除共享的 UID 父目录，也不触碰其他配置的 socket。

消息由四字节大端长度和 UTF-8 JSON 对象组成，单帧上限为 16 MiB。所有消息带 `protocol: 1`。请求包含字符串 `id`、`token`、`method` 和对象 `params`。成功响应包含 `ok: true` 与 `result`，失败响应包含 `ok: false` 与 `error.code`、`error.message`。进度广播使用 `event` 与 `data`。

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `status` | 空对象 | `progress`、`repository`、`settings`、模型状态、监控能力及失败文件 |
| `settings.get` | 空对象 | 版本化设置对象 |
| `settings.set` | 要修改的设置字段 | 验证、原子保存后的完整设置 |
| `search` | `query`、`kind`、`root`、`code`、`limit`、`semantic` | `hits`、是否执行语义查询及降级原因 |
| `files.list` | `code`、`kind`、`root`、`limit`、可选 `cursor`，每页最多 100 项 | `files`、`nextCursor`、`hasMore`、`browseGeneration`、`metadataPending`、`visibilityEpoch` |
| `document.preview` | `path`、`code`、`start`、`maxSegments`，最多 100 个片段 | `segments`、`text`、`partial`、`next`、`state` |
| `index.start`、`index.reconcile` | 空对象 | 索引进度 |
| `index.pause`、`index.resume` | 空对象 | 持久化暂停状态及索引进度 |
| `index.retry` | 空对象 | 清除已记录失败后重新扫描 |
| `models.reload` | 空对象 | 校验后的模型可用性及管线指纹 |
| `cancel` | `id` 为被取消请求的 ID | 是否找到并取消请求 |
| `shutdown` | 空对象 | 接受退出请求后关闭本配置的服务 |

搜索和文档预览只访问当前目录规则允许的源文件。代码搜索关闭时，服务拒绝代码查询。预览在独立提取进程中执行，同时最多运行两个任务。客户端取消、断线和超时会取消对应工作，已经取消但尚未释放的搜索仍计入排队上限，防止反复取消形成无界队列。

当前包含目录和排除目录在数据库召回时参与过滤，因此暂停索引后修改目录规则也不会让被排除的高排位结果占用结果数量。支持格式的文件提取失败时仍保留真实文件名和类型，用于关键词查找。正文和向量保持为空，已有且来源版本一致的正文前缀不会被文件名占位记录覆盖，失败原因继续通过进度和文件状态报告。

文件结果发布前重新校验平台文件身份、时间戳、设置代际和完整模型、提取指纹。配置变更与 SQLite 提交共享短期发布锁，ANN 重建在该锁之外执行。查询结果携带同一 SQLite 快照中的源版本，返回前与实际文件重新比较，避免新版本元数据使旧片段误判为有效。

完整目录扫描与系统目录监控共同工作。监控覆盖已遍历的目录，包括空目录。超过 8192 个目录或系统监控资源不足时，通过 `watcherWarning` 明确报告，并继续每 30 秒对账。暂停发生在当前提取批次结束处，已提交的长文档片段和续读位置保存在隔离配置目录中。

单个文件达到 5000 个片段上限时，已有前缀保持可搜索，状态明确报告部分索引。持久化终态阻止周期性对账从头覆盖这些片段，只有文件版本变化、管线变化或显式 `index.retry` 才重新处理。模型管线切换先提交数据库事务，再发布内存中的模型空间。切换失败返回 `pipeline_transition_failed` 并停止推理和索引，关键词检索保持可用，`models.reload` 可在故障排除后重试。

测试入口为 `qcs-tests-client` 和 `qcs-tests-service`。前者使用模拟本地服务验证传输与客户端契约，后者启动实际服务和实际工作进程，验证隔离、恢复、索引、预览及分类切换。模型切换故障测试通过 `qcs-service-core` 的 C++ 构造接口注入存储提交失败，并通过真实 IPC 验证错误、降级与恢复。每个用例使用独立 `TestEnvironment`，测试不会连接正常应用的运行配置。
