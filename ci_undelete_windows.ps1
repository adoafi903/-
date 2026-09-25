# Make real NTFS and exFAT drives, fragment the free space, let Windows delete
# photos and a video, then recover them from the raw volume and the raw disk.
$ErrorActionPreference = 'Continue'
python ci_undelete_check.py prepare testfiles
# External HDDs do not support TRIM, so deleted data stays on the disk. The test
# disks here would honour TRIM and zero it, so switch TRIM off to match an HDD.
fsutil behavior set DisableDeleteNotify 1
$failed = 0
foreach ($fs in @('NTFS', 'exFAT')) {
  $letter = if ($fs -eq 'NTFS') { 'R' } else { 'S' }
  $vhd = Join-Path $env:RUNNER_TEMP "test_$fs.vhdx"
  "create vdisk file=`"$vhd`" maximum=400 type=expandable`r`nattach vdisk`r`ncreate partition primary`r`nformat fs=$fs quick label=TEST`r`nassign letter=$letter" | Out-File -Encoding ascii dp.txt
  diskpart /s dp.txt | Out-Null
  $i = 0
  while ($i -lt 3000) {
    $i++
    fsutil file createnew "$($letter):\fill$i.bin" 1048576 | Out-Null
    if ($LASTEXITCODE -ne 0) { break }
  }
  Get-ChildItem "$($letter):\fill*.bin" | Where-Object { [int]($_.BaseName -replace 'fill', '') % 2 -eq 0 } | Remove-Item -Force
  New-Item -ItemType Directory "$($letter):\DCIM\100CANON" | Out-Null
  Copy-Item testfiles\* "$($letter):\DCIM\100CANON\"
  Remove-Item "$($letter):\DCIM" -Recurse -Force
  Write-VolumeCache -DriveLetter $letter
  "$fs volume: filled $i MB, then deleted DCIM"
  python ci_undelete_check.py check "\\.\$($letter):" expected.json $fs
  if ($LASTEXITCODE -ne 0) { $failed++ }
  $disk = (Get-Partition -DriveLetter $letter).DiskNumber
  python ci_undelete_check.py check "\\.\PhysicalDrive$disk" expected.json $fs
  if ($LASTEXITCODE -ne 0) { $failed++ }
  "select vdisk file=`"$vhd`"`r`ndetach vdisk" | Out-File -Encoding ascii dp.txt
  diskpart /s dp.txt | Out-Null
}
exit $failed
