# Builds the standalone Windows app into dist\BotondsProtocolSimulator and zips it.
# Usage:  .\build.ps1          (needs the .venv with requirements.txt + pyinstaller installed)
# native tools log to stderr, so failures are detected via $LASTEXITCODE rather than ErrorActionPreference
Set-Location $PSScriptRoot

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No .venv found. Create it first: python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt pyinstaller" }
& $py -m pip show pyinstaller *> $null
if ($LASTEXITCODE -ne 0) { & $py -m pip install pyinstaller }

$version = (& $py -c "import simulator; print(simulator.__version__)").Trim()
$name = "BotondsProtocolSimulator"
$out = "dist\$name"

& $py -m PyInstaller simulator.spec --noconfirm --clean 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item config.yaml "$out\config.yaml" -Force
Copy-Item README.md "$out\README.md" -Force
@"
Botond's Industrial Data Protocol Simulator $version

Run BotondsProtocolSimulator.exe, then open http://127.0.0.1:8088
Settings are stored in config.yaml next to the exe.

  BotondsProtocolSimulator.exe --port 9000 --host 0.0.0.0   (UI on another port / from the network)
  BotondsProtocolSimulator.exe --headless                   (no UI, start all enabled instances)
"@ | Set-Content "$out\START_HERE.txt" -Encoding utf8

$zip = "dist\$name-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
# .NET zip with retries: freshly built files are often briefly locked by antivirus scanning
Add-Type -AssemblyName System.IO.Compression.FileSystem
for ($i = 1; $i -le 5; $i++) {
    try {
        if (Test-Path $zip) { Remove-Item $zip }
        [System.IO.Compression.ZipFile]::CreateFromDirectory((Resolve-Path $out), (Join-Path (Resolve-Path dist) "$name-$version-win64.zip"), "Optimal", $true)
        break
    } catch {
        if ($i -eq 5) { throw "Zipping failed: $_" }
        Start-Sleep -Seconds 3
    }
}
Write-Host "`nBuilt $out\$name.exe and $zip"
