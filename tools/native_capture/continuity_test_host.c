#define OVERPARSE_TEST_HOST
#include "binding_layout.h"

typedef uintptr_t (*Callback)(void *, const unsigned char *);
static const uintptr_t expected_return = UINT64_C(0x1122334455667788);
static unsigned char *profile, *owner, *network;
static uintptr_t *listener;
static volatile LONG calls, churn_done;
static SRWLOCK model_lock = SRWLOCK_INIT;

static uintptr_t original(void *client, const unsigned char *packet) {
    (void)packet;
    if (client != owner) return 0;
    InterlockedIncrement(&calls);
    return expected_return;
}

static uintptr_t *new_listener(void) {
    uintptr_t *p = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!p) return NULL;
    p[0] = (uintptr_t)profile + 0x34c19f0; p[1] = 2;
    p[2] = (uintptr_t)&original; p[3] = 0; p[4] = (uintptr_t)owner;
    return p;
}

static int fail(const char *text) {
    fprintf(stderr, "FAIL: %s (Win32=%lu)\n", text, GetLastError());
    return 1;
}

static BOOL connect_model(void) {
    profile = VirtualAlloc(NULL, 0x3ceb000, MEM_RESERVE, PAGE_NOACCESS);
    owner = VirtualAlloc(NULL, 0x2000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    network = VirtualAlloc(NULL, 0x11000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!profile || !owner || !network ||
        !VirtualAlloc(profile + 0x3cea000, 4096, MEM_COMMIT, PAGE_READWRITE) ||
        !VirtualAlloc(profile + 0x34c1000, 4096, MEM_COMMIT, PAGE_READWRITE)) return FALSE;
    *(uintptr_t *)(profile + 0x3ceacf8) = (uintptr_t)owner;
    *(uintptr_t *)owner = (uintptr_t)profile + 0x35a5680;
    *(uintptr_t *)(owner + 0x1498) = (uintptr_t)network;
    *(uintptr_t *)network = (uintptr_t)profile + 0x35a5560;
    *(uintptr_t *)(network + 0x48) = (uintptr_t)profile + 0x35a55e0;
    *(uintptr_t *)(profile + 0x34c19f8) = (uintptr_t)profile + 0x1770e0;
    listener = new_listener();
    if (!listener) return FALSE;
    *(uintptr_t *)(network + 0x101d8) = (uintptr_t)listener;
    return TRUE;
}

static BOOL send_packet(int damage) {
    unsigned char packet[84] = {0};
    packet[0] = 84; packet[5] = 4; packet[6] = 0x52;
    memcpy(packet + 0x30, &damage, 4);
    AcquireSRWLockShared(&model_lock);
    Callback call = (Callback)listener[2];
    uintptr_t result = call(owner, packet);
    ReleaseSRWLockShared(&model_lock);
    return result == expected_return;
}

static BOOL set_output(OpcTestConfig *config, const wchar_t *root, const wchar_t *part) {
    int n = swprintf(config->config.output_directory, OPC_PATH_CHARS, L"%ls\\%ls", root, part);
    return n > 0 && n < OPC_PATH_CHARS && CreateDirectoryW(config->config.output_directory, NULL);
}

static DWORD WINAPI churn_bindings(void *unused) {
    (void)unused;
    /* The model serializes its own dispatcher and destruction. The capture
       worker does not take this lock, exercising the thread guard when this
       thread is suspended while it owns an unrelated application lock. */
    for (unsigned i = 0; i < 150; ++i) {
        uintptr_t *next = new_listener();
        if (!next) return 1;
        AcquireSRWLockExclusive(&model_lock);
        uintptr_t *old = listener;
        listener = next;
        *(uintptr_t *)(network + 0x101d8) = (uintptr_t)next;
        VirtualFree(old, 0, MEM_RELEASE);
        ReleaseSRWLockExclusive(&model_lock);
        if (!send_packet(900000 + (int)i)) return 2;
        Sleep(1);
    }
    InterlockedExchange(&churn_done, 1);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    if (!connect_model()) return fail("model allocation");
    HMODULE dll = LoadLibraryW(argv[1]);
    if (!dll) return fail("test core");
    OpcControl start = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStartTest");
    OpcControl stop = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStop");
    OpcControl poll = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureMaintain");
    OpcControl health = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureGetHealth");
    if (!start || !stop || !poll || !health) return fail("exports");
    wchar_t root[OPC_PATH_CHARS];
    if (!GetFullPathNameW(argv[2], OPC_PATH_CHARS, root, NULL) || !CreateDirectoryW(root, NULL)) return fail("root");
    OpcTestConfig config = {0};
    config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    config.callback = (void *)(uintptr_t)&original; config.profile_base = profile;
    if (!set_output(&config, root, L"part-1") || start(&config)) return fail("first start");
    for (int i = 1; i <= 10; ++i) if (!send_packet(i)) return fail("first recording");
    if (stop(NULL) || stop(NULL)) return fail("idempotent stop");
    for (int i = 0; i < 7; ++i) if (!send_packet(777777)) return fail("paused forwarding");
    if (!set_output(&config, root, L"part-2") || start(&config)) return fail("restart without game exit");
    for (int i = 11; i <= 20; ++i) if (!send_packet(i)) return fail("second recording");
    uintptr_t *old = listener;
    listener = new_listener();
    if (!listener) return 1;
    *(uintptr_t *)(network + 0x101d8) = (uintptr_t)listener;
    VirtualFree(old, 0, MEM_RELEASE);
    if (poll(NULL)) return fail("freed listener replaced");
    for (int i = 21; i <= 30; ++i) if (!send_packet(i)) return fail("reattached recording");
    *(uintptr_t *)(owner + 0x1498) = 0;
    if (poll(NULL) != ERROR_NOT_READY) return fail("disconnect detected");
    OpcHealth state = {0}; state.size = sizeof(state); state.abi = OPC_ABI;
    if (health(&state) || !state.requested || state.recording || state.binding_ready) return fail("disconnected health");
    if (!send_packet(888888)) return fail("forward through old connection while paused");
    *(uintptr_t *)(owner + 0x1498) = (uintptr_t)network;
    if (poll(NULL)) return fail("connection restored");
    for (int i = 31; i <= 40; ++i) if (!send_packet(i)) return fail("restored recording");
    uintptr_t saved = listener[2];
    listener[2] = (uintptr_t)&new_listener; /* foreign registration must not be replaced */
    if (poll(NULL) != ERROR_INVALID_DATA || listener[2] != (uintptr_t)&new_listener) return fail("foreign registration refused");
    listener[2] = saved;
    if (poll(NULL)) return fail("recover after foreign registration removed");
    HANDLE churn = CreateThread(NULL, 0, churn_bindings, NULL, 0, NULL);
    if (!churn) return fail("churn thread");
    unsigned polls = 0;
    while (!InterlockedCompareExchange(&churn_done, 0, 0)) {
        (void)poll(NULL); /* Transient missing/partial objects can be retried. */
        Sleep(1);
        if (++polls > 1000) return fail("churn deadline");
    }
    if (WaitForSingleObject(churn, 2000) != WAIT_OBJECT_0) return fail("churn joins");
    DWORD result;
    GetExitCodeThread(churn, &result); CloseHandle(churn);
    if (result) return fail("churn thread result");
    /* A thread exiting during the strict guard can cancel an attachment.
       Honor the production 5-second retry backoff instead of assuming the
       first poll after the churn thread exits must immediately succeed. */
    ULONGLONG settle_deadline = GetTickCount64() + 6500;
    DWORD settled;
    while ((settled = poll(NULL)) != 0) {
        if (health(&state) || state.guard_fatal || GetTickCount64() >= settle_deadline) {
            SetLastError(settled);
            return fail("churn completed");
        }
        Sleep(25);
    }
    for (int i = 41; i <= 50; ++i) if (!send_packet(i)) return fail("after churn");
    if (stop(NULL)) return fail("second drain");
    if (health(&state) || state.recording || state.requested || state.binding_generation < 3 || state.connection_gaps < 3)
        return fail("final health");
    if (InterlockedCompareExchange(&calls, 0, 0) != 208) return fail("every original call exactly once");
    printf("PASS: stop/restart, listener destruction, disconnect, foreign slot, concurrent replacement; generations=%llu gaps=%llu\n",
           (unsigned long long)state.binding_generation, (unsigned long long)state.connection_gaps);
    return 0;
}
