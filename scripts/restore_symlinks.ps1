# restore_symlinks.ps1
# Restores symlinks for common/ and configs/ inside ecc/app/ and graphrag/app/
# Requires: Windows Developer Mode enabled OR run as Administrator

param(
    [switch]$EnableDevMode  # Pass -EnableDevMode to also enable Developer Mode (requires Admin)
)

$repoRoot = $PSScriptRoot | Split-Path -Parent

# Optionally enable Developer Mode (requires Admin)
if ($EnableDevMode) {
    $regPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
    if (-not (Test-Path $regPath)) {
        New-Item -Path $regPath -Force | Out-Null
    }
    Set-ItemProperty -Path $regPath -Name "AllowDevelopmentWithoutDevLicense" -Value 1 -Type DWord -Force
    Write-Host "[OK] Developer Mode enabled via registry." -ForegroundColor Green
}

# Verify symlink creation is possible
try {
    $testLink = Join-Path $repoRoot "_symlink_test_"
    New-Item -ItemType SymbolicLink -Path $testLink -Target $repoRoot -ErrorAction Stop | Out-Null
    Remove-Item $testLink -Force
} catch {
    Write-Host ""
    Write-Host "[ERROR] Cannot create symlinks." -ForegroundColor Red
    Write-Host "  Option 1: Enable Windows Developer Mode in Settings -> System -> For developers" -ForegroundColor Yellow
    Write-Host "  Option 2: Run this script as Administrator with -EnableDevMode flag:" -ForegroundColor Yellow
    Write-Host "            powershell -ExecutionPolicy Bypass -File scripts\restore_symlinks.ps1 -EnableDevMode" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# Enable git core.symlinks for this repo
Set-Location $repoRoot
git config core.symlinks true
Write-Host "[OK] git config core.symlinks = true" -ForegroundColor Green

# Define symlinks to restore: [link path] -> [target relative to link's parent]
$symlinks = @(
    @{ Link = "ecc\app\common";     Target = "..\..\common"  },
    @{ Link = "ecc\app\configs";    Target = "..\..\configs" },
    @{ Link = "graphrag\app\common";  Target = "..\..\common"  },
    @{ Link = "graphrag\app\configs"; Target = "..\..\configs" }
)

foreach ($s in $symlinks) {
    $linkPath   = Join-Path $repoRoot $s.Link
    $targetPath = Join-Path (Split-Path $linkPath -Parent) $s.Target

    # Remove existing file/folder/broken symlink
    if (Test-Path $linkPath -ErrorAction SilentlyContinue) {
        $item = Get-Item $linkPath -Force -ErrorAction SilentlyContinue
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            Write-Host "  Removing existing symlink: $($s.Link)"
        } else {
            Write-Host "  Removing fake file (was text): $($s.Link)"
        }
        Remove-Item $linkPath -Force -Recurse -ErrorAction SilentlyContinue
    }

    # Create the symlink
    $resolved = Resolve-Path $targetPath -ErrorAction SilentlyContinue
    if (-not $resolved) {
        Write-Host "[WARN] Target does not exist: $targetPath — skipping $($s.Link)" -ForegroundColor Yellow
        continue
    }

    New-Item -ItemType SymbolicLink -Path $linkPath -Target $targetPath -ErrorAction Stop | Out-Null
    Write-Host "[OK] Created symlink: $($s.Link)  ->  $($s.Target)" -ForegroundColor Green
}

# Tell git about the restored symlinks
Write-Host ""
Write-Host "Updating git index..."
git update-index --no-assume-unchanged ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs 2>$null
git checkout HEAD -- ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs
Write-Host ""
Write-Host "Done! Verify with: git status" -ForegroundColor Cyan
git status --short ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs
