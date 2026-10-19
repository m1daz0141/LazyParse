/* A load-attempt marker only. Does not load another DLL or create a thread.
   The output path is supplied by the offline build in eos-probe-path.h. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>

#ifndef PROBE_OUTPUT_PATH
#error Build with build_eos_probe.ps1
#endif

typedef struct {
    char magic[8];
    uint32_t version;
    uint32_t process_id;
    FILETIME time;
} ProbeMarker;
_Static_assert(sizeof(ProbeMarker) == 24, "marker ABI");

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)module; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        ProbeMarker marker = {{'O','P','C','E','O','S','1',0},1,GetCurrentProcessId(),{0,0}};
        GetSystemTimeAsFileTime(&marker.time);
        HANDLE file = CreateFileW(PROBE_OUTPUT_PATH, GENERIC_WRITE, FILE_SHARE_READ,
                                  NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (file != INVALID_HANDLE_VALUE) {
            DWORD written;
            WriteFile(file, &marker, sizeof(marker), &written, NULL);
            CloseHandle(file);
        }
        /* Match a failed optional library load; do not remain as a partial EOS
           implementation or let the client call unimplemented EOS exports. */
        return FALSE;
    }
    return TRUE;
}
