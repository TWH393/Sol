# Project instructions

- Product source of truth: `docs/机器人重做-已确认需求与总体设计.md`.
- The sole active workspace is D:/SOL. The migrated app was reported usable by the user. The user subsequently authorized retiring unused legacy AstrBot/NapCat/LLBot components, switching Sol autostart to this workspace, and publishing sanitized source to a public GitHub repository. Preserve private backups and unrelated projects, including WeChat and any shared Python still used elsewhere.
- The user authorized implementation, but explicitly deferred tests and interactive acceptance until the full implementation is ready. Do not run unit tests, launch the app for QA, send QQ messages, or make cloud inference calls without later authorization. Reading code and building deliverables are development work, not acceptance.
- Do not change unrelated running services, accounts or credentials. The explicit legacy cleanup above permits stopping the retired QQ bridge, relay, launcher and named legacy QQ containers; never stop the new D:/SOL app or other projects by process name alone.
- Preserve a concrete handoff in `开发接续.md` before stopping. Do not call an untested or partially implemented feature verified.
- Keep framework-specific behavior in `runtime.py` and the small bridge plugin. Do not scatter monkey patches through tools.
- Shared modules own configuration, credentials, memory, task authorization, files, usage and delivery. Tools return structured results; do not create duplicated task-tracking helpers in each tool.
- Source and archives must exclude `state/`, credentials, runtime caches, model weights and production data.
- First version runs one robot. Persist robot IDs so future multi-account work does not require rewriting ownership.
- Do not update the Codex memory folder; no memory update was requested.
- Product name is Sol. Open `Sol.sln`; the primary desktop is `desktop/Sol.Desktop` (C# WPF). `companion/engine.py` is the headless engine. Do not restore the old launcher/Python GUI as the normal entry.
- Read `docs/Sol桌面架构.md` for the native/browser migration boundary. Preserve the same API/data authority; no separate native configuration database.
- Packaging must use the private interpreter recipe, never assume a copied venv is portable. Keep account data and credentials out of distribution payloads.
- The user subsequently authorized narrow COLOR-ONLY visual inspection of every page. Native color-review previews and local browser navigation/theme checks are allowed for that purpose. Do not broaden this into model/QQ/tool tests, account changes or task execution. The earlier ban still applies to functional acceptance.
