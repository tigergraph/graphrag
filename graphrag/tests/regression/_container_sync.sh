#!/usr/bin/env bash
# Shared helpers for the regression run_*.sh scripts.
#
# The regression code and test_questions datasets are NOT baked into the
# graphrag image and are no longer bind-mounted. Instead they are copied into
# the running container on demand, right before a run — so the default
# `docker compose up` carries no test artifacts, and nothing needs to be
# installed on the host. Copies are refreshed on every call (idempotent).

# sync_regression_to_container <container> <regression_dir>
# Copies <regression_dir> and its sibling ../test_questions into
# /code/tests/{regression,test_questions} in the container.
sync_regression_to_container() {
    local container="$1"
    local reg_dir="$2"
    local tq_dir
    tq_dir="$(cd "${reg_dir}/../test_questions" && pwd)"
    docker exec "${container}" sh -c \
        "rm -rf /code/tests && mkdir -p /code/tests/regression /code/tests/test_questions"
    docker cp "${reg_dir}/." "${container}:/code/tests/regression/"
    docker cp "${tq_dir}/." "${container}:/code/tests/test_questions/"
}

# copy_results_from_container <container> <regression_dir>
# Brings the eval results written inside the container back to the host,
# replacing what the old bind mount did automatically.
copy_results_from_container() {
    local container="$1"
    local reg_dir="$2"
    mkdir -p "${reg_dir}/results"
    docker cp "${container}:/code/tests/regression/results/." "${reg_dir}/results/" 2>/dev/null || true
}
