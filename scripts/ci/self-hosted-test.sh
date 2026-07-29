#!/usr/bin/env bash

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly RELEASE_HELPER="${SCRIPT_DIR}/self-hosted-test.py"

prepare_release() {
    local release_number
    local release_notes="${RUNNER_TEMP}/release-notes.md"
    local release_tag="distro-${GITHUB_SHA}"
    local release_title="Nanvix Distribution ${GITHUB_SHA::7}"
    local releases_json="${RUNNER_TEMP}/releases.json"

    gh api "repos/${REPOSITORY}/releases?per_page=100" > "${releases_json}"
    release_number="$(
        python3 "${RELEASE_HELPER}" find-draft-release \
            "${releases_json}" \
            "${release_tag}" \
            "${GITHUB_SHA}"
    )"

    if [[ -z "${release_number}" ]]; then
        cat > "${release_notes}" <<EOF
Nanvix distribution images built from merge commit ${GITHUB_SHA}.

Included configurations:
- Linux/KVM: BusyBox
- Linux/KVM: CPython
- Linux/KVM: QuickJS
- Linux/KVM: CPython, QuickJS, and BusyBox
- Windows/WHP: BusyBox
- Windows/WHP: CPython
- Windows/WHP: QuickJS
- Windows/WHP: CPython, QuickJS, and BusyBox
EOF

        release_number="$(
            gh api \
                --method POST \
                -f tag_name="${release_tag}" \
                -f target_commitish="${GITHUB_SHA}" \
                -f name="${release_title}" \
                -F "body=@${release_notes}" \
                -F draft=true \
                -F prerelease=false \
                "repos/${REPOSITORY}/releases" \
                --jq .id
        )"
    fi

    if [[ ! "${release_number}" =~ ^[0-9]+$ ]]; then
        echo "::error::Invalid release ID: ${release_number}"
        exit 1
    fi
    echo "release-id=${release_number}" >> "${GITHUB_OUTPUT}"
}

pre_checkout_cleanup() {
    if [[ -d "${GITHUB_WORKSPACE}/images" ]]; then
        echo "Removing images directory before checkout..."
        sudo rm -rf "${GITHUB_WORKSPACE}/images"
    fi
}

clean_submodule_build_artifacts() {
    git submodule foreach --recursive git clean -ffdx
}

relocate_cargo_state() {
    local cargo_home="${HOME}/.cache/nanvix-distro-cargo"
    local cargo_target="${HOME}/.cache/nanvix-distro-target"

    rm -rf "${cargo_home}" "${cargo_target}"
    mkdir -p "${cargo_home}" "${cargo_target}"
    ln -sfn "${HOME}/.cargo/bin" "${cargo_home}/bin"
    ln -sfn "${cargo_target}" nanvix/target
    echo "CARGO_HOME=${cargo_home}" >> "${GITHUB_ENV}"
    echo "CI_NANVIX_TARGET_DIR=${cargo_target}" >> "${GITHUB_ENV}"
}

setup_kvm() {
    sudo modprobe kvm
    sudo modprobe kvm_intel || sudo modprobe kvm_amd || true
    sudo chmod 666 /dev/kvm || true
}

install_python_dependencies() {
    python3 -m pip install --user --break-system-packages black pyright tomli-w
}

check_distro_tooling() {
    black --target-version py312 --check z.py nanvix_distro tests scripts
    pyright
    python3 -m unittest discover -v
}

ensure_docker_daemon() {
    local attempt

    if ! docker info &>/dev/null; then
        echo "::warning::Docker daemon not responding; attempting to start it"
        sudo systemctl start docker 2>/dev/null \
            || sudo service docker start 2>/dev/null \
            || true
        for attempt in 1 2 3 4 5; do
            docker info &>/dev/null && break
            if [[ "${attempt}" -lt 5 ]]; then
                sleep 2
            fi
        done
    fi
    docker info
}

build() {
    python3 z.py --verbose build
}

