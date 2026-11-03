param([string]$NativeBuild = 'build/Sol-ui-update', [switch]$EngineOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskBuild = Join-Path $taskRoot 'build'
$taskSource = [IO.Path]::GetFullPath((Join-Path $taskRoot $NativeBuild))
$taskInstalled = Join-Path $taskBuild 'Sol'
if (-not $taskSource.StartsWith($taskBuild + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or $taskSource -eq $taskInstalled) { throw '更新构建必须位于本项目build的独立目录' }
if ((-not $EngineOnly -and -not (Test-Path -LiteralPath (Join-Path $taskSource 'Sol.exe'))) -or -not (Test-Path -LiteralPath (Join-Path $taskInstalled 'engine/companion'))) { throw '缺少原生构建或本项目私有引擎，未修改已安装程序' }
foreach ($taskProcess in @(Get-Process -Name Sol,python,pythonw,sol-qq-launcher -ErrorAction SilentlyContinue)) {
    if ($EngineOnly -and $taskProcess.ProcessName -eq 'Sol') { continue }
    if ($taskProcess.Path -and $taskProcess.Path.StartsWith($taskInstalled + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        if ($EngineOnly) { throw '请在Sol的设置与运行环境中点击“停止内部引擎”，等待停止完成；不用关闭窗口或退出托盘。' }
        throw '请先从托盘退出本项目Sol，等待内部组件退出后再更新；不会结束其他QQ或机器人'
    }
}
$taskRollback = Join-Path $taskBuild ('Sol-update-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$taskSite = Join-Path $taskInstalled 'python/Lib/site-packages'
$taskOptionalPdf = @()
if (-not $EngineOnly) {
    $taskOptionalPdf += (Join-Path $taskSite 'pymupdf/layout')
    $taskOptionalPdf += @(Get-ChildItem -LiteralPath $taskSite -Directory -Filter 'pymupdf_layout-*.dist-info' | ForEach-Object { $_.FullName })
}
$taskDisabledPdf = @()
foreach ($taskOptional in $taskOptionalPdf) {
    if (-not (Test-Path -LiteralPath $taskOptional)) { continue }
    $taskResolved = (Resolve-Path -LiteralPath $taskOptional).Path
    if (-not $taskResolved.StartsWith($taskInstalled + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '可选PDF扩展不在本项目私有解释器内，拒绝移动' }
    $taskAncestor = Get-Item -LiteralPath $taskResolved
    while ($taskAncestor -and $taskAncestor.FullName.StartsWith($taskInstalled, [StringComparison]::OrdinalIgnoreCase)) {
        if ($taskAncestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '可选PDF扩展路径包含链接，拒绝移动' }
        $taskAncestor = $taskAncestor.Parent
    }
    $taskOptionalBackup = [IO.Path]::GetFullPath((Join-Path $taskRollback 'optional-pdf-layout'))
    if (-not $taskOptionalBackup.StartsWith($taskBuild + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '可选PDF扩展备份路径无效' }
    New-Item -ItemType Directory -Path $taskOptionalBackup -Force | Out-Null
    Move-Item -LiteralPath $taskResolved -Destination (Join-Path $taskOptionalBackup ([IO.Path]::GetFileName($taskResolved)))
    $taskDisabledPdf += $taskResolved
}
$taskFiles = [Collections.Generic.List[object]]::new()
if (-not $EngineOnly) {
    foreach ($taskFile in Get-ChildItem -LiteralPath $taskSource -File -Recurse) {
        $taskRelative = [IO.Path]::GetRelativePath($taskSource, $taskFile.FullName)
        if ($taskRelative -eq 'sol.desktop.json') { continue }
        $taskFiles.Add(@{source=$taskFile.FullName;relative=$taskRelative})
    }
}
foreach ($taskFile in Get-ChildItem -LiteralPath (Join-Path $taskRoot 'companion') -File -Recurse) {
    if ($taskFile.FullName -match '[\\/]__pycache__[\\/]') { continue }
    if ($taskFile.Extension -ne '.py' -and $taskFile.FullName -notmatch '[\\/]companion[\\/](web|workers)[\\/]') { continue }
    $taskFiles.Add(@{source=$taskFile.FullName;relative=Join-Path 'engine' ([IO.Path]::GetRelativePath($taskRoot, $taskFile.FullName))})
}
foreach ($taskRelative in @('vendor/plan_bridge/bridge.py','vendor/plan_bridge/launch.py','vendor/image/comfy_backend.py','pyproject.toml','THIRD_PARTY.md')) {
    $taskFiles.Add(@{source=Join-Path $taskRoot $taskRelative;relative=Join-Path 'engine' $taskRelative})
}
$taskHashes = @{}
if (Test-Path -LiteralPath (Join-Path $taskRoot 'runtime/qq_native')) {
    foreach ($taskFile in Get-ChildItem -LiteralPath (Join-Path $taskRoot 'runtime/qq_native') -File) {
        $taskFiles.Add(@{source=$taskFile.FullName;relative=Join-Path 'engine/runtime/qq_native' $taskFile.Name})
    }
}
foreach ($taskFile in $taskFiles) {
    $taskDestination = Join-Path $taskInstalled $taskFile.relative
    if (Test-Path -LiteralPath $taskDestination) {
        $taskPrevious = Join-Path $taskRollback $taskFile.relative
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($taskPrevious)) -Force | Out-Null
        Copy-Item -LiteralPath $taskDestination -Destination $taskPrevious -Force
    }
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($taskDestination)) -Force | Out-Null
    Copy-Item -LiteralPath $taskFile.source -Destination $taskDestination -Force
    $taskSourceHash = (Get-FileHash -LiteralPath $taskFile.source -Algorithm SHA256).Hash
    if ((Get-FileHash -LiteralPath $taskDestination -Algorithm SHA256).Hash -ne $taskSourceHash) { throw ('部署文件校验失败：'+$taskFile.relative+'；上一版备份：'+$taskRollback) }
    $taskHashes[$taskFile.relative] = $taskSourceHash
}
$taskShortcutUpdated = $false
if (-not $EngineOnly -and (Test-Path -LiteralPath (Join-Path $taskInstalled 'branding/sol-app.ico'))) {
    $taskShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sol.lnk'
    if (Test-Path -LiteralPath $taskShortcutPath) {
        $taskShell = New-Object -ComObject WScript.Shell
        $taskLink = $taskShell.CreateShortcut($taskShortcutPath)
        if ($taskLink.TargetPath -eq (Join-Path $taskInstalled 'Sol.exe')) {
            Copy-Item -LiteralPath $taskShortcutPath -Destination (Join-Path $taskRollback 'Sol.lnk') -Force
            $taskLink.IconLocation = (Join-Path $taskInstalled 'branding/sol-app.ico') + ',0'
            $taskLink.Save()
            $taskShortcutUpdated = $true
        }
    }
}
$taskReceipt = @{product='Sol';kind=$(if ($EngineOnly) {'engine-only'} else {'acceptance-fixes'});deployed=(Get-Date -Format o);native=$(if ($EngineOnly) {$null} else {$taskSource});desktop_unchanged=[bool]$EngineOnly;installed=$taskInstalled;previous_files=$taskRollback;disabled_optional_pdf_layout=$taskDisabledPdf;shortcut_updated=$taskShortcutUpdated;configuration_unchanged=$true;functional_tests=$false;app_started=$false;files=$taskHashes}
$taskReceipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskBuild 'Sol-update-receipt.json') -Encoding utf8
Write-Host ('已更新桌面原入口，保留数据与配置：' + $taskInstalled)
Write-Host ('上一版程序文件：' + $taskRollback)
if ($EngineOnly) { Write-Host '桌面窗口可保持打开。请点击“重新连接内部引擎”加载新代码；本脚本不会自动启动或登录。' }
Write-Host '未启动程序、QQ或模型进行测试。'
