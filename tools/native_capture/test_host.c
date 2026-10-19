#define OVERPARSE_TEST_HOST
#include "win_util.h"

typedef uintptr_t (*Callback)(void *, const unsigned char *);
static const uintptr_t expected_return = UINT64_C(0x1122334455667788);
static volatile LONG64 *client;
static Callback callback;
static Callback *method_slot;

static uintptr_t call_slot(void *owner, const unsigned char *packet) {
    return (*method_slot)(owner, packet);
}

typedef LONG (NTAPI *MapSection)(HANDLE, HANDLE, PVOID *, ULONG_PTR, SIZE_T,
                                PLARGE_INTEGER, PSIZE_T, int, ULONG, ULONG);

static void make_packet(unsigned char packet[84], unsigned attack, int damage) {
    memset(packet, 0, 84);
    packet[0] = 84; packet[5] = 4; packet[6] = 0x52;
    uint32_t source = 10000001, target = 100, receiver = 10000001;
    memcpy(packet + 8, &receiver, 4);
    memcpy(packet + 0x14, &target, 4);
    memcpy(packet + 0x20, &source, 4);
    memcpy(packet + 0x2c, &attack, 4);
    memcpy(packet + 0x30, &damage, 4);
}

static DWORD WINAPI burst(void *arg) {
    unsigned index = (unsigned)(uintptr_t)arg;
    for (unsigned i = 0; i < 250; ++i) {
        unsigned char packet[84];
        make_packet(packet, 1000 + index, (int)i + 1);
        if (callback((void *)client, packet) != expected_return) return 1;
    }
    return 0;
}

