#include "runtime_control.h"
#include "atomic_file.h"

#ifndef AUTO_SESSION_DIR
#error Build with build_autostart.ps1
#endif
#ifdef OVERPARSE_RUNTIME_TEST
const wchar_t *CaptureRuntimeTestRoot(void);
#undef AUTO_SESSION_DIR
#define AUTO_SESSION_DIR CaptureRuntimeTestRoot()
#endif

DWORD WINAPI CaptureStart(void *config);
DWORD WINAPI CaptureStop(void *unused);
DWORD WINAPI CaptureMaintain(void *unused);
DWORD WINAPI CaptureGetHealth(void *health);
DWORD WINAPI CaptureObserverStart(void *config);
DWORD WINAPI CaptureObserverStop(void *unused);
DWORD CaptureProbeBinding(const wchar_t *directory);

static wchar_t run_directory[OPC_PATH_CHARS], capture_directory[OPC_PATH_CHARS], run_name[96];
static uint64_t command_id, segment_number, receive_baseline;
static DWORD command_error, last_capture_error;
static OpcCommandKind last_command;
static BOOL desired, shutdown_requested, writer_failed;
static OpcHealth health;
static uint64_t segment_start_qpc, segment_end_qpc;
static ULONGLONG next_observer_attempt;

static DWORD refresh_health(void) {
    health.size = sizeof(health); health.abi = OPC_ABI;
    return CaptureGetHealth(&health);
}

static BOOL create_run(void) {
    uint64_t timestamp = opc_filetime();
    for (unsigned attempt = 0; attempt < 16; ++attempt) {
        swprintf(run_name, 96, L"run-%llu-%lu-%u", (unsigned long long)timestamp, GetCurrentProcessId(), attempt);
        int n = swprintf(run_directory, OPC_PATH_CHARS, L"%ls\\%ls", AUTO_SESSION_DIR, run_name);
        if (n <= 0 || n >= OPC_PATH_CHARS) return FALSE;
        if (CreateDirectoryW(run_directory, NULL)) return TRUE;
        if (GetLastError() != ERROR_ALREADY_EXISTS) return FALSE;
    }
    return FALSE;
}

static BOOL save_state(const char *state, DWORD error) {
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\autostart-state.json", AUTO_SESSION_DIR);
    swprintf(temp, OPC_PATH_CHARS + 64, L"%ls\\autostart-state.json.tmp", AUTO_SESSION_DIR);
    char run_json[OPC_PATH_CHARS * 8], capture_json[OPC_PATH_CHARS * 8];
    if (!opc_json_path(run_json, sizeof(run_json), run_directory) ||
        !opc_json_path(capture_json, sizeof(capture_json), capture_directory)) return FALSE;
    uint64_t now = GetTickCount64();
    long long age = !health.last_callback_tick ? -1 :
        (long long)(now >= health.last_callback_tick ? now - health.last_callback_tick : 0);
    const char *receive_state = !health.recording ? "not_recording" :
        health.callbacks_total == receive_baseline ? "awaiting_first_packet" : age >= 15000 ? "quiet" : "receiving";
    char text[OPC_PATH_CHARS * 18];
    int n = snprintf(text, sizeof(text),
        "{\n  \"control_abi\":1,\"state\":\"%s\",\"pid\":%lu,\"win32_error\":%lu,\n"
        "  \"state_filetime\":%llu,\n"
        "  \"run_name\":\"%ls\",\"run_directory\":\"%s\",\"capture_directory\":\"%s\",\n"
        "  \"segment_number\":%llu,\"last_command_id\":%llu,\"last_command\":\"%s\",\"last_command_error\":%lu,\n"
        "  \"desired_recording\":%s,\"recording\":%s,\"binding_ready\":%s,\n"
        "  \"binding_error\":%lu,\"guard_error\":%lu,\"io_error\":%lu,\n"
        "  \"guard_stage\":%u,\"guard_native_status\":\"0x%08x\",\"guard_threads\":%u,\"guard_fatal\":%s,\n"
        "  \"callbacks_total\":%llu,\"binding_generation\":%llu,\"connection_gaps\":%llu,\n"
        "  \"receive_state\":\"%s\",\"receive_age_ms\":%lld,\"last_capture_error\":%lu,\n"
        "  \"classic_hash_required\":\"%s\",\n"
        "  \"identity_observing\":%s,\"identity_context_error\":%lu,\"identity_context_written\":%llu,"
        "\"identity_context_dropped\":%llu,\"identity_context_watermark_qpc\":%llu\n}\n",
        state, GetCurrentProcessId(), error, (unsigned long long)opc_filetime(), run_name, run_json, capture_json,
        (unsigned long long)segment_number, (unsigned long long)command_id,
        opc_command_name(last_command), command_error, desired ? "true" : "false",
        health.recording ? "true" : "false", health.binding_ready ? "true" : "false",
        (unsigned long)health.binding_error, (unsigned long)health.guard_error, (unsigned long)health.io_error,
        health.guard_stage, health.guard_native_status, health.guard_threads, health.guard_fatal ? "true" : "false",
        (unsigned long long)health.callbacks_total, (unsigned long long)health.binding_generation,
        (unsigned long long)health.connection_gaps, receive_state, age, last_capture_error, OPC_CLASSIC_SHA256,
        health.observing ? "true" : "false", (unsigned long)health.context_error,
        (unsigned long long)health.context_written, (unsigned long long)health.context_dropped,
        (unsigned long long)health.context_watermark_qpc);
    if (n <= 0 || (size_t)n >= sizeof(text)) return FALSE;
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return FALSE;
    DWORD written;
    BOOL ok = WriteFile(file, text, (DWORD)n, &written, NULL) && written == (DWORD)n;
    CloseHandle(file);
    return ok && opc_replace_published_file(temp, path) == ERROR_SUCCESS;
}

