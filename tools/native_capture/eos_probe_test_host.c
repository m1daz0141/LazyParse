#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    SetLastError(0);
    HMODULE dll = LoadLibraryW(argv[1]);
    DWORD error = GetLastError();
    if (dll || error != ERROR_DLL_INIT_FAILED) {
        fprintf(stderr, "Unexpected LoadLibrary result: Win32=%lu\n", error); return 1;
    }
    unsigned char marker[24] = {0}; DWORD bytes;
    HANDLE file = CreateFileW(argv[2], GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return 1;
    BOOL ok = ReadFile(file, marker, sizeof(marker), &bytes, NULL);
    CloseHandle(file);
    uint32_t version, pid;
    memcpy(&version, marker + 8, 4); memcpy(&pid, marker + 12, 4);
    if (!ok || bytes != 24 || memcmp(marker, "OPCEOS1\0", 8) || version != 1 || pid != GetCurrentProcessId())
        return 1;
    const wchar_t *name = wcsrchr(argv[1], L'\\'); name = name ? name + 1 : argv[1];
    if (GetModuleHandleW(name)) return 1;
    printf("PASS: load attempt recorded for PID=%lu; DLL returned FALSE and is not resident\n", GetCurrentProcessId());
    return 0;
}
