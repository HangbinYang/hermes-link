$ErrorActionPreference = "Stop"

$defaultGithubRepository = "HangbinYang/hermes-link"
$defaultInstallRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Hermes Link" } else { Join-Path $HOME "AppData/Local/Hermes Link" }
$installRoot = if ($env:HERMES_LINK_INSTALL_ROOT) { $env:HERMES_LINK_INSTALL_ROOT } else { $defaultInstallRoot }
$venvDir = if ($env:HERMES_LINK_VENV_DIR) { $env:HERMES_LINK_VENV_DIR } else { Join-Path $installRoot "venv" }
$githubRepository = if ($env:HERMES_LINK_GITHUB_REPOSITORY) { $env:HERMES_LINK_GITHUB_REPOSITORY } else { $defaultGithubRepository }
$ref = if ($env:HERMES_LINK_REF) { $env:HERMES_LINK_REF } else { "main" }
$refType = if ($env:HERMES_LINK_REF_TYPE) { $env:HERMES_LINK_REF_TYPE } else { "branch" }
$packageSpec = if ($env:HERMES_LINK_PACKAGE_SPEC) { $env:HERMES_LINK_PACKAGE_SPEC } else { $null }
$restartAfterUpdate = if ($env:HERMES_LINK_RESTART_AFTER_UPDATE) { $env:HERMES_LINK_RESTART_AFTER_UPDATE } else { "1" }

function Get-DefaultPackageSpec {
  switch ($refType) {
    "branch" { return "https://github.com/$githubRepository/archive/refs/heads/$ref.tar.gz" }
    "tag" { return "https://github.com/$githubRepository/archive/refs/tags/$ref.tar.gz" }
    "commit" { return "git+https://github.com/$githubRepository.git@$ref" }
    default { throw "Unsupported HERMES_LINK_REF_TYPE: $refType. Use branch, tag, or commit." }
  }
}

$cli = Join-Path $venvDir "Scripts/hermes-link.exe"
if (-not (Test-Path $cli)) {
  throw "Hermes Link is not installed in $venvDir. Run scripts/install.ps1 first."
}

if (-not $packageSpec) {
  $packageSpec = Get-DefaultPackageSpec
}

$status = & $cli status --json | ConvertFrom-Json
$wasRunning = [bool]$status.service.running
& $cli update --spec $packageSpec

if ($restartAfterUpdate -eq "1" -and $wasRunning) {
  & $cli restart
}

Write-Host "Hermes Link updated."
Write-Host "  source: $packageSpec"
Write-Host "  cli:  $cli"
