$ErrorActionPreference = "Stop"

$defaultInstallRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Hermes Link" } else { Join-Path $HOME "AppData/Local/Hermes Link" }
$installRoot = if ($env:HERMES_LINK_INSTALL_ROOT) { $env:HERMES_LINK_INSTALL_ROOT } else { $defaultInstallRoot }
$venvDir = if ($env:HERMES_LINK_VENV_DIR) { $env:HERMES_LINK_VENV_DIR } else { Join-Path $installRoot "venv" }
$removeData = if ($env:HERMES_LINK_REMOVE_DATA) { $env:HERMES_LINK_REMOVE_DATA } else { "0" }

$cli = Join-Path $venvDir "Scripts/hermes-link.exe"
if (-not (Test-Path $cli)) {
  throw "Hermes Link is not installed in $venvDir."
}

$args = @("uninstall", "--yes")
if ($removeData -eq "1") {
  $args += "--remove-data"
}

& $cli @args
Write-Host "Hermes Link uninstalled."
