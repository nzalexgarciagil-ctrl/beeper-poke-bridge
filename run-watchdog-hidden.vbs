' Launches watchdog.ps1 with no console window (run mode 0 = hidden).
' Used by the PokeBridge scheduled task so nothing flashes once a minute.
Dim sh, here
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & here & "\watchdog.ps1""", 0, False
