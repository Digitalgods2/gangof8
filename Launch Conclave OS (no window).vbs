' Conclave OS — elegant windowless launcher.
' Double-click in Explorer: starts the dashboard server hidden (no console
' window) and opens the dashboard in your browser. Use "Stop Conclave OS.bat"
' to stop it (there's no window to close).

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

py = root & "\.venv\Scripts\python.exe"
If Not fso.FileExists(py) Then py = "python"

' backend: "cli" = real local agents (costs tokens) | "mock" = free/offline
backend = "cli"

' start the server hidden (0 = no window), don't wait
sh.Run """" & py & """ cli.py serve --backend " & backend, 0, False

' give it a moment to bind, then open the dashboard
WScript.Sleep 3000
sh.Run "http://127.0.0.1:8790/", 1, False
