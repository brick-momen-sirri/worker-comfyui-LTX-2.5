[CmdletBinding()]
param(
    [ValidateSet('int8', 'bf16', 'cq-v2')][string]$Profile = 'int8',
    [string]$Tag,
    [switch]$RuntimeOnly,
    [switch]$Push,
    [string]$HFTokenFile
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
    $dockerBin = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
    $dockerPath = Join-Path $dockerBin 'docker.exe'
    if (-not (Test-Path -LiteralPath $dockerPath)) { throw 'Docker Desktop is not installed or docker.exe is unavailable.' }
    # The credential helper also needs this directory after a fresh installation.
    $env:PATH = "$dockerBin;$env:PATH"
} else { $dockerPath = $dockerCommand.Source }
if (-not $Tag) {
    $Tag = if ($RuntimeOnly) { "worker-comfyui-ltx25:$Profile-runtime-check" } else { "worker-comfyui-ltx25:$Profile" }
}
$arguments = @('buildx', 'build', '--platform', 'linux/amd64', '--progress', 'plain', '--build-arg', "MODEL_PROFILE=$Profile", '--tag', $Tag)
if ($RuntimeOnly) {
    $arguments += @('--target', 'runtime')
} elseif ($HFTokenFile) {
    $tokenPath = (Resolve-Path -LiteralPath $HFTokenFile).Path
    $arguments += @('--secret', "id=hf_token,src=$tokenPath")
} elseif ($env:HF_TOKEN) {
    $arguments += @('--secret', 'id=hf_token,env=HF_TOKEN')
} else {
    throw 'Set HF_TOKEN privately or provide -HFTokenFile. Model builds require access to the gated repositories.'
}
if ($Push) { $arguments += '--push' } else { $arguments += '--load' }
$arguments += $projectRoot
& $dockerPath @arguments
exit $LASTEXITCODE
