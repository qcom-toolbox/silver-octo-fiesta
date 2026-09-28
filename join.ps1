# Join the pieces back into the ISO and check it (Windows PowerShell).
$iso = Join-Path $PSScriptRoot 'gentoo-desktop-generic-mesa-20260927.iso'
$out = [IO.File]::Create($iso)
Get-ChildItem -Path $PSScriptRoot -Filter 'gentoo-desktop-generic-mesa-20260927.iso.*' |
    Where-Object { $_.Name -match '\.\d{3}$' } | Sort-Object Name |
    ForEach-Object { $in = [IO.File]::OpenRead($_.FullName); $in.CopyTo($out); $in.Close() }
$out.Close()
$want = ((Get-Content (Join-Path $PSScriptRoot 'SHA256SUMS')) -split ' ')[0]
$got = (Get-FileHash $iso -Algorithm SHA256).Hash.ToLower()
if ($got -eq $want) { Write-Host "OK: $iso" } else { Write-Host "CHECKSUM MISMATCH: got $got, expected $want" }
