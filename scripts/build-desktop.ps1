param([switch]$BundleEngine, [switch]$IncludeResources, [switch]$CreateShortcut, [switch]$FreshUserData)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskBuild = Join-Path $taskRoot 'build'
$taskDestination = Join-Path $taskBuild 'Sol'
$taskPrevious = Join-Path $taskBuild 'Sol.previous'
$taskOutput = Join-Path $taskBuild ('Sol-staging-' + [Guid]::NewGuid().ToString('N'))
function Assert-SolBuildPath([string]$taskPath) {
    $taskResolved = [IO.Path]::GetFullPath($taskPath)
    if (-not $taskResolved.StartsWith($taskBuild + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '构建路径超出本项目 build 目录' }
}
foreach ($taskPath in @($taskDestination, $taskPrevious, $taskOutput)) { Assert-SolBuildPath $taskPath }
foreach ($taskProcess in @(Get-Process -Name Sol -ErrorAction SilentlyContinue)) {
    if ($taskProcess.Path -eq (Join-Path $taskDestination 'Sol.exe')) { throw '请先从托盘退出本项目 Sol，再重新构建。' }
}
$taskDotnet = Join-Path $env:ProgramFiles 'dotnet\dotnet.exe'
if (-not (Test-Path -LiteralPath $taskDotnet)) {
    $taskCommand = Get-Command dotnet -ErrorAction SilentlyContinue
    if (-not $taskCommand) { throw '缺少 .NET 10 SDK。源码构建需要 SDK，发布后的用户无需安装 SDK。' }
    $taskDotnet = $taskCommand.Source
}
& $taskDotnet publish (Join-Path $taskRoot 'desktop\Sol.Desktop\Sol.Desktop.csproj') -c Release -r win-x64 --self-contained true -p:DebugType=None -p:DebugSymbols=false -o $taskOutput --nologo
if ($LASTEXITCODE -ne 0) { throw 'Sol 原生桌面构建失败。' }
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if ($IncludeResources -and -not $BundleEngine) { throw '包含资源需要同时指定 -BundleEngine。' }
if ($BundleEngine) {
    $taskArguments = @((Join-Path $PSScriptRoot 'bundle_engine.py'), $taskOutput)
    if ($IncludeResources) { $taskArguments += '--resources' }
    & $taskPython @taskArguments
    if ($LASTEXITCODE -ne 0) { throw '私有引擎打包失败；没有启动应用或替换桌面入口。' }
    $taskOptions = @{engineRoot='engine';python='python\python.exe'}
} else {
    $taskOptions = @{engineRoot=$taskRoot;python=$taskPython}
}
if (-not $FreshUserData) { $taskOptions.dataRoot = Join-Path $taskRoot 'state' }
$taskUtf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $taskOutput 'sol.desktop.json'), ($taskOptions | ConvertTo-Json), $taskUtf8)
if (Test-Path -LiteralPath $taskPrevious) { Remove-Item -LiteralPath $taskPrevious -Recurse -Force }
if (Test-Path -LiteralPath $taskDestination) { Move-Item -LiteralPath $taskDestination -Destination $taskPrevious }
try { Move-Item -LiteralPath $taskOutput -Destination $taskDestination }
catch {
    if ((Test-Path -LiteralPath $taskPrevious) -and -not (Test-Path -LiteralPath $taskDestination)) { Move-Item -LiteralPath $taskPrevious -Destination $taskDestination }
    throw
}
$taskOutput = $taskDestination
if (Test-Path -LiteralPath $taskPrevious) { Remove-Item -LiteralPath $taskPrevious -Recurse -Force }
if ($CreateShortcut) {
    $taskShell = New-Object -ComObject WScript.Shell
    $taskLink = $taskShell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sol.lnk'))
    $taskLink.TargetPath = Join-Path $taskOutput 'Sol.exe'
    $taskLink.WorkingDirectory = $taskOutput
    $taskLink.IconLocation = (Join-Path $taskOutput 'branding\sol-app.ico') + ',0'
    $taskLink.Description = 'Sol · QQ、模型、记忆与任务'
    $taskLink.Save()
    # Remove only the obsolete shortcut pointing at this project's old launcher.
    $taskLegacy = Join-Path ([Environment]::GetFolderPath('Desktop')) '同伴.lnk'
    if (Test-Path -LiteralPath $taskLegacy) {
        $taskOld = $taskShell.CreateShortcut($taskLegacy)
        if ($taskOld.TargetPath -eq (Join-Path $taskRoot 'Companion.exe')) { Remove-Item -LiteralPath $taskLegacy }
    }
}
Write-Host ('Sol 已构建：' + (Join-Path $taskOutput 'Sol.exe'))
Write-Host '仅构建与打包，没有启动界面、QQ 或模型。'
