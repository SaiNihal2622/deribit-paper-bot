' VBScript silent launcher — no console window ever.
' Replaces cmd.exe wrapper around pythonw.exe to eliminate the brief flash
' that schtasks.exe shows when invoking a .bat file.
'
' Usage: cscript //nologo health_check_silent.vbs <python_script> <log_path>
' Falls back to WScript.MessageBox on error so the user can see what failed.

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

If WScript.Arguments.Count < 2 Then
  WScript.Echo "usage: health_check_silent.vbs <script> <log>"
  WScript.Quit 2
End If

scriptPath = objFSO.GetAbsolutePathName(WScript.Arguments(0))
logPath   = objFSO.GetAbsolutePathName(WScript.Arguments(1))

pyw = "C:\Program Files\Python312\pythonw.exe"

' Run pythonw.exe in a hidden window. WindowStyle=0 = HIDDEN.
intWindowStyle = 0  ' HIDE
bWaitOnReturn   = True

' Redirect stdout/stderr to log file. Append so we keep history.
Set objExec = objShell.Exec("""" & pyw & """ """ & scriptPath & """ >> """ & logPath & """ 2>&1")

Do While objExec.Status = 0
  WScript.Sleep 100
Loop

WScript.Quit objExec.ExitCode