static void log_event(const char *state, DWORD error) {
    static char prior[64];
    static uint64_t prior_id = UINT64_MAX, prior_generation = UINT64_MAX, prior_gaps = UINT64_MAX;
    static DWORD prior_error = UINT32_MAX;
    if (!strcmp(prior, state) && prior_id == command_id && prior_generation == health.binding_generation &&
        prior_gaps == health.connection_gaps && prior_error == error) return;
    char line[700];
    int n = snprintf(line, sizeof(line),
        "{\"filetime\":%llu,\"tick\":%llu,\"state\":\"%s\",\"win32_error\":%lu,"
        "\"command_id\":%llu,\"segment\":%llu,\"binding_generation\":%llu,\"connection_gaps\":%llu}\n",
        (unsigned long long)opc_filetime(), (unsigned long long)GetTickCount64(), state, error,
        (unsigned long long)command_id, (unsigned long long)segment_number,
        (unsigned long long)health.binding_generation, (unsigned long long)health.connection_gaps);
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\continuity-events.jsonl", run_directory);
    HANDLE file = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ, NULL, OPEN_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD written;
    BOOL ok = n > 0 && (size_t)n < sizeof(line) && WriteFile(file, line, (DWORD)n, &written, NULL) && written == (DWORD)n;
    CloseHandle(file);
    if (ok) {
        strcpy(prior, state); prior_id = command_id; prior_generation = health.binding_generation;
        prior_gaps = health.connection_gaps; prior_error = error;
    }
}

static DWORD save_identity_reference(void) {
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\identity-context.json", capture_directory);
    swprintf(temp, OPC_PATH_CHARS + 64, L"%ls\\identity-context.json.tmp", capture_directory);
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    char text[800], end[32] = "null";
    if (segment_end_qpc) snprintf(end, sizeof(end), "%llu", (unsigned long long)segment_end_qpc);
    int n = snprintf(text, sizeof(text),
        "{\"schema_version\":1,\"run_name\":\"%ls\",\"pid\":%lu,\"source_sha256\":\"%s\","
        "\"context_start_filetime\":%llu,\"context_path\":\"../identity-context/identity.opmeta\","
        "\"qpc_frequency\":%llu,\"start_qpc\":%llu,\"end_qpc\":%s}",
        run_name, GetCurrentProcessId(), OPC_CLASSIC_SHA256,
        (unsigned long long)health.context_start_filetime, (unsigned long long)frequency.QuadPart,
        (unsigned long long)segment_start_qpc, end);
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    DWORD bytes;
    BOOL ok = n > 0 && (size_t)n < sizeof(text) && WriteFile(file, text, (DWORD)n, &bytes, NULL) && bytes == (DWORD)n;
    CloseHandle(file);
    return ok ? opc_replace_published_file(temp, path) : ERROR_WRITE_FAULT;
}

static DWORD stop_segment(void) {
    DWORD error = CaptureStop(NULL);
    if (error == ERROR_INVALID_STATE && !health.requested) error = 0;
    refresh_health();
    if (capture_directory[0] && segment_start_qpc && !segment_end_qpc && !health.draining) {
        LARGE_INTEGER now; QueryPerformanceCounter(&now);
        segment_end_qpc = (uint64_t)now.QuadPart;
        DWORD reference_error = save_identity_reference();
        if (reference_error) { segment_end_qpc = 0; return reference_error; }
    }
    return error;
}

static DWORD start_segment(void) {
    if (!health.observing || health.context_error) return health.context_error ? health.context_error : ERROR_NOT_READY;
    int n = swprintf(capture_directory, OPC_PATH_CHARS, L"%ls\\segment-%06llu", run_directory,
                     (unsigned long long)++segment_number);
    if (n <= 0 || n >= OPC_PATH_CHARS) return ERROR_BAD_PATHNAME;
    if (!CreateDirectoryW(capture_directory, NULL)) return GetLastError();
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    segment_start_qpc = (uint64_t)now.QuadPart; segment_end_qpc = 0;
    DWORD reference_error = save_identity_reference();
    if (reference_error) return reference_error;
    OpcConfig config = {0}; config.size = sizeof(config); config.abi = OPC_ABI;
    wcscpy(config.output_directory, capture_directory);
    receive_baseline = health.callbacks_total;
    return CaptureStart(&config);
}

