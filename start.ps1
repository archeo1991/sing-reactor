param(
  [switch]$NoBrowser,
  [switch]$ProbeOnly,
  [switch]$NoWait,
  [switch]$StopExisting,
  [int]$ApiPort = 18768,
  [int]$WebPort = 4190
)

$ErrorActionPreference = 'Stop'

$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\')
$python = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
$vendorPython = Join-Path $root '.tools\python'
$manifestName = if ($ApiPort -eq 18768 -and $WebPort -eq 4190) { '.sing-reactor.processes.json' } else { ".sing-reactor.processes.$ApiPort-$WebPort.json" }
$manifestPath = Join-Path $root $manifestName
$apiProcess = $null
$webProcess = $null
$ownedThisRun = $false
$manifestCommitted = $false
$mutex = $null
$mutexHeld = $false

function Get-NormalizedPath([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
  try { return [System.IO.Path]::GetFullPath($Path).TrimEnd('\').ToLowerInvariant() } catch { return $null }
}

function Get-ProcessInfo([int]$ProcessId) {
  return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Get-CreationToken($ProcessInfo) {
  if ($null -eq $ProcessInfo -or $null -eq $ProcessInfo.CreationDate) { return $null }
  $value = $ProcessInfo.CreationDate
  if ($value -is [DateTime]) { return $value.ToUniversalTime().Ticks.ToString() }
  try { return [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$value).ToUniversalTime().Ticks.ToString() } catch { return $null }
}

function Get-PortOwner([int]$Port) {
  $connections = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
  if ($connections.Count -eq 0) { $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }
  $owners = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
  if ($owners.Count -eq 1) { return [int]$owners[0] }
  return $null
}

function Get-PortDescription([int]$Port) {
  $ownerPid = Get-PortOwner $Port
  if ($null -eq $ownerPid) { return "端口 $Port（无法确定唯一 PID）" }
  $info = Get-ProcessInfo $ownerPid
  $name = if ($null -ne $info) { $info.Name } else { '未知进程' }
  return "端口 $Port（PID $ownerPid，$name）"
}

function Test-ApiIdentity([int]$Port) {
  try {
    $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    return $response.ok -eq $true -and $response.product -eq 'Sing Reactor' -and $response.service -eq 'api'
  } catch {
    # 兼容增加 /health 之前已运行的旧实例：空 identify 请求是只读操作，并返回稳定产品错误形状。
    try {
      Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/identify?url=" -TimeoutSec 2 | Out-Null
      return $false
    } catch {
      try {
        $payload = $_.ErrorDetails.Message | ConvertFrom-Json
        return $payload.ok -eq $false -and $payload.error -eq 'missing_url'
      } catch { return $false }
    }
  }
}

function Test-WebIdentity([int]$Port) {
  try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/index.html" -UseBasicParsing -TimeoutSec 2
    return $response.StatusCode -eq 200 -and $response.Content -match '<title>\s*Sing Reactor(?:\s|·|<)' -and $response.Content -match 'id="app"'
  } catch { return $false }
}

function Test-Role($Info, [string]$Role, [int]$Port) {
  if ($null -eq $Info -or (Get-NormalizedPath $Info.ExecutablePath) -ne (Get-NormalizedPath $python)) { return $false }
  $command = [string]$Info.CommandLine
  if ($Role -eq 'api') { return $command -match '(?i)(?:^|\s)["'']?(?:[^"'']*[\\/])?server[\\/]server\.py["'']?(?:\s|$)' }
  return $command -match '(?i)\s-m\s+http\.server\s+' + [regex]::Escape([string]$Port) + '(?:\s|$)' -and $command -match '(?i)\s--bind\s+127\.0\.0\.1(?:\s|$)'
}

function Test-ManifestProcess($Entry, [string]$Role, [int]$Port, [switch]$RequireHealthy) {
  if ($null -eq $Entry -or [int]$Entry.pid -le 0 -or $Entry.role -ne $Role -or [int]$Entry.port -ne $Port) { return $false }
  $info = Get-ProcessInfo ([int]$Entry.pid)
  if ($null -eq $info -or (Get-CreationToken $info) -ne [string]$Entry.creationToken) { return $false }
  if (-not (Test-Role $info $Role $Port)) { return $false }
  if ((Get-PortOwner $Port) -ne [int]$Entry.pid) { return $false }
  if ($RequireHealthy) {
    if ($Role -eq 'api') { return Test-ApiIdentity $Port }
    return Test-WebIdentity $Port
  }
  return $true
}

function Read-Manifest {
  if (-not (Test-Path -LiteralPath $manifestPath)) { return $null }
  try { return Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json } catch { return $null }
}

function Write-Manifest($Manifest) {
  $temp = "$manifestPath.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
  $json = $Manifest | ConvertTo-Json -Depth 8
  [System.IO.File]::WriteAllText($temp, $json, (New-Object System.Text.UTF8Encoding($false)))
  try {
    if (Test-Path -LiteralPath $manifestPath) { [System.IO.File]::Replace($temp, $manifestPath, $null) }
    else { [System.IO.File]::Move($temp, $manifestPath) }
  } finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
  }
}

function New-Manifest($ApiInfo, $WebInfo, [bool]$Adopted) {
  return [ordered]@{
    schema = 'sing-reactor-processes'; version = 1; root = $root
    ports = [ordered]@{ api = $ApiPort; web = $WebPort }
    api = [ordered]@{ pid = [int]$ApiInfo.ProcessId; creationToken = Get-CreationToken $ApiInfo; executablePath = [System.IO.Path]::GetFullPath($ApiInfo.ExecutablePath); role = 'api'; expectedCommand = 'server/server.py'; port = $ApiPort }
    web = [ordered]@{ pid = [int]$WebInfo.ProcessId; creationToken = Get-CreationToken $WebInfo; executablePath = [System.IO.Path]::GetFullPath($WebInfo.ExecutablePath); role = 'web'; expectedCommand = "-m http.server $WebPort --bind 127.0.0.1"; port = $WebPort }
    launcher = [ordered]@{ pid = $PID; createdAt = [DateTime]::UtcNow.ToString('o'); adoptedExisting = $Adopted }
  }
}

function Test-ManifestHeader($Manifest) {
  return $null -ne $Manifest -and $Manifest.schema -eq 'sing-reactor-processes' -and [int]$Manifest.version -eq 1 -and (Get-NormalizedPath $Manifest.root) -eq (Get-NormalizedPath $root) -and [int]$Manifest.ports.api -eq $ApiPort -and [int]$Manifest.ports.web -eq $WebPort
}

function Test-ManifestHealthy($Manifest) {
  return (Test-ManifestHeader $Manifest) -and (Test-ManifestProcess $Manifest.api 'api' $ApiPort -RequireHealthy) -and (Test-ManifestProcess $Manifest.web 'web' $WebPort -RequireHealthy)
}

function Stop-SafelyOwnedManifestProcess($Entry, [string]$Role, [int]$Port) {
  if (-not (Test-ManifestProcess $Entry $Role $Port)) { return $false }
  Stop-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
  try { Wait-Process -Id ([int]$Entry.pid) -Timeout 5 -ErrorAction SilentlyContinue } catch {}
  return $true
}

function Remove-ManifestIfSame($Manifest) {
  if (-not (Test-Path -LiteralPath $manifestPath)) { return }
  $current = Read-Manifest
  if ($null -ne $current -and [string]$current.api.creationToken -eq [string]$Manifest.api.creationToken -and [string]$current.web.creationToken -eq [string]$Manifest.web.creationToken) {
    Remove-Item -LiteralPath $manifestPath -Force
  }
}

function Open-App {
  if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$WebPort/index.html" }
}

function Release-LaunchMutex {
  if ($mutexHeld -and $null -ne $mutex) { $mutex.ReleaseMutex(); $script:mutexHeld = $false }
  if ($null -ne $mutex) { $mutex.Dispose(); $script:mutex = $null }
}

try {
  if (-not (Test-Path -LiteralPath $python)) { throw "未找到 Python 运行时：$python" }

  $sha = [System.Security.Cryptography.SHA256]::Create()
  try { $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '') } finally { $sha.Dispose() }
  $mutex = New-Object System.Threading.Mutex($false, "Local\SingReactor-$hash")
  Write-Host '正在等待 Sing Reactor 启动锁...'
  $mutexHeld = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
  if (-not $mutexHeld) { throw '等待另一个启动程序完成超时，请稍后重试。' }

  $manifest = Read-Manifest
  if (Test-ManifestHealthy $manifest) {
    if ($StopExisting) {
      Stop-SafelyOwnedManifestProcess $manifest.web 'web' $WebPort | Out-Null
      Stop-SafelyOwnedManifestProcess $manifest.api 'api' $ApiPort | Out-Null
      Remove-ManifestIfSame $manifest
      Write-Host 'Sing Reactor 已正常停止，实例清单已删除。'
      return
    }
    Write-Host "已有实例正在运行（API PID $($manifest.api.pid)，网页 PID $($manifest.web.pid)）。"
    Open-App
    return
  }

  if ($null -ne $manifest) {
    Write-Host '检测到无效或过期的实例清单，正在仅清理可严格验证的本项目残留...'
    if (Test-ManifestHeader $manifest) {
      Stop-SafelyOwnedManifestProcess $manifest.web 'web' $WebPort | Out-Null
      Stop-SafelyOwnedManifestProcess $manifest.api 'api' $ApiPort | Out-Null
    }
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
  }

  $apiOwner = Get-PortOwner $ApiPort
  $webOwner = Get-PortOwner $WebPort

  if ($null -ne $apiOwner -or $null -ne $webOwner) {
    $apiInfo = if ($null -ne $apiOwner) { Get-ProcessInfo $apiOwner } else { $null }
    $webInfo = if ($null -ne $webOwner) { Get-ProcessInfo $webOwner } else { $null }
    $legacyValid = $null -ne $apiInfo -and $null -ne $webInfo -and (Test-Role $apiInfo 'api' $ApiPort) -and (Test-Role $webInfo 'web' $WebPort) -and (Test-ApiIdentity $ApiPort) -and (Test-WebIdentity $WebPort)
    if ($legacyValid) {
      $manifest = New-Manifest $apiInfo $webInfo $true
      Write-Manifest $manifest
      Write-Host "已有健康的 Sing Reactor legacy 实例正在运行，已安全纳管（API PID $apiOwner，网页 PID $webOwner）。"
      Open-App
      return
    }
    $conflicts = @()
    if ($null -ne $apiOwner) { $conflicts += Get-PortDescription $ApiPort }
    if ($null -ne $webOwner) { $conflicts += Get-PortDescription $WebPort }
    throw "启动端口被占用且无法安全证明属于完整的 Sing Reactor 实例：$($conflicts -join '；')。脚本不会终止这些进程；若它们不是 Sing Reactor，请手工处理后重试。"
  }

  if ($StopExisting) { Write-Host '当前没有正在运行的 Sing Reactor 实例。'; return }
  if ($ProbeOnly) { Write-Host '未发现可复用的 Sing Reactor 实例，两个端口均空闲。'; return }

  if (Test-Path -LiteralPath $vendorPython) { $env:PYTHONPATH = $vendorPython + [System.IO.Path]::PathSeparator + $env:PYTHONPATH }
  $env:SING_REACTOR_ROOT = $root
  $env:SING_REACTOR_API_PORT = [string]$ApiPort

  $apiProcess = Start-Process -FilePath $python -ArgumentList @('server\server.py') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root '.server.out.log') -RedirectStandardError (Join-Path $root '.server.err.log') -PassThru
  $webProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'http.server', "$WebPort", '--bind', '127.0.0.1') -WorkingDirectory (Join-Path $root 'app') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root '.web.out.log') -RedirectStandardError (Join-Path $root '.web.err.log') -PassThru
  $ownedThisRun = $true

  $deadline = (Get-Date).AddSeconds(20)
  do {
    if ($apiProcess.HasExited) { throw "API 进程 $($apiProcess.Id) 在就绪前退出，退出代码：$($apiProcess.ExitCode)。请查看项目根目录日志。" }
    if ($webProcess.HasExited) { throw "网页进程 $($webProcess.Id) 在就绪前退出，退出代码：$($webProcess.ExitCode)。请查看项目根目录日志。" }
    $ready = (Get-PortOwner $ApiPort) -eq $apiProcess.Id -and (Get-PortOwner $WebPort) -eq $webProcess.Id -and (Test-ApiIdentity $ApiPort) -and (Test-WebIdentity $WebPort)
    if (-not $ready) { Start-Sleep -Milliseconds 250 }
  } while (-not $ready -and (Get-Date) -lt $deadline)
  if (-not $ready) { throw '等待 Sing Reactor 服务身份验证就绪超时，请查看项目根目录日志。' }

  $apiInfo = Get-ProcessInfo $apiProcess.Id
  $webInfo = Get-ProcessInfo $webProcess.Id
  $manifest = New-Manifest $apiInfo $webInfo $false
  Write-Manifest $manifest
  $manifestCommitted = $true

  Release-LaunchMutex
  Open-App
  Write-Host "Sing Reactor 已启动：http://127.0.0.1:$WebPort/index.html"
  Write-Host "网页端口：$WebPort；API 端口：$ApiPort"
  Write-Host "API 进程：$($apiProcess.Id)；网页进程：$($webProcess.Id)"
  if ($NoWait) { Write-Host '服务将在后台继续运行；再次启动会复用此实例。'; $ownedThisRun = $false; return }
  Write-Host '请保持此窗口打开；异常关闭后，下次启动会从实例清单恢复识别。日志位于项目根目录。'
  Read-Host '按回车正常停止两个服务并关闭'
} catch {
  Write-Error $_.Exception.Message
  exit 1
} finally {
  Release-LaunchMutex
  if ($ownedThisRun) {
    if ($null -ne $webProcess -and -not $webProcess.HasExited) { Stop-Process -Id $webProcess.Id -ErrorAction SilentlyContinue }
    if ($null -ne $apiProcess -and -not $apiProcess.HasExited) { Stop-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue }
    if ($manifestCommitted) { Remove-ManifestIfSame $manifest }
  }
}
