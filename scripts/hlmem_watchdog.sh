#!/usr/bin/env bash

# Standalone HL-Mem watchdog for Windows Task Scheduler + Git Bash.
# Keep this script independent from both the HL-Mem and Hermes Python environments.

set -u
umask 077

unset VIRTUAL_ENV PYTHONPATH PYTHONHOME

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="${HL_MEM_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}"
VAR_DIR="$REPO_ROOT/var"
STATE_FILE="$VAR_DIR/watchdog.state"
WATCHDOG_LOG="$VAR_DIR/watchdog.log"
SERVICE_STARTUP_LOG="${HL_MEM_STARTUP_LOG:-$VAR_DIR/hlmem_startup.log}"
LOCK_DIR="$VAR_DIR/watchdog.lock"
CRASH_ROOT="$VAR_DIR/crash-packages"

HEALTH_URL="${HL_MEM_HEALTH_URL:-http://127.0.0.1:8200/healthz}"
HEALTH_TIMEOUT_SECONDS="${HL_MEM_WATCHDOG_TIMEOUT_SECONDS:-5}"
FAILURE_THRESHOLD="${HL_MEM_WATCHDOG_FAILURE_THRESHOLD:-3}"
COOLDOWN_SECONDS="${HL_MEM_WATCHDOG_COOLDOWN_SECONDS:-60}"
LOCK_STALE_SECONDS="${HL_MEM_WATCHDOG_LOCK_STALE_SECONDS:-300}"
TERMINATION_WAIT_SECONDS="${HL_MEM_WATCHDOG_TERMINATION_WAIT_SECONDS:-10}"
START_GRACE_SECONDS="${HL_MEM_WATCHDOG_START_GRACE_SECONDS:-1}"
DIAGNOSTIC_TIMEOUT_SECONDS="${HL_MEM_WATCHDOG_DIAGNOSTIC_TIMEOUT_SECONDS:-10}"
SERVICE_PORT="${HL_MEM_PORT:-8200}"
DEFAULT_HOME="${HOME:-/c/Users/Administrator}"
START_SCRIPT="${HL_MEM_START_SCRIPT:-$DEFAULT_HOME/bin/start_hlmem.sh}"
SYSTEM_BASH="${HL_MEM_BASH:-/usr/bin/bash}"

CURL_BIN="${HL_MEM_CURL:-curl}"
NETSTAT_BIN="${HL_MEM_NETSTAT:-netstat.exe}"
POWERSHELL_BIN="${HL_MEM_POWERSHELL:-powershell.exe}"
TASKLIST_BIN="${HL_MEM_TASKLIST:-tasklist.exe}"
TASKKILL_BIN="${HL_MEM_TASKKILL:-taskkill.exe}"
PY_SPY_BIN="${HL_MEM_PY_SPY:-py-spy.exe}"
TIMEOUT_BIN="${HL_MEM_TIMEOUT:-timeout}"
LOCK_TOKEN="$$:$(date +%s):$RANDOM"

mkdir -p "$VAR_DIR"
touch "$WATCHDOG_LOG"

timestamp() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

log_message() {
    printf '%s %s\n' "$(timestamp)" "$*" >> "$WATCHDOG_LOG"
}

require_positive_integer() {
    local name="$1"
    local value="$2"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        log_message "configuration_error name=$name value=$value"
        exit 2
    fi
}

require_positive_integer "timeout_seconds" "$HEALTH_TIMEOUT_SECONDS"
require_positive_integer "failure_threshold" "$FAILURE_THRESHOLD"
require_positive_integer "cooldown_seconds" "$COOLDOWN_SECONDS"
require_positive_integer "lock_stale_seconds" "$LOCK_STALE_SECONDS"
require_positive_integer "termination_wait_seconds" "$TERMINATION_WAIT_SECONDS"
require_positive_integer "start_grace_seconds" "$START_GRACE_SECONDS"
require_positive_integer "diagnostic_timeout_seconds" "$DIAGNOSTIC_TIMEOUT_SECONDS"
require_positive_integer "service_port" "$SERVICE_PORT"
if ! command -v "$TIMEOUT_BIN" >/dev/null 2>&1; then
    log_message "configuration_error name=timeout_command value=$TIMEOUT_BIN"
    exit 2
fi

