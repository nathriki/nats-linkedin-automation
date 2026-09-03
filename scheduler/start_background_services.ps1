# Starts NATS and the Telegram approval bot hidden, at Windows login.
# Placed in the Startup folder so it runs automatically -- doesn't need
# admin rights, unlike Task Scheduler registration on this machine.

$workDir = "Q:\Supern4thers\W0RK\Coresix EU\AI Automation Test\nats-linkedin-automation"
$natsPath = "C:\Users\wafum\AppData\Local\Microsoft\WinGet\Packages\NATSAuthors.NATSServer_Microsoft.Winget.Source_8wekyb3d8bbwe\nats-server-v2.14.6-windows-amd64\nats-server.exe"
$pythonPath = "$workDir\.venv\Scripts\python.exe"

# Give NATS a moment's head start so the bot has something to connect to.
Start-Process -FilePath $natsPath -ArgumentList "-c", "nats\nats-server.conf" `
    -WorkingDirectory $workDir -WindowStyle Hidden `
    -RedirectStandardOutput "$workDir\nats\nats-server.log" `
    -RedirectStandardError "$workDir\nats\nats-server.err.log"

Start-Sleep -Seconds 5

Start-Process -FilePath $pythonPath -ArgumentList "approval_bot\telegram_approval_bot.py" `
    -WorkingDirectory $workDir -WindowStyle Hidden `
    -RedirectStandardOutput "$workDir\approval_bot\bot.log" `
    -RedirectStandardError "$workDir\approval_bot\bot.err.log"
