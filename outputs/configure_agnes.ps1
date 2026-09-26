$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:AGNES_API_KEY)) {
    throw 'AGNES_API_KEY is not present in this process.'
}

$targetDir = Join-Path $env:USERPROFILE '.config\virtual-idol'
$targetPath = Join-Path $targetDir 'agnes.env'
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null

$content = @(
    "AGNES_API_KEY=$($env:AGNES_API_KEY)"
    'AGNES_BASE_URL=https://apihub.agnes-ai.com/v1'
    'AGNES_MODEL=agnes-3.0-flash'
) -join [Environment]::NewLine

[System.IO.File]::WriteAllText($targetPath, $content + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
$grant = "$($env:USERNAME):(F)"
& icacls.exe $targetPath /inheritance:r /grant:r $grant | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not restrict ACL for $targetPath"
}

[pscustomobject]@{
    configured = $true
    path = $targetPath
    model = 'agnes-3.0-flash'
    base_url = 'https://apihub.agnes-ai.com/v1'
} | ConvertTo-Json -Compress
