# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        "clean-workspace",
        "clean-submodule-build-artifacts",
        "create-short-drive-mapping",
        "restore-directory-symlinks",
        "refresh-path",
        "set-rust-environment",
        "isolate-cargo-home",
        "verify-rust-toolchain",
        "setup-prerequisites",
        "install-python-dependencies",
        "check-distro-tooling",
        "build-test-nanvix-core",
        "download-pinned-distribution-guests",
        "create-distribution-images",
        "smoke-test-busybox",
        "package-release-distributions",
        "stage-release-distributions",
        "print-sccache-statistics",
        "remove-drive-mapping"
    )]
    [string]$Task
)

$ErrorActionPreference = "Stop"

function Clean-Workspace {
    # Remove leftover junctions before checkout recursively cleans the workspace.
    $nanvix = Join-Path $env:GITHUB_WORKSPACE "nanvix"
    if (Test-Path $nanvix) {
        Get-ChildItem -Path $nanvix -Recurse -Force -Directory |
            Where-Object {
                $_.Attributes -band [IO.FileAttributes]::ReparsePoint
            } |
            ForEach-Object {
                cmd /c rmdir $_.FullName 2>$null
                Write-Host "Removed junction: $($_.FullName)"
            }
    }
    if (Test-Path N:\) {
        subst N: /D
    }
}

function Clean-SubmoduleBuildArtifacts {
    git submodule foreach --recursive git clean -ffdx
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot clean submodule build artifacts"
    }
}

function Create-ShortDriveMapping {
    # Keep absolute make paths below cmd.exe's 8191-character limit.
    if (Test-Path N:\) {
        subst N: /D
    }
    subst N: $env:GITHUB_WORKSPACE
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot map N: to $env:GITHUB_WORKSPACE"
    }
    Get-Item N:\nanvix\Makefile
}

function Restore-DirectorySymlinks {
    Set-Location N:\nanvix
    git ls-files -s | ForEach-Object {
        if ($_ -match '^120000\s+\S+\s+\S+\t(.+)$') {
            $relativePath = $Matches[1]
            $fullPath = Join-Path $PWD $relativePath
            if ((Test-Path $fullPath) -and
                -not (Test-Path $fullPath -PathType Container)) {
                $target = (Get-Content $fullPath -Raw).Trim()
                if (-not $target -or $target.Length -gt 500) {
                    return
                }
                $parentDirectory = Split-Path $fullPath
                $resolved = [System.IO.Path]::GetFullPath(
                    (Join-Path $parentDirectory $target)
                )
                if (Test-Path $resolved -PathType Container) {
                    Remove-Item $fullPath -Force
                    cmd /c mklink /J "$fullPath" "$resolved" | Out-Null
                    Write-Host "Junction: $relativePath -> $target"
                }
                elseif (Test-Path $resolved -PathType Leaf) {
                    Copy-Item $resolved $fullPath -Force
                    Write-Host "Copied: $relativePath -> $target"
                }
            }
        }
    }
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:PATH = "$machinePath;$userPath"
    "PATH=$env:PATH" | Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
}

function Set-RustEnvironment {
    "CARGO_HOME=C:\Users\Administrator\.cargo" |
        Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
}

function Isolate-CargoHome {
    $cargoTarget = Join-Path $env:CARGO_HOME "bin"
    $isolatedCargo = Join-Path $env:RUNNER_TEMP ".cargo"
    New-Item -ItemType Directory -Force -Path $isolatedCargo | Out-Null
    $cargoBin = Join-Path $isolatedCargo "bin"
    if (Test-Path $cargoBin) {
        Remove-Item $cargoBin -Force -Recurse
    }
    cmd /c "mklink /J `"$cargoBin`" `"$cargoTarget`"" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot link isolated Cargo bin directory"
    }
    "CARGO_HOME=$isolatedCargo" |
        Out-File -FilePath $env:GITHUB_ENV -Append -Encoding utf8
    Write-Host "Isolated CARGO_HOME to $isolatedCargo"
}

function Verify-RustToolchain {
    Set-Location N:\nanvix
    $toolchain = (
        Get-Content rust-toolchain |
            Select-String '^channel\s*='
    ).ToString() -replace '.*=\s*"([^"]+)".*', '$1'
    Write-Host "Expected toolchain: $toolchain"
    rustup show
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot inspect the Rust toolchain"
    }
    cargo --version
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot inspect the Cargo version"
    }
}

