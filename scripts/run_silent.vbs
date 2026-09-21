' VBScript silent launcher for pythonw.exe scripts.
' Runs pythonw.exe with the window hidden, captures stdout/stderr, and writes
' them to a log file. Uses Exec (not Shell.Run) so no console window appears.
' This replaces the .bat wrapper that briefly flashes a cmd.exe window.
'
' Usage: cscript //nologo run_silent.vbs <script_path> <log_path>
'
' Returns the python script's exit code.

Set objFSO   = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 2 Then
  WScript.Quit 2
End If

scriptPath = objFSO.GetAbsolutePathName(WScript.Arguments(0))
logPath   = objFSO.GetAbsolutePathName(WScript.Arguments(1))
pyw       = "C:\Program Files\Python312\pythonw.exe"

' Build the command. We DO NOT pass redirection syntax through Exec — it
' doesn't honour shell redirection. Instead we capture stdout/stderr below.
cmd = """" & pyw & """ """ & scriptPath & """"

' Append to log file (open in text mode for Unicode safety)
Set objLog = objFSO.OpenTextFile(logPath, 8, True, -1)  ' 8 = ForAppending, -1 = Unicode

' Stamp the run
objLog.WriteLine ""
objLog.WriteLine "===== run_silent start " & Now() & " pid=" & objShell.Exec(cmd).ProcessID & " ====="

' Execute and capture stdout/stderr (Exec opens its own pipes)
Set objExec = objShell.Exec(cmd)

' Read stdout
Do Until objExec.StdOut.AtEndOfStream
  objLog.WriteLine objExec.StdOut.ReadLine
Loop

' Read stderr
Do Until objExec.StdErr.AtEndOfStream
  objLog.WriteLine objExec.StdErr.ReadLine
Loop

' Wait for exit
Do While objExec.Status = 0
  WScript.Sleep 100
Loop

objLog.WriteLine "===== run_silent exit=" & objExec.ExitCode & " ====="
objLog.Close

WScript.Quit objExec.ExitCode