static int check(BOOL condition, const char *message) {
    if (!condition) fprintf(stderr, "FAIL: %s (Win32=%lu)\n", message, GetLastError());
    return condition ? 0 : 1;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 3 && (argc != 4 || (wcscmp(argv[3], L"--mapped") && wcscmp(argv[3], L"--slot")))) {
        fputs("Usage: CaptureTestHost.exe <test-DLL> <new-output-directory> [--mapped|--slot]\n", stderr);
        return 2;
    }
    BOOL mapped = argc == 4;
    BOOL slot_mode = mapped && !wcscmp(argv[3], L"--slot");
    HMODULE module = LoadLibraryW(argv[1]);
    if (check(module != NULL, "LoadLibraryW")) return 1;
    OpcControl start = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureStartTest");
    OpcControl production_start = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureStart");
    OpcControl stop = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureStop");
    OpcControl probe_protect = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureProbeProtectTest");
    if (check(start && stop && production_start && probe_protect, "exports")) return 1;
    OpcTestConfig config = {0};
    config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    if (!GetFullPathNameW(argv[2], OPC_PATH_CHARS, config.config.output_directory, NULL)) return 1;
    if (!CreateDirectoryW(config.config.output_directory, NULL)) return 1;
    /* Production entry must reject a process with no Classic module. */
    if (check(production_start(&config.config) == ERROR_MOD_NOT_FOUND, "reject non-game host")) return 1;
    unsigned char fixture[4096] = {0};
    unsigned char *code;
    client = VirtualAlloc(NULL, 8192, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (check(client != NULL, "fixture client memory")) return 1;
    /* Same complete 36-byte entry as the examined Classic callback, but the
       relative tail jump targets a tiny local stub, never any game code. */
    const unsigned char entry[36] = {
        0x33,0xc0,0x48,0x89,0x81,0xcc,0x14,0,0,0x44,0x0f,0xb6,0x42,2,
        0x0f,0xb7,2,0x41,0xc1,0xe0,0x10,0x44,0x0b,0xc0,0x44,0x01,
        0x81,0xbc,0x14,0,0,0xe9,0x1c,0,0,0
    };
    memcpy(fixture, entry, sizeof(entry));
    /* lock inc qword [rcx]; mov rax, expected_return; ret */
    const unsigned char stub[] = {0xf0,0x48,0xff,0x01,0x48,0xb8,
                                 0x88,0x77,0x66,0x55,0x44,0x33,0x22,0x11,0xc3};
    memcpy(fixture + 64, stub, sizeof(stub));
    DWORD old;
    const unsigned char *control = NULL;
    if (mapped) {
        wchar_t path[OPC_PATH_CHARS + 64];
        swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\mapped-fixture.bin", config.config.output_directory);
        HANDLE file = CreateFileW(path, GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE,
                                  FILE_SHARE_READ | FILE_SHARE_DELETE, NULL, CREATE_NEW,
                                  FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE, NULL);
        if (check(file != INVALID_HANDLE_VALUE, "mapped fixture file")) return 1;
        DWORD bytes;
        if (check(WriteFile(file, fixture, sizeof(fixture), &bytes, NULL) && bytes == sizeof(fixture),
                  "mapped fixture contents")) return 1;
        HANDLE mapping = CreateFileMappingW(file, NULL, PAGE_EXECUTE_READ, 0, 0, NULL);
        if (check(mapping != NULL, "read/execute section")) return 1;
        if (slot_mode) {
            MapSection map = (MapSection)(uintptr_t)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtMapViewOfSection");
            SIZE_T view_size = 4096;
            code = NULL;
            if (check(map && map(mapping, GetCurrentProcess(), (PVOID *)&code, 0, 0, NULL,
                                 &view_size, 2, 0x400000, PAGE_EXECUTE_READ) >= 0,
                      "immutable executable fixture")) return 1;
        } else code = MapViewOfFile(mapping, FILE_MAP_READ | FILE_MAP_EXECUTE, 0, 0, 4096);
        control = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 4096);
        if (check(code && control, "mapped fixture views")) return 1;
        MEMORY_BASIC_INFORMATION region;
        if (check(VirtualQuery(code, &region, sizeof(region)) && region.Type == MEM_MAPPED &&
                  region.Protect == PAGE_EXECUTE_READ && region.AllocationProtect == PAGE_EXECUTE_READ,
                  "same mapping attributes as live callback")) return 1;
        /* Keep mapping/file handles and views alive with the pinned hook. */
    } else {
        code = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
        if (check(code != NULL, "private fixture code")) return 1;
        memcpy(code, fixture, sizeof(fixture));
        if (!VirtualProtect(code, 4096, PAGE_EXECUTE_READ, &old)) return 1;
    }
    FlushInstructionCache(GetCurrentProcess(), code, 4096);
    callback = (Callback)code;
    config.callback = code;
    if (slot_mode) {
        /* Reject a slot in immutable executable memory before creating files. */
        config.method_slot = (void **)code;
        if (check(start(&config) == ERROR_NOACCESS, "reject non-writable method slot")) return 1;
        method_slot = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
        if (check(method_slot != NULL, "private callback slot")) return 1;
        *method_slot = (Callback)code;
        config.method_slot = (void **)method_slot;
        callback = call_slot;
    }
    if (check(start(&config) == 0, "install hook")) return 1;
    if (mapped && !slot_mode && check(code[0] == 0xe9 && !memcmp(control, fixture, sizeof(fixture)),
                        "hook changed private view only")) return 1;
    if (slot_mode && check(*method_slot != (Callback)code && !memcmp(code, fixture, sizeof(fixture)) &&
                          !memcmp(control, fixture, sizeof(fixture)), "slot installed without code changes")) return 1;
    /* A reserved, uncommitted page gives a genuine Windows protection failure
       in this fixture, independent of game or anti-cheat behavior. */
    void *reserved = VirtualAlloc(NULL, 4096, MEM_RESERVE, PAGE_NOACCESS);
    if (check(reserved != NULL, "reserved diagnostic fixture")) return 1;
    SetLastError(0x12345678);
    BOOL direct_success = VirtualProtect(reserved, 4096, PAGE_EXECUTE_READWRITE, &old);
    DWORD direct_error = GetLastError();
    if (check(!direct_success && direct_error && direct_error != 0x12345678, "direct protection failure")) return 1;
    if (check(probe_protect(reserved) == direct_error, "preserve protection failure error")) return 1;
    /* Keep the region reserved until the status file has been written. */
    unsigned char packet[84];
    make_packet(packet, 777, 12345);
    for (unsigned i = 0; i < 20; ++i) {
        if (check(callback((void *)client, packet) == expected_return, "preserve return value")) return 1;
        Sleep(5); /* ensures an ordinary, loss-free baseline */
    }
    make_packet(packet, 778, -234);
    callback((void *)client, packet);
    packet[5] = 3; callback((void *)client, packet); /* unrelated packet */
    packet[5] = 4; packet[7] = 1; callback((void *)client, packet); /* full u16 subtype */
    packet[7] = 0; packet[0] = 80; callback((void *)client, packet); /* wrong length */
    packet[0] = 84; packet[3] = 1; callback((void *)client, packet); /* invalid 24-bit length */
    Sleep(50);
    HANDLE threads[4];
    for (unsigned i = 0; i < 4; ++i) threads[i] = CreateThread(NULL, 0, burst, (void *)(uintptr_t)i, 0, NULL);
    if (check(threads[0] && threads[1] && threads[2] && threads[3], "burst threads")) return 1;
    WaitForMultipleObjects(4, threads, TRUE, INFINITE);
    for (unsigned i = 0; i < 4; ++i) {
        DWORD result;
        GetExitCodeThread(threads[i], &result); CloseHandle(threads[i]);
        if (check(result == 0, "concurrent forwarding")) return 1;
    }
    if (check(stop(NULL) == 0, "flush and stop")) return 1;
    make_packet(packet, 999, 999);
    if (check(callback((void *)client, packet) == expected_return, "forward after stop")) return 1;
    LONG64 count = InterlockedCompareExchange64(client, 0, 0);
    if (check(count == 1026, "every original callback ran exactly once")) return 1;
    VirtualFree(reserved, 0, MEM_RELEASE);
    printf("PASS: %s, 1026 original calls, filters, concurrency, stop forwarding\n",
           slot_mode ? "immutable-code callback slot" : "x64 trampoline");
    /* Pinned hook deliberately remains valid until process exit. */
    return 0;
}
