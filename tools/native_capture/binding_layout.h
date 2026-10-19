#pragma once
#include "win_util.h"

/* Exact-build layout derived from the client constructor, listener factory,
   member-function adapter and packet dispatcher. No scan or page changes. */
typedef struct {
    uintptr_t client, network, listener, method_slot, method;
    const char *stage;
    DWORD error;
} BindingSnapshot;

static inline BOOL binding_read(uintptr_t address, void *out, SIZE_T bytes) {
    SIZE_T copied = 0;
    return address && ReadProcessMemory(GetCurrentProcess(), (void *)address, out, bytes, &copied) && copied == bytes;
}

static inline BOOL binding_writable_slot(void **slot) {
    MEMORY_BASIC_INFORMATION m;
    uintptr_t p = (uintptr_t)slot;
    return !(p & (sizeof(void *) - 1)) &&
        VirtualQuery(slot, &m, sizeof(m)) == sizeof(m) && m.State == MEM_COMMIT &&
        m.Type == MEM_PRIVATE && m.Protect == PAGE_READWRITE &&
        p >= (uintptr_t)m.BaseAddress && m.RegionSize >= sizeof(void *) &&
        p - (uintptr_t)m.BaseAddress <= m.RegionSize - sizeof(void *);
}

static inline DWORD binding_inspect_methods(uintptr_t base, void *expected_method, void *installed_method, BindingSnapshot *s) {
    memset(s, 0, sizeof(*s));
    s->stage = "client_global";
    if (!binding_read(base + 0x3ceacf8, &s->client, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (!s->client) return s->error = ERROR_NOT_READY;
    s->stage = "client_vtable";
    uintptr_t vtable;
    if (!binding_read(s->client, &vtable, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (vtable != base + 0x35a5680) return s->error = ERROR_INVALID_DATA;
    s->stage = "network_object";
    if (!binding_read(s->client + 0x1498, &s->network, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (!s->network) return s->error = ERROR_NOT_READY;
    if (!binding_read(s->network, &vtable, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (vtable != base + 0x35a5560) return s->error = ERROR_INVALID_DATA;
    s->stage = "socket_vtable";
    if (!binding_read(s->network + 0x48, &vtable, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (vtable != base + 0x35a55e0) return s->error = ERROR_INVALID_DATA;
    s->stage = "listener_object";
    if (!binding_read(s->network + 0x101d8, &s->listener, 8)) return s->error = ERROR_PARTIAL_COPY;
    if (!s->listener) return s->error = ERROR_NOT_READY;
    unsigned char object[0x30];
    if (!binding_read(s->listener, object, sizeof(object))) return s->error = ERROR_PARTIAL_COPY;
    uintptr_t method, owner, adapter;
    LONG refs, adjustment;
    memcpy(&vtable, object, 8);
    memcpy(&refs, object + 8, 4);
    memcpy(&method, object + 0x10, 8);
    s->method = method;
    memcpy(&adjustment, object + 0x18, 4);
    memcpy(&owner, object + 0x20, 8);
    s->stage = "listener_binding";
    if (vtable != base + 0x34c19f0 || (method != (uintptr_t)expected_method &&
        (!installed_method || method != (uintptr_t)installed_method)) ||
        owner != s->client || adjustment || refs <= 0 || refs > 1000000)
        return s->error = ERROR_INVALID_DATA;
    if (!binding_read(vtable + 8, &adapter, 8) || adapter != base + 0x1770e0)
        return s->error = ERROR_INVALID_DATA;
    s->method_slot = s->listener + 0x10;
    s->stage = "method_slot_protection";
    if (!binding_writable_slot((void **)s->method_slot)) return s->error = ERROR_NOACCESS;
    /* Read-only monitoring may race a transition. Mutating callers must
       repeat this entire check inside the thread guard. */
    uintptr_t current_client, current_network, current_listener;
    s->stage = "owner_recheck";
    if (!binding_read(base + 0x3ceacf8, &current_client, 8) ||
        !binding_read(s->client + 0x1498, &current_network, 8) ||
        !binding_read(s->network + 0x101d8, &current_listener, 8))
        return s->error = ERROR_PARTIAL_COPY;
    if (current_client != s->client || current_network != s->network || current_listener != s->listener)
        return s->error = ERROR_RETRY;
    s->stage = "binding_ready";
    return ERROR_SUCCESS;
}

static inline DWORD binding_inspect(uintptr_t base, void *expected_method, BindingSnapshot *s) {
    return binding_inspect_methods(base, expected_method, NULL, s);
}