run_bounded() {
    "$TIMEOUT_BIN" --signal=TERM --kill-after=2s "${DIAGNOSTIC_TIMEOUT_SECONDS}s" "$@"
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    now_epoch="$(date +%s)"
    lock_epoch="$(stat -c %Y "$LOCK_DIR" 2>/dev/null || printf '%s' "$now_epoch")"
    [[ "$lock_epoch" =~ ^[0-9]+$ ]] || lock_epoch="$now_epoch"
    lock_age=$((now_epoch - lock_epoch))
    if (( lock_age < LOCK_STALE_SECONDS )); then
        log_message "watchdog_skipped reason=already_running lock_age_seconds=$lock_age"
        exit 0
    fi

    owner_pid="$(sed -n 's/^pid=//p' "$LOCK_DIR/owner" 2>/dev/null | head -n 1)"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && [[ "$owner_pid" != "$$" ]] && kill -0 "$owner_pid" 2>/dev/null; then
        log_message "watchdog_skipped reason=lock_owner_alive owner_pid=$owner_pid lock_age_seconds=$lock_age"
        exit 0
    fi
    rm -f -- "$LOCK_DIR/owner"
    if ! rmdir "$LOCK_DIR" 2>/dev/null || ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log_message "watchdog_skipped reason=stale_lock_reclaim_failed lock_age_seconds=$lock_age"
        exit 0
    fi
    log_message "stale_lock_reclaimed owner_pid=${owner_pid:-unknown} lock_age_seconds=$lock_age"
fi
printf 'pid=%s\ncreated_epoch=%s\ntoken=%s\n' "$$" "$(date +%s)" "$LOCK_TOKEN" > "$LOCK_DIR/owner"

PROBE_ERROR_FILE="$VAR_DIR/.watchdog-probe-error.$$"
STATE_TEMP_FILE="$VAR_DIR/.watchdog-state.$$"

cleanup() {
    rm -f -- "$PROBE_ERROR_FILE" "$STATE_TEMP_FILE"
    current_lock_token="$(sed -n 's/^token=//p' "$LOCK_DIR/owner" 2>/dev/null | head -n 1)"
    if [[ "$current_lock_token" == "$LOCK_TOKEN" ]]; then
        rm -f -- "$LOCK_DIR/owner"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

failures=0
last_restart_epoch=0
if [[ -f "$STATE_FILE" ]]; then
    while IFS='=' read -r key value; do
        case "$key" in
            failures)
                [[ "$value" =~ ^[0-9]+$ ]] && failures="$value"
                ;;
            last_restart_epoch)
                [[ "$value" =~ ^[0-9]+$ ]] && last_restart_epoch="$value"
                ;;
        esac
    done < "$STATE_FILE"
fi

save_state() {
    printf 'failures=%s\nlast_restart_epoch=%s\n' "$failures" "$last_restart_epoch" > "$STATE_TEMP_FILE"
    mv -f -- "$STATE_TEMP_FILE" "$STATE_FILE"
}

epoch_milliseconds() {
    local value
    value="$(date +%s%3N 2>/dev/null || true)"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
    else
        printf '%s000\n' "$(date +%s)"
    fi
}

probe_started_ms="$(epoch_milliseconds)"
probe_output=""
probe_output="$(
    "$CURL_BIN" \
        --fail \
        --silent \
        --show-error \
        --max-time "$HEALTH_TIMEOUT_SECONDS" \
        "$HEALTH_URL" \
        2> "$PROBE_ERROR_FILE"
)"
curl_status=$?
if (( curl_status == 0 )); then
    probe_finished_ms="$(epoch_milliseconds)"
    duration_ms=$((probe_finished_ms - probe_started_ms))
    failures=0
    save_state
    log_message "healthz_probe_succeeded url=$HEALTH_URL duration_ms=$duration_ms"
    exit 0
fi

probe_finished_ms="$(epoch_milliseconds)"
duration_ms=$((probe_finished_ms - probe_started_ms))
probe_error="$(tr '\r\n' '  ' < "$PROBE_ERROR_FILE" | cut -c1-500)"
failures=$((failures + 1))
save_state
log_message "healthz_probe_failed url=$HEALTH_URL duration_ms=$duration_ms curl_exit=$curl_status failures=$failures error=${probe_error:-unknown}"

