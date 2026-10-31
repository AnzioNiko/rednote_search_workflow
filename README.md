# 社媒热度分析工作流

这是一个从 OpenCLI 开始搭建的小红书监控工作流。当前提供配置文件驱动的采集 CLI，以及仅供本机访问的 WebUI，用于维护监控对象、采集参数、OpenCLI 环境、手动分步运行和定时工作流。媒体多模态分析已有可选接入；完整趋势报告和 MCP 工具暂未接入。

## 输入文件

首次运行前，将 `input` 目录中的示例配置复制为实际配置：

```powershell
Copy-Item input\keywords.example.json input\keywords.json
Copy-Item input\xiaohongshu_users.example.json input\xiaohongshu_users.json
Copy-Item input\filters.example.json input\filters.json
Copy-Item input\app_settings.example.json input\app_settings.json
Copy-Item input\llm_settings.example.json input\llm_settings.json
Copy-Item input\workflows.example.json input\workflows.json
```

- `input/keywords.json`：关键词列表，例如 `{"keywords": ["咖啡店", "手冲咖啡"]}`。
- `input/xiaohongshu_users.json`：监控账号列表，可填主页链接或对象，例如 `{"users": [{"name": "账号名称", "profile_url": "https://www.xiaohongshu.com/user/profile/用户ID"}]}`。
- `input/filters.json`：采集设置和笔记指标筛选，包括关键词采集条数、账号作品数与详情上限、搜索排序和范围，以及点赞/收藏/评论筛选条件；WebUI 会读写此文件。
- `input/app_settings.json`：本机 OpenCLI 启动文件和浏览器 Profile 配置；路径留空时使用当前 `PATH` 和 OpenCLI 默认 Profile。
- `input/llm_settings.json`：多模态模型端点、模型、凭据标识及媒体处理限制；不包含密钥值。

`keywords` 和 `users` 都为空时，工作流会要求先补充输入，不会生成空采集结果。
实际配置、定时工作流状态、去重记录和 `output` 运行结果均已加入 `.gitignore`；公开仓库只保留不含真实值的示例文件。

## 前置条件

1. 已安装 Python 3.10 或更新版本、Node.js 和 OpenCLI 1.8.8 或更新版本；也可在 WebUI“设置 → 项目依赖”中更新 Python 依赖及通过 npm/pnpm 安装的 OpenCLI。
2. Chrome 中 OpenCLI 扩展已连接，并已登录小红书；`opencli doctor` 需通过。
3. 工作流默认遵循当前 `PATH` 中 `opencli` 的解析顺序。Windows 上请确保 pnpm/npm 的 `opencli.CMD` 排在 WindowsApps 的旧 App shim 前面；也可通过 `OPENCLI_BIN` 明确指定 pnpm/npm 启动脚本。程序会直接调用对应 Node 入口，避免把关键词交给 Shell 解释。

## 本地 WebUI

在仓库根目录运行：

```powershell
python webui/server.py
```

服务默认只绑定 `127.0.0.1`，并尝试打开 `http://127.0.0.1:8765/`。Windows 用户可在仓库根目录双击 `start_webui.bat`，或在终端运行 `start_webui.bat`；需要改端口时可运行 `start_webui.bat --port 8766`。服务停止后按任意键关闭启动窗口。也可直接运行 `python webui/server.py --port 8766`；按 `Ctrl+C` 停止服务。

WebUI 分为“开始运行”“工作流”“查看结果”“监控对象”和“设置”。“开始运行”可单独执行脚本提取、笔记筛选、CSV 整理或大模型分析。笔记筛选位于脚本提取和 CSV 整理之间，按点赞、收藏、评论的条件筛选采集 JSON 并生成新的 `xhs_run_*.json`，原始采集文件不会被覆盖；同一条件组内按“且”判断，不同条件组之间按“或”判断。例如可以设置 `(点赞 > 1000 且 评论 > 50) 或 (点赞 > 10000)`。脚本提取可开启“跨采集跳过已爬取笔记”：程序优先用 24 位小红书笔记 ID、无法取得 ID 时用规范化链接的 128 位哈希识别笔记，在读取详情和指标筛选前跳过历史重复项；未通过指标筛选的笔记同样会写入 `input/seen_notes.txt`。记录文件只保存排序后的紧凑键，不保存标题、正文或完整链接。大模型分析会选择一个历史 CSV，只处理“媒体解析状态”不为 `success` 的行，并把分析结果直接写回该 CSV 和对应采集 JSON。“工作流”支持将脚本提取、笔记筛选、CSV 整理和大模型分析按顺序组合；筛选步骤可以选择本工作流前一步产生的 JSON 或历史 JSON，并保存自己的筛选条件。笔记筛选和 CSV 整理均可选择只使用本工作流最近一步的 JSON，或合并此前全部采集与筛选结果后再执行当前任务；合并整理生成 CSV 时会按笔记去重。工作流可设为每日或每周按本机时间运行，并可手动立即运行；配置保存在 `input/workflows.json`。定时执行由 WebUI 服务提供，因此服务关闭期间不会触发，到期计划会在服务重新启动后补跑。当前任务状态和日志会显示在启动任务的“开始运行”或“工作流”页面；“查看结果”可选择并直接预览 `output` 中历史生成的 CSV，完整文件也可下载。“设置”集中管理 OpenCLI、浏览器 Profile、依赖更新、媒体处理和模型凭据；依赖更新后请重启 WebUI。运行期间不能重复启动其它任务；当前不提供运行中止功能。

该页面为本机单用户工具，不提供远程访问、用户认证或 MCP 接口。不要通过端口转发或反向代理公开服务。

## 图片与视频分析

