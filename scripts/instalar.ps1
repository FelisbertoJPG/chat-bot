# Prepara o projeto em uma maquina Windows: confere Python e Ollama, baixa os modelos
# e cria o ambiente Python. Pode rodar mais de uma vez: o que ja existe e pulado.

$ErrorActionPreference = "Continue"
$raiz = Split-Path $PSScriptRoot -Parent
$modelos = @("qwen3.5:4b", "qwen3-embedding:0.6b")

function Passo($texto) { Write-Host ""; Write-Host "== $texto" -ForegroundColor Cyan }

Passo "1/4 Python"
$pyExe = $null; $pyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = "python" }
if (-not $pyExe) {
    Write-Host "Python nao encontrado. Instale o Python 3.10+ em https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}
& $pyExe @pyArgs --version

Passo "2/4 Ollama"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) {
    Write-Host "Ollama nao encontrado. Instale em https://ollama.com/download e rode este script de novo." -ForegroundColor Red
    exit 1
}
function OllamaNoAr { try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $true } catch { $false } }
if (-not (OllamaNoAr)) {
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (OllamaNoAr); $i++) { Start-Sleep -Seconds 1 }
}
Write-Host "Ollama no ar"

Passo "3/4 Modelos (cerca de 3,8 GB na primeira vez)"
$instalados = (Invoke-RestMethod "http://127.0.0.1:11434/api/tags").models | ForEach-Object { $_.name }
foreach ($m in $modelos) {
    if ($instalados -contains $m) { Write-Host "OK  $m" -ForegroundColor Green }
    else { & $ollama pull $m }
}

Passo "4/4 Bibliotecas Python"
$venvPython = Join-Path $raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & $pyExe @pyArgs -m venv (Join-Path $raiz ".venv") }
& $venvPython -m pip install -q -r (Join-Path $raiz "requirements.txt")
& $venvPython -c "import flask, ollama, pypdf, anthropic; print('Bibliotecas OK')"

Write-Host ""
Write-Host "Pronto. Para abrir o chatbot: iniciar.bat" -ForegroundColor Green
