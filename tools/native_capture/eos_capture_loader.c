/* Optional-library entry. Returns FALSE like the tested marker probe.
   Its worker entry is Windows' LoadLibraryW, never code from this DLL, which
   will be unloaded after failed initialization. All paths are build-pinned. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>

#ifndef AUTO_CAPTURE_DLL_PATH
#error Build with build_autostart.ps1
#endif

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)module; (void)reserved;
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    /* One attempt per active session even if the client retries the optional load.
       The guard handle deliberately lives until process exit. */
    HANDLE guard = CreateMutexW(NULL, FALSE, AUTO_GUARD_NAME);
    if (!guard) return FALSE;
    if (GetLastError() == ERROR_ALREADY_EXISTS) { CloseHandle(guard); return FALSE; }
    const wchar_t path[] = AUTO_CAPTURE_DLL_PATH;
    wchar_t *owned_path = HeapAlloc(GetProcessHeap(), 0, sizeof(path));
    if (!owned_path) { CloseHandle(guard); return FALSE; }
    memcpy(owned_path, path, sizeof(path));
    FARPROC load = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HANDLE thread = load ? CreateThread(NULL, 0, (LPTHREAD_START_ROUTINE)(uintptr_t)load,
                                        owned_path, 0, NULL) : NULL;
    if (!thread) {
        HeapFree(GetProcessHeap(), 0, owned_path);
        CloseHandle(guard);
    } else {
        CloseHandle(thread);
        /* Keep the short path allocation valid for the Windows worker until
           process exit. No pointer into this soon-unloaded DLL escapes. */
    }
    return FALSE;
}
