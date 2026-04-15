$ErrorActionPreference = "Stop"

$defaultGithubRepository = "HangbinYang/hermes-link"
$defaultInstallRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Hermes Link" } else { Join-Path $HOME "AppData/Local/Hermes Link" }
$installRoot = if ($env:HERMES_LINK_INSTALL_ROOT) { $env:HERMES_LINK_INSTALL_ROOT } else { $defaultInstallRoot }
$venvDir = if ($env:HERMES_LINK_VENV_DIR) { $env:HERMES_LINK_VENV_DIR } else { Join-Path $installRoot "venv" }
$githubRepository = if ($env:HERMES_LINK_GITHUB_REPOSITORY) { $env:HERMES_LINK_GITHUB_REPOSITORY } else { $defaultGithubRepository }
$ref = if ($env:HERMES_LINK_REF) { $env:HERMES_LINK_REF } else { "main" }
$refType = if ($env:HERMES_LINK_REF_TYPE) { $env:HERMES_LINK_REF_TYPE } else { "branch" }
$packageSpec = if ($env:HERMES_LINK_PACKAGE_SPEC) { $env:HERMES_LINK_PACKAGE_SPEC } else { $null }
$enableAutostart = if ($env:HERMES_LINK_ENABLE_AUTOSTART) { $env:HERMES_LINK_ENABLE_AUTOSTART } else { "0" }
$startAfterInstall = if ($env:HERMES_LINK_START_AFTER_INSTALL) { $env:HERMES_LINK_START_AFTER_INSTALL } else { "1" }

function Get-PythonCommand {
  if ($env:PYTHON) {
    return $env:PYTHON
  }

  foreach ($candidate in @("python", "python3")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
      return $candidate
    }
  }

  throw "Python 3.11 or newer is required, but no python executable was found."
}

function Get-DefaultPackageSpec {
  switch ($refType) {
    "branch" { return "https://github.com/$githubRepository/archive/refs/heads/$ref.tar.gz" }
    "tag" { return "https://github.com/$githubRepository/archive/refs/tags/$ref.tar.gz" }
    "commit" { return "git+https://github.com/$githubRepository.git@$ref" }
    default { throw "Unsupported HERMES_LINK_REF_TYPE: $refType. Use branch, tag, or commit." }
  }
}

$python = Get-PythonCommand

if (-not $packageSpec) {
  $packageSpec = Get-DefaultPackageSpec
}

& $python -c "import sys; raise SystemExit('Hermes Link requires Python 3.11 or newer.') if sys.version_info < (3, 11) else None"

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
& $python -m venv $venvDir
& (Join-Path $venvDir "Scripts/python.exe") -m pip install --upgrade pip
& (Join-Path $venvDir "Scripts/pip.exe") install --upgrade $packageSpec
$installArgs = @("install")
$installArgs += if ($startAfterInstall -eq "1") { "--start" } else { "--no-start" }
$installArgs += if ($enableAutostart -eq "1") { "--autostart" } else { "--no-autostart" }
& (Join-Path $venvDir "Scripts/hermes-link.exe") @installArgs

Write-Host "Hermes Link installed."
Write-Host "  source: $packageSpec"
Write-Host "  venv: $venvDir"
Write-Host "  cli:  $(Join-Path $venvDir 'Scripts/hermes-link.exe')"
