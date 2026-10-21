/* Optional-library entry. Returns FALSE like the tested marker probe.
   Its worker entry is Windows' LoadLibraryW, never code from this DLL, which
   will be unloaded after failed initialization. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>

#ifdef LAZYPARSE_PORTABLE
/* Deployment fills this fixed UTF-16 field, preserving the PE layout. The
   stable path points outside the one-file GUI's temporary extraction folder. */
static volatile wchar_t portable_core_path[1024] = L"LAZYPARSE_CORE_PATH_V1";
#define AUTO_CAPTURE_DLL_PATH ((const wchar_t *)portable_core_path)
#ifdef OVERPARSE_AUTOSTART_TEST
/* Synthetic hosts must not share the live game's one-attempt guard. */
#define AUTO_GUARD_NAME L"Local\\LazyParsePortableCaptureTest"
#else
#define AUTO_GUARD_NAME L"Local\\LazyParsePortableCapture"
#endif
#elif !defined(AUTO_CAPTURE_DLL_PATH)
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
    const wchar_t *path = AUTO_CAPTURE_DLL_PATH;
    size_t path_bytes = (wcslen(path) + 1) * sizeof(wchar_t);
    wchar_t *owned_path = HeapAlloc(GetProcessHeap(), 0, path_bytes);
    if (!owned_path) { CloseHandle(guard); return FALSE; }
    memcpy(owned_path, path, path_bytes);
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