function Setup-Prerequisites {
    Set-Location N:\nanvix
    $osVersion = [System.Environment]::OSVersion.Version
    if ($osVersion.Build -ge 22000) {
        Write-Host (
            "Detected Windows build $($osVersion.Build) " +
            "(Windows 11 or later). Running z.ps1 setup..."
        )
        .\z.ps1 setup
    }
    else {
        Write-Host (
            "Detected Windows build $($osVersion.Build) (< 22000). " +
            "Skipping z.ps1 setup on CI runner."
        )
    }
}

function Install-PythonDependencies {
    python -m pip install black pyright tomli-w
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot install Python dependencies"
    }
}

function Check-DistroTooling {
    python -m black --target-version py312 --check z.py nanvix_distro tests scripts
    if ($LASTEXITCODE -ne 0) {
        throw "Black checks failed"
    }
    python -m pyright
    if ($LASTEXITCODE -ne 0) {
        throw "Pyright checks failed"
    }
    python -m unittest discover -v
    if ($LASTEXITCODE -ne 0) {
        throw "Distro unit tests failed"
    }
}

function Build-TestNanvixCore {
    Set-Location N:\nanvix

    # Invoke make from the subst drive so $(CURDIR) remains short enough for cmd.exe.
    $env:HOME = $env:USERPROFILE -replace '\\', '/'
    $gitRoot = Split-Path (Split-Path (Get-Command git).Source)
    $gitUsrBin = Join-Path $gitRoot "usr\bin"
    if (Test-Path $gitUsrBin) {
        $env:PATH += ";$gitUsrBin"
    }
    $makeArguments = @(
        "DEPLOYMENT_MODE=standalone",
        "WHP=yes",
        "SYSROOT_DIR=N:/build/sysroot"
    )
    if ($env:RELEASE_FLAG) {
        $makeArguments += $env:RELEASE_FLAG
    }
    $testMakeArguments = $makeArguments + @(
        "all",
        "test",
        "MACHINE=$env:MACHINE_TYPE",
        "LOG_LEVEL=$env:TEST_LOG_LEVEL"
    )
    Write-Host "[INFO] make $($testMakeArguments -join ' ')"
    & make @testMakeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Build or tests failed with exit code $LASTEXITCODE"
    }

    $installMakeArguments = $makeArguments + @(
        "install",
        "MACHINE=$env:MACHINE_TYPE",
        "LOG_LEVEL=$env:LOG_LEVEL"
    )
    Write-Host "[INFO] make $($installMakeArguments -join ' ')"
    & make @installMakeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Install failed with exit code $LASTEXITCODE"
    }
}

