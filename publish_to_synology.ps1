# PhotoCollect verified release publisher for the Synology LAN update center.
[CmdletBinding()]
param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$NasReleasePath = "\\192.168.110.79\PhotoCollectUpdates\releases",
    [string]$PublicBaseUrl = "http://192.168.110.79:18080",
    [string]$NtfyBaseUrl = "http://192.168.110.79:18081",
    [string]$Topic = "photocollect-updates",
    [string]$AcceptanceReport,
    [string]$ReleaseNotes,
    [switch]$SkipLocalResult,
    [switch]$InitializeTokenOnly,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "目标目录不存在：$directory"
    }

    $temporary = Join-Path $directory (
        ".{0}.uploading.{1}" -f ([System.IO.Path]::GetFileName($Path)), ([Guid]::NewGuid().ToString("N"))
    )

    try {
        Write-Utf8NoBom -Path $temporary -Content $Content
        $written = [System.IO.File]::ReadAllText($temporary)
        if ($written -ne $Content) {
            throw "临时文本回读不一致：$temporary"
        }

        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $backup = "{0}.bak.{1}.{2}" -f (
                $Path,
                (Get-Date -Format "yyyyMMdd-HHmmss"),
                ([Guid]::NewGuid().ToString("N"))
            )
            try {
                [System.IO.File]::Replace($temporary, $Path, $backup, $true)
            }
            catch {
                throw "SMB 不支持安全原子替换，旧文件已保留：$Path。原始错误：$($_.Exception.Message)"
            }
        }
        else {
            [System.IO.File]::Move($temporary, $Path)
        }

        $committed = [System.IO.File]::ReadAllText($Path)
        if ($committed -ne $Content) {
            throw "提交后的文本回读不一致：$Path"
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Get-ProjectVersion {
    param([Parameter(Mandatory = $true)][string]$Root)

    $sourcePath = Join-Path $Root "con.py"
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "找不到版本来源：$sourcePath"
    }

    $source = [System.IO.File]::ReadAllText($sourcePath)
    $match = [regex]::Match(
        $source,
        '(?m)^\s*APP_VERSION\s*=\s*"(?<version>[^"]+)"'
    )
    if (-not $match.Success) {
        throw "无法从 con.py 读取 APP_VERSION。"
    }

    $version = $match.Groups["version"].Value
    if ($version -notmatch '^\d+\.\d+\.\d+$') {
        throw "APP_VERSION 格式不受支持：$version"
    }
    return $version
}

function Find-AcceptanceReport {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Version
    )

    $outputs = Join-Path $Root "outputs"
    $patterns = @(
        "acceptance-v$Version-desktop-*.json",
        "acceptance-v$Version-package-*.json",
        "acceptance-exe-v$Version.json"
    )

    foreach ($pattern in $patterns) {
        $candidate = Get-ChildItem -LiteralPath $outputs -Filter $pattern -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($null -ne $candidate) {
            return $candidate.FullName
        }
    }

    throw "找不到 v$Version 的 EXE 验收报告，请先完成打包和验收。"
}

function Get-VerifiedRelease {
    param(
        [Parameter(Mandatory = $true)][string]$ReportPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "验收报告不存在：$ReportPath"
    }

    $report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.ok -ne $true) {
        throw "验收报告没有通过：$ReportPath"
    }
    if ([string]$report.version -ne $ExpectedVersion) {
        throw "验收报告版本 $($report.version) 与源码版本 $ExpectedVersion 不一致。"
    }

    $exePath = [string]$report.runtime.executable
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "验收通过的 EXE 已不存在：$exePath"
    }

    $item = Get-Item -LiteralPath $exePath
    $actualHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $expectedHash = ([string]$report.runtime.sha256).ToLowerInvariant()
    $expectedSize = [int64]$report.runtime.size_bytes

    if ($expectedHash -notmatch '^[0-9a-f]{64}$') {
        throw "验收报告中的 SHA-256 格式无效。"
    }
    if ($expectedSize -le 0) {
        throw "验收报告中的文件大小无效。"
    }
    if ($item.Length -ne $expectedSize) {
        throw "EXE 大小已变化：验收值 $expectedSize，当前值 $($item.Length)。"
    }
    if ($actualHash -ne $expectedHash) {
        throw "EXE SHA-256 已变化，拒绝发布未经当前报告验收的文件。"
    }

    return [pscustomobject]@{
        ReportPath = $ReportPath
        ExePath    = $exePath
        Size       = $item.Length
        Sha256     = $actualHash
    }
}

