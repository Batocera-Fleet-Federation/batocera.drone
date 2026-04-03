param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$Username,

    [Parameter(Mandatory = $true)]
    [string]$Password,

    [string]$OutputDir = ".\downloads",

    [switch]$Overwrite,

    [switch]$VerifyTls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-BasicAuthHeader {
    param(
        [Parameter(Mandatory = $true)][string]$User,
        [Parameter(Mandatory = $true)][string]$Pass
    )

    $bytes = [System.Text.Encoding]::UTF8.GetBytes("${User}:${Pass}")
    $token = [System.Convert]::ToBase64String($bytes)
    return "Basic $token"
}

function Encode-PathSegment {
    param([Parameter(Mandatory = $true)][string]$Value)
    return [System.Uri]::EscapeDataString($Value)
}

if (-not $VerifyTls) {
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
}

$base = $BaseUrl.TrimEnd("/")
$outputRoot = [System.IO.Path]::GetFullPath($OutputDir)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null

$headers = @{
    Authorization = Get-BasicAuthHeader -User $Username -Pass $Password
}

function Invoke-ApiJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    $url = "$base$Path"
    return Invoke-RestMethod -Method Get -Uri $url -Headers $headers
}

function Download-ApiFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $tmp = "$Destination.part"
    $dir = Split-Path -Parent $Destination
    [System.IO.Directory]::CreateDirectory($dir) | Out-Null

    try {
        Invoke-WebRequest -Method Get -Uri "$base$Path" -Headers $headers -OutFile $tmp | Out-Null
        Move-Item -Path $tmp -Destination $Destination -Force
    } catch {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

try {
    $systemsPayload = Invoke-ApiJson -Path "/systems"
} catch {
    Write-Error "Failed to list systems: $($_.Exception.Message)"
    exit 1
}

$systems = @($systemsPayload.systems)
if ($systems.Count -eq 0) {
    Write-Output "No systems returned by API."
    exit 0
}

$downloaded = 0
$skipped = 0

foreach ($system in $systems) {
    $systemName = [string]$system.name
    $systemDir = Join-Path -Path $outputRoot -ChildPath $systemName
    [System.IO.Directory]::CreateDirectory($systemDir) | Out-Null

    Write-Output "System: $systemName"

    $systemEnc = Encode-PathSegment -Value $systemName
    try {
        $romsPayload = Invoke-ApiJson -Path "/systems/$systemEnc"
    } catch {
        Write-Warning "  Failed to list ROMs: $($_.Exception.Message)"
        continue
    }

    $roms = @($romsPayload.roms)
    if ($roms.Count -eq 0) {
        Write-Output "  No ROMs found."
        continue
    }

    foreach ($rom in $roms) {
        $romName = [string]$rom.name
        $uniqueId = [string]$rom.unique_id
        $dest = Join-Path -Path $systemDir -ChildPath $romName

        if ((Test-Path -LiteralPath $dest) -and (-not $Overwrite)) {
            Write-Output "  Skip: $romName (exists)"
            $skipped++
            continue
        }

        $uniqueEnc = Encode-PathSegment -Value $uniqueId
        $romPath = "/systems/$systemEnc/$uniqueEnc"
        Write-Output "  Download: $romName"

        try {
            Download-ApiFile -Path $romPath -Destination $dest
            $downloaded++
        } catch {
            Write-Warning "    Failed: $($_.Exception.Message)"
        }
    }
}

Write-Output "Done. Downloaded: $downloaded, Skipped: $skipped, Output: $outputRoot"
