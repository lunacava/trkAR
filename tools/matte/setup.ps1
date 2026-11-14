# Set up the trkAR Matte relay on Windows: Python 3.12 and the libraries go into .venv in this folder.
# Everything is downloaded on this computer from the official sources (uv from astral.sh, Python builds and the
# libraries from PyPI); nothing is redistributed by trkAR. The trkAR Camera add-on runs this from "Set Up RVM Relay".
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# - uv (https://docs.astral.sh/uv/) is installed if it is not found
# - NVIDIA GPU (nvidia-smi works): onnxruntime-gpu. Otherwise onnxruntime-directml (DirectML, most GPUs)
# - The RVM model is downloaded into models\ only if it is missing (the Releases zip already has it)
# - The last line is SETUP_OK on success
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
  $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (Test-Path $candidate) { $uv = $candidate }
}
if (-not $uv) {
  Write-Output "installing uv (https://astral.sh/uv)"
  $env:UV_NO_MODIFY_PATH = "1"
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
}
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Output "creating .venv (Python 3.12)"
  & $uv venv -p 3.12 .venv
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$packages = @("numpy", "onnxruntime-directml", "pillow")
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
  & $smi.Source *> $null
  if ($LASTEXITCODE -eq 0) { $packages = @("numpy", "onnxruntime-gpu", "pillow") }
}
Write-Output ("installing " + ($packages -join " "))
& $uv pip install -q -p .venv @packages
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
New-Item -ItemType Directory -Force -Path models | Out-Null
$model = "models\rvm_mobilenetv3_fp32.onnx"
if (-not (Test-Path $model) -or (Get-Item $model).Length -eq 0) {
  Write-Output "downloading $model"
  Invoke-WebRequest -UseBasicParsing -OutFile $model `
    "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx"
}
Write-Output "SETUP_OK"
