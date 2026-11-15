# 大肥鱼 · 一键启动（兜底用；正常情况下 DSH 一起来它会自己启动）
$ErrorActionPreference = 'SilentlyContinue'

$pids = @(Get-Process 'DeepSeek Harness' | ForEach-Object { $_.Id })
if (-not $pids) {
  Write-Host "DSH 没开着 —— 先把 DeepSeek Harness 打开，等 20 秒再点这个。" -ForegroundColor Yellow
  Read-Host "按回车退出"
  exit 1
}

$ports = Get-NetTCPConnection -State Listen | Where-Object { $pids -contains $_.OwningProcess } |
  Select-Object -ExpandProperty LocalPort -Unique

foreach ($p in $ports) {
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$p/dsh-wechat-bridge/state" -UseBasicParsing -TimeoutSec 2
    if ($r.StatusCode -ne 200) { continue }
    $s = $r.Content | ConvertFrom-Json
    if ($s.桥接.运行中) {
      Write-Host "大肥鱼已经在跑了（端口 $p，已运行 $($s.桥接.运行秒) 秒）" -ForegroundColor Green
      Write-Host "总开关=$($s.配置.总开关)  试运行=$($s.配置.试运行)"
      Read-Host "按回车退出"
      exit 0
    }
    $o = Invoke-RestMethod "http://127.0.0.1:$p/dsh-wechat-bridge/bridge/start" -Method Post -Body '{}' -ContentType 'application/json' -TimeoutSec 60
    Write-Host "已启动：$($o.说)（端口 $p）" -ForegroundColor Green
    Write-Host "给它 15 秒左右接通微信窗口，然后就能收发消息了。"
    Read-Host "按回车退出"
    exit 0
  } catch { }
}

Write-Host "没找到 DSH 的桥接端口 —— DSH 可能还没启动完，等一会儿再点一次。" -ForegroundColor Red
Read-Host "按回车退出"
