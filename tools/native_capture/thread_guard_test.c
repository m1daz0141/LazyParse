#include "thread_guard.h"
#include <stdio.h>

static volatile LONG done, ticks;
static DWORD WINAPI churn(void *unused) {
    (void)unused;
    while (!InterlockedCompareExchange(&done, 0, 0)) {
        InterlockedIncrement(&ticks);
        Sleep(0);
    }
    return 0;
}
static BOOL WINAPI deny_context(HANDLE thread, LPCONTEXT context) {
    (void)thread; (void)context;
    SetLastError(ERROR_ACCESS_DENIED);
    return FALSE;
}
static int fail(const char *message) {
    fprintf(stderr, "FAIL: %s (%lu)\n", message, GetLastError());
    return 1;
}
int main(void) {
    OpcThreadOps ops;
    if (opc_thread_ops_init(&ops)) return fail("thread APIs");
    HANDLE active = CreateThread(NULL, 0, churn, NULL, 0, NULL);
    HANDLE held = CreateThread(NULL, 0, churn, NULL, CREATE_SUSPENDED, NULL);
    if (!active || !held) return fail("fixture threads");
    for (unsigned iteration = 0; iteration < 30; ++iteration) {
        OpcThreadGuard guard;
        DWORD error = opc_guard_enter(&guard, &ops);
        if (error) { SetLastError(error); return fail("complete guard"); }
        LONG before = InterlockedCompareExchange(&ticks, 0, 0);
        for (volatile unsigned spin = 0; spin < 10000; ++spin) {}
        LONG after = InterlockedCompareExchange(&ticks, 0, 0);
        error = opc_guard_leave(&guard);
        if (before != after || error) return fail("threads stopped and restored");
    }
    OpcThreadOps denied = ops;
    denied.get_context = deny_context;
    OpcThreadGuard guard;
    if (opc_guard_enter(&guard, &denied) != ERROR_ACCESS_DENIED || guard.count)
        return fail("failed guard rolls back");
    LONG before = InterlockedCompareExchange(&ticks, 0, 0);
    Sleep(20);
    if (InterlockedCompareExchange(&ticks, 0, 0) == before) return fail("worker resumed after failure");
    if (ResumeThread(held) != 1) return fail("previous suspend count preserved");
    InterlockedExchange(&done, 1);
    HANDLE threads[2] = {active, held};
    if (WaitForMultipleObjects(2, threads, TRUE, 2000) != WAIT_OBJECT_0) return fail("threads finish");
    CloseHandle(active); CloseHandle(held);
    puts("PASS: complete thread guard, rollback on denied context, prior suspend count preserved");
    return 0;
}
