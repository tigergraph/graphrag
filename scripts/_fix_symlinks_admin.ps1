$repo = "C:\Users\VeerapunagalingamBha\Desktop\graphrag"

# Enable git symlinks
& git -C $repo config core.symlinks true

# Remove any existing items at these paths
$targets = @(
    "$repo\ecc\app\common",
    "$repo\ecc\app\configs",
    "$repo\graphrag\app\common",
    "$repo\graphrag\app\configs"
)
foreach ($t in $targets) {
    if (Test-Path $t -ErrorAction SilentlyContinue) {
        Remove-Item $t -Force -Recurse
        Write-Host "Removed: $t"
    }
}

# Use mklink /d from the correct parent dir to preserve relative targets
Set-Location "$repo\ecc\app"
cmd /c mklink /d common   ..\..\common
cmd /c mklink /d configs  ..\..\configs

Set-Location "$repo\graphrag\app"
cmd /c mklink /d common   ..\..\common
cmd /c mklink /d configs  ..\..\configs

Set-Location $repo
Write-Host ""
Write-Host "Git status:"
& git status --short ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs
Write-Host "Done. Press Enter to close."
Read-Host
