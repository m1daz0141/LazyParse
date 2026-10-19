#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    HMODULE loaded = LoadLibraryW(argv[1]);
    DWORD error = GetLastError();
    if (loaded || error != ERROR_DLL_INIT_FAILED) {
        fprintf(stderr, "Bootstrap result unexpected: %lu\n", error); return 1;
    }
    wchar_t state[32768], stop[32768];
    swprintf(state, 32768, L"%ls\\autostart-state.json", argv[2]);
    char pid_marker[64], run[96] = {0};
    snprintf(pid_marker, sizeof(pid_marker), "\"pid\":%lu,", GetCurrentProcessId());
    BOOL observed = FALSE;
    for (unsigned i = 0; i < 100; ++i) {
        HANDLE file = CreateFileW(state, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                                  NULL, OPEN_EXISTING, 0, NULL);
        if (file != INVALID_HANDLE_VALUE) {
            char buf[4096] = {0}; DWORD n;
            if (ReadFile(file, buf, sizeof(buf)-1, &n, NULL) && strstr(buf, "waiting_for_classic") && strstr(buf, pid_marker)) {
                const char *p = strstr(buf, "\"run_name\":\"");
                if (p) {
                    p += strlen("\"run_name\":\"");
                    const char *end = strchr(p, '"');
                    if (end && end - p < 96) { memcpy(run, p, (size_t)(end - p)); observed = TRUE; }
                }
            }
            CloseHandle(file);
        }
        if (observed) break;
        Sleep(50);
    }
    if (!observed) { fputs("Worker state not observed\n", stderr); return 1; }
    const wchar_t *base = wcsrchr(argv[1], L'\\'); base = base ? base + 1 : argv[1];
    if (GetModuleHandleW(base)) return 1;
    swprintf(stop, 32768, L"%ls\\%hs\\control.command", argv[2], run);
    HANDLE signal = CreateFileW(stop, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (signal == INVALID_HANDLE_VALUE) return 1;
    const char command[] = "OPC-CONTROL-1\n1\nshutdown\n";
    DWORD written;
    if (!WriteFile(signal, command, sizeof(command) - 1, &written, NULL) || written != sizeof(command) - 1) return 1;
    CloseHandle(signal);
    for (unsigned i = 0; i < 100; ++i) {
        HANDLE file = CreateFileW(state, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                                  NULL, OPEN_EXISTING, 0, NULL);
        char buf[4096] = {0}; DWORD n = 0;
        if (file != INVALID_HANDLE_VALUE) {
            ReadFile(file, buf, sizeof(buf)-1, &n, NULL); CloseHandle(file);
        }
        if (strstr(buf, "\"state\":\"shutdown\"") && strstr(buf, pid_marker)) {
            printf("PASS: optional bootstrap unloaded; capture worker ran and stopped outside DLL initialization\n");
            return 0;
        }
        Sleep(50);
    }
    return 1;
}
