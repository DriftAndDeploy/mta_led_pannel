# Publish the firmware you just exported from Arduino IDE for over-the-air update.
#
#   1. Bump FW_BUILD in firmware/subway_display/subway_display.ino
#   2. Arduino IDE: Sketch > Export Compiled Binary
#   3. powershell -ExecutionPolicy Bypass -File tools\publish_firmware.ps1
#   4. git add/commit/push, then run deploy/update.sh on the Pi
$ErrorActionPreference = "Stop"

$repo  = Split-Path -Parent $PSScriptRoot
$build = Join-Path $repo "firmware\subway_display\build"
$dest  = Join-Path $repo "server\firmware\firmware.bin"

function Get-BuildNumber([string]$path) {
    $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($path))
    $m = [regex]::Match($text, "##MTAPANEL_BUILD=(\d+)##")
    if ($m.Success) { return [int]$m.Groups[1].Value } else { return $null }
}

$bin = Get-ChildItem $build -Recurse -Filter "subway_display.ino.bin" -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $bin) { throw "No exported binary in $build. In Arduino IDE use Sketch > Export Compiled Binary." }

$new = Get-BuildNumber $bin.FullName
if (-not $new) { throw "Build tag not found in $($bin.FullName) — is this the panel sketch?" }

if (Test-Path $dest) {
    $old = Get-BuildNumber $dest
    if ($old -and $new -le $old) {
        throw "Build $new is not newer than the published build $old. Bump FW_BUILD and export again."
    }
}

Copy-Item $bin.FullName $dest -Force
$kb = [math]::Round($bin.Length / 1KB)
Write-Host "Published build $new ($kb KB, exported $($bin.LastWriteTime)) -> server\firmware\firmware.bin"
Write-Host ""
Write-Host "Next:"
Write-Host "  git add server/firmware/firmware.bin firmware/"
Write-Host "  git commit -m `"Firmware build $new`""
Write-Host "  git push"
Write-Host "  ssh janniel-pi@raspberrypi.local '~/code/mta_led_pannel/deploy/update.sh'"
