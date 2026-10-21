#define OVERPARSE_TEST_HOST
#include "binding_layout.h"

typedef uintptr_t (*Callback)(void *, const unsigned char *);
typedef struct { unsigned char *base, *owner, *network; uintptr_t *listener; Callback original; } Model;
static volatile LONG calls, block_old, entered_old;
static uintptr_t original_old(void *owner, const unsigned char *packet) {
    (void)owner; (void)packet;
    InterlockedIncrement(&calls);
    if (InterlockedCompareExchange(&block_old, 0, 0)) {
        InterlockedExchange(&entered_old, 1);
        while (InterlockedCompareExchange(&block_old, 0, 0)) Sleep(0);
    }
    return 101;
}
static uintptr_t original_new(void *owner, const unsigned char *packet) {
    (void)owner; (void)packet; InterlockedIncrement(&calls); return 202;
}
static uintptr_t foreign(void *owner, const unsigned char *packet) { (void)owner; (void)packet; return 0; }
static int fail(const char *message) { fprintf(stderr, "FAIL: %s (%lu)\n", message, GetLastError()); return 1; }
static BOOL create_model(Model *m, Callback callback) {
    m->base = VirtualAlloc(NULL, (OPC_REQUIRED_IMAGE_SIZE + 0xffffu) & ~(SIZE_T)0xffffu, MEM_RESERVE, PAGE_NOACCESS);
    m->owner = VirtualAlloc(NULL, 0x2000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    m->network = VirtualAlloc(NULL, 0x11000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    m->listener = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    m->original = callback;
    if (!m->base || !m->owner || !m->network || !m->listener ||
        !VirtualAlloc(m->base + (OPC_CLIENT_GLOBAL_RVA & ~0xfffu), 4096, MEM_COMMIT, PAGE_READWRITE) ||
        !VirtualAlloc(m->base + (OPC_LISTENER_VTABLE_RVA & ~0xfffu), 4096, MEM_COMMIT, PAGE_READWRITE)) return FALSE;
    *(uintptr_t *)(m->base + OPC_CLIENT_GLOBAL_RVA) = (uintptr_t)m->owner;
    *(uintptr_t *)m->owner = (uintptr_t)m->base + OPC_CLIENT_VTABLE_RVA;
    *(uintptr_t *)(m->owner + 0x1498) = (uintptr_t)m->network;
    *(uintptr_t *)m->network = (uintptr_t)m->base + OPC_NETWORK_VTABLE_RVA;
    *(uintptr_t *)(m->network + 0x48) = (uintptr_t)m->base + OPC_SOCKET_VTABLE_RVA;
    *(uintptr_t *)(m->base + OPC_LISTENER_VTABLE_RVA + 8) = (uintptr_t)m->base + OPC_LISTENER_ADAPTER_RVA;
    m->listener[0] = (uintptr_t)m->base + OPC_LISTENER_VTABLE_RVA;
    m->listener[1] = 2; m->listener[2] = (uintptr_t)callback; m->listener[4] = (uintptr_t)m->owner;
    *(uintptr_t *)(m->network + 0x101d8) = (uintptr_t)m->listener;
    return TRUE;
}
static void destroy_model(Model *m) {
    VirtualFree(m->listener, 0, MEM_RELEASE); VirtualFree(m->network, 0, MEM_RELEASE);
    VirtualFree(m->owner, 0, MEM_RELEASE); VirtualFree(m->base, 0, MEM_RELEASE);
}
static BOOL emit(Model *m, int damage, uintptr_t expected) {
    unsigned char packet[84] = {0}; packet[0] = 84; packet[5] = 4; packet[6] = 0x52;
    memcpy(packet + 0x30, &damage, 4);
    return ((Callback)m->listener[2])(m->owner, packet) == expected;
}
static DWORD WINAPI blocked_forward(void *model) { return emit(model, 77, 101) ? 0 : 1; }
static BOOL set_output(OpcTestConfig *config, const wchar_t *root, const wchar_t *part) {
    return swprintf(config->config.output_directory, OPC_PATH_CHARS, L"%ls\\%ls", root, part) > 0 &&
        CreateDirectoryW(config->config.output_directory, NULL);
}
int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    HMODULE dll = LoadLibraryW(argv[1]);
    if (!dll) return fail("load fixture core");
    OpcControl observe = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStartTest");
    OpcControl finish = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStop");
    OpcControl refresh = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureTestRefreshProfile");
    OpcControl start = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStartTest");
    OpcControl stop = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureStop");
    OpcControl poll = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureMaintain");
    OpcControl health = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureGetHealth");
    OpcControl deny = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureTestSetGuardDenied");
    if (!observe || !finish || !refresh || !start || !stop || !poll || !health || !deny) return fail("fixture exports");
    wchar_t root[OPC_PATH_CHARS];
    if (!GetFullPathNameW(argv[2], OPC_PATH_CHARS, root, NULL) || !CreateDirectoryW(root, NULL)) return fail("root");
    Model old = {0}, next = {0};
    if (!create_model(&old, original_old) || !create_model(&next, original_new)) return fail("models");
    OpcTestConfig config = {0}; config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    config.profile_base = old.base; config.callback = (void *)(uintptr_t)old.original;
    swprintf(config.config.output_directory, OPC_PATH_CHARS, L"%ls\\identity-context", root);
    if (observe(&config) || !emit(&old, 1, 101)) return fail("initial observation");
    config.profile_base = next.base; config.callback = (void *)(uintptr_t)next.original;
    if (refresh(&config) != ERROR_REVISION_MISMATCH || next.listener[2] != (uintptr_t)next.original)
        return fail("relocation without observed unload refused");
    if (refresh(NULL)) return fail("observe module absent");
    InterlockedExchange(&block_old, 1);
    HANDLE sender = CreateThread(NULL, 0, blocked_forward, &old, 0, NULL);
    if (!sender) return fail("old forwarding thread");
    ULONGLONG deadline = GetTickCount64() + 2000;
    while (!InterlockedCompareExchange(&entered_old, 0, 0)) {
        if (GetTickCount64() > deadline) return fail("old forwarding entered");
        Sleep(1);
    }
    if (refresh(&config) != ERROR_RETRY || next.listener[2] != (uintptr_t)next.original)
        return fail("in-flight old original prevents target switch");
    InterlockedExchange(&block_old, 0);
    if (WaitForSingleObject(sender, 2000) != WAIT_OBJECT_0) return fail("old forwarding completes");
    DWORD code; GetExitCodeThread(sender, &code); CloseHandle(sender);
    if (code) return fail("old forwarding return preserved");
    destroy_model(&old); /* Old addresses are deliberately inaccessible now. */
    next.listener[2] = (uintptr_t)foreign;
    if (refresh(&config) != ERROR_INVALID_DATA || next.listener[2] != (uintptr_t)foreign)
        return fail("foreign replacement refused");
    next.listener[2] = (uintptr_t)next.original;
    if (deny((void *)(uintptr_t)1) || refresh(&config) != ERROR_ACCESS_DENIED ||
        next.listener[2] != (uintptr_t)next.original || deny(NULL)) return fail("guard denial preserved");
    if (refresh(&config) || poll(NULL)) return fail("verified model reattached");
    OpcHealth state = {0}; state.size = sizeof(state); state.abi = OPC_ABI;
    if (health(&state) || state.binding_generation != 2 || state.connection_gaps != 1 || !state.observing)
        return fail("observer and generation survive relocation");
    if (!set_output(&config, root, L"part-1") || start(&config) || !emit(&next, 1001, 202) || stop(NULL))
        return fail("record after module relocation");
    if (refresh(NULL)) return fail("same-address module absence");
    next.listener[2] = (uintptr_t)next.original;
    if (refresh(&config) || poll(NULL) || !set_output(&config, root, L"part-2") || start(&config) ||
        !emit(&next, 2002, 202) || stop(NULL)) return fail("same-address reload and drain");
    if (!set_output(&config, root, L"part-3") || start(&config) || !emit(&next, 3001, 202) || refresh(NULL))
        return fail("uninterrupted recording before reload");
    if (health(&state) || !state.requested || state.recording) return fail("recording waits through module absence");
    Model third = {0};
    if (!create_model(&third, original_old)) return fail("third model");
    config.profile_base = third.base; config.callback = (void *)(uintptr_t)third.original;
    destroy_model(&next);
    if (refresh(&config) || poll(NULL) || !emit(&third, 3002, 101) || stop(NULL) || finish(NULL))
        return fail("recording resumes after relocation without a new file");
    if (health(&state) || state.binding_generation != 4 || state.connection_gaps != 3 || state.observing ||
        state.context_error || state.io_error || state.guard_fatal || calls != 6) return fail("final health and forwarding counts");
    puts("PASS: relocated/same-address module reload, old forwarding barrier, freed old objects, foreign/guard refusal, observer continuity, original returns");
    return 0;
}
