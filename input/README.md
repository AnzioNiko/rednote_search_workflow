# 输入配置

`*.json` 是 WebUI 和命令行实际读写的本机配置，已加入 `.gitignore`。仓库中只保留 `*.example.json` 示例文件。

首次使用前，在仓库根目录将需要的示例复制为不带 `.example` 的文件名，例如：

```powershell
Copy-Item input\keywords.example.json input\keywords.json
Copy-Item input\xiaohongshu_users.example.json input\xiaohongshu_users.json
Copy-Item input\filters.example.json input\filters.json
Copy-Item input\app_settings.example.json input\app_settings.json
Copy-Item input\llm_settings.example.json input\llm_settings.json
Copy-Item input\workflows.example.json input\workflows.json
```

配置中可以保存关键词、监控账号、定时计划和模型端点等本机信息。提交代码前不要强制加入实际配置。
