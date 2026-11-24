# 在桌面创建"代码检查助手"快捷方式（每次启动都会刷新，文件夹搬家后也能自动修正）
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "代码检查助手.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $root "启动网页版.bat"
$lnk.WorkingDirectory = $root
$lnk.IconLocation = (Join-Path $root "app.ico") + ",0"
$lnk.Description = "代码检查助手：检查代码、运行测试、AI 讲解错误"
$lnk.Save()
