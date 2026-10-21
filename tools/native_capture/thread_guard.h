#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

/* Instrumentation guard for a short data-pointer update. No heap allocation,
   loader calls, file I/O or user locks are allowed between enter and leave.
   Handles are obtained through the ordinary ntdll export; no direct syscall. */
#define OPC_GUARD_THREADS 512
typedef LONG (NTAPI *OpcNextThread)(HANDLE, HANDLE, ACCESS_MASK, ULONG, ULONG, PHANDLE);
typedef struct {
    OpcNextThread next_thread;
    DWORD (WINAPI *suspend_thread)(HANDLE);
    DWORD (WINAPI *resume_thread)(HANDLE);
    BOOL (WINAPI *get_context)(HANDLE, LPCONTEXT);
    BOOL (WINAPI *same_object)(HANDLE, HANDLE);
} OpcThreadOps;
typedef struct {
    HANDLE handle;
    DWORD id;
    BOOL suspended;
} OpcGuardEntry;
typedef struct {
    DWORD owner_thread_id, failed_thread_id, failed_thread_exit_code;
    DWORD failed_thread_query_error, operation_error, resume_error;
} OpcGuardDiagnostics;
typedef struct {
    OpcGuardEntry threads[OPC_GUARD_THREADS];
    unsigned count;
    ULONGLONG deadline;
    DWORD owner_id;
    const OpcThreadOps *ops;
    DWORD stage, native_status, resume_error, suspended_count;
    OpcGuardDiagnostics diagnostics;
} OpcThreadGuard;

DWORD opc_thread_ops_init(OpcThreadOps *ops);
DWORD opc_guard_enter(OpcThreadGuard *guard, const OpcThreadOps *ops);
DWORD opc_guard_leave(OpcThreadGuard *guard);
/* Internal capture/autostart interface; public health ABI remains unchanged. */
void CaptureGuardDiagnostics(OpcGuardDiagnostics *out);