DWORD WINAPI CaptureAutostart(void *unused) {
    (void)unused;
    wchar_t executable[32768];
    DWORD n = GetModuleFileNameW(NULL, executable, 32768);
    if (!n || n >= 32768) return ERROR_BAD_PATHNAME;
    const wchar_t *name = wcsrchr(executable, L'\\'); name = name ? name + 1 : executable;
#ifdef OVERPARSE_AUTOSTART_TEST
    const wchar_t *expected = L"AutoCaptureTestHost.exe";
#elif defined(OVERPARSE_RUNTIME_TEST)
    const wchar_t *expected = L"AutoRuntimeTestHost.exe";
#else
    const wchar_t *expected = L"pso2.exe";
#endif
    if (_wcsicmp(name, expected)) return ERROR_INVALID_NAME;
    if (!create_run()) return GetLastError();
    DWORD probe_error = ERROR_MOD_NOT_FOUND;
    unsigned publish_failures = 0;
    for (;;) {
        refresh_health();
        if (!health.requested && !shutdown_requested) {
#ifdef OVERPARSE_RUNTIME_TEST
            probe_error = CaptureProbeBinding(run_directory);
#else
            probe_error = GetModuleHandleW(L"pso2classic.dll") ? CaptureProbeBinding(run_directory) : ERROR_MOD_NOT_FOUND;
#endif
        }
        if (!shutdown_requested && !health.observing && !probe_error && GetTickCount64() >= next_observer_attempt) {
            OpcConfig context = {0}; context.size = sizeof(context); context.abi = OPC_ABI;
            swprintf(context.output_directory, OPC_PATH_CHARS, L"%ls\\identity-context", run_directory);
            DWORD observer_error_code = CaptureObserverStart(&context);
            if (observer_error_code) next_observer_attempt = GetTickCount64() + 5000;
            refresh_health();
        }
        if (health.observing) { (void)CaptureMaintain(NULL); refresh_health(); }
        OpcCommand command;
        DWORD parse_error = opc_read_command(run_directory, &command);
        if (!parse_error && command.id > command_id) {
            DWORD error = ERROR_SUCCESS;
            if (command.id != command_id + 1) error = ERROR_INVALID_DATA;
            else if (command.kind == OPC_COMMAND_START) {
                if (health.requested) error = ERROR_ALREADY_INITIALIZED;
                else if (probe_error) error = probe_error;
                else {
                    error = start_segment();
                    desired = error == ERROR_SUCCESS;
                    last_capture_error = error;
                    writer_failed = FALSE;
                }
            } else {
                desired = FALSE;
                error = stop_segment();
                if (command.kind == OPC_COMMAND_SHUTDOWN) shutdown_requested = TRUE;
            }
            command_id = command.id; last_command = command.kind; command_error = error;
            refresh_health();
        }
        DWORD error = ERROR_SUCCESS;
        const char *state;
        if (!desired && health.draining) {
            error = stop_segment();
            refresh_health();
        }
        if (shutdown_requested) {
            error = stop_segment();
            refresh_health();
            if (error != ERROR_TIMEOUT && error != ERROR_BUSY) {
                DWORD observer_stop_error = CaptureObserverStop(NULL);
                if (observer_stop_error == ERROR_TIMEOUT || observer_stop_error == ERROR_BUSY) { Sleep(100); continue; }
                if (!error) error = observer_stop_error;
                refresh_health();
                log_event("shutdown", error);
                save_state("shutdown", error);
                return error;
            }
            state = "stopping";
        } else if (desired && health.requested) {
            error = CaptureMaintain(NULL);
            refresh_health();
            if (health.io_error || health.context_error) {
                last_capture_error = health.io_error ? health.io_error : health.context_error;
                writer_failed = TRUE;
                desired = FALSE;
                (void)stop_segment();
                refresh_health();
                state = "writer_error";
            } else state = health.recording ? "recording" : "waiting_for_binding";
        } else {
            error = probe_error ? probe_error : health.context_error;
            state = health.draining ? "stopping" : writer_failed ? "writer_error" : last_capture_error ? "capture_start_failed" :
                probe_error == ERROR_MOD_NOT_FOUND ? "waiting_for_classic" :
                probe_error ? "waiting_for_binding" : health.context_error ? "identity_context_error" :
                !health.observing ? "waiting_for_identity_context" : "binding_ready_waiting_for_start";
            if (segment_number && !last_capture_error && !health.draining && !error && health.observing) state = "paused";
        }
        if (parse_error != ERROR_FILE_NOT_FOUND && parse_error != ERROR_PATH_NOT_FOUND && parse_error) {
            state = "control_error"; error = parse_error;
        }
        log_event(state, error);
        if (!save_state(state, error)) {
            if (++publish_failures >= 10) {
                desired = FALSE;
                (void)stop_segment();
                last_capture_error = ERROR_WRITE_FAULT;
            }
        } else publish_failures = 0;
        Sleep(500);
    }
}
