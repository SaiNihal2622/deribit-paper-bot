' crypto_supervisor_wrapper.vbs
' Boot-time survival wrapper for the crypto-options-bot supervisor.
' Drops to startup folder for logon-time recovery:
'   Copy-Item system\crypto_supervisor_wrapper.vbs "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\"
' Runs powershell.exe hidden, which runs crypto_supervisor_loop.ps1.
' If ANY layer dies (bot, NSSM service, scheduled task), the layer below re-launches it.

Set WshShell = CreateObject("WScript.Shell")
strProjectRoot = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
strCmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & strProjectRoot & "\system\crypto_supervisor_loop.ps1"""

' WshShell.Run: 0 = hidden window, False = don't wait for completion
WshShell.Run strCmd, 0, False
