' Launches start_background_services.ps1 completely hidden (no console
' flash). A COPY of this file lives in the Startup folder so it runs
' automatically at login -- deliberately using an ABSOLUTE path to the
' project's .ps1 here rather than resolving relative to this file's own
' location, since the Startup-folder copy does NOT have the .ps1 sitting
' next to it (only the project's scheduler/ directory does).
Set objShell = CreateObject("WScript.Shell")
psScript = "Q:\Supern4thers\W0RK\Coresix EU\AI Automation Test\nats-linkedin-automation\scheduler\start_background_services.ps1"
objShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & psScript & """", 0, False
