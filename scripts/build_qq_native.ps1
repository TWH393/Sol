$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskOutput = Join-Path $taskRoot 'runtime/qq_native'
cmake -S (Join-Path $taskRoot 'qq-native') -B (Join-Path $taskRoot 'build/qq-native') -G 'Visual Studio 18 2026' -A x64
if ($LASTEXITCODE -ne 0) { throw 'Sol QQ加载器配置失败' }
cmake --build (Join-Path $taskRoot 'build/qq-native') --config Release
if ($LASTEXITCODE -ne 0) { throw 'Sol QQ加载器构建失败' }
New-Item -ItemType Directory -Path $taskOutput -Force | Out-Null
foreach ($taskName in @('sol-qq-launcher.exe','sol-qq-loader.dll')) { Copy-Item -LiteralPath (Join-Path $taskRoot ('build/qq-native/Release/'+$taskName)) -Destination (Join-Path $taskOutput $taskName) -Force }
Copy-Item -LiteralPath (Join-Path $taskRoot 'qq-native/bootstrap.cjs') -Destination (Join-Path $taskOutput 'bootstrap.cjs') -Force
Copy-Item -LiteralPath (Join-Path $taskRoot 'vendor/minhook/LICENSE.txt') -Destination (Join-Path $taskOutput 'MinHook-LICENSE.txt') -Force
node --check (Join-Path $taskOutput 'bootstrap.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Sol QQ接入脚本语法错误' }
Write-Host 'Sol QQ加载组件已构建，未启动QQ或发送消息。'
