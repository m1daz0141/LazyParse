#include "binding_layout.h"

static int expect(DWORD actual, DWORD expected, const char *name) {
    if (actual == expected) return 0;
    fprintf(stderr, "%s: expected %lu, got %lu\n", name, expected, actual);
    return 1;
}

int main(void) {
    unsigned char *base = VirtualAlloc(NULL, 0x3ceb000, MEM_RESERVE, PAGE_NOACCESS);
    unsigned char *client = VirtualAlloc(NULL, 0x2000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    unsigned char *network = VirtualAlloc(NULL, 0x11000, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    uintptr_t *listener = VirtualAlloc(NULL, 4096, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!base || !client || !network || !listener ||
        !VirtualAlloc(base + 0x3cea000, 4096, MEM_COMMIT, PAGE_READWRITE) ||
        !VirtualAlloc(base + 0x34c1000, 4096, MEM_COMMIT, PAGE_READWRITE)) return 1;
    *(uintptr_t *)(base + 0x3ceacf8) = (uintptr_t)client;
    *(uintptr_t *)client = (uintptr_t)base + 0x35a5680;
    *(uintptr_t *)(client + 0x1498) = (uintptr_t)network;
    *(uintptr_t *)network = (uintptr_t)base + 0x35a5560;
    *(uintptr_t *)(network + 0x48) = (uintptr_t)base + 0x35a55e0;
    *(uintptr_t *)(network + 0x101d8) = (uintptr_t)listener;
    listener[0] = (uintptr_t)base + 0x34c19f0;
    listener[1] = 2;
    listener[2] = (uintptr_t)base + OPC_CALLBACK_RVA;
    listener[3] = 0;
    listener[4] = (uintptr_t)client;
    *(uintptr_t *)(base + 0x34c19f8) = (uintptr_t)base + 0x1770e0;
    BindingSnapshot s;
    void *target = base + OPC_CALLBACK_RVA;
    if (expect(binding_inspect((uintptr_t)base, target, &s), 0, "valid binding") ||
        s.method_slot != (uintptr_t)&listener[2]) return 1;
    listener[2]++;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_INVALID_DATA, "wrong callback")) return 1;
    listener[2]--;
    listener[3] = 1;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_INVALID_DATA, "nonzero adjustment")) return 1;
    listener[3] = 0;
    listener[4] = (uintptr_t)network;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_INVALID_DATA, "wrong owner")) return 1;
    listener[4] = (uintptr_t)client;
    DWORD old;
    if (!VirtualProtect(listener, 4096, PAGE_READONLY, &old)) return 1;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_NOACCESS, "read-only slot")) return 1;
    if (!VirtualProtect(listener, 4096, old, &old)) return 1;
    *(uintptr_t *)(client + 0x1498) = 0;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_NOT_READY, "missing network")) return 1;
    *(uintptr_t *)(client + 0x1498) = (uintptr_t)network;
    *(uintptr_t *)(base + 0x3ceacf8) = 0;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_NOT_READY, "missing client")) return 1;
    *(uintptr_t *)(base + 0x3ceacf8) = 1;
    if (expect(binding_inspect((uintptr_t)base, target, &s), ERROR_PARTIAL_COPY, "unreadable client")) return 1;
    puts("PASS: binding lookup, method/owner/adjustment validation, writable-slot gate, unavailable objects");
    VirtualFree(listener, 0, MEM_RELEASE);
    VirtualFree(network, 0, MEM_RELEASE);
    VirtualFree(client, 0, MEM_RELEASE);
    VirtualFree(base, 0, MEM_RELEASE);
    return 0;
}
