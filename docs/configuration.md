# 配置与数据目录

`QCS_HOME` 指定 QCopilots Search 的数据基目录。GUI、CLI、服务和工作进程使用同一套 `RuntimePaths` 规则，在基目录下按配置格式版本隔离数据，数据位置不随安装目录、仓库目录或当前工作目录变化。

## 目录选择

| 内容 | 优先级，从高到低 |
| --- | --- |
| 数据基目录（base） | `--profile-dir` → 非空 `QCS_HOME` → 平台默认目录 |
| 当前数据目录（profile） | `<base>/profiles/v4` |
| 模型目录 | `--model-dir` → `QCOPILOTS_SEARCH_MODEL_DIR` → `<profile>/models` |

| 平台 | 默认数据基目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\qcs`，例如 `C:\Users\xiaob\AppData\Local\qcs` |
| macOS | `~/Library/Application Support/qcs` |
| Linux | `$XDG_DATA_HOME/qcs`，未设置时为 `~/.local/share/qcs` |

`QCS_HOME` 和 `--profile-dir` 都表示基目录，必须使用绝对路径。应用统一追加 `profiles/v4`，例如 `QCS_HOME=D:\QCS` 对应 `D:\QCS\profiles\v4`。不要把已追加版本的最终 profile 路径作为基目录再次传入。空变量等同未设置，相对路径、普通文件或不可用目录会明确报错，不会回退到默认基目录。仅设置 `QCOPILOTS_SEARCH_PROFILE_DIR` 会提示改用 `QCS_HOME`，该变量不参与目录选择。

Windows 当前 PowerShell 会话示例：

```powershell
$env:QCS_HOME = 'D:\QCS'
& "$env:LOCALAPPDATA\Programs\qcs\bin\qcs-gui.exe"
```

可在 Windows 用户环境变量中持久设置 `QCS_HOME`。修改后新启动的进程才会读取新值，已运行实例继续使用原目录。上例中的程序路径应替换为实际安装位置。

macOS / Linux 示例：

```bash
export QCS_HOME="$HOME/qcs-data"
/path/to/qcs-gui
```

开发和测试可以直接选择独立实例：

```powershell
& '<qcs-cli 的绝对路径>' --profile-dir 'D:\qcs-test-profile' --model-dir 'D:\qcs-models' status
```

路径对象分别保存基目录与最终 profile。父进程向服务与 worker 显式传递绝对基目录和模型目录，子进程只追加一次 `profiles/v4`。实例锁、IPC 认证、数据库和临时文件绑定最终 profile。同一规范化 profile 只运行一个 GUI 主实例和一个服务，不同 profile 可以并行。开发脚本与自动测试使用独立基目录，不访问正常用户数据。

## 配置版本与模型

新版只读取 `<base>/profiles/v4/qcs-settings.json`，配置字段 `version` 必须为整数 `4`。旧格式、未来格式、非法 JSON 和已删除的模型选择字段均明确报错，不会覆盖文件或自动改写成新版。配置中不再保存模型选择，应用只支持 EmbeddingGemma 2，输出 768 维向量。锁定文件与能力见[依赖说明](dependencies.md#模型契约)。

首次启动使用默认设置，需要重新选择搜索目录并建立索引。基目录中旧版本的配置、索引、模型和日志保持原样，不自动读取、迁移或删除。软件版本升级但配置格式仍为 v4 时，继续使用同一 profile，不因每次升级重新建立索引。

独立的模型管理对话框提供 Gemma 的下载、离线导入和校验，成功完成后自动请求重新加载模型。设置中没有模型选择页面。默认模型目录为 `<base>/profiles/v4/models`。旧版的 Gemma 文件可以通过离线导入复制到新版模型目录，导入会验证大小和 SHA-256，源文件保持不变。显式传入 `--model-dir` 时可以共享已有模型目录，目录中仍需包含清单要求的两份准确文件。

CLI 使用 `model manifest` 查看清单，使用 `model status`、`model verify`、`model download`、`model import` 和 `model reload` 管理模型。清单命令可离线执行，不创建 profile。以下示例把旧版目录中的模型导入新版，使用自定义基目录时须传入与 GUI 相同的 `--profile-dir`：

```powershell
& '<qcs-cli 的绝对路径>' --profile-dir 'D:\QCS' model import 'D:\QCS\models'
& '<qcs-cli 的绝对路径>' --profile-dir 'D:\QCS' model verify
& '<qcs-cli 的绝对路径>' --profile-dir 'D:\QCS' model reload
```

模型缺失或加载失败时仍可使用文件名及已提取正文的关键词搜索，并显示具体原因。恢复模型后按文件和管线指纹补做必要的语义索引，不因临时缺失而清空已有向量。目录元数据尚未核对或暂时离线时，保留磁盘缓存但不展示未经确认的旧结果。

## 搜索目录与过滤规则

普通文件搜索递归读取所选目录内所有受支持的文件，包括 Git 仓库和嵌套项目中的文本、Word、PDF、图片及音视频。普通文件索引不要求开启代码搜索。普通目录的 `excludes` 优先于 `roots`，被排除的目录及其子目录不会因为再次加入搜索根而被重新纳入。代码搜索继续使用独立的目录、排除条件和 Git ignore 规则。

`qcs-settings.json` 的 `generalFileRules` 显式保存普通文件的自动过滤开关和匹配条件。四个开关默认均为 `false`，只有主动开启后才过滤对应内容：

| 字段 | 含义 |
| --- | --- |
| `excludeHidden` | 排除隐藏文件、隐藏目录和点号开头的名称 |
| `excludeProjects` | 排除匹配项目标记的目录及其整个子目录树 |
| `projectFileNames`、`projectDirectorySuffixes` | 项目标记名称和目录后缀，例如 `.git`、`CMakeLists.txt`、`xcodeproj` |
| `excludeNamedDirectories` | 启用目录名称和后缀过滤 |
| `directoryNames`、`directorySuffixes` | 需要过滤的目录名称和后缀，例如 `node_modules`、`venv`、`app`、`noindex` |
| `excludeMediaDatasets` | 过滤符合数量与机器命名比例条件的文件夹中的图片、音频和视频，普通文档仍保留 |
| `mediaDatasetMinimumFiles` | 同一种媒体的最少文件数，默认 `300` |
| `mediaDatasetMachineNamedPercent` | 其中机器命名文件的最少百分比，默认 `70` |

名称列表按完整名称匹配，区分大小写。后缀不含点，匹配时不区分大小写。列表可以清空，表示不匹配任何名称或后缀。素材数量必须为正整数，比例必须为 `0` 到 `100` 的整数。

机器命名判断识别较长的十六进制名称和带连续数字的名称。常见相机、手机及截图名称前缀不计作机器命名。素材过滤只统计当前文件夹，不把整个目录树合并统计。项目和素材开关开启后，同样作用于直接选中的搜索根，没有截图或搜索根的隐式豁免。

文件中始终写出完整规则，包括关闭开关所对应的匹配条件。有效的 v4 配置缺少可选规则字段时采用默认值，旧格式配置不会通过此机制迁移。未知规则字段、错误类型和无效阈值会明确报错，避免拼写错误被静默忽略。

手工修改 JSON 后，可以重启该 profile 的 GUI 和服务，也可以在线执行以下命令重新读取配置。使用自定义 profile 时须传入相同的 `--profile-dir`。重载会校验完整配置，失败时保留当前活动设置，但不会替你恢复已编辑的磁盘文件，需要修正文件后再次重载：

```powershell
& '<qcs-cli 的绝对路径>' config reload
```

也可以将下面的内容保存为 UTF-8 文件 `qcs-settings-patch.json`，通过 CLI 在线修改一个开关。服务会保留其余条件，验证并原子保存配置，然后自动重新扫描：

```json
{"generalFileRules":{"excludeHidden":true}}
```

```powershell
& '<qcs-cli 的绝对路径>' config set --file '<qcs-settings-patch.json 的绝对路径>'
```

GUI 保存其他设置时会保留这些规则。符号链接、Windows 目录联接、尚未下载的云占位文件和不可读取的文件不参与正常索引。提取失败只影响对应文件，正常文件继续索引，失败文件仍可按文件名查找。

## 数据布局与备份

```text
<数据基目录>/
  qcs-settings.json           旧版配置，保持原样
  index/                     旧版索引，保持原样
  models/                    旧版模型，保持原样
  profiles/v4/
    qcs-settings.json         版本 4 的目录规则与应用设置
    qcs-service.lock          新版服务实例锁
    qcs-ipc-token             新版服务的 IPC 认证信息
    index/                   Gemma 的 SQLite、关键词/向量索引与进度
    models/                  Gemma 的主模型和投影文件
    cache/                   可重新生成的缓存
    logs/                    日志
    tmp/                     临时文件
