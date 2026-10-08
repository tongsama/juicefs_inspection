# install.ps1 - download, verify and install a patched build (juicefs or
# rclone, windows-amd64) published on the tongsama/juicefs_inspection
# GitHub Releases.
#
# Usage:
#   $env:KAZ_PRODUCT='rclone'; irm https://raw.githubusercontent.com/tongsama/juicefs_inspection/main/release/install.ps1 | iex
#
# Environment:
#   KAZ_PRODUCT        juicefs or rclone (required)
#   KAZ_VERSION        release tag to install (default: the newest release of the product)
#   KAZ_INSTALL_DIR    install directory (default: %LOCALAPPDATA%\Programs\<product>)
#   KAZ_DOWNLOAD_BASE  releases base URL (default: this repository; used by tests)
#   KAZ_API_BASE       GitHub API URL of this repository (default; used by tests)
#
# This script is run through Invoke-Expression, so it reports failures with
# `throw` and never calls `exit` (which would close the caller's session).
# It does not modify PATH and does not install WinFsp.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = 'tongsama/juicefs_inspection'
    $product = $env:KAZ_PRODUCT
    if (-not $product) { throw "set `$env:KAZ_PRODUCT to juicefs or rclone before running install.ps1" }
    $product = $product.ToLowerInvariant()
    if (@('juicefs', 'rclone') -notcontains $product) { throw "unknown product: $product (expected juicefs or rclone)" }
    $base = "https://github.com/$repo/releases"
    if ($env:KAZ_DOWNLOAD_BASE) { $base = $env:KAZ_DOWNLOAD_BASE.TrimEnd('/') }
    $api = "https://api.github.com/repos/$repo"
    if ($env:KAZ_API_BASE) { $api = $env:KAZ_API_BASE.TrimEnd('/') }
    $version = $env:KAZ_VERSION
    $installDir = Join-Path $env:LOCALAPPDATA "Programs\$product"
    if ($env:KAZ_INSTALL_DIR) { $installDir = $env:KAZ_INSTALL_DIR }
    $exeName = "$product.exe"
    $asset = "$product-windows-amd64.zip"

    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ($arch -ne 'AMD64') { throw "unsupported architecture: $arch (supported: windows-amd64)" }

    # Get-VersionKey returns @(major, minor, patch, kaz) for a release tag of
    # the product, or $null for any other tag. JuiceFS also accepts the older
    # form v<x.y.z>-kaz.<n>.
    function Get-VersionKey([string]$tag) {
        $t = $tag
        if ($t.StartsWith("${product}-v")) { $t = $t.Substring($product.Length + 2) }
        elseif ($product -eq 'juicefs' -and $t -match '^v\d') { $t = $t.Substring(1) }
        else { return $null }
        if ($t -notmatch '^(\d+)\.(\d+)\.(\d+)-kaz\.(\d+)$') { return $null }
        return , @([int]$Matches[1], [int]$Matches[2], [int]$Matches[3], [int]$Matches[4])
    }

    # Compare-VersionKey returns a positive number when key $a is newer than $b.
    function Compare-VersionKey($a, $b) {
        for ($i = 0; $i -lt 4; $i++) { if ($a[$i] -ne $b[$i]) { return $a[$i] - $b[$i] } }
        return 0
    }

    if ($version -and $null -eq (Get-VersionKey $version)) {
        $hint = ''
        if ($product -eq 'juicefs') { $hint = ' or the older v<x.y.z>-kaz.<n>' }
        throw "KAZ_VERSION=$version is not a $product release tag (expected $product-v<x.y.z>-kaz.<n>$hint)"
    }
    if (-not $version) {
        try {
            $releases = Invoke-RestMethod -Uri "$api/releases?per_page=100" -UseBasicParsing
        } catch {
            $resp = $_.Exception.Response
            $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
            if ($code -eq 403 -or $code -eq 429) {
                throw "GitHub API rate limit reached. Set `$env:KAZ_VERSION to a release tag to install it without the API"
            }
            throw "GitHub API request failed: $($_.Exception.Message). Set `$env:KAZ_VERSION to a release tag to install it without the API"
        }
        $bestKey = $null
        foreach ($r in $releases) {
            $k = Get-VersionKey $r.tag_name
            if ($null -eq $k) { continue }
            if ($null -eq $bestKey -or (Compare-VersionKey $k $bestKey) -gt 0) { $bestKey = $k; $version = $r.tag_name }
        }
        if (-not $version) { throw "no $product release found ($api/releases)" }
        Write-Host "newest $product release: $version"
    }
    $label = $version
    $url = "$base/download/$version"

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

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('kaz-install-' + [Guid]::NewGuid().ToString('N'))
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
        $exe = Join-Path $x $exeName
        if (-not (Test-Path $exe)) { throw "archive does not contain $exeName" }

        New-Item -ItemType Directory -Force -Path $installDir | Out-Null
        $dest = Join-Path $installDir $exeName
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
            throw "cannot replace $dest (is $product running? a running $exeName cannot be replaced; stop it, retry, then start it again to use the new version): $($_.Exception.Message)"
        }

        $new = (& $dest version | Select-Object -First 1)
        Write-Host "installed: $dest"
        Write-Host "version:   $new"
        Write-Host "PATH was not modified; run it by full path or add $installDir to PATH yourself."


        # Another copy found on PATH keeps being run by services and shells.
        $others = @(Get-Command $product -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object {
                $same = $false
                try { $same = ((Resolve-Path $_.Source -ErrorAction Stop).Path -ieq (Resolve-Path $dest -ErrorAction Stop).Path) } catch { }
                $_.Source -and -not $same
            })
        foreach ($o in $others) {
            Write-Warning "another $product exists at $($o.Source) and is not replaced; services or shells using it keep the old binary"
        }
        $winfsp = @(
            "${env:ProgramFiles(x86)}\WinFsp\bin\winfsp-x64.dll",
            "$env:ProgramFiles\WinFsp\bin\winfsp-x64.dll"
        ) | Where-Object { Test-Path $_ }
        if (-not $winfsp) {
            Write-Warning "WinFsp was not found. It is required for '$product mount' on Windows: https://winfsp.dev/rel/"
        }
    } finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -Path $tmp
    }
}
