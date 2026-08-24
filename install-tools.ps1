$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$target = Join-Path $root '.tools\python'
$requirements = Join-Path $root 'requirements.txt'

if (-not (Test-Path -LiteralPath $python)) {
  throw "未找到所需的 Python 运行时：$python"
}
if (-not (Test-Path -LiteralPath $requirements)) {
  throw "未找到 Python requirements 文件：$requirements"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-Host '正在安装 Python 视频/语音依赖，需要网络连接。'
Write-Host '本脚本不安装 Node.js、tesseract.js、OCR 语言数据或 Whisper 模型。'
Write-Host 'requirements 文件：'
Write-Host $requirements
Write-Host '安装目录：'
Write-Host $target
Write-Host ''

& $python -m pip install --target $target --requirement $requirements
if ($LASTEXITCODE -ne 0) {
  throw "Python 依赖安装失败，pip 退出代码：$LASTEXITCODE"
}

Write-Host ''
Write-Host 'Python 视频/语音依赖安装完成，可以重新启动 Sing Reactor。'
Write-Host 'Node.js、tesseract.js、OCR 语言数据和 Whisper 模型仍需另行准备。'
Read-Host '按回车关闭'
