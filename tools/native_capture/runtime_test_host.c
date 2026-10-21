/* Control-worker fixture. The capture backend is a stub; native packet and
   object-lifetime behavior is covered separately by ContinuityTestHost. */
#include "runtime_control.h"
#include "thread_guard.h"

void CaptureGuardDiagnostics(OpcGuardDiagnostics *out) { ZeroMemory(out, sizeof(*out)); }

DWORD WINAPI CaptureAutostart(void *unused);
static wchar_t root[OPC_PATH_CHARS], current_directory[OPC_PATH_CHARS];
static OpcHealth current;
static HANDLE output = INVALID_HANDLE_VALUE;
static BOOL was_disconnected;
static wchar_t context_directory[OPC_PATH_CHARS];
const wchar_t *CaptureRuntimeTestRoot(void) { return root; }

static BOOL marker(const wchar_t *name) {
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\%ls", root, name);
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}
static DWORD write_json(const wchar_t *directory, const wchar_t *name, const char *text) {
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\%ls", directory, name);
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_DELETE, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    DWORD n;
    BOOL ok = WriteFile(file, text, (DWORD)strlen(text), &n, NULL) && n == strlen(text);
    CloseHandle(file);
    return ok ? 0 : ERROR_WRITE_FAULT;
}
static void write_status(void) {
    if (!current_directory[0]) return;
    char text[300];
    snprintf(text, sizeof(text), "{\"mode\":\"runtime_fixture\",\"recording\":%s,\"stopped\":%s,\"written\":0}",
             current.recording ? "true" : "false", current.requested ? "false" : "true");
    (void)write_json(current_directory, L"capture-status.json", text);
}
DWORD CaptureProbeBinding(const wchar_t *directory) {
    return write_json(directory, L"binding-probe.json", "{\"stage\":\"binding_ready\",\"win32_error\":0}");
}
static void context_status(BOOL stopped) {
    if (!context_directory[0]) return;
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    current.context_watermark_qpc = (uint64_t)now.QuadPart;
    char text[600];
    snprintf(text, sizeof(text), "{\"schema_version\":1,\"pid\":%lu,\"source_sha256\":\"%s\","
        "\"start_filetime\":%llu,\"written\":0,\"dropped\":0,\"invalid\":0,\"io_error\":0,"
        "\"published_through_qpc\":%llu,\"stopped\":%s}", GetCurrentProcessId(), OPC_CLASSIC_SHA256,
        (unsigned long long)current.context_start_filetime, (unsigned long long)current.context_watermark_qpc,
        stopped ? "true" : "false");
    (void)write_json(context_directory, L"context-status.json", text);
}
DWORD WINAPI CaptureObserverStart(void *argument) {
    OpcConfig *config = argument;
    wcscpy(context_directory, config->output_directory);
    if (!CreateDirectoryW(context_directory, NULL)) return GetLastError();
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\identity.opmeta", context_directory);
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    LARGE_INTEGER frequency; QueryPerformanceFrequency(&frequency);
    OpcHeader header = {{'O','P','M','E','T','A','0','1'},1,168,(uint64_t)frequency.QuadPart,opc_filetime()};
    DWORD bytes;
    BOOL ok = WriteFile(file, &header, sizeof(header), &bytes, NULL) && bytes == sizeof(header);
    CloseHandle(file);
    if (!ok) return ERROR_WRITE_FAULT;
    current.context_start_filetime = header.start_filetime;
    current.observing = current.binding_ready = 1;
    current.binding_generation = 1;
    context_status(FALSE);
    return 0;
}
DWORD WINAPI CaptureObserverStop(void *unused) {
    (void)unused; current.observing = 0; context_status(TRUE); return 0;
}
DWORD WINAPI CaptureStart(void *argument) {
    OpcConfig *config = argument;
    if (current.requested) return ERROR_ALREADY_INITIALIZED;
    wcscpy(current_directory, config->output_directory);
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\capture.opcap", current_directory);
    output = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (output == INVALID_HANDLE_VALUE) return GetLastError();
    OpcHeader header = {{'O','P','C','A','P','0','1',0},1,sizeof(OpcRecord),10000000,opc_filetime()};
    DWORD bytes;
    if (!WriteFile(output, &header, sizeof(header), &bytes, NULL) || bytes != sizeof(header)) return ERROR_WRITE_FAULT;
    current.requested = current.recording = current.binding_ready = 1;
    current.io_error = 0;
    if (!current.binding_generation) current.binding_generation = 1;
    write_status();
    return 0;
}
DWORD WINAPI CaptureStop(void *unused) {
    (void)unused;
    current.requested = current.recording = 0;
    if (output != INVALID_HANDLE_VALUE) { CloseHandle(output); output = INVALID_HANDLE_VALUE; }
    write_status();
    return current.io_error;
}
DWORD WINAPI CaptureMaintain(void *unused) {
    (void)unused;
    context_status(FALSE);
    if (marker(L"writer-error.fixture")) {
        current.io_error = ERROR_WRITE_FAULT; current.recording = 0;
        return ERROR_WRITE_FAULT;
    }
    if (marker(L"disconnect.fixture")) {
        if (!was_disconnected) { ++current.connection_gaps; was_disconnected = TRUE; }
        current.recording = current.binding_ready = 0;
        current.binding_error = ERROR_NOT_READY;
        return ERROR_NOT_READY;
    }
    if (was_disconnected) { was_disconnected = FALSE; ++current.binding_generation; }
    current.recording = current.requested;
    current.binding_ready = 1;
    current.binding_error = 0;
    ++current.callbacks_total; current.last_callback_tick = GetTickCount64();
    write_status();
    return 0;
}
DWORD WINAPI CaptureGetHealth(void *argument) {
    OpcHealth *h = argument;
    if (h->size != sizeof(*h) || h->abi != OPC_ABI) return ERROR_INVALID_PARAMETER;
    *h = current;
    h->size = sizeof(*h); h->abi = OPC_ABI;
    return 0;
}
static int parser_test(void) {
    const char *bad[] = {"", "OPC-CONTROL-1\n0\nstart\n", "OPC-CONTROL-1\n-1\nstart\n",
        "OPC-CONTROL-1\n18446744073709551616\nstart\n", "OPC-CONTROL-1\n1\nstart\nextra",
        "OPC-CONTROL-1\n1\nunknown\n", "OPC-CONTROL-1\r\n1\r\nstop\r\n"};
    OpcCommand command;
    for (unsigned i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i)
        if (opc_parse_command(bad[i], strlen(bad[i]), &command) != ERROR_INVALID_DATA) return 1;
    const char good[] = "OPC-CONTROL-1\n18446744073709551615\nshutdown\n";
    if (opc_parse_command(good, sizeof(good)-1, &command) || command.id != UINT64_MAX || command.kind != OPC_COMMAND_SHUTDOWN) return 1;
    puts("PASS: command parser rejects malformed, oversized and incomplete requests");
    return 0;
}
int wmain(int argc, wchar_t **argv) {
    if (argc != 2) return 2;
    if (!wcscmp(argv[1], L"--parser-test")) return parser_test();
    if (!GetFullPathNameW(argv[1], OPC_PATH_CHARS, root, NULL)) return 1;
    return (int)CaptureAutostart(NULL);
}
