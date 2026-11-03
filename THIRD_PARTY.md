# Third-party components

- AstrBot 4.28.2: vendored from the installed source snapshot under `vendor/astrbot`; AGPL-3.0-or-later. Its original license is retained. No production configuration, plugins or conversation data are included.
- GPT plan adapter and image workers: reused from the user's local projects as isolated adapters/algorithms. Their prior routing, memory and management plugins are not copied as the application architecture.
- LLBot/PMHQ: retained only as an optional legacy compatibility path. The current installer builds Sol's native QQ loader and does not fetch or require these images. Their authorization and licenses remain with the respective projects; no credentials are included.
- Image and speech model weights: external resources, not included in the source archive; retain their upstream licenses.
- Python dependencies retain their individual licenses.
- Sol私有发行包保留基础PyMuPDF，排除可选pymupdf.layout机器学习扩展及其发行元数据，避免普通PDF操作自动加载额外NumPy/ONNX栈。仅过滤自己的发行包，不修改上游源码或用户的其他Python环境；更新现有私有包时将可选目录移入build程序备份。

Desktop integration uses .NET WPF and Windows Forms NotifyIcon. The former Python preview GUI and its pywebview/pystray dependencies have been removed.
## Sol QQ原型的本地加载器

- 本地函数挂接使用MinHook v1.3.4；保留其BSD许可证及版权说明，完整文本位于`vendor/minhook/LICENSE.txt`，运行组件同目录附`MinHook-LICENSE.txt`。
- `qq-native`中的启动器、NAPI引导和文字收发适配代码为本项目实现。SDK方法/参数事实参考公开的NTQQ接口声明（[NapCatQQ wrapper](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/wrapper.ts)、登录与消息服务声明；此前原型也参考LLOneBot v4.5.0 IPC定义）。未引入、打包或执行NapCat/LLBot运行时，未复制其完整框架实现；API参考快照仅保存在排除交付的build/research目录。
- 官方QQ二进制仅从用户已有安装目录复制为本机独立副本，不包含在Sol源码交付包中；不复制账号资料或旧登录记录。
- 附件接收继续只参考公开SDK的[消息服务声明](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/services/NodeIKernelMsgService.ts)、[下载完成回调字段](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-core/listeners/NodeIKernelMsgListener.ts)及图片/文件元素定义，适配实现仍由Sol自行维护；参考快照在build/research，不纳入运行组件。
- 附件发送参考相同公开SDK的SendPicElement/SendFileElement字段、getRichMediaFilePathForGuild参数及sendMsg/onMsgInfoListUpdate确认契约；另查[OneBot附件字段映射](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/api/file.ts)确认文件名、图片类型枚举与原图缓存位置。Sol自行实现文件库路径校验、流式摘要、发送暂存与回执等待，不引入该项目运行依赖；调研快照仅留build/research，不纳入源码交付。
- 原生引用收发仅参考公开ReplyElement字段、消息序号/时间/客户端序号及[reply参数映射](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/api/msg.ts)。Sol自行实现有界内存引用缓存、同会话校验、历史消息读取的短超时、引用缺失说明及首次气泡引用；引用缓存不持久化为重启后发送目标，不引入该项目运行组件。



- Microsoft.Web.WebView2 SDK 1.0.4314.35：用于C#桌面承载同一完整界面，遵守微软SDK许可；原许可保留在assets/licenses/WebView2-SDK-license.txt并随桌面输出复制。Evergreen浏览器运行时由Windows/Edge提供，不将用户浏览器资料包含在交付中。

- 本批通用MCP使用Model Context Protocol Python SDK 1.x（MIT）和Playwright既有Python依赖；SDK与浏览器由私有环境/资源包提供，不复制个人浏览器账号。Docker工具镜像基于官方Python/Debian镜像，内装软件保留各自许可证与发行包元数据；镜像不纳入Sol源码压缩包。
