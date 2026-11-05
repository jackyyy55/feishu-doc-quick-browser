' 飞书工作日志桌面贴片 —— 启动入口（双击即可，无命令行窗口）
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

appDir = "C:\Users\Administrator\WorkBuddy\2026-11-04-11-23-20\feishu-log-widget"
pyw = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"

If Not fso.FileExists(pyw) Then pyw = "pythonw.exe"

sh.CurrentDirectory = appDir
sh.Run """" & pyw & """ """ & appDir & "\app.py""", 1, False
