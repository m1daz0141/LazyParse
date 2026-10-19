/* Standalone Windows mapping compatibility fixture. Does not access PSO2. */
#include <windows.h>
#include <stdio.h>
#include <string.h>

typedef LONG (NTAPI *MapSection)(HANDLE, HANDLE, PVOID *, ULONG_PTR, SIZE_T,
                                PLARGE_INTEGER, PSIZE_T, int, ULONG, ULONG);

int main(int argc, char **argv) {
    BOOL immutable = argc == 2 && !strcmp(argv[1], "--immutable");
    if (argc != 1 && !immutable) return 2;
    HANDLE mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, NULL, PAGE_EXECUTE_READ,
                                        0, 4096, NULL);
    if (!mapping) { fprintf(stderr, "CreateFileMappingW: %lu\n", GetLastError()); return 1; }
    unsigned char *view = NULL;
    if (immutable) {
        /* Reproduce the loader's 0x400000 allocation flag in our own
           process only, using the ordinary ntdll export, not a syscall. */
        MapSection map = (MapSection)(uintptr_t)GetProcAddress(GetModuleHandleW(L"ntdll.dll"),
                                                              "NtMapViewOfSection");
        SIZE_T size = 4096;
        LONG status = map ? map(mapping, GetCurrentProcess(), (PVOID *)&view, 0, 0,
                                NULL, &size, 2, 0x400000, PAGE_EXECUTE_READ) : -1;
        if (status < 0) { fprintf(stderr, "Own-process map fixture: NTSTATUS=0x%lx\n", (ULONG)status); return 1; }
    } else {
        view = MapViewOfFile(mapping, FILE_MAP_READ | FILE_MAP_EXECUTE, 0, 0, 4096);
    }
    const unsigned char *control = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 4096);
    if (!view || !control) { fprintf(stderr, "MapViewOfFile: %lu\n", GetLastError()); return 1; }
    MEMORY_BASIC_INFORMATION before = {0}, after = {0};
    if (!VirtualQuery(view, &before, sizeof(before))) return 1;
    DWORD old = 0;
    SetLastError(0);
    BOOL rwx = VirtualProtect(view + 0xf0, 5, PAGE_EXECUTE_READWRITE, &old);
    DWORD rwx_error = rwx ? 0 : GetLastError();
    if (rwx && !VirtualProtect(view + 0xf0, 5, old, &old)) return 1;
    SetLastError(0);
    BOOL cow = VirtualProtect(view + 0xf0, 5, PAGE_EXECUTE_WRITECOPY, &old);
    DWORD cow_error = cow ? 0 : GetLastError();
    BOOL private_write = FALSE, restored = FALSE;
    if (cow) {
        view[0xf0] = 0x7a;
        private_write = view[0xf0] == 0x7a && control[0xf0] == 0;
        restored = VirtualProtect(view + 0xf0, 5, old, &old);
    }
    if (!VirtualQuery(view, &after, sizeof(after))) return 1;
    printf("{\"immutable\":%s,\"state\":%lu,\"type\":%lu,\"allocation_protection\":%lu,"
           "\"initial_protection\":%lu,\"rwx_success\":%s,\"rwx_error\":%lu,"
           "\"cow_success\":%s,\"cow_error\":%lu,\"write_is_private\":%s,"
           "\"restored\":%s,\"final_protection\":%lu}\n",
           immutable ? "true" : "false", before.State, before.Type, before.AllocationProtect, before.Protect,
           rwx ? "true" : "false", rwx_error, cow ? "true" : "false", cow_error,
           private_write ? "true" : "false", restored ? "true" : "false", after.Protect);
    UnmapViewOfFile(view);
    UnmapViewOfFile(control);
    CloseHandle(mapping);
    BOOL matched = before.State == MEM_COMMIT && before.Type == MEM_MAPPED &&
           before.AllocationProtect == PAGE_EXECUTE_READ && before.Protect == PAGE_EXECUTE_READ &&
           !rwx && rwx_error == ERROR_INVALID_PARAMETER && after.Protect == PAGE_EXECUTE_READ;
    if (immutable) return matched && !cow && cow_error == ERROR_INVALID_PARAMETER ? 0 : 1;
    return matched && cow && private_write && restored ? 0 : 1;
}
