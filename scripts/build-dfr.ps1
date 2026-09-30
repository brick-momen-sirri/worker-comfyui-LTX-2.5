[CmdletBinding()]
param(
    [string]$Tag = 'momensirribrick/worker-ltx25-dfr:4k-v1',
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
    if (-not (Test-Path -LiteralPath $dockerPath)) { throw 'Docker Desktop is unavailable.' }
    $env:PATH = "$dockerBin;$env:PATH"
} else { $dockerPath = $dockerCommand.Source }
$arguments = @('buildx', 'build', '--platform', 'linux/amd64', '--progress', 'plain',
    '--file', (Join-Path $projectRoot 'Dockerfile.dfr'), '--tag', $Tag)
if ($RuntimeOnly) {
    if ($Push) { throw 'Do not publish a runtime-only image as a deployable DFR worker.' }
    $arguments += @('--target', 'runtime')
} elseif ($HFTokenFile) {
    $tokenPath = (Resolve-Path -LiteralPath $HFTokenFile).Path
    $arguments += @('--secret', "id=hf_token,src=$tokenPath")
} elseif ($env:HF_TOKEN) {
    $arguments += @('--secret', 'id=hf_token,env=HF_TOKEN')
} else {
    throw 'Supply -HFTokenFile or set HF_TOKEN privately. Full builds include all six models.'
}
# Keep the tested Docker Desktop containerd exporter for local and registry builds.
# Level 1 reduces compression work on the model layer; reuse existing layer blobs.
# Full-build unpacking needs 220-250 GB additional capacity; see docs/dfr-4k.md.
$imageOutput = 'type=image,compression=gzip,compression-level=1,unpack=true'
if ($Push) { $imageOutput += ',push=true' }
$arguments += @('--output', $imageOutput)
$arguments += $projectRoot
& $dockerPath @arguments
exit $LASTEXITCODE
