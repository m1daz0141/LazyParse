#include "thread_guard.h"
#include <stdint.h>

DWORD opc_thread_ops_init(OpcThreadOps *ops) {
    ops->next_thread = (OpcNextThread)(uintptr_t)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtGetNextThread");
    ops->suspend_thread = SuspendThread;
    ops->resume_thread = ResumeThread;
    ops->get_context = GetThreadContext;
    return ops->next_thread ? ERROR_SUCCESS : ERROR_PROC_NOT_FOUND;
}

DWORD opc_guard_leave(OpcThreadGuard *g) {
    DWORD error = ERROR_SUCCESS;
    for (unsigned i = g->count; i; --i) {
        OpcGuardEntry *e = &g->threads[i - 1];
        if (e->suspended) {
            /* Undo precisely our one increment, preserving prior suspension. */
            if (g->ops->resume_thread(e->handle) == (DWORD)-1) error = GetLastError();
            e->suspended = FALSE;
        }
        CloseHandle(e->handle);
    }
    g->count = 0;
    g->resume_error = error;
    return error;
}

DWORD opc_guard_enter(OpcThreadGuard *g, const OpcThreadOps *ops) {
    g->count = 0;
    g->stage = g->native_status = g->resume_error = g->suspended_count = 0;
    g->ops = ops;
    g->owner_id = GetCurrentThreadId();
    g->deadline = GetTickCount64() + 100;
    if (!ops->next_thread) return ERROR_PROC_NOT_FOUND;
    DWORD error = ERROR_SUCCESS;
    unsigned stable = 0;
    /* Re-enumerate after suspension so a thread created during enumeration
       cannot silently escape the guard. Any incomplete pass cancels writes. */
    for (unsigned pass = 0; pass < 8 && stable < 2; ++pass) {
        HANDLE cursor = NULL;
        BOOL temporary_cursor = FALSE;
        unsigned added = 0;
        for (;;) {
            HANDLE next = NULL;
            g->stage = 1; /* enumerate */
            LONG status = ops->next_thread(GetCurrentProcess(), cursor,
                THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_LIMITED_INFORMATION,
                0, 0, &next);
            if (temporary_cursor) CloseHandle(cursor);
            cursor = NULL;
            temporary_cursor = FALSE;
            if ((ULONG)status == 0x8000001aUL) break; /* STATUS_NO_MORE_ENTRIES */
            if (status < 0) {
                g->native_status = (DWORD)status;
                error = (ULONG)status == 0xc0000022UL ? ERROR_ACCESS_DENIED : ERROR_GEN_FAILURE;
                goto fail;
            }
            cursor = next;
            temporary_cursor = TRUE;
            g->stage = 2; /* identify */
            DWORD id = GetThreadId(next);
            if (!id) { error = GetLastError(); CloseHandle(next); goto fail; }
            BOOL known = FALSE;
            for (unsigned i = 0; i < g->count; ++i) if (g->threads[i].id == id) { known = TRUE; break; }
            if (!known) {
                if (g->count == OPC_GUARD_THREADS) { CloseHandle(next); error = ERROR_TOO_MANY_TCBS; goto fail; }
                OpcGuardEntry *e = &g->threads[g->count++];
                e->handle = next; e->id = id; e->suspended = FALSE;
                temporary_cursor = FALSE;
                if (id != g->owner_id) {
                    g->stage = 3; /* suspend */
                    if (ops->suspend_thread(next) == (DWORD)-1) { error = GetLastError(); goto fail; }
                    e->suspended = TRUE;
                    ++g->suspended_count;
                    /* Also synchronize with in-flight kernel work before the
                       protected read/update, as debugger context reads do. */
                    CONTEXT context = {0};
                    context.ContextFlags = CONTEXT_CONTROL;
                    g->stage = 4; /* context barrier */
                    if (!ops->get_context(next, &context)) { error = GetLastError(); goto fail; }
                }
                ++added;
            }
            if (GetTickCount64() >= g->deadline) {
                if (temporary_cursor) CloseHandle(cursor);
                error = ERROR_TIMEOUT; goto fail;
            }
        }
        stable = added ? 0 : stable + 1;
    }
    if (stable < 2) { error = ERROR_RETRY; goto fail; }
    g->stage = 5; /* all peer threads quiescent */
    return ERROR_SUCCESS;
fail: {
        DWORD resume_error = opc_guard_leave(g);
        return resume_error ? resume_error : error;
    }
}
