' VBScript silent launcher for powershell scripts (hidden window).
' Mirrors run_silent.vbs but invokes powershell.exe with a hidden window
' instead of pythonw.exe. Captures stdout/stderr to a log file.
'
' Usage: cscript //nologo run_silent_ps.vbs <ps1_path> <log_path>

Set objFSO   = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 2 Then
  WScript.Quit 2
End If

scriptPath = objFSO.GetAbsolutePathName(WScript.Arguments(0))
logPath   = objFSO.GetAbsolutePathName(WScript.Arguments(1))
psExe     = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

' Build command. -WindowStyle Hidden ensures the powershell window never
' appears, even though the underlying binary is console-subsystem.
cmd = """" & psExe & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """"

' Open log file (append, Unicode)
Set objLog = objFSO.OpenTextFile(logPath, 8, True, -1)

objLog.WriteLine ""
objLog.WriteLine "===== run_silent_ps start " & Now() & " ====="

' Exec the powershell — this creates a hidden console
Set objExec = objShell.Exec(cmd)

Do Until objExec.StdOut.AtEndOfStream
  objLog.WriteLine objExec.StdOut.ReadLine
Loop
Do Until objExec.StdErr.AtEndOfStream
  objLog.WriteLine objExec.StdErr.ReadLine
Loop

Do While objExec.Status = 0
  WScript.Sleep 100
Loop

objLog.WriteLine "===== run_silent_ps exit=" & objExec.ExitCode & " ====="
objLog.Close

WScript.Quit objExec.ExitCode