if (( failures < FAILURE_THRESHOLD )); then
    exit 0
fi

now_epoch="$(date +%s)"
if (( last_restart_epoch > 0 && now_epoch - last_restart_epoch < COOLDOWN_SECONDS )); then
    cooldown_remaining=$((COOLDOWN_SECONDS - (now_epoch - last_restart_epoch)))
    log_message "restart_suppressed reason=cooldown remaining_seconds=$cooldown_remaining failures=$failures"
    exit 0
fi

incident_timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
incident_dir="$CRASH_ROOT/$incident_timestamp"
mkdir -p "$incident_dir"
log_message "incident_capture_started directory=$incident_dir failures=$failures"

grep 'healthz_probe_' "$WATCHDOG_LOG" | tail -n 50 > "$incident_dir/healthz-probes.log" || true

port_snapshot_ok=true
if ! run_bounded "$NETSTAT_BIN" -ano -p tcp > "$incident_dir/port-listeners.txt" 2>&1; then
    port_snapshot_ok=false
    printf '\nnetstat failed or exceeded %s seconds.\n' "$DIAGNOSTIC_TIMEOUT_SECONDS" \
        >> "$incident_dir/port-listeners.txt"
fi
mapfile -t listener_pids < <(
    tr -d '\r' < "$incident_dir/port-listeners.txt" \
        | awk -v port="$SERVICE_PORT" '$2 ~ (":" port "$") && $4 == "LISTENING" {print $5}' \
        | grep -E '^[0-9]+$' \
        | sort -u
)

if (( ${#listener_pids[@]} == 0 )); then
    printf 'No LISTENING process found for port %s.\n' "$SERVICE_PORT" > "$incident_dir/process-tree.txt"
    printf 'No LISTENING process found for port %s.\n' "$SERVICE_PORT" > "$incident_dir/process-resources.txt"
else
    run_bounded "$POWERSHELL_BIN" -NoProfile -NonInteractive -Command '
$rootIds = @($args | ForEach-Object { [int]$_ })
$all = @(Get-CimInstance Win32_Process)
function Write-ProcessTree([int]$ProcessId, [int]$Depth) {
    $process = $all | Where-Object { $_.ProcessId -eq $ProcessId } | Select-Object -First 1
    if ($null -eq $process) {
        Write-Output (("  " * $Depth) + "PID=" + $ProcessId + " <not found>")
        return
    }
    Write-Output (("  " * $Depth) + "PID=" + $process.ProcessId + " PPID=" + $process.ParentProcessId + " Name=" + $process.Name + " CommandLine=" + $process.CommandLine)
    foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $ProcessId }) {
        Write-ProcessTree -ProcessId $child.ProcessId -Depth ($Depth + 1)
    }
}
foreach ($rootId in $rootIds) { Write-ProcessTree -ProcessId $rootId -Depth 0 }
' "${listener_pids[@]}" > "$incident_dir/process-tree.txt" 2>&1 \
        || printf '\nPowerShell process tree failed or timed out.\n' >> "$incident_dir/process-tree.txt"

    : > "$incident_dir/process-resources.txt"
    for process_id in "${listener_pids[@]}"; do
        printf '=== tasklist PID %s ===\n' "$process_id" >> "$incident_dir/process-resources.txt"
        run_bounded "$TASKLIST_BIN" /FI "PID eq $process_id" /V \
            >> "$incident_dir/process-resources.txt" 2>&1 \
            || printf 'tasklist failed or timed out.\n' >> "$incident_dir/process-resources.txt"
        run_bounded "$POWERSHELL_BIN" -NoProfile -NonInteractive -Command '
$processId = [int]$args[0]
Get-Process -Id $processId -ErrorAction Stop |
    Select-Object Id, ProcessName, CPU, WorkingSet64, PrivateMemorySize64,
        @{Name="ThreadCount";Expression={$_.Threads.Count}}, StartTime |
    Format-List
' "$process_id" >> "$incident_dir/process-resources.txt" 2>&1 \
            || printf 'PowerShell resource snapshot failed or timed out.\n' \
                >> "$incident_dir/process-resources.txt"
    done
fi

run_bounded find "$VAR_DIR" -maxdepth 1 -type f \
    \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) \
    -printf '%p\t%s bytes\t%TY-%Tm-%TdT%TH:%TM:%TS%TZ\n' \
    > "$incident_dir/database-files.txt" 2>&1 || true

