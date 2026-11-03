param([switch]$SkipModels)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskVenv = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskVenv 'Scripts\python.exe'
Set-Location -LiteralPath $taskRoot
Write-Host '[1/7] 准备软件专属 Python 环境'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskPyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $taskBundledPython = Join-Path $taskRoot 'toolchain\python\python.exe'
    if (Test-Path -LiteralPath $taskBundledPython) {
        & $taskBundledPython -m venv $taskVenv
    } elseif ($taskPyLauncher) {
        & $taskPyLauncher.Source -3.12 -m venv $taskVenv
    } else {
        throw '请先安装 Python 3.12，然后重新运行安装。'
    }
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
}
Write-Host '[2/7] 安装核心与全部功能依赖'
& $taskPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw '安装 pip 失败。' }
& $taskPython -m pip install -r (Join-Path $taskRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw '锁定依赖安装失败。' }
& $taskPython -m pip install --no-deps --no-build-isolation -e .
if ($LASTEXITCODE -ne 0) { throw '软件依赖安装失败。' }
& $taskPython -m pip install --no-deps --no-build-isolation -e (Join-Path $taskRoot 'vendor\astrbot')
if ($LASTEXITCODE -ne 0) { throw 'AstrBot 核心依赖安装失败。' }
Write-Host '[3/7] 准备浏览器与媒体组件'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $taskRoot 'runtime\browsers'
& $taskPython -m playwright install --no-shell chromium
if ($LASTEXITCODE -ne 0) { throw '网页截图组件安装失败。' }
foreach ($taskPackage in @(@('git','Git.Git'),@('node','OpenJS.NodeJS.LTS'))) {
    if (-not (Get-Command $taskPackage[0] -ErrorAction SilentlyContinue)) {
        $taskWinget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $taskWinget) { throw ('缺少组件 '+$taskPackage[0]+'，请安装后重试。') }
        & $taskWinget.Source install --id $taskPackage[1] -e --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw ('组件安装失败：'+$taskPackage[0]) }
    }
}
Write-Host '[4/7] 准备 Docker 执行环境'
$taskDocker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $taskDocker) {
    $taskWinget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $taskWinget) { throw '需要 Docker Desktop；当前没有 winget，请安装后重新运行。' }
    & $taskWinget.Source install --id Docker.DockerDesktop -e --accept-package-agreements --accept-source-agreements
    throw 'Docker 已交给安装程序处理。如提示重启，请保存工作并手动重启，再运行本安装程序。'
}
& $taskPython (Join-Path $PSScriptRoot 'build_tools.py')
if ($LASTEXITCODE -ne 0) { throw '代码执行镜像构建失败，请启动 Docker Linux 引擎后重新运行。' }
& (Join-Path $PSScriptRoot 'build_qq_native.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Sol 原生QQ组件构建失败；需要Visual Studio C++工具链与CMake。' }
Write-Host '[5/7] 准备本地辅助模型与媒体资源'
if (-not $SkipModels) {
    & $taskPython (Join-Path $PSScriptRoot 'prepare_resources.py')
    if ($LASTEXITCODE -ne 0) { throw '本地资源准备未完成，请查看安装输出。' }
}
Write-Host '[6/7] 构建桌面启动入口'
Write-Host '[7/7] 创建快捷方式'
if ($SkipModels) { & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BundleEngine -CreateShortcut }
else { & (Join-Path $PSScriptRoot 'build-desktop.ps1') -BundleEngine -IncludeResources -CreateShortcut }
if ($SkipModels) { Write-Host '开发依赖已安装；本次跳过模型准备，不代表完整安装完成。' }
else { Write-Host '安装完成。打开“Sol”，填写机器人账号、管理员、模型和接入授权。未进行模型推理或QQ测试。' }
