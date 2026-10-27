# Downloads what the tray app needs for local GPU processing (run once in
# PowerShell; no admin rights needed):
#   - llama.cpp's server, CUDA 13 build (works with RTX 50xx / Blackwell; needs
#     a current NVIDIA driver)
#   - Qwen3-VL 8B Instruct (Q8_0, ~8.7 GB) and its vision projector (~1.2 GB)
# into %LOCALAPPDATA%\CalendarSend. The app finds them there by itself.
#
#   powershell -ExecutionPolicy Bypass -File clients\setup_local_gpu.ps1
#   powershell -ExecutionPolicy Bypass -File clients\setup_local_gpu.ps1 -Quant Q4_K_M   # smaller, ~5 GB
param(
    [string]$Release = "b11804",
    [ValidateSet("Q8_0", "Q4_K_M")] [string]$Quant = "Q8_0"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # much faster Invoke-WebRequest
$base = Join-Path $env:LOCALAPPDATA "CalendarSend"
$llamaDir = Join-Path $base "llama"
$modelDir = Join-Path $base "models"
New-Item -ItemType Directory -Force -Path $llamaDir, $modelDir | Out-Null

# --- llama.cpp (CUDA 13, x64) -----------------------------------------------------
$server = Join-Path $llamaDir "llama-server.exe"
if (-not (Test-Path $server)) {
    Write-Host "Looking up llama.cpp $Release ..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$Release"
    $bin = $rel.assets | Where-Object { $_.name -match "^llama-.*bin-win-cuda-13[.0-9]*-x64\.zip$" } | Select-Object -First 1
    $rt = $rel.assets | Where-Object { $_.name -match "^cudart-.*win-cuda-13[.0-9]*-x64\.zip$" } | Select-Object -First 1
    if (-not $bin -or -not $rt) {
        throw "No CUDA 13 Windows build found in release $Release. Download it by hand from https://github.com/ggml-org/llama.cpp/releases and unzip both files into $llamaDir"
    }
    foreach ($asset in @($bin, $rt)) {
        $zip = Join-Path $env:TEMP $asset.name
        Write-Host "Downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB) ..."
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip
        Expand-Archive $zip -DestinationPath $llamaDir -Force
        Remove-Item $zip
    }
    # Some archives contain a top-level folder; move its contents up.
    if (-not (Test-Path $server)) {
        $found = Get-ChildItem $llamaDir -Recurse -Filter "llama-server.exe" | Select-Object -First 1
        if ($found) { Get-ChildItem $found.DirectoryName | Move-Item -Destination $llamaDir -Force }
    }
    if (-not (Test-Path $server)) { throw "llama-server.exe not found after unpacking" }
}
Write-Host "llama-server: $server"

# --- Model ---------------------------------------------------------------------------
$hf = "https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main"
$files = @("Qwen3VL-8B-Instruct-$Quant.gguf", "mmproj-Qwen3VL-8B-Instruct-F16.gguf")
foreach ($name in $files) {
    $target = Join-Path $modelDir $name
    if (Test-Path $target) { Write-Host "$name already there"; continue }
    Write-Host "Downloading $name (several GB, takes a while) ..."
    curl.exe -fL --retry 3 -C - -o "$target.part" "$hf/$name"
    if ($LASTEXITCODE -ne 0) { throw "download of $name failed" }
    Move-Item "$target.part" $target -Force
}

# --- Quick check: does the server start on the GPU? ------------------------------------
Write-Host "Checking that llama-server sees the GPU ..."
& $server --list-devices 2>&1 | Select-String -Pattern "CUDA|Vulkan|NVIDIA" | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "Done. In the tray app: Settings -> Local GPU -> tick 'Process on this PC's GPU'."
Write-Host "The paths are filled in automatically:"
Write-Host "  llama-server.exe : $server"
Write-Host "  Model            : $(Join-Path $modelDir $files[0])"
Write-Host "  Vision projector : $(Join-Path $modelDir $files[1])"
