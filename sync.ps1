# 把微信桥接的本地改动提交并推到 GitHub
# 用法：powershell -File sync.ps1 "这次改了什么"
param([string]$msg = "")
$ErrorActionPreference = 'Continue'
$git = Join-Path $env:USERPROFILE ".dsh\tools\git\cmd\git.exe"
if (-not (Test-Path $git)) { Write-Host "没找到 git：$git" -ForegroundColor Red; exit 1 }
Set-Location $PSScriptRoot

& $git add -A
$st = (& $git status --porcelain)
if (-not $st) {
  Write-Host "没有改动，不用提交。" -ForegroundColor Yellow
} else {
  if (-not $msg) { $msg = "更新：" + (Get-Date -Format "yyyy-MM-dd HH:mm") }
  & $git commit -m $msg | Out-Host
}

# 没配 remote 就只本地提交
$remote = (& $git remote) 2>$null
if (-not $remote) {
  Write-Host "还没配远程仓库（git remote add origin ...），先只提交到本地。" -ForegroundColor Yellow
  exit 0
}

& $git push origin HEAD 2>&1 | Out-Host
if ($LASTEXITCODE -eq 0) {
  Write-Host "已推送到远程。" -ForegroundColor Green
} else {
  Write-Host "推送失败（多半是凭据）：看上面 git 的报错。" -ForegroundColor Red
  exit 1
}