create_distribution_images() {
    python3 z.py --verbose dist busybox
    python3 z.py --verbose dist javascript
    python3 z.py --verbose dist python
    python3 z.py --verbose menuconfig ci-composed --include all
}

smoke_test_busybox() {
    printf 'exit\n' | timeout 120s \
        python3 z.py --verbose run busybox 2>&1 \
        | tee build/dist/busybox/busybox-smoke.log
    grep -q "NANVIX_BUSYBOX_READY" build/dist/busybox/busybox-smoke.log
}

smoke_test_composed() {
    {
        echo "python3 -c 'print(\"PYTHON_COMPONENT_READY\")'"
        echo "qjs -e 'console.log(\"JAVASCRIPT_COMPONENT_READY\")'"
        echo "exit"
    } | timeout 180s python3 z.py --verbose run ci-composed 2>&1 \
        | tee build/dist/ci-composed/composed-smoke.log
    grep -q "PYTHON_COMPONENT_READY" build/dist/ci-composed/composed-smoke.log
    grep -q "JAVASCRIPT_COMPONENT_READY" build/dist/ci-composed/composed-smoke.log
}

package_distribution() {
    local profile="$1"
    local components="$2"
    local commit_timestamp="$3"
    local source="build/dist/${profile}"
    local archive="release-distributions/nanvix-distro-linux-x86-microvm-256mb-${components}-${GITHUB_SHA}.tar.gz"

    if [[ ! -d "${source}" ]]; then
        echo "::error::Distribution output not found: ${source}"
        return 1
    fi

    tar \
        --sort=name \
        --mtime="@${commit_timestamp}" \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        -czf "${archive}" \
        -C "${source}" \
        nanvixd.elf \
        bin/kernel.elf \
        bin/nanvix.initrd \
        bin/nanvix.ramfs
}

package_release_distributions() {
    local commit_timestamp

    commit_timestamp="$(git show -s --format=%ct "${GITHUB_SHA}")"
    mkdir -p release-distributions
    package_distribution python cpython "${commit_timestamp}"
    package_distribution javascript quickjs "${commit_timestamp}"
    package_distribution busybox busybox "${commit_timestamp}"
    package_distribution ci-composed cpython-quickjs-busybox "${commit_timestamp}"
}

reclaim_build_space() {
    mkdir -p ci-logs
    cp build/dist/busybox/busybox-smoke.log ci-logs/
    cp build/dist/ci-composed/composed-smoke.log ci-logs/
    rm -rf build
    git submodule foreach --recursive git clean -ffdx
    ln -sfn "${CI_NANVIX_TARGET_DIR}" nanvix/target
    df -h "${GITHUB_WORKSPACE}" "${HOME}"
}

run_tests() {
    python3 z.py --verbose test
}

