# 在桌面创建"代码检查助手"快捷方式（每次启动都会刷新，文件夹搬家后也能自动修正）
# -PythonW <pythonw.exe 路径>：快捷方式直接运行无窗口启动器；不传则指向 启动网页版.bat
param([string]$PythonW = "")
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "代码检查助手.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
if ($PythonW -ne "" -and (Test-Path $PythonW)) {
  $lnk.TargetPath = $PythonW
  $lnk.Arguments = "-m codecheck.launcher"
} else {
  $lnk.TargetPath = Join-Path $root "启动网页版.bat"
  $lnk.Arguments = ""
}
$lnk.WorkingDirectory = $root
$lnk.IconLocation = (Join-Path $root "app.ico") + ",0"
$lnk.Description = "代码检查助手：检查代码、运行测试、AI 讲解错误"
$lnk.Save()