function Download-PinnedDistributionGuests {
    $headers = @{
        Accept = "application/vnd.github+json"
        Authorization = "Bearer $env:GH_TOKEN"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $packages = @(
        @{
            Name = "busybox"
            Asset = "busybox-windows-x86-microvm-standalone-256mb.zip"
            Required = @("bin\busybox.elf")
        },
        @{
            Name = "quickjs"
            Asset = "quickjs-windows-x86-microvm-standalone-256mb.zip"
            Required = @("bin\qjs.elf")
        },
        @{
            Name = "cpython"
            Asset = "cpython-windows-x86-microvm-standalone-256mb.zip"
            Required = @("bin\python.elf", "cpython-ramfs.img")
        }
    )

    foreach ($package in $packages) {
        $name = $package.Name
        $repository = "usr/bin/$name"
        git -C $repository fetch --tags --force origin
        if ($LASTEXITCODE -ne 0) {
            throw "Cannot fetch $name release tags"
        }
        $tag = git -C $repository describe --tags --exact-match
        if ($LASTEXITCODE -ne 0 -or -not $tag) {
            throw "$name gitlink is not at a release tag"
        }

        $asset = $package.Asset
        $releaseUrl = "https://api.github.com/repos/nanvix/$name/releases/tags/$tag"
        $release = Invoke-RestMethod -Uri $releaseUrl -Headers $headers
        $matches = @($release.assets | Where-Object { $_.name -eq $asset })
        if ($matches.Count -ne 1) {
            throw "Pinned $name release does not publish exactly one $asset"
        }
        $metadata = $matches[0]
        if (-not ($metadata.digest -is [string]) -or
            -not $metadata.digest.StartsWith("sha256:")) {
            throw "Pinned $name release does not publish a SHA-256 digest for $asset"
        }

        $archive = Join-Path $env:RUNNER_TEMP $asset
        $destination = Join-Path $env:GITHUB_WORKSPACE "build\deps\$name"
        if (Test-Path $destination) {
            Remove-Item -Recurse -Force $destination
        }
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        Invoke-WebRequest -Uri $metadata.browser_download_url -OutFile $archive
        $expectedDigest = $metadata.digest.Substring("sha256:".Length)
        $actualDigest = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
        if ($actualDigest -ine $expectedDigest) {
            throw "Pinned $name archive SHA-256 digest mismatch"
        }
        Expand-Archive -Path $archive -DestinationPath $destination -Force

        foreach ($required in $package.Required) {
            $requiredPath = Join-Path $destination $required
            if (-not (Test-Path $requiredPath -PathType Leaf)) {
                throw "Pinned $name archive does not contain $required"
            }
            Get-Item $requiredPath | Format-Table FullName, Length
        }
    }
}

function Create-DistributionImages {
    python z.py --verbose dist busybox
    if ($LASTEXITCODE -ne 0) { throw "Cannot create BusyBox distribution" }
    python z.py --verbose dist javascript
    if ($LASTEXITCODE -ne 0) { throw "Cannot create JavaScript distribution" }
    python z.py --verbose dist python
    if ($LASTEXITCODE -ne 0) { throw "Cannot create Python distribution" }
    python z.py --verbose menuconfig ci-composed --include all
    if ($LASTEXITCODE -ne 0) { throw "Cannot create composed distribution" }
}

function Smoke-TestBusybox {
    $distribution = Join-Path $env:GITHUB_WORKSPACE "build\dist\busybox"
    $standardInput = Join-Path $env:RUNNER_TEMP "busybox-stdin.txt"
    $standardOutput = Join-Path $env:RUNNER_TEMP "busybox-stdout.txt"
    $standardError = Join-Path $env:RUNNER_TEMP "busybox-stderr.txt"
    $log = Join-Path $distribution "busybox-smoke.log"
    Set-Content -Path $standardInput -Value "exit" -Encoding ascii

    $process = Start-Process -FilePath (Get-Command python).Source `
        -ArgumentList @("z.py", "--verbose", "run", "busybox") `
        -WorkingDirectory $env:GITHUB_WORKSPACE `
        -PassThru -NoNewWindow `
        -RedirectStandardInput $standardInput `
        -RedirectStandardOutput $standardOutput `
        -RedirectStandardError $standardError

    if (-not $process.WaitForExit(120000)) {
        $process.Kill($true)
        throw "WHP smoke test timed out after 120 seconds"
    }
    $process.WaitForExit()

    Get-Content $standardOutput, $standardError -ErrorAction SilentlyContinue |
        Tee-Object -FilePath $log
    if ($process.ExitCode -ne 0) {
        throw "WHP smoke test failed with exit code $($process.ExitCode)"
    }
    if (-not (Select-String -Path $log -Pattern "NANVIX_BUSYBOX_READY" -Quiet)) {
        throw "WHP smoke marker not found"
    }
}

function New-DistributionPackage {
    param(
        [string]$Profile,
        [string]$Components,
        [DateTime]$CommitTimestamp,
        [string]$ReleaseDirectory
    )

    $source = Join-Path $env:GITHUB_WORKSPACE "build\dist\$Profile"
    $archiveName = (
        "nanvix-distro-windows-x86-microvm-256mb-" +
        "$Components-$($env:GITHUB_SHA).zip"
    )
    $archive = Join-Path $ReleaseDirectory $archiveName
    $required = @(
        "nanvixd.exe",
        "bin\kernel.elf",
        "bin\nanvix.initrd",
        "bin\nanvix.ramfs"
    )

    if (-not (Test-Path $source -PathType Container)) {
        throw "Distribution output not found: $source"
    }
    foreach ($path in $required) {
        if (-not (Test-Path (Join-Path $source $path) -PathType Leaf)) {
            throw "Distribution artifact not found: $Profile/$path"
        }
    }

    Get-ChildItem -LiteralPath $source -Recurse -Force | ForEach-Object {
        $_.LastWriteTimeUtc = $CommitTimestamp
    }
    if (Test-Path $archive) {
        Remove-Item -Force $archive
    }
    Push-Location $source
    try {
        Compress-Archive `
            -Path @("nanvixd.exe", "bin") `
            -DestinationPath $archive `
            -CompressionLevel Optimal
    }
    finally {
        Pop-Location
    }
    if ((Get-Item $archive).Length -le 0) {
        throw "Distribution archive is empty: $archive"
    }
}

function Package-ReleaseDistributions {
    $releaseDirectory = Join-Path $env:GITHUB_WORKSPACE "release-distributions"
    $commitTimestampSeconds = git show -s --format=%ct $env:GITHUB_SHA
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot resolve commit timestamp for $env:GITHUB_SHA"
    }
    $commitTimestamp = [DateTimeOffset]::FromUnixTimeSeconds(
        [long]$commitTimestampSeconds
    ).UtcDateTime
    New-Item -ItemType Directory -Force -Path $releaseDirectory | Out-Null

    New-DistributionPackage python cpython $commitTimestamp $releaseDirectory
    New-DistributionPackage javascript quickjs $commitTimestamp $releaseDirectory
    New-DistributionPackage busybox busybox $commitTimestamp $releaseDirectory
    New-DistributionPackage `
        ci-composed `
        cpython-quickjs-busybox `
        $commitTimestamp `
        $releaseDirectory
}

function Stage-ReleaseDistributions {
    $releaseId = $env:RELEASE_ID
    if ($releaseId -notmatch '^\d+$') {
        throw "Invalid release ID: $releaseId"
    }
    $apiUrl = "$env:GITHUB_API_URL/repos/$env:REPOSITORY"
    $uploadsUrl = "https://uploads.github.com/repos/$env:REPOSITORY"
    $headers = @{
        Accept = "application/vnd.github+json"
        Authorization = "Bearer $env:GH_TOKEN"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $assets = @(
        Get-ChildItem `
            -LiteralPath (
                Join-Path $env:GITHUB_WORKSPACE "release-distributions"
            ) `
            -File `
            -Filter "*.zip" |
            Sort-Object Name
    )

    if ($assets.Count -ne 4) {
        throw "Expected 4 Windows release distribution images, found $($assets.Count)"
    }

    $releaseAssets = @(
        Invoke-RestMethod `
            -Uri "$apiUrl/releases/$releaseId/assets?per_page=100" `
            -Headers $headers
    )
    foreach ($asset in $assets) {
        foreach ($existing in @(
            $releaseAssets | Where-Object { $_.name -eq $asset.Name }
        )) {
            Invoke-RestMethod `
                -Method Delete `
                -Uri "$apiUrl/releases/assets/$($existing.id)" `
                -Headers $headers
        }

        $assetName = [Uri]::EscapeDataString($asset.Name)
        $uploaded = Invoke-RestMethod `
            -Method Post `
            -Uri "$uploadsUrl/releases/$releaseId/assets?name=$assetName" `
            -Headers $headers `
            -ContentType "application/zip" `
            -InFile $asset.FullName
        if ($uploaded.name -ne $asset.Name) {
            throw "Uploaded asset has unexpected name: $($uploaded.name)"
        }
        if ($uploaded.state -ne "uploaded" -or $uploaded.size -le 0) {
            throw (
                "Uploaded asset is incomplete: " +
                ($uploaded | ConvertTo-Json -Compress)
            )
        }
    }
}

function Print-SccacheStatistics {
    if (Get-Command sccache -ErrorAction SilentlyContinue) {
        sccache --show-stats
    }
}

function Remove-DriveMapping {
    if (Test-Path N:\) {
        subst N: /D
    }
}

switch ($Task) {
    "clean-workspace" { Clean-Workspace }
    "clean-submodule-build-artifacts" { Clean-SubmoduleBuildArtifacts }
    "create-short-drive-mapping" { Create-ShortDriveMapping }
    "restore-directory-symlinks" { Restore-DirectorySymlinks }
    "refresh-path" { Refresh-Path }
    "set-rust-environment" { Set-RustEnvironment }
    "isolate-cargo-home" { Isolate-CargoHome }
    "verify-rust-toolchain" { Verify-RustToolchain }
    "setup-prerequisites" { Setup-Prerequisites }
    "install-python-dependencies" { Install-PythonDependencies }
    "check-distro-tooling" { Check-DistroTooling }
    "build-test-nanvix-core" { Build-TestNanvixCore }
    "download-pinned-distribution-guests" { Download-PinnedDistributionGuests }
    "create-distribution-images" { Create-DistributionImages }
    "smoke-test-busybox" { Smoke-TestBusybox }
    "package-release-distributions" { Package-ReleaseDistributions }
    "stage-release-distributions" { Stage-ReleaseDistributions }
    "print-sccache-statistics" { Print-SccacheStatistics }
    "remove-drive-mapping" { Remove-DriveMapping }
}