stage_release_distributions() {
    local api_url="${GITHUB_API_URL}/repos/${REPOSITORY}"
    local asset
    local asset_id
    local asset_name
    local release_assets="${RUNNER_TEMP}/release-assets.json"
    local release_number="${RELEASE_ID}"
    local upload_json
    local uploads_url="https://uploads.github.com/repos/${REPOSITORY}"
    local -a api_headers=(
        --header "Accept: application/vnd.github+json"
        --header "Authorization: Bearer ${GH_TOKEN}"
        --header "X-GitHub-Api-Version: 2022-11-28"
    )
    local -a assets

    mapfile -t assets < <(
        find release-distributions -maxdepth 1 -type f -name '*.tar.gz' -print | sort
    )
    if [[ "${#assets[@]}" -ne 4 ]]; then
        echo "::error::Expected 4 release distribution images, found ${#assets[@]}"
        exit 1
    fi
    if [[ ! "${release_number}" =~ ^[0-9]+$ ]]; then
        echo "::error::Invalid release ID: ${release_number}"
        exit 1
    fi

    curl \
        --fail-with-body \
        --silent \
        --show-error \
        --location \
        "${api_headers[@]}" \
        --output "${release_assets}" \
        "${api_url}/releases/${release_number}/assets?per_page=100"

    for asset in "${assets[@]}"; do
        asset_name="$(basename "${asset}")"
        while IFS= read -r asset_id; do
            curl \
                --fail-with-body \
                --silent \
                --show-error \
                --location \
                --request DELETE \
                "${api_headers[@]}" \
                "${api_url}/releases/assets/${asset_id}"
        done < <(
            python3 "${RELEASE_HELPER}" find-release-asset-ids \
                "${release_assets}" \
                "${asset_name}"
        )

        upload_json="${RUNNER_TEMP}/${asset_name}.json"
        curl \
            --fail-with-body \
            --silent \
            --show-error \
            --location \
            --request POST \
            "${api_headers[@]}" \
            --header "Content-Type: application/gzip" \
            --data-binary "@${asset}" \
            --output "${upload_json}" \
            "${uploads_url}/releases/${release_number}/assets?name=${asset_name}"
        python3 "${RELEASE_HELPER}" validate-uploaded-asset \
            "${upload_json}" \
            "${asset_name}"
    done
}

remove_relocated_cargo_state() {
    df -h "${GITHUB_WORKSPACE}" "${HOME}"
    if [[ -L nanvix/target ]]; then
        rm -f nanvix/target
    fi
    rm -rf \
        "${HOME}/.cache/nanvix-distro-cargo" \
        "${HOME}/.cache/nanvix-distro-target"
}

print_sccache_statistics() {
    if command -v sccache &>/dev/null; then
        sccache --show-stats
    fi
}

publish_release() {
    local published_json="${RUNNER_TEMP}/published-release.json"
    local release_number="${RELEASE_ID}"
    local release_json="${RUNNER_TEMP}/release.json"
    local release_tag="distro-${GITHUB_SHA}"

    if [[ ! "${release_number}" =~ ^[0-9]+$ ]]; then
        echo "::error::Invalid release ID: ${release_number}"
        exit 1
    fi
    gh api "repos/${REPOSITORY}/releases/${release_number}" > "${release_json}"
    python3 "${RELEASE_HELPER}" validate-release \
        "${release_json}" \
        "${release_tag}" \
        "${GITHUB_SHA}"

    gh api \
        --method PATCH \
        -f tag_name="${release_tag}" \
        -f target_commitish="${GITHUB_SHA}" \
        -F draft=false \
        -f make_latest=true \
        "repos/${REPOSITORY}/releases/${release_number}" \
        > "${published_json}"

    python3 "${RELEASE_HELPER}" validate-published-release \
        "${published_json}" \
        "${release_tag}" \
        "${GITHUB_SHA}"
}

main() {
    case "${1:-}" in
        prepare-release) prepare_release ;;
        pre-checkout-cleanup) pre_checkout_cleanup ;;
        clean-submodule-build-artifacts) clean_submodule_build_artifacts ;;
        relocate-cargo-state) relocate_cargo_state ;;
        setup-kvm) setup_kvm ;;
        install-python-dependencies) install_python_dependencies ;;
        check-distro-tooling) check_distro_tooling ;;
        ensure-docker-daemon) ensure_docker_daemon ;;
        build) build ;;
        create-distribution-images) create_distribution_images ;;
        smoke-test-busybox) smoke_test_busybox ;;
        smoke-test-composed) smoke_test_composed ;;
        package-release-distributions) package_release_distributions ;;
        reclaim-build-space) reclaim_build_space ;;
        test) run_tests ;;
        stage-release-distributions) stage_release_distributions ;;
        remove-relocated-cargo-state) remove_relocated_cargo_state ;;
        print-sccache-statistics) print_sccache_statistics ;;
        publish-release) publish_release ;;
        *)
            echo "usage: $0 <task>" >&2
            exit 2
            ;;
    esac
}

main "$@"
