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
static DWORD WINAPI deny_suspend(HANDLE thread) {
    (void)thread;
    SetLastError(ERROR_ACCESS_DENIED);
    return (DWORD)-1;
}
static OpcThreadOps real_ops;
static HANDLE retiring_thread;
static DWORD retiring_id, retiring_code;
static volatile LONG retire_requested;
static unsigned race_observed;
static DWORD WINAPI retire(void *unused) {
    (void)unused;
    while (!InterlockedCompareExchange(&retire_requested, 0, 0)) Sleep(0);
    return retiring_code;
}
static BOOL finish_retiring_thread(void) {
    InterlockedExchange(&retire_requested, 1);
    return WaitForSingleObject(retiring_thread, 2000) == WAIT_OBJECT_0;
}
static DWORD WINAPI exit_before_suspend(HANDLE thread) {
    if (GetThreadId(thread) != retiring_id) return real_ops.suspend_thread(thread);
    if (!finish_retiring_thread()) return real_ops.suspend_thread(thread);
    ++race_observed;
    SetLastError(ERROR_ACCESS_DENIED);
    return (DWORD)-1;
}
static BOOL WINAPI exit_before_context(HANDLE thread, LPCONTEXT context) {
    if (GetThreadId(thread) != retiring_id) return real_ops.get_context(thread, context);
    if (real_ops.resume_thread(thread) == (DWORD)-1 || !finish_retiring_thread()) return FALSE;
    ++race_observed;
    SetLastError(ERROR_ACCESS_DENIED);
    return FALSE;
}
static DWORD WINAPI exit_during_resume(HANDLE thread) {
    DWORD result = real_ops.resume_thread(thread);
    if (GetThreadId(thread) != retiring_id || result == (DWORD)-1) return result;
    if (!finish_retiring_thread()) return (DWORD)-1;
    ++race_observed;
    SetLastError(ERROR_ACCESS_DENIED);
    return (DWORD)-1;
}
static int check_exit_race(const OpcThreadOps *ops, unsigned phase, DWORD exit_code) {
    real_ops = *ops;
    retiring_code = exit_code;
    InterlockedExchange(&retire_requested, 0);
    race_observed = 0;
    retiring_thread = CreateThread(NULL, 0, retire, NULL, 0, &retiring_id);
    if (!retiring_thread) return fail("retiring fixture thread");
    OpcThreadOps raced = *ops;
    if (phase == 0) raced.suspend_thread = exit_before_suspend;
    if (phase == 1) raced.get_context = exit_before_context;
    if (phase == 2) raced.resume_thread = exit_during_resume;
    OpcThreadGuard guard;
    DWORD error = opc_guard_enter(&guard, &raced);
    if (exit_code == STILL_ACTIVE) {
        if (error != ERROR_ACCESS_DENIED || guard.count ||
            guard.diagnostics.failed_thread_exit_code != STILL_ACTIVE ||
            guard.diagnostics.operation_error != ERROR_ACCESS_DENIED)
            return fail("ambiguous exit status remains fail-closed");
    } else {
        if (error) { SetLastError(error); return fail("terminated peer permits complete guard"); }
        if (opc_guard_leave(&guard)) return fail("terminated peer cleanup");
    }
    if (race_observed != 1) return fail("exit race fixture was exercised");
    CloseHandle(retiring_thread);
    return 0;
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
    if (guard.stage != 4 || !guard.diagnostics.failed_thread_id ||
        guard.diagnostics.failed_thread_id == guard.owner_id ||
        guard.diagnostics.failed_thread_exit_code != STILL_ACTIVE ||
        guard.diagnostics.failed_thread_query_error || guard.diagnostics.resume_error ||
        guard.diagnostics.operation_error != ERROR_ACCESS_DENIED)
        return fail("context denial diagnostics");
    denied = ops;
    denied.suspend_thread = deny_suspend;
    if (opc_guard_enter(&guard, &denied) != ERROR_ACCESS_DENIED || guard.count ||
        guard.stage != 3 || !guard.diagnostics.failed_thread_id ||
        guard.diagnostics.failed_thread_exit_code != STILL_ACTIVE ||
        guard.diagnostics.failed_thread_query_error || guard.diagnostics.resume_error ||
        guard.diagnostics.operation_error != ERROR_ACCESS_DENIED)
        return fail("suspend denial diagnostics and rollback");
    LONG before = InterlockedCompareExchange(&ticks, 0, 0);
    Sleep(20);
    if (InterlockedCompareExchange(&ticks, 0, 0) == before) return fail("worker resumed after failure");
    for (unsigned phase = 0; phase < 3; ++phase) {
        if (check_exit_race(&ops, phase, 0)) return 1;
    }
    if (check_exit_race(&ops, 0, STILL_ACTIVE)) return 1;
    if (ResumeThread(held) != 1) return fail("previous suspend count preserved");
    InterlockedExchange(&done, 1);
    HANDLE threads[2] = {active, held};
    if (WaitForMultipleObjects(2, threads, TRUE, 2000) != WAIT_OBJECT_0) return fail("threads finish");
    CloseHandle(active); CloseHandle(held);
    puts("PASS: complete guard, terminated-thread races, live denial rollback, ambiguous status refusal, prior suspend count preserved");
    return 0;
}