function Assert-PublisherToken {
    param([Parameter(Mandatory = $true)][string]$Token)

    if ($Token -notmatch '^tk_[A-Za-z0-9]{29}$') {
        throw "publisher 令牌格式异常；应以 tk_ 开头且总长度为 32。"
    }
    return $Token
}

function Get-PublisherTokenPath {
    $localAppData = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::LocalApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw "无法确定当前用户的 LocalAppData 目录。"
    }
    return Join-Path $localAppData "PhotoCollect\publisher-token.dpapi"
}

function Save-ProtectedPublisherToken {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Token
    )

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $plain = [Text.Encoding]::UTF8.GetBytes($Token)
    $entropy = [Text.Encoding]::UTF8.GetBytes("PhotoCollect.publisher.v1")
    $protected = [Security.Cryptography.ProtectedData]::Protect(
        $plain,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $encoded = [Convert]::ToBase64String($protected)
    $temporary = "$Path.tmp.$([Guid]::NewGuid().ToString('N'))"
    try {
        Write-Utf8NoBom -Path $temporary -Content $encoded
        [System.IO.File]::Move($temporary, $Path, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
        [Array]::Clear($plain, 0, $plain.Length)
    }
}

function Read-ProtectedPublisherToken {
    param([Parameter(Mandatory = $true)][string]$Path)

    $encoded = [System.IO.File]::ReadAllText($Path).Trim()
    $protected = [Convert]::FromBase64String($encoded)
    $entropy = [Text.Encoding]::UTF8.GetBytes("PhotoCollect.publisher.v1")
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    try {
        return [Text.Encoding]::UTF8.GetString($plain)
    }
    finally {
        [Array]::Clear($plain, 0, $plain.Length)
    }
}

function Get-PublisherToken {
    $clipboard = $null
    try {
        $clipboard = Get-Clipboard -Raw
    }
    catch {
        Write-Warning "无法读取剪贴板，将尝试已保存的发布令牌。"
    }

    $clipboardText = if ($null -eq $clipboard) { "" } else { ([string]$clipboard).Trim() }
    if ($clipboardText.StartsWith("tk_")) {
        $token = Assert-PublisherToken -Token $clipboardText
        $tokenPath = Get-PublisherTokenPath
        Save-ProtectedPublisherToken -Path $tokenPath -Token $token
        Set-Clipboard -Value ""
        Write-Host "      publisher 令牌已由 Windows 当前用户加密保存；以后无需再次复制。"
        return $token
    }

    $tokenPath = Get-PublisherTokenPath
    if (Test-Path -LiteralPath $tokenPath -PathType Leaf) {
        try {
            return Assert-PublisherToken -Token (
                Read-ProtectedPublisherToken -Path $tokenPath
            )
        }
        catch {
            throw "已保存的 publisher 令牌无法读取。请删除 $tokenPath，复制有效令牌后重试。"
        }
    }

    throw "首次发布前，请先复制 publisher 令牌到剪贴板；脚本会用 Windows 当前用户加密保存。"
}

function Assert-ManifestMatches {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [Parameter(Mandatory = $true)][string]$ExpectedUrl,
        [Parameter(Mandatory = $true)][int64]$ExpectedSize,
        [Parameter(Mandatory = $true)][string]$ExpectedHash,
        [Parameter(Mandatory = $true)][string]$ExpectedReleaseId
    )

    if ([int]$Manifest.schema_version -ne 1) {
        throw "清单 schema_version 不正确。"
    }
    if ([string]$Manifest.app -ne "PhotoCollect") {
        throw "清单 app 不正确。"
    }
    if ([string]$Manifest.channel -ne "stable") {
        throw "清单 channel 不正确。"
    }
    if ([string]$Manifest.version -ne $ExpectedVersion) {
        throw "清单 version 不正确。"
    }
    if ([string]$Manifest.release_id -ne $ExpectedReleaseId) {
        throw "清单 release_id 不正确。"
    }
    if ([string]$Manifest.file.name -ne $ExpectedName) {
        throw "清单文件名不正确。"
    }
    if ([string]$Manifest.file.url -ne $ExpectedUrl) {
        throw "清单下载地址不正确。"
    }
    if ([int64]$Manifest.file.size_bytes -ne $ExpectedSize) {
        throw "清单文件大小不正确。"
    }
    if (([string]$Manifest.file.sha256).ToLowerInvariant() -ne $ExpectedHash) {
        throw "清单 SHA-256 不正确。"
    }
}

