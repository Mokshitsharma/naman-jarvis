# Restarts Naman (the orb, the control center, automation, Telegram) from this project folder.
$project = Split-Path -Parent $PSScriptRoot
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'naman start' -or $_.CommandLine -match 'ToolForge\\examples\\(xkcd|pokeapi)\\server\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800
Start-Process -FilePath (Join-Path $project ".venv\Scripts\pythonw.exe") -ArgumentList "-m", "naman", "start", "--no-greet" -WorkingDirectory $project
"restarted"
