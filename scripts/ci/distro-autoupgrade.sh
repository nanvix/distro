#!/usr/bin/env bash

# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

set -euo pipefail

upgrade() {
    local sdk_args=()

    if [[ "${EVENT_NAME}" == "repository_dispatch" && -z "${SDK_VERSION}" ]]; then
        echo "::error::Distro release candidate did not specify an SDK version"
        exit 1
    fi
    if [[ -n "${SDK_VERSION}" ]]; then
        if [[ ! "${SDK_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-sdk\.[0-9]+$ ]]; then
            echo "::error::Invalid distro release candidate SDK: ${SDK_VERSION}"
            exit 1
        fi
        sdk_args=(--sdk-version "${SDK_VERSION}")
    fi

    python3 z.py --verbose upgrade --defer-incomplete-release-set "${sdk_args[@]}"
    if git diff --quiet HEAD --; then
        echo "changed=false" >> "${GITHUB_OUTPUT}"
        echo "::notice::No coherent newer distro release set is available yet"
        return
    fi
    echo "changed=true" >> "${GITHUB_OUTPUT}"
    echo "sdk_version=$(jq -r '.sdk_version' config/sdk-release.json)" >> "${GITHUB_OUTPUT}"
}

install_python_dependencies() {
    python3 -m pip install ./usr/lib/zutils
}

test_upgraded_release_set() {
    python3 -m unittest discover -v
}

wait_for_ci() {
    local conclusion=""
    local head_ref
    local head_sha
    local run_id=""
    local run_state
    local run_status=""

    head_sha="$(
        gh pr view "${PR_NUMBER}" \
            --repo "${REPOSITORY}" \
            --json headRefOid \
            --jq .headRefOid
    )"
    head_ref="$(
        gh pr view "${PR_NUMBER}" \
            --repo "${REPOSITORY}" \
            --json headRefName \
            --jq .headRefName
    )"

    gh workflow run self-hosted-test.yml \
        --repo "${REPOSITORY}" \
        --ref "${head_ref}"

    # The workflow_dispatch event may take a few seconds to register its run.
    for _ in {1..30}; do
        run_id="$(
            gh run list \
                --repo "${REPOSITORY}" \
                --workflow self-hosted-test.yml \
                --event workflow_dispatch \
                --commit "${head_sha}" \
                --limit 1 \
                --json databaseId \
                --jq '.[0].databaseId // empty'
        )"
        [[ -n "${run_id}" ]] && break
        sleep 10
    done

    if [[ -z "${run_id}" ]]; then
        echo "::error::Self-Hosted Test did not start for commit ${head_sha}"
        exit 1
    fi

    for _ in {1..360}; do
        if run_state="$(
            gh api \
                "repos/${REPOSITORY}/actions/runs/${run_id}" \
                --jq '[.status, (.conclusion // "")] | @tsv' \
                2>&1
        )"; then
            IFS=$'\t' read -r run_status conclusion <<< "${run_state}"
            [[ "${run_status}" == "completed" ]] && break
        else
            echo "::warning::Could not query CI run ${run_id}; retrying: ${run_state}"
        fi
        sleep 30
    done

    if [[ "${run_status}" != "completed" ]]; then
        echo "::error::Timed out waiting for CI run ${run_id}"
        exit 1
    fi
    if [[ "${conclusion}" != "success" ]]; then
        echo "::error::CI run ${run_id} concluded ${conclusion}"
        exit 1
    fi

    echo "head-sha=${head_sha}" >> "${GITHUB_OUTPUT}"
}

merge_pull_request() {
    gh pr merge \
        --repo "${REPOSITORY}" \
        --merge \
        --match-head-commit "${MERGE_HEAD_SHA}" \
        "${PR_NUMBER}"
}

main() {
    case "${1:-}" in
        upgrade)
            upgrade
            ;;
        install-python-dependencies)
            install_python_dependencies
            ;;
        test-upgraded-release-set)
            test_upgraded_release_set
            ;;
        wait-for-ci)
            wait_for_ci
            ;;
        merge-pull-request)
            merge_pull_request
            ;;
        *)
            echo "usage: $0 <task>" >&2
            exit 2
            ;;
    esac
}

main "$@"
