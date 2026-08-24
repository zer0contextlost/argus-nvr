# open-browser.ps1 — open Sentinel in the default browser after install.
Start-Sleep -Seconds 5   # give the service a moment to start
Start-Process "http://localhost:8080"