```

更换 `QCS_HOME` 只选择另一基目录中的 `profiles/v4`，不自动搬迁、合并或删除原目录，也不会改变已经保存的搜索范围。应用只索引设置中选定的目录，数据基目录本身不是默认搜索目录。不同安装目录中的旧版与新版便携程序可以分别手动运行，各自的服务、锁和缓存保持隔离。

备份前退出该 profile 的 GUI、服务和 worker，避免复制正在写入的数据库。备份新版时保留 `profiles/v4` 中的设置和完整 `index` 目录，需要时同时备份模型。使用独立模型目录时另行保存。锁、IPC token 和临时文件不作为可恢复业务数据。恢复到相同配置格式的 profile 后，应用会检查来源文件与管线指纹，必要时重建对应索引。

## 登录自动启动

安装器与登录启动保持每用户一个产品入口。新版首次启动不继承旧版的登录启动设置。只有在设置页明确启用新版登录启动时，才接管产品入口并保存当前基目录与模型目录的绝对路径。普通启动按当前环境解析目录，系统登录启动则恢复启用时保存的目录，不依赖登录进程是否继承 `QCS_HOME`。

新版定位文件固定为 `<平台默认基目录>/profiles/v4/qcs-login-startup.json`，与 `QCS_HOME` 无关。定位文件自身使用 schema 2，保存 `profileBaseDir` 和 `modelDir`，恢复时从基目录解析最终 profile。使用自定义基目录时，默认位置仅用于定位文件、短期操作锁和更新事务标记，不创建模型、索引等业务数据。旧定位文件保持原样，新版不会回读它。

| 平台 | 系统机制 |
| --- | --- |
| Windows | 当前用户 Run 项，程序参数为内部选项 `--login-startup` |
| Linux | XDG autostart desktop entry，使用相同启动参数和应用图标 |
| macOS | `SMAppService.mainApp`，在系统确认登录启动后恢复目录 |

Windows Run 命令受到 260 字符限制，程序安装路径过长时会要求选择较短安装目录。数据目录保存在定位文件中，不拼入启动命令。

更换 `QCS_HOME` 不会自动更改登录目标。设置页会显示现有目标，选择“使用当前目录登录启动”可明确切换。其他 profile 的旧窗口不能关闭后来由新 profile 接管的目标。保存无关设置不会修改启动项。

注册失败时恢复原目标。macOS 等待系统批准时保留目标并显示待授权状态。定位文件损坏、目标缺失或不可用时明确报错，不回退到默认 profile。修复目录后正常启动应用，重新应用登录设置即可。若上次更新因崩溃或回滚失败未完成，程序会拒绝使用未确认目标。手动以目标 `QCS_HOME` 启动，在设置页选择“使用当前目录登录启动”并保存可明确修复，不需要手动删除定位文件。

若旧登录启动入口调用了新版程序，但新版定位文件尚不存在，程序会提示重新设置登录启动并退出，不读取旧配置或自动选择默认目录。便携版的多版本手动运行不代表两套独立安装注册或登录入口。

显式传入 `--profile-dir`、`--model-dir` 的开发会话和 Qt test mode 不访问真实登录配置。`--background` 只控制窗口显示，不读取定位文件。登录启动若没有可用托盘，会显示主窗口，避免产生无法打开的隐藏应用。

## 其他运行设置

- `QCOPILOTS_SEARCH_BACKEND=auto` 默认使用当前包后端支持的全部兼容 GPU。单卡失败时保留健康 GPU，全部 GPU 失败后报告原因并建立一个 CPU 后备 worker。`cpu` 强制 CPU，`gpu` 要求真实 GPU 初始化和自检成功，不允许 CPU 回退。
- `QCOPILOTS_SEARCH_CPU_BASELINE=1` 强制使用通用 x64 CPU 后端，便于兼容性排查。
- `QCOPILOTS_SEARCH_THREADS` 设置默认推理线程数，实例设置可覆盖它。
- 搜索目录、排除目录、代码空间、外部查看器和语言在设置页管理。删除索引配置不会删除源文件。

模型缺失时仍可使用文件名及已提取正文的关键词搜索。模型下载或离线导入均验证锁定大小和 SHA-256，详细契约见[依赖说明](dependencies.md)。

## 多 GPU 索引

每张兼容 GPU 运行一个独立索引 worker，并加载完整文本模型和多模态 projector。多个文件动态分配给空闲设备，较快的卡可以处理更多文件。单个文件的当前批次仍由一张卡处理，只有一个大文件时不保证多卡加速。每张卡都需要容纳完整模型及计算缓冲区，不能把两张卡的显存相加当作一个模型的可用显存。

ROCm 包使用 HIP 支持的 AMD GPU，CUDA 包使用对应 CUDA 版本支持的 NVIDIA GPU。Vulkan 包使用其后端可见的兼容设备。应用不会因电脑有另一厂商的显卡就为当前包临时加载另一个 GPU 后端。

主窗口显示索引 GPU 的可用、忙碌及故障数量，各设备行显示任务批次数、当前文件和错误，提示信息包含设备 ID。设备枚举只说明设备可被选择，模型加载、自检和预热成功后才计为可用。空闲释放后保留历史任务数，服务断开时不继续显示设备忙碌。故障设备在本次会话内停止自动重试，修复驱动或显存问题后重新加载模型或重启服务可重新探测。

索引进度统计当前文件数量，设备行统计累计成功发布的批次数，两者不要求相等。每个索引批次最多处理 40 个片段，例如一个包含 85 个片段的文件需要 3 批，最终只增加 1 个已完成文件。长文件续处理、文件修改后重新索引都会增加批次，未变化文件的定期扫描不会增加批次。计数在设备槽位重新创建时归零，单纯空闲释放模型不会清零，因此也不能用批次总和反推当前文件数量。鼠标悬停在设备状态上可查看统计说明。

通常不需要手动选择设备。诊断或比较单卡性能时，可显式启动服务并重复传入 `--index-device`，每个参数是探测所得的 JSON 对象，字段为 `backend`、`deviceId`、`name`。有 ID 时必须精确匹配 ID，仅无 ID 时允许唯一名称匹配。设备缺失或身份歧义会明确报错，不会改用第一张卡。此选项仅选择索引设备，查询保留独立 worker。

跨平台真实双卡检查入口、隔离目录要求和计时含义见[测试指南](testing.md#真实多-gpu-检查)。