function Get-HttpFileInfo {
    param([Parameter(Mandatory = $true)][string]$Url)

    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    $response = $null
    $stream = $null
    $sha = $null

    try {
        $response = $client.GetAsync(
            $Url,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "EXE 下载地址返回 HTTP $([int]$response.StatusCode)：$Url"
        }

        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $buffer = New-Object byte[] (1MB)
        [int64]$total = 0

        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            [void]$sha.TransformBlock($buffer, 0, $read, $buffer, 0)
            $total += $read
        }
        [void]$sha.TransformFinalBlock((New-Object byte[] 0), 0, 0)
        $hash = ([BitConverter]::ToString($sha.Hash)).Replace("-", "").ToLowerInvariant()

        return [pscustomobject]@{
            Size   = $total
            Sha256 = $hash
        }
    }
    finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $client.Dispose()
    }
}

function Get-RemoteManifest {
    param([Parameter(Mandatory = $true)][string]$Url)

    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 15
        }
        catch {
            $lastError = $_
            if ($attempt -lt 3) {
                Start-Sleep -Seconds ([Math]::Pow(2, $attempt - 1))
            }
        }
    }
    throw "读取远程清单失败：$($lastError.Exception.Message)"
}

function Send-ReleaseNotification {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Token,
        [Parameter(Mandatory = $true)][string]$Body
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $response = Invoke-WebRequest `
                -Uri $Url `
                -Method Post `
                -Headers @{ Authorization = "Bearer $Token" } `
                -ContentType "text/plain; charset=utf-8" `
                -Body $Body `
                -UseBasicParsing `
                -TimeoutSec 15
            if ([int]$response.StatusCode -eq 200) {
                return
            }
            throw "ntfy 返回 HTTP $([int]$response.StatusCode)。"
        }
        catch {
            $lastError = $_
            if ($attempt -lt 3) {
                Start-Sleep -Seconds ([Math]::Pow(2, $attempt - 1))
            }
        }
    }
    throw "ntfy 通知连续三次失败：$($lastError.Exception.Message)"
}

function Save-PublishResult {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)]$Data,
        [string]$Path
    )

    try {
        $resultDirectory = Join-Path $Root "outputs"
        if (-not (Test-Path -LiteralPath $resultDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $resultDirectory | Out-Null
        }
        if ([string]::IsNullOrWhiteSpace($Path)) {
            $Path = Join-Path $resultDirectory (
                "publish-nas-v{0}-{1}.json" -f $Data.version, (Get-Date -Format "yyyyMMdd-HHmmss")
            )
        }
        Write-Utf8NoBom -Path $Path -Content ($Data | ConvertTo-Json -Depth 6)
        return $Path
    }
    catch {
        Write-Warning "发布状态已确定，但本地记录写入失败：$($_.Exception.Message)"
        return $null
    }
}

if ($InitializeTokenOnly) {
    $initializedToken = $null
    try {
        $initializedToken = Get-PublisherToken
        Write-Host "publisher 令牌已就绪。" -ForegroundColor Green
        exit 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
    finally {
        $initializedToken = $null
    }
}

$publisherToken = $null
$uploadedTemporary = $null
$publishLock = $null
$releaseCommitted = $false
$resultPath = $null
$notificationSent = $false
$version = $null
$release = $null
$destinationExe = $null
$fileUrl = $null
$manifestUrl = $null

try {
    $root = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $version = Get-ProjectVersion -Root $root

    if ([string]::IsNullOrWhiteSpace($AcceptanceReport)) {
        $AcceptanceReport = Find-AcceptanceReport -Root $root -Version $version
    }
    else {
        $AcceptanceReport = (Resolve-Path -LiteralPath $AcceptanceReport).Path
    }

    $release = Get-VerifiedRelease -ReportPath $AcceptanceReport -ExpectedVersion $version

    if (-not (Test-Path -LiteralPath $NasReleasePath -PathType Container)) {
        throw "无法访问群晖发布目录：$NasReleasePath"
    }

    if ([string]::IsNullOrWhiteSpace($ReleaseNotes)) {
        $ReleaseNotes = "PhotoCollect v$version 稳定版"
    }

    try {
        $publicUri = [Uri]$PublicBaseUrl
        $ntfyUri = [Uri]$NtfyBaseUrl
    }
    catch {
        throw "PublicBaseUrl 或 NtfyBaseUrl 不是有效 URL。"
    }
    if (-not $publicUri.IsAbsoluteUri -or $publicUri.Scheme -notin @("http", "https")) {
        throw "PublicBaseUrl 必须是 HTTP 或 HTTPS 绝对地址。"
    }
    if (-not $ntfyUri.IsAbsoluteUri -or $ntfyUri.Scheme -notin @("http", "https")) {
        throw "NtfyBaseUrl 必须是 HTTP 或 HTTPS 绝对地址。"
    }
    if ($Topic -notmatch '^[A-Za-z0-9_-]{1,64}$') {
        throw "ntfy topic 格式无效。"
    }

    $fileName = "PhotoCollect-v$version.exe"
    $versionManifestName = "PhotoCollect-v$version.json"
    $destinationExe = Join-Path $NasReleasePath $fileName
    $latestPath = Join-Path $NasReleasePath "latest.json"
    $versionManifestPath = Join-Path $NasReleasePath $versionManifestName
    $indexPath = Join-Path $NasReleasePath "index.html"
    $healthPath = Join-Path $NasReleasePath "health.txt"
    $fileUrl = $PublicBaseUrl.TrimEnd("/") + "/" + $fileName
    $manifestUrl = $PublicBaseUrl.TrimEnd("/") + "/latest.json"
    $publishedAt = (Get-Date).ToUniversalTime().ToString("o")

    Write-Host ""
    Write-Host "准备发布 PhotoCollect v$version" -ForegroundColor Cyan
    Write-Host "验收报告：$($release.ReportPath)"
    Write-Host "来源文件：$($release.ExePath)"
    Write-Host "文件大小：$($release.Size) bytes"
    Write-Host "SHA-256 ：$($release.Sha256)"
    Write-Host "群晖目录：$NasReleasePath"
    Write-Host "下载地址：$fileUrl"
    Write-Host ""

    if (-not $Yes) {
        $answer = Read-Host "确认无误请输入 PUBLISH"
        if ($answer -cne "PUBLISH") {
            Write-Warning "用户取消，未写入群晖。"
            exit 2
        }
    }

    $publisherToken = Get-PublisherToken

    $releaseId = "PhotoCollect:${version}:$($release.Sha256)"
    $manifest = [ordered]@{
        schema_version = 1
        app            = "PhotoCollect"
        channel        = "stable"
        version        = $version
        release_id     = $releaseId
        published_at   = $publishedAt
        notes          = $ReleaseNotes
        file           = [ordered]@{
            name       = $fileName
            url        = $fileUrl
            size_bytes = $release.Size
            sha256     = $release.Sha256
        }
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 6
    $manifestObject = $manifestJson | ConvertFrom-Json

    $lockPath = Join-Path $NasReleasePath ".photocollect-publish.lock"
    try {
        $publishLock = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch {
        throw "另一个发布流程正在运行，或无法取得群晖发布锁：$($_.Exception.Message)"
    }

    $alreadyPublished = $false
    $currentSemanticVersion = [version]$version
    if (Test-Path -LiteralPath $versionManifestPath -PathType Leaf) {
        $existingManifest = Get-Content -LiteralPath $versionManifestPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $existingVersionReleaseId = $existingManifest.PSObject.Properties["release_id"]
        if (
            $null -eq $existingVersionReleaseId -or
            [string]$existingVersionReleaseId.Value -ne $releaseId
        ) {
            throw "版本号未递增，请先修改 con.py 中的 APP_VERSION；群晖已有 v$version 的不同发布内容。"
        }
        Assert-ManifestMatches `
            -Manifest $existingManifest `
            -ExpectedVersion $version `
            -ExpectedName $fileName `
            -ExpectedUrl $fileUrl `
            -ExpectedSize $release.Size `
            -ExpectedHash $release.Sha256 `
            -ExpectedReleaseId $releaseId
    }

    if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
        $existingLatest = Get-Content -LiteralPath $latestPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $existingLatestVersionText = [string]$existingLatest.version
        if ($existingLatestVersionText -notmatch '^\d+\.\d+\.\d+$') {
            throw "群晖上的 latest.json 版本格式无效，拒绝覆盖。"
        }
        $existingLatestVersion = [version]$existingLatest.version
        $releaseIdProperty = $existingLatest.PSObject.Properties["release_id"]
        if ($existingLatestVersion -gt $currentSemanticVersion) {
            throw "群晖上的 latest.json 版本更高，拒绝降级发布：v$existingLatestVersionText > v$version"
        }
        if ($existingLatestVersion -eq $currentSemanticVersion) {
            if ($null -eq $releaseIdProperty -or [string]$releaseIdProperty.Value -ne $releaseId) {
                throw "版本号未递增，请先修改 con.py 中的 APP_VERSION；v$version 已发布过不同内容。"
            }
            Assert-ManifestMatches `
                -Manifest $existingLatest `
                -ExpectedVersion $version `
                -ExpectedName $fileName `
                -ExpectedUrl $fileUrl `
                -ExpectedSize $release.Size `
                -ExpectedHash $release.Sha256 `
                -ExpectedReleaseId $releaseId
            $alreadyPublished = $true
        }
    }

    Write-Host "[1/5] 上传并核对不可变版本文件 ..."
    if (Test-Path -LiteralPath $destinationExe -PathType Leaf) {
        $existingItem = Get-Item -LiteralPath $destinationExe
        $existingHash = (Get-FileHash -LiteralPath $destinationExe -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existingItem.Length -ne $release.Size -or $existingHash -ne $release.Sha256) {
            throw "版本号未递增，请先修改 con.py 中的 APP_VERSION；群晖已存在同版本但内容不同的 EXE：$destinationExe"
        }
        Write-Host "      同版本 EXE 已存在且大小、哈希一致，复用现有文件。"
    }
    else {
        $uploadedTemporary = Join-Path $NasReleasePath (
            ".$fileName.uploading.{0}" -f ([Guid]::NewGuid().ToString("N"))
        )
        Copy-Item -LiteralPath $release.ExePath -Destination $uploadedTemporary
        $uploadedItem = Get-Item -LiteralPath $uploadedTemporary
        $uploadedHash = (Get-FileHash -LiteralPath $uploadedTemporary -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($uploadedItem.Length -ne $release.Size -or $uploadedHash -ne $release.Sha256) {
            throw "群晖临时 EXE 的大小或哈希不一致，拒绝发布。"
        }
        Move-Item -LiteralPath $uploadedTemporary -Destination $destinationExe
        $uploadedTemporary = $null
    }

    if (-not (Test-Path -LiteralPath $versionManifestPath -PathType Leaf)) {
        Write-AtomicText -Path $versionManifestPath -Content $manifestJson
    }

    Write-Host "[2/5] 准备稳定下载首页和健康文件 ..."
    $indexHtml = @'
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PhotoCollect 更新</title>
  <style>
    body{margin:0;background:#f4f7fb;color:#172033;font:16px/1.65 "Segoe UI","Microsoft YaHei",sans-serif}
    main{max-width:760px;margin:8vh auto;padding:36px;background:#fff;border:1px solid #dfe7f1;border-radius:18px;box-shadow:0 16px 44px #183b6614}
    h1{margin:0 0 8px;font-size:30px}.muted{color:#66758a}.card{margin:26px 0;padding:22px;border-radius:14px;background:#f7faff}
    a.button{display:inline-block;padding:12px 20px;border-radius:10px;background:#1769e0;color:#fff;text-decoration:none;font-weight:700}
    code{word-break:break-all}.steps{padding-left:22px}
  </style>
</head>
<body>
  <main>
    <h1 id="title">PhotoCollect 更新</h1>
    <p id="notes" class="muted">正在读取 latest.json…</p>
    <div class="card">
      <p><strong>文件：</strong><span id="file">—</span></p>
      <p><strong>大小：</strong><span id="size">—</span></p>
      <p><strong>SHA-256：</strong><code id="hash">—</code></p>
      <a id="download" class="button" href="#" hidden>下载最新版本</a>
    </div>
    <ol class="steps">
      <li>先关闭正在运行的 PhotoCollect。</li>
      <li>下载新版本，并保留旧 EXE 作为临时备份。</li>
      <li>确认新版本能正常打开后，再删除旧 EXE。</li>
    </ol>
    <p id="published" class="muted"></p>
  </main>
  <script>
    fetch("latest.json?ts=" + Date.now(), {cache:"no-store"})
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(m => {
        document.getElementById("title").textContent = "PhotoCollect v" + m.version;
        document.getElementById("notes").textContent = m.notes || "";
        document.getElementById("file").textContent = m.file.name;
        document.getElementById("size").textContent = (m.file.size_bytes / 1048576).toFixed(1) + " MB";
        document.getElementById("hash").textContent = m.file.sha256;
        document.getElementById("published").textContent = "发布时间：" + m.published_at;
        const link = document.getElementById("download");
        link.href = m.file.name;
        link.hidden = false;
      })
      .catch(e => { document.getElementById("notes").textContent = "暂时无法读取最新版：" + e.message; });
  </script>
</body>
</html>
'@
    if (-not (Test-Path -LiteralPath $indexPath -PathType Leaf)) {
        Write-AtomicText -Path $indexPath -Content $indexHtml
    }
    if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
        if ([System.IO.File]::ReadAllText($healthPath).Trim() -ne "ok") {
            throw "health.txt 内容不是 ok，拒绝覆盖未知文件。"
        }
    }
    else {
        Write-AtomicText -Path $healthPath -Content "ok`n"
    }

    Write-Host "[3/5] 从 HTTP 完整下载并复核大小、SHA-256 ..."
    $httpFile = Get-HttpFileInfo -Url $fileUrl
    if ($httpFile.Size -ne $release.Size -or $httpFile.Sha256 -ne $release.Sha256) {
        throw "HTTP 下载得到的 EXE 与验收产物不一致，latest.json 未更新。"
    }

    Write-Host "[4/5] 原子提交 latest.json 发布指针 ..."
    $previousLatestJson = $null
    if (-not $alreadyPublished) {
        if (Test-Path -LiteralPath $latestPath -PathType Leaf) {
            $previousLatestJson = [System.IO.File]::ReadAllText($latestPath)
        }
        Write-AtomicText -Path $latestPath -Content $manifestJson
        $releaseCommitted = $true
    }
    else {
        $releaseCommitted = $true
        Write-Host "      latest.json 已指向相同 release_id，保持不变。"
    }

    try {
        $remoteManifest = Get-RemoteManifest -Url $manifestUrl
        Assert-ManifestMatches `
            -Manifest $remoteManifest `
            -ExpectedVersion $version `
            -ExpectedName $fileName `
            -ExpectedUrl $fileUrl `
            -ExpectedSize $release.Size `
            -ExpectedHash $release.Sha256 `
            -ExpectedReleaseId $releaseId
    }
    catch {
        if (-not $alreadyPublished) {
            try {
                if ($null -ne $previousLatestJson) {
                    Write-AtomicText -Path $latestPath -Content $previousLatestJson
                }
                elseif (Test-Path -LiteralPath $latestPath -PathType Leaf) {
                    [System.IO.File]::Delete($latestPath)
                }
                $releaseCommitted = $false
            }
            catch {
                throw "latest.json HTTP 验证失败，并且回滚也失败：$($_.Exception.Message)"
            }
        }
        throw
    }

    Write-Host "[5/5] 推送 release.ready 通知 ..."
    $notification = [ordered]@{
        type         = "release.ready"
        release_id   = $releaseId
        version      = $version
        manifest_url = $manifestUrl
        sha256       = $release.Sha256
        size_bytes   = $release.Size
    } | ConvertTo-Json -Compress
    Send-ReleaseNotification `
        -Url ($NtfyBaseUrl.TrimEnd("/") + "/" + $Topic) `
        -Token $publisherToken `
        -Body $notification
    $notificationSent = $true

    if (-not $SkipLocalResult) {
        $resultData = [ordered]@{
            ok              = $true
            status          = "published_notified"
            release_id      = $releaseId
            version         = $version
            source          = $release.ExePath
            acceptance      = $release.ReportPath
            destination     = $destinationExe
            download_url    = $fileUrl
            manifest_url    = $manifestUrl
            sha256          = $release.Sha256
            size_bytes      = $release.Size
            notification    = $notificationSent
            completed_at    = (Get-Date).ToString("o")
        }
        $resultPath = Save-PublishResult -Root $root -Data $resultData
    }

    Write-Host ""
    Write-Host "发布成功：PhotoCollect v$version" -ForegroundColor Green
    Write-Host "同事下载页：$($PublicBaseUrl.TrimEnd('/'))/"
    Write-Host "版本清单：$manifestUrl"
    if ($null -ne $resultPath) {
        Write-Host "发布记录：$resultPath"
    }
}
catch {
    $failureMessage = $_.Exception.Message
    if ($releaseCommitted) {
        if (-not $SkipLocalResult -and $null -ne $version -and $null -ne $release) {
            $partialData = [ordered]@{
                ok              = $false
                status          = "published_followup_failed"
                version         = $version
                destination     = $destinationExe
                download_url    = $fileUrl
                manifest_url    = $manifestUrl
                sha256          = $release.Sha256
                size_bytes      = $release.Size
                notification    = $notificationSent
                error           = $failureMessage
                completed_at    = (Get-Date).ToString("o")
            }
            $resultPath = Save-PublishResult -Root $root -Data $partialData -Path $resultPath
        }
        [Console]::Error.WriteLine("版本文件和 latest.json 已发布，但通知或收尾失败：$failureMessage")
        exit 3
    }

    [Console]::Error.WriteLine("发布未提交，原有 latest.json 保持不变：$failureMessage")
    exit 1
}
finally {
    if ($null -ne $uploadedTemporary -and (Test-Path -LiteralPath $uploadedTemporary -PathType Leaf)) {
        Remove-Item -LiteralPath $uploadedTemporary -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $publishLock) {
        $publishLock.Dispose()
    }
    $publisherToken = $null
}
