# Sol 透明应用图标 · 2026-11-03

用户提供 `codex-clipboard-c04c101a-e23a-44dd-8c03-3570df1acf60.png`，明确系统托盘图标不改。

- 原图原样保存为 `sol-app-source.png`（1536×1024 RGBA）。
- `scripts/prepare_app_icon.py` 只裁掉全透明边距、加透明正方形留白并等比编码；不重画、改色或拉伸。
- `sol-app.png` 为正方形透明画布；`sol-app.ico` 包含16/20/24/32/40/48/64/128/256像素。
- EXE图标、WPF窗口/任务栏图标、桌面Sol快捷方式使用独立 `sol-app.ico`。
- 系统托盘继续由App.UpdateTrayTheme使用原sol-main.ico/sol-dark.ico；网页favicon、侧栏Logo和加载动画保持原资源。
- 原生发布成功不代表用户已验收；安装需要完全退出Sol，不能只停内部引擎。不得自动启动QQ或模型来检查图标。
