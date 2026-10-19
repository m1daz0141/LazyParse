#define OVERPARSE_TEST_HOST
#include "win_util.h"
typedef uintptr_t (*Callback)(void *, const unsigned char *);
static Callback *slot;
static OpcControl start, stop, poll, health;
static unsigned calls;
static uint64_t begun, ended, context_started;
static uintptr_t original(void *owner, const unsigned char *packet) { (void)owner; (void)packet; ++calls; return 123; }
static int fail(const char *message) { fprintf(stderr, "FAIL: %s, win32=%lu\n", message, GetLastError()); return 1; }
static void ref(unsigned char *packet, unsigned offset, uint32_t id, unsigned type) {
    memcpy(packet + offset, &id, 4); packet[offset+8] = (unsigned char)type; packet[offset+10] = type == 4 ? 0 : 8;
}
static BOOL emit(unsigned major, unsigned minor, uint32_t id) {
    unsigned char packet[84] = {0}; packet[0] = 84; packet[5] = (unsigned char)major; packet[6] = (unsigned char)minor;
    if (major == 6) { packet[0] = 20; memcpy(packet+8, &id, 4); }
    else if (major == 4) { packet[0] = 32; ref(packet, 8, 42, 4); ref(packet, 20, id, 6); }
    else ref(packet, 8, id, 6);
    return (*slot)(NULL, packet) == 123;
}
static BOOL hit(uint32_t self, uint32_t target, int value) {
    unsigned char packet[84] = {0}; packet[0] = 84; packet[5] = 4; packet[6] = 0x52;
    ref(packet, 8, self, 4); ref(packet, 20, target, 6); ref(packet, 32, self, 4);
    uint32_t attack = 99; memcpy(packet+0x2c, &attack, 4); memcpy(packet+0x30, &value, 4);
    return (*slot)(NULL, packet) == 123;
}
static BOOL reference(const wchar_t *root, const wchar_t *directory) {
    wchar_t path[OPC_PATH_CHARS + 64]; swprintf(path, OPC_PATH_CHARS+64, L"%ls\\identity-context.json", directory);
    const wchar_t *run = wcsrchr(root, L'\\'); run = run ? run+1 : root;
    LARGE_INTEGER frequency; QueryPerformanceFrequency(&frequency);
    char text[800], end[32] = "null";
    if (ended) snprintf(end, sizeof(end), "%llu", (unsigned long long)ended);
    int n = snprintf(text, sizeof(text), "{\"schema_version\":1,\"run_name\":\"%ls\",\"pid\":%lu,"
        "\"source_sha256\":\"not_a_game_capture\",\"context_start_filetime\":%llu,"
        "\"context_path\":\"../identity-context/identity.opmeta\",\"qpc_frequency\":%llu,\"start_qpc\":%llu,\"end_qpc\":%s}",
        run, GetCurrentProcessId(), (unsigned long long)context_started, (unsigned long long)frequency.QuadPart,
        (unsigned long long)begun, end);
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return FALSE;
    DWORD bytes; BOOL ok = n > 0 && WriteFile(file, text, (DWORD)n, &bytes, NULL) && bytes == (DWORD)n;
    CloseHandle(file); return ok;
}
static BOOL begin(OpcTestConfig *config, const wchar_t *root, unsigned number) {
    swprintf(config->config.output_directory, OPC_PATH_CHARS, L"%ls\\segment-%06u", root, number);
    if (!CreateDirectoryW(config->config.output_directory, NULL)) return FALSE;
    LARGE_INTEGER now; QueryPerformanceCounter(&now); begun = (uint64_t)now.QuadPart; ended = 0;
    return reference(root, config->config.output_directory) && !start(config);
}
static BOOL end(OpcTestConfig *config, const wchar_t *root) {
    if (stop(NULL)) return FALSE;
    LARGE_INTEGER now; QueryPerformanceCounter(&now); ended = (uint64_t)now.QuadPart;
    return reference(root, config->config.output_directory);
}
int wmain(int argc, wchar_t **argv) {
    BOOL shared_reader_test = argc == 4 && wcscmp(argv[3], L"--shared-status-reader") == 0;
    BOOL quest_notice_test = argc == 4 && wcscmp(argv[3], L"--quest-notices") == 0;
    if (argc != 3 && !shared_reader_test && !quest_notice_test) return 2;
    HMODULE dll = LoadLibraryW(argv[1]); if (!dll) return fail("load");
    start = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStartTest");
    stop = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStop");
    poll = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureMaintain");
    health = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureGetHealth");
    OpcControl observe = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStartTest");
    OpcControl finish = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStop");
    if (!start || !stop || !poll || !health || !observe || !finish) return fail("exports");
    if (!CreateDirectoryW(argv[2], NULL)) return fail("run directory");
    slot = VirtualAlloc(NULL, 4096, MEM_COMMIT|MEM_RESERVE, PAGE_READWRITE); if (!slot) return 1;
    *slot = original;
    OpcTestConfig config = {0}; config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    config.callback = (void *)(uintptr_t)&original; config.method_slot = (void **)slot;
    swprintf(config.config.output_directory, OPC_PATH_CHARS, L"%ls\\identity-context", argv[2]);
    if (observe(&config)) return fail("observer before recording");
    OpcHealth state = {0}; state.size = sizeof(state); state.abi = OPC_ABI;
    if (health(&state) || !state.observing || state.requested || state.recording) return fail("observer-only health");
    context_started = state.context_start_filetime;
    if (!emit(6,0,42) || !emit(8,13,20000001)) return fail("initial identities");
    if (quest_notice_test) {
        unsigned char packet[128] = {0}; packet[0] = 128; packet[5] = 3; packet[6] = 0x24;
        ref(packet, 96, 7007, 0x30); /* Synthetic quest-header candidate only. */
        if ((*slot)(NULL, packet) != 123 || !emit(0x0b,0x14,0) || !emit(0x0b,0x10,0) || !emit(7,0,0))
            return fail("quest diagnostic forwarding");
    }
    if (shared_reader_test) {
        wchar_t status_path[OPC_PATH_CHARS + 64];
        swprintf(status_path, OPC_PATH_CHARS + 64, L"%ls\\identity-context\\context-status.json", argv[2]);
        HANDLE reader = INVALID_HANDLE_VALUE;
        ULONGLONG ready_deadline = GetTickCount64() + 5000;
        while (reader == INVALID_HANDLE_VALUE && GetTickCount64() < ready_deadline) {
            reader = CreateFileW(status_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                 NULL, OPEN_EXISTING, 0, NULL);
            if (reader == INVALID_HANDLE_VALUE) Sleep(5);
        }
        if (reader == INVALID_HANDLE_VALUE) return fail("shared status reader");
        BOOL emitted = emit(8,13,999);
        /* Hold longer than the publication retry budget, while the actual
           observer receives and publishes a new identity and heartbeat. */
        Sleep(250);
        CloseHandle(reader);
        if (!emitted || health(&state) || state.context_error || state.context_written < 3)
            return fail("observer publication under shared status reader");
        puts("PASS: real observer publishes while a shared status reader stays open");
    }
    if (!begin(&config,argv[2],1) || !hit(42,20000001,100) || !end(&config,argv[2])) return fail("first segment");
    if (!emit(4,6,20000001) || !emit(8,12,20000001) || !emit(8,13,300)) return fail("paused identities");
    if (!begin(&config,argv[2],2) || !hit(42,20000001,200) || !hit(42,300,300) || !end(&config,argv[2])) return fail("second segment");
    *slot = original;
    ULONGLONG deadline = GetTickCount64()+7000;
    while (poll(NULL)) { if (GetTickCount64() > deadline) return fail("reset reattachment"); Sleep(25); }
    if (!begin(&config,argv[2],3) || !hit(42,300,50) || !end(&config,argv[2])) return fail("reset segment");
    if (!emit(6,0,84) || !emit(8,13,400)) return fail("new local identity");
    if (!begin(&config,argv[2],4) || !hit(84,400,400) || !end(&config,argv[2])) return fail("fourth segment");
    if (finish(NULL) || health(&state) || state.observing || state.context_error || state.context_dropped ||
        calls != 12u + (shared_reader_test ? 1u : 0u) + (quest_notice_test ? 4u : 0u))
        return fail("observer drain and forwarding");
    puts("PASS: pre-start identity, paused despawn/reuse, reset invalidation, new local identity");
    return 0;
}
