# install.ps1 - download, verify and install the patched JuiceFS build
# (windows-amd64) published on the tongsama/juicefs_inspection GitHub Releases.
#
# Usage:
#   irm https://github.com/tongsama/juicefs_inspection/releases/latest/download/install.ps1 | iex
#
# Environment:
#   JFS_VERSION        release tag to install (default: latest release)
#   JFS_INSTALL_DIR    install directory (default: %LOCALAPPDATA%\Programs\juicefs)
#   JFS_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#
# This script is run through Invoke-Expression, so it reports failures with
# `throw` and never calls `exit` (which would close the caller's session).
# It does not modify PATH and does not install WinFsp.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $base = 'https://github.com/tongsama/juicefs_inspection/releases'
    if ($env:JFS_DOWNLOAD_BASE) { $base = $env:JFS_DOWNLOAD_BASE.TrimEnd('/') }
    $version = $env:JFS_VERSION
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\juicefs'
    if ($env:JFS_INSTALL_DIR) { $installDir = $env:JFS_INSTALL_DIR }
    $asset = 'juicefs-windows-amd64.zip'

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ($arch -ne 'AMD64') { throw "unsupported architecture: $arch (supported: windows-amd64)" }

    $label = 'latest'
    $url = "$base/latest/download"
    if ($version) { $label = $version; $url = "$base/download/$version" }

    # Save-Asset downloads one release asset into $dir and tells a 404 apart
    # from other failures.
    function Save-Asset([string]$name, [string]$dir) {
        $out = Join-Path $dir $name
        try {
            Invoke-WebRequest -Uri "$url/$name" -OutFile $out -UseBasicParsing
        } catch {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -eq 404) {
                throw "not found (404): $url/$name -- check that release '$label' and its assets exist"
            }
            throw "download failed: $url/$name : $($_.Exception.Message)"
        }
        return $out
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('jfs-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        Write-Host "downloading $asset ($label) from $url"
        $zip = Save-Asset $asset $tmp
        $sums = Save-Asset 'checksums.txt' $tmp

        $expected = $null
        foreach ($line in Get-Content $sums) {
            $parts = $line.Trim() -split '\s+', 2
            if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -eq $asset) {
                $expected = $parts[0].ToLower()
                break
            }
        }
        if (-not $expected) { throw "no checksum entry for $asset in checksums.txt" }
        $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
        if ($actual -ne $expected) { throw "checksum mismatch for $asset (expected $expected, got $actual)" }
        Write-Host 'checksum ok'

        $x = Join-Path $tmp 'x'
        Expand-Archive -Path $zip -DestinationPath $x
        $exe = Join-Path $x 'juicefs.exe'
        if (-not (Test-Path $exe)) { throw 'archive does not contain juicefs.exe' }

        New-Item -ItemType Directory -Force -Path $installDir | Out-Null
        $dest = Join-Path $installDir 'juicefs.exe'
        if (Test-Path $dest) {
            $old = 'unknown'
            try { $old = (& $dest version | Select-Object -First 1) } catch { }
            Write-Host "current: $old"
        }

        $staged = "$dest.new"
        Copy-Item -Force -Path $exe -Destination $staged
        try {
            Move-Item -Force -Path $staged -Destination $dest
        } catch {
            Remove-Item -Force -ErrorAction SilentlyContinue -Path $staged
            throw "cannot replace $dest (is juicefs running? stop it and retry): $($_.Exception.Message)"
        }

        $new = (& $dest version | Select-Object -First 1)
        Write-Host "installed: $dest"
        Write-Host "version:   $new"
        Write-Host "PATH was not modified; run it by full path or add $installDir to PATH yourself."

        $winfsp = @(
            "${env:ProgramFiles(x86)}\WinFsp\bin\winfsp-x64.dll",
            "$env:ProgramFiles\WinFsp\bin\winfsp-x64.dll"
        ) | Where-Object { Test-Path $_ }
        if (-not $winfsp) {
            Write-Warning 'WinFsp was not found. It is required to mount JuiceFS on Windows: https://winfsp.dev/rel/'
        }
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -Path $tmp
    }
}
