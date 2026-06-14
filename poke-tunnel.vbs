' Hidden launcher (no console window) for the Poke tunnel.
' Forwards Poke -> Beeper Desktop's local MCP so Poke can read your chats.
' Resolves its own folder so it works wherever the repo is cloned.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & dir & "\poke-tunnel.bat""", 0, False
