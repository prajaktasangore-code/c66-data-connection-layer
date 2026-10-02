<#
.SYNOPSIS
    Builds the enterprise-connectors handoff bundle for other developers.

.DESCRIPTION
    Produces, under dist\:
      enterprise-connectors-<version>\          the folder to hand over
        enterprise_connectors-<version>-py3-none-any.whl
        HOW_TO_CONNECT_AND_FETCH_DATA.md
        connections.example.yaml
        .env.example
      enterprise-connectors-<version>.zip      the same folder, zipped

    The bundle contains the package only: no tests, harness, reports or secrets. The build stops if the
    wheel contains anything other than the package, or if a real .env would be included.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_package.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path "pyproject.toml" -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $root "dist"
$name = "enterprise-connectors-$version"
$bundle = Join-Path $dist $name
$wheelName = "enterprise_connectors-$version-py3-none-any.whl"
Write-Host "Building $name ..."

# Fresh output for this version only (older versions in dist\ are kept).
foreach ($old in @($bundle, "$bundle.zip", (Join-Path $dist $wheelName))) {
    if (Test-Path $old) { Remove-Item -Recurse -Force $old }
}
New-Item -ItemType Directory -Force $dist | Out-Null

# 1. Build the wheel (pip runs the hatchling backend in an isolated environment).
py -m pip wheel . --no-deps --quiet --disable-pip-version-check -w $dist
if ($LASTEXITCODE -ne 0) { throw "pip wheel failed" }
$wheel = Join-Path $dist $wheelName
if (-not (Test-Path $wheel)) { throw "Expected wheel not found: $wheel" }

# 2. Safety check: only the package may be inside the wheel.
$check = @'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names if not (n.startswith("enterprise_connectors/") or n.startswith("enterprise_connectors-"))
       or "/tests/" in n or "connector_tests" in n or n.endswith(".env") or "test_reports" in n]
print(f"wheel files: {len(names)}")
if bad:
    print("unexpected files in wheel:", *bad, sep="\n  ")
    sys.exit(1)
'@
$check | py - $wheel
if ($LASTEXITCODE -ne 0) { throw "Wheel contains files that must not ship" }

# 3. Assemble the bundle.
New-Item -ItemType Directory -Force $bundle | Out-Null
Copy-Item $wheel $bundle
Copy-Item "HOW_TO_CONNECT_AND_FETCH_DATA.md" $bundle
Copy-Item "connections.yaml" (Join-Path $bundle "connections.example.yaml")
Copy-Item ".env.example" $bundle

# 4. Safety check: the templates must not contain real values from .env.
if (Test-Path ".env") {
    $secretCheck = @'
import sys, pathlib
env = {}
for line in pathlib.Path(".env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, _, v = line.partition("="); env[k.strip()] = v.strip().strip("'\"")
sys.path.insert(0, "src")
from enterprise_connectors.registry.connector_registry import BUILTIN_CONNECTORS
public = set(BUILTIN_CONNECTORS)  # connector type names appear in the docs by design
secret = [v for k, v in env.items() if any(w in k.upper() for w in ("PASSWORD", "SECRET", "TOKEN", "KEY")) and len(v) >= 4]
for v in [v for v in secret if v in public]:
    print("WARNING: a password in .env is the same as a connector type name; it cannot be checked and is weak - change it")
secret = [v for v in secret if v not in public]
leaks = [f.name for f in pathlib.Path(sys.argv[1]).iterdir() if f.suffix != ".whl"
         and any(s in f.read_text(encoding="utf-8") for s in secret)]
if leaks:
    print("real secret values found in:", ", ".join(leaks)); sys.exit(1)
print(f"secret check: {len(secret)} value(s) checked, none found in the bundle")
'@
    $secretCheck | py - $bundle
    if ($LASTEXITCODE -ne 0) { throw "Bundle contains a real secret value; fix the template files" }
}

# 5. Zip it.
Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath "$bundle.zip" -Force

Write-Host ""
Write-Host "Done. Hand over either of these:"
Write-Host "  $bundle\"
Write-Host "  $bundle.zip"
Get-ChildItem $bundle -Force | Format-Table Name, Length -AutoSize
