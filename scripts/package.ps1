$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
& (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'package_source.py')
if ($LASTEXITCODE -ne 0) { throw 'Sol 源码打包失败。' }
