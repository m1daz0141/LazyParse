#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include "atomic_file.h"

static BOOL put(const wchar_t *path, const char *text) {
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return FALSE;
    DWORD bytes;
    BOOL ok = WriteFile(file, text, 3, &bytes, NULL) && bytes == 3;
    CloseHandle(file);
    return ok;
}
static DWORD WINAPI release_reader(void *argument) {
    Sleep(30);
    CloseHandle((HANDLE)argument);
    return 0;
}
int wmain(int argc, wchar_t **argv) {
    if (argc != 2) return 2;
    wchar_t path[4096], temp[4096];
    swprintf(path, 4096, L"%ls\\status.json", argv[1]);
    swprintf(temp, 4096, L"%ls\\status.tmp", argv[1]);
    if (!put(path, "old") || !put(temp, "new")) return 3;
    HANDLE reader = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_EXISTING, 0, NULL);
    if (reader == INVALID_HANDLE_VALUE) return 4;
    HANDLE thread = CreateThread(NULL, 0, release_reader, reader, 0, NULL);
    if (!thread) { CloseHandle(reader); return 5; }
    DWORD error = opc_replace_published_file(temp, path);
    WaitForSingleObject(thread, INFINITE); CloseHandle(thread);
    if (error) return 6;
    puts("PASS: temporary reader lock recovers");

    if (!put(temp, "old")) return 7;
    reader = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                         NULL, OPEN_EXISTING, 0, NULL);
    if (reader == INVALID_HANDLE_VALUE) return 8;
    ULONGLONG started = GetTickCount64();
    error = opc_replace_published_file(temp, path);
    ULONGLONG elapsed = GetTickCount64() - started;
    CloseHandle(reader);
    if ((error != ERROR_ACCESS_DENIED && error != ERROR_SHARING_VIOLATION && error != ERROR_LOCK_VIOLATION) || elapsed > 1000)
        return 9;
    if (opc_replace_published_file(temp, path)) return 10;
    printf("PASS: permanent lock returns original Win32=%lu within bound\n", error);

    if (!put(temp, "new")) return 11;
    reader = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                         NULL, OPEN_EXISTING, 0, NULL);
    if (reader == INVALID_HANDLE_VALUE) return 12;
    error = opc_replace_published_file(temp, path);
    char data[3]; DWORD bytes;
    BOOL old_snapshot = ReadFile(reader, data, 3, &bytes, NULL) && bytes == 3 && data[0] == 'o';
    CloseHandle(reader);
    if (error || !old_snapshot) return 13;
    puts("PASS: shared-delete reader keeps old snapshot while publication succeeds");
    return 0;
}
