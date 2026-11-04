# Abre o chatbot: garante que o Ollama esta no ar, sobe o servidor e abre o navegador.

$raiz = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Ambiente nao instalado. Rode instalar.bat primeiro." -ForegroundColor Red
    exit 1
}

function OllamaNoAr { try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $true } catch { $false } }
if (-not (OllamaNoAr)) {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (OllamaNoAr); $i++) { Start-Sleep -Seconds 1 }
}

# Abre o navegador assim que o servidor responder.
Start-Job -ScriptBlock {
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:5000/" -UseBasicParsing -TimeoutSec 1 | Out-Null; Start-Process "http://127.0.0.1:5000"; break }
        catch { Start-Sleep -Milliseconds 500 }
    }
} | Out-Null

Write-Host "Chatbot em http://127.0.0.1:5000  (feche esta janela para parar)" -ForegroundColor Green
$env:PYTHONIOENCODING = "utf-8"
Set-Location $raiz
& $venvPython app.py
