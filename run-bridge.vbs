' Hidden launcher for the Beeper -> Poke bridge (no console window).
' Resolves its own folder so it works wherever the repo is cloned.
' The app writes its own rotating log to bridge.log -- no redirect needed here.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run "cmd /c """ & dir & "\run-bridge.bat""", 0, False