: > "$incident_dir/service-logs-tail.txt"
run_bounded find "$REPO_ROOT" "$VAR_DIR" -maxdepth 1 -type f \
    \( -iname '*.log' -o -iname 'hlmem_startup.log' \) -print0 \
    > "$incident_dir/service-log-files.list" 2>/dev/null || true
while IFS= read -r -d '' log_file; do
    printf '\n===== %s =====\n' "$log_file" >> "$incident_dir/service-logs-tail.txt"
    run_bounded tail -n 200 "$log_file" >> "$incident_dir/service-logs-tail.txt" 2>&1 || true
done < "$incident_dir/service-log-files.list"

: > "$incident_dir/python-stacks.txt"
if command -v "$PY_SPY_BIN" >/dev/null 2>&1 && (( ${#listener_pids[@]} > 0 )); then
    for process_id in "${listener_pids[@]}"; do
        printf '===== py-spy PID %s =====\n' "$process_id" >> "$incident_dir/python-stacks.txt"
        run_bounded "$PY_SPY_BIN" dump --pid "$process_id" >> "$incident_dir/python-stacks.txt" 2>&1 || true
    done
else
    printf 'py-spy unavailable; see process-tree.txt and process-resources.txt.\n' > "$incident_dir/python-stacks.txt"
fi

log_message "incident_capture_finished directory=$incident_dir listener_pids=${listener_pids[*]:-none}"

if [[ ! -f "$START_SCRIPT" ]]; then
    log_message "restart_aborted reason=start_script_missing path=$START_SCRIPT"
    exit 1
fi
if [[ "$port_snapshot_ok" != "true" ]]; then
    log_message "restart_aborted reason=listener_snapshot_failed incident=$incident_dir"
    exit 1
fi
if ! command -v "$SYSTEM_BASH" >/dev/null 2>&1; then
    log_message "restart_aborted reason=system_bash_missing path=$SYSTEM_BASH"
    exit 1
fi

termination_failed=false
for process_id in "${listener_pids[@]}"; do
    if run_bounded "$TASKKILL_BIN" /PID "$process_id" /T /F >> "$incident_dir/restart.txt" 2>&1; then
        log_message "process_tree_terminated pid=$process_id"
    else
        termination_failed=true
        log_message "process_tree_termination_failed pid=$process_id"
    fi
done
if [[ "$termination_failed" == "true" ]]; then
    log_message "restart_aborted reason=termination_failed incident=$incident_dir"
    exit 1
fi

termination_deadline=$(( $(date +%s) + TERMINATION_WAIT_SECONDS ))
while true; do
    if ! run_bounded "$NETSTAT_BIN" -ano -p tcp > "$incident_dir/port-listeners-after-termination.txt" 2>&1; then
        log_message "restart_aborted reason=listener_recheck_failed incident=$incident_dir"
        exit 1
    fi
    mapfile -t remaining_listener_pids < <(
        tr -d '\r' < "$incident_dir/port-listeners-after-termination.txt" \
            | awk -v port="$SERVICE_PORT" '$2 ~ (":" port "$") && $4 == "LISTENING" {print $5}' \
            | grep -E '^[0-9]+$' \
            | sort -u
    )
    if (( ${#remaining_listener_pids[@]} == 0 )); then
        break
    fi
    if (( $(date +%s) >= termination_deadline )); then
        log_message "restart_aborted reason=port_still_listening listener_pids=${remaining_listener_pids[*]}"
        exit 1
    fi
    sleep 1
done

nohup "$SYSTEM_BASH" --noprofile --norc "$START_SCRIPT" >> "$SERVICE_STARTUP_LOG" 2>&1 < /dev/null &
launcher_pid=$!
sleep "$START_GRACE_SECONDS"
if ! kill -0 "$launcher_pid" 2>/dev/null; then
    wait "$launcher_pid" 2>/dev/null || true
    log_message "restart_aborted reason=launcher_exited start_script=$START_SCRIPT incident=$incident_dir"
    exit 1
fi

last_restart_epoch="$(date +%s)"
failures=0
save_state
log_message "restart_triggered launcher_pid=$launcher_pid start_script=$START_SCRIPT incident=$incident_dir"

exit 0
