#!/bin/bash
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Shared timing utility library for azure-scale benchmarking.
# Source this file from other scripts: source timing_utils.sh

# --- Global state ---
declare -A _TIMER_START
declare -A _TIMER_END
declare -A _TIMER_DURATION
declare -A _APP_ACTIVE_TIME
_TIMER_RUN_ID=""
_TIMER_RUN_START=""
_TIMER_JSON_OUTPUT="timing_report.json"
_TIMER_DEPLOYMENT_TYPE="juju"

# Initialize the timing system
# Usage: timer_init "deployment_type" ["output_file"]
timer_init() {
    _TIMER_DEPLOYMENT_TYPE="${1:-juju}"
    _TIMER_JSON_OUTPUT="${2:-timing_report.json}"
    _TIMER_RUN_ID=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    _TIMER_RUN_START=$(date +%s.%N)
    _APP_ACTIVE_TIME=()
}

# Start a named phase
# Usage: timer_start "phase_name"
timer_start() {
    local phase="$1"
    _TIMER_START["$phase"]=$(date +%s.%N)
    echo "[TIMING] Phase '$phase' started at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

# End a named phase and compute duration
# Usage: timer_end "phase_name"
timer_end() {
    local phase="$1"
    _TIMER_END["$phase"]=$(date +%s.%N)
    local start="${_TIMER_START[$phase]}"
    if [ -z "$start" ]; then
        echo "[TIMING] WARNING: phase '$phase' was never started"
        return 1
    fi
    _TIMER_DURATION["$phase"]=$(echo "${_TIMER_END[$phase]} - $start" | bc -l)
    echo "[TIMING] Phase '$phase' completed in ${_TIMER_DURATION[$phase]}s"
}

# Track per-application readiness during the wait-for-active loop.
# Call this each iteration with the juju status JSON.
# Usage: track_app_status "$(juju status --format=json)" "$wait_loop_start_time"
track_app_status() {
    local status_json="$1"
    local loop_start="$2"

    # Parse all applications and their statuses
    while IFS='|' read -r app status; do
        if [ "$status" = "active" ]; then
            if [ -z "${_APP_ACTIVE_TIME[$app]}" ]; then
                local now
                now=$(date +%s.%N)
                _APP_ACTIVE_TIME["$app"]=$(echo "$now - $loop_start" | bc -l)
            fi
        fi
    done < <(echo "$status_json" | jq -r '.applications | to_entries[] | "\(.key)|\(.value["application-status"].current)"')
}

# Emit the timing report as JSON
# Usage: emit_timing_report ["extra_json_key" "extra_json_value"]
emit_timing_report() {
    local run_end
    run_end=$(date +%s.%N)
    local total_duration
    total_duration=$(echo "$run_end - $_TIMER_RUN_START" | bc -l)

    # Build phases JSON
    local phases_json="{}"
    for phase in "${!_TIMER_DURATION[@]}"; do
        local duration="${_TIMER_DURATION[$phase]}"
        local start="${_TIMER_START[$phase]}"
        local end="${_TIMER_END[$phase]}"
        phases_json=$(echo "$phases_json" | jq --arg p "$phase" \
            --argjson dur "$duration" \
            --argjson start "$start" \
            --argjson end "$end" \
            '.[$p] = {start: $start, end: $end, duration_seconds: $dur}')
    done

    # Build application readiness JSON
    local app_json="{}"
    for app in "${!_APP_ACTIVE_TIME[@]}"; do
        local active_at="${_APP_ACTIVE_TIME[$app]}"
        app_json=$(echo "$app_json" | jq --arg a "$app" \
            --argjson secs "$active_at" \
            '.[$a] = {seconds_from_wait_start: $secs}')
    done

    # Compute spinup time (bootstrap through node_configured phases)
    local spinup_seconds=0
    for phase in bootstrap tofu_apply wait_active node_configured_gpu node_configured_cpu install_deps; do
        if [ -n "${_TIMER_DURATION[$phase]}" ]; then
            spinup_seconds=$(echo "$spinup_seconds + ${_TIMER_DURATION[$phase]}" | bc -l)
        fi
    done

    # Compute teardown time
    local teardown_seconds=0
    for phase in teardown_tofu teardown_juju; do
        if [ -n "${_TIMER_DURATION[$phase]}" ]; then
            teardown_seconds=$(echo "$teardown_seconds + ${_TIMER_DURATION[$phase]}" | bc -l)
        fi
    done

    # Assemble final JSON
    local extra="${3:-}"
    jq -n \
        --arg run_id "$_TIMER_RUN_ID" \
        --arg dep_type "$_TIMER_DEPLOYMENT_TYPE" \
        --argjson phases "$phases_json" \
        --argjson apps "$app_json" \
        --argjson spinup "$spinup_seconds" \
        --argjson teardown "$teardown_seconds" \
        --argjson total "$total_duration" \
        '{
            run_id: $run_id,
            deployment_type: $dep_type,
            phases: $phases,
            application_readiness: $apps,
            totals: {
                spinup_seconds: $spinup,
                teardown_seconds: $teardown,
                total_seconds: $total
            }
        }' > "$_TIMER_JSON_OUTPUT"

    echo "[TIMING] Report written to $_TIMER_JSON_OUTPUT"
}

# Print a human-readable timing summary to stdout
print_timing_summary() {
    echo ""
    echo "========== TIMING SUMMARY =========="
    echo "Run ID: $_TIMER_RUN_ID"
    echo "Deployment type: $_TIMER_DEPLOYMENT_TYPE"
    echo ""
    echo "--- Phases ---"
    for phase in bootstrap tofu_apply wait_active node_configured_gpu node_configured_cpu \
                  install_deps reframe_suite copy_results teardown_tofu teardown_juju; do
        if [ -n "${_TIMER_DURATION[$phase]}" ]; then
            printf "  %-25s %10.2f s\n" "$phase" "${_TIMER_DURATION[$phase]}"
        fi
    done
    echo ""
    echo "--- Application Readiness ---"
    for app in "${!_APP_ACTIVE_TIME[@]}"; do
        printf "  %-25s %10.2f s\n" "$app" "${_APP_ACTIVE_TIME[$app]}"
    done
    echo ""
    echo "--- Totals ---"
    local run_end
    run_end=$(date +%s.%N)
    local total
    total=$(echo "$run_end - $_TIMER_RUN_START" | bc -l)
    printf "  %-25s %10.2f s\n" "total" "$total"
    echo "===================================="
}
