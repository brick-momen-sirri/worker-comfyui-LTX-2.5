[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$secretDirectory = Join-Path $projectRoot '.secrets'
$tokenPath = Join-Path $secretDirectory 'hf_token'

New-Item -ItemType Directory -Force -Path $secretDirectory | Out-Null
$secureToken = Read-Host 'Hugging Face read token' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)

try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($plainToken) -or
        $plainToken.Length -gt 512 -or
        $plainToken -match '\s' -or
        -not [Text.Encoding]::ASCII.GetString([Text.Encoding]::ASCII.GetBytes($plainToken)).Equals($plainToken)) {
        throw 'The token must be a nonempty ASCII value without spaces.'
    }
    [IO.File]::WriteAllText($tokenPath, $plainToken, [Text.UTF8Encoding]::new($false))
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    Remove-Variable plainToken, secureToken -ErrorAction SilentlyContinue
}

Write-Host "Token saved to the ignored secret file: $tokenPath"