WebUI“设置”中的媒体与模型区域可配置 OpenAI 兼容的多模态 Chat Completions 端点、模型、媒体下载/抽帧上限以及可选音频转写模型。普通设置修改后会自动保存；填写端点后可点“获取模型”读取兼容 `/models` 接口返回的模型 ID 并从下拉框选择，若供应商不支持该接口，仍可手动填写。分析功能默认关闭；启用后，在“开始运行”选择一份 CSV，程序会分析其中所有“媒体解析状态”不为 `success` 的行，使用 `opencli xiaohongshu download` 下载图片和视频，依赖 FFmpeg 提取缩放后的图片与视频关键帧，再把媒体、笔记上下文发送到所配置的 LLM 端点。可选填写音频转写模型（需兼容 `/audio/transcriptions`）。原始媒体文件保存在 `output/media`，该目录已加入忽略规则。**分析时，媒体内容和笔记上下文会通过 HTTPS 发送给你配置的模型服务商**；请确认服务商与数据处理方式符合你的要求。分析结果会直接写回所选 CSV 和对应采集 JSON，并更新媒体摘要、画面文字、视频关键点、音频转写和解析状态。未配置转写模型时仍会分析视频画面，但不会转写音频。

### API 密钥保护

API 密钥可在 WebUI 输入框中录入，默认隐藏，也可选择显示。浏览器先使用本机服务端临时 RSA 公钥封装随机 AES 密钥，再以 AES-GCM 加密 API 密钥；发往本机 HTTP 服务的请求体只包含密文。服务端私钥只保存在进程内存，解密后直接写入 Windows Credential Manager；不会写入 JSON、日志或仓库文件。配置文件中仅保存凭据标识，运行时才从 Credential Manager 读取。带密钥的远程端点必须使用 HTTPS；带密钥的 HTTP 端点会被拒绝。向 LLM 服务认证时，密钥通过 HTTPS 请求头传输，因此服务商会收到密钥用于验证，但网络传输加密。

也可以在仓库根目录运行下面命令，通过隐藏终端输入设置密钥（不会回显）：

```powershell
py -3 scripts/manage_llm_key.py set social-trend-llm
```

命令中的标识必须与 WebUI 的“密钥凭据标识”一致。检查或删除密钥：

```powershell
py -3 scripts/manage_llm_key.py status social-trend-llm
py -3 scripts/manage_llm_key.py delete social-trend-llm
```

端点、模型、凭据标识及媒体上限保存在 `input/llm_settings.json`，其中不包含密钥。模型列表按钮会请求端点的 OpenAI 兼容 `/models` 接口并只把模型 ID 返回给页面；若端点不支持该接口，可手动填写。此版本的密钥安全存储实现基于 Windows Credential Manager；本机无密钥 HTTP 模型端点可使用回环地址。

## 命令行执行

```powershell
python scripts/run_xhs_workflow.py
```

也可以在运行命令中指定目录或 OpenCLI 路径：

```powershell
python scripts/run_xhs_workflow.py `
  --input-dir input `
  --output-dir output `
  --opencli-bin "$env:APPDATA\npm\opencli.cmd"
```

每次命令行运行在 `output` 下创建一个按 UTC+08:00 本地时间生成的 JSON 文件名，日期与时间以 `_` 分隔，文件名中不写时区，例如 `xhs_run_20261010_111500.123456.json`；默认同时生成对应的 `xhs_summary_20261010_111500.123456.csv`。命令行也支持 `--skip-media-analysis` 和 `--skip-aggregation`，让采集、媒体分析与 CSV 整合分开执行。JSON 保留每条 OpenCLI 命令的参数、原始标准输出、标准错误、退出码和解析后的返回值；CSV 汇总关键词与监控账号的笔记记录，并按笔记 ID 或链接去重。CSV 包含标题、作者、链接、主题/来源、正文、标签和点赞、收藏、评论、分享、浏览、曝光、观看等列；源数据未提供的指标留空，不会推算。一个关键词或账号失败不会抹掉其它已取得的结果。

也可以单独重新整合某份历史结果：

```powershell
python scripts/aggregate_results.py output/xhs_run_20261010_111500.123456.json
```

按 `input/filters.json` 中的指标条件筛选一份历史采集结果：

```powershell
python scripts/filter_results.py output/xhs_run_20261010_111500.123456.json
```

命令会生成新的筛选结果文件，通常为同一 `output` 目录下的 `xhs_run_*.json`；随后可把这个新文件交给 `aggregate_results.py` 整理成 CSV。

## 关键词与账号数据获取

- 每个关键词先执行 `xiaohongshu search`，筛选条件来自 `input/filters.json`。
- 对搜索返回的笔记执行 `xiaohongshu note`，保存公开笔记的标题、作者、正文、标签及点赞/收藏/评论等返回字段。
- 可选执行 `xiaohongshu creator-note-detail`，保存创作者中心的曝光、观看、互动、观看来源、观众画像和趋势数据原始返回值。2026-10-10 登录创作者中心后重试了 10 条关键词搜索得到的第三方笔记，均返回 `EMPTY_RESULT`；该命令应只用于当前登录创作者有权限的作品。默认关闭，可在 filters.json 中显式启用；失败会作为可选指标警告保存。
- 每个监控用户执行 `xiaohongshu user` 获取公开笔记，再对返回笔记执行 `xiaohongshu note`。

## 输出说明

JSON 顶层分为 `keyword_searches`、`user_searches`、`opencli`、`inputs`、`errors` 和 `warnings`。每个 OpenCLI 原始返回值都保留 `raw_stdout` 与 `raw_stderr`，`return_value` 便于后续程序直接读取。采集原文可能包含公开笔记内容及带访问参数的来源链接，请勿把 `output` 上传到公开仓库。
