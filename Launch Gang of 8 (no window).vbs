' Gang of 8 — elegant windowless launcher.
' Double-click in Explorer: starts the dashboard server hidden (no console
' window) and opens the dashboard in your browser. Use "Stop Gang of 8.bat"
' to stop it (there's no window to close).

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root

' Stop whatever is listening on the dashboard port before launching. This is
' the same target used by Stop Gang of 8.bat. PowerShell avoids the hidden
' cmd.exe console wait that can stall a windowless launcher.
ps = sh.ExpandEnvironmentStrings( _
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
stopCommand = _
    "$pids = @(Get-NetTCPConnection -LocalPort 8790 -State Listen " & _
    "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty " & _
    "OwningProcess -Unique); foreach ($processId in $pids) { " & _
    "Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }"
sh.Run """" & ps & """ -NoLogo -NoProfile -NonInteractive " & _
    "-WindowStyle Hidden -Command """ & stopCommand & """", 0, True
WScript.Sleep 250

py = root & "\.venv\Scripts\python.exe"
If Not fso.FileExists(py) Then py = "python"

' backend: "cli" = real local agents (costs tokens) | "mock" = free/offline
backend = "cli"

' start the server hidden (0 = no window), don't wait
sh.Run """" & py & """ cli.py serve --backend " & backend, 0, False

' give it a moment to bind, then open the dashboard
WScript.Sleep 3000
explorer = sh.ExpandEnvironmentStrings("%SystemRoot%\explorer.exe")
sh.Run """" & explorer & """ ""http://127.0.0.1:8790/""", 1, False
