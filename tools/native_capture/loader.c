/* Ordinary Windows DLL loading only. No drivers, manual mapping, privilege
   adjustment, anti-cheat changes or retry/fallback injection paths. */
#include "win_util.h"
#include <tlhelp32.h>
#include <stdlib.h>

#ifdef OVERPARSE_LOADER_TEST
#define TARGET_NAME L"CaptureRemoteTestHost.exe"
#define DLL_NAME L"CaptureLoaderFixture.dll"
#else
#define TARGET_NAME L"pso2.exe"
#define DLL_NAME L"OverParseCapture.dll"
#endif

static DWORD fail(const char *operation, DWORD error) {
    fprintf(stderr, "%s failed: Win32=%lu\n", operation, error);
    return error ? error : ERROR_FUNCTION_FAILED;
}

static const wchar_t *basename_of(const wchar_t *path) {
    const wchar_t *back = wcsrchr(path, L'\\');
    return back ? back + 1 : path;
}

static void print_integrity(HANDLE process, const char *label) {
    HANDLE token;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) {
        printf("%s token query: Win32=%lu\n", label, GetLastError()); return;
    }
    unsigned char data[1024]; DWORD needed;
    if (GetTokenInformation(token, TokenIntegrityLevel, data, sizeof(data), &needed)) {
        TOKEN_MANDATORY_LABEL *mandatory = (TOKEN_MANDATORY_LABEL *)data;
        UCHAR count = *GetSidSubAuthorityCount(mandatory->Label.Sid);
        DWORD rid = *GetSidSubAuthority(mandatory->Label.Sid, count - 1);
        printf("%s integrity RID=0x%lx (%s)\n", label, rid,
               rid >= SECURITY_MANDATORY_HIGH_RID ? "high or system" : "medium or lower");
    } else printf("%s integrity query: Win32=%lu\n", label, GetLastError());
    CloseHandle(token);
}

static DWORD find_module(DWORD pid, const wchar_t *name, MODULEENTRY32W *result) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snap == INVALID_HANDLE_VALUE) return GetLastError();
    MODULEENTRY32W item = {0}; item.dwSize = sizeof(item);
    DWORD error = ERROR_MOD_NOT_FOUND;
    if (Module32FirstW(snap, &item)) {
        do {
            if (!_wcsicmp(item.szModule, name)) { *result = item; error = 0; break; }
        } while (Module32NextW(snap, &item));
    } else error = GetLastError();
    CloseHandle(snap);
    return error;
}

static int list_targets(void) {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return (int)fail("process snapshot", GetLastError());
    PROCESSENTRY32W entry = {0}; entry.dwSize = sizeof(entry);
    unsigned found = 0;
    if (Process32FirstW(snap, &entry)) do {
        if (!_wcsicmp(entry.szExeFile, TARGET_NAME)) {
            MODULEENTRY32W classic;
            DWORD error = find_module(entry.th32ProcessID, L"pso2classic.dll", &classic);
            printf("PID=%lu ClassicModule=%s Win32=%lu\n", entry.th32ProcessID,
                   error ? "unavailable" : "loaded", error);
            ++found;
        }
    } while (Process32NextW(snap, &entry));
    CloseHandle(snap);
    if (!found) puts("No target process is running.");
    return 0;
}

static DWORD check_target(DWORD pid, HANDLE *process, BOOL modifying) {
    DWORD rights = PROCESS_QUERY_LIMITED_INFORMATION;
    if (modifying) rights |= PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                            PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ;
    *process = OpenProcess(rights, FALSE, pid);
    if (!*process) return GetLastError();
    wchar_t path[32768]; DWORD length = 32768;
    if (!QueryFullProcessImageNameW(*process, 0, path, &length)) return GetLastError();
    if (_wcsicmp(basename_of(path), TARGET_NAME)) return ERROR_INVALID_NAME;
    BOOL wow64;
    if (!IsWow64Process(*process, &wow64)) return GetLastError();
    if (wow64) return ERROR_BAD_EXE_FORMAT;
    wprintf(L"Target: PID=%lu %ls\n", pid, path);
    print_integrity(GetCurrentProcess(), "Loader");
    print_integrity(*process, "Target");
    return 0;
}

static DWORD check_classic(DWORD pid) {
#ifdef OVERPARSE_LOADER_TEST
    (void)pid;
    return 0;
#else
    MODULEENTRY32W classic;
    DWORD error = find_module(pid, L"pso2classic.dll", &classic);
    if (error) return error;
    char hash[65];
    error = opc_hash_file(classic.szExePath, hash);
    if (error) return error;
    if (strcmp(hash, OPC_CLASSIC_SHA256)) return ERROR_REVISION_MISMATCH;
    wprintf(L"Classic: %ls\n", classic.szExePath);
    printf("Classic SHA-256: %s\n", hash);
    return 0;
#endif
}

static DWORD remote_call(HANDLE process, void *function, void *argument, DWORD *code) {
    HANDLE thread = CreateRemoteThread(process, NULL, 0, (LPTHREAD_START_ROUTINE)function,
                                       argument, 0, NULL);
    if (!thread) return GetLastError();
    DWORD wait = WaitForSingleObject(thread, 15000);
    DWORD error = ERROR_SUCCESS;
    if (wait != WAIT_OBJECT_0) error = wait == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
    else if (!GetExitCodeThread(thread, code)) error = GetLastError();
    CloseHandle(thread);
    return error;
}

/* Resolve the actual owner (KernelBase if forwarded), then use the same RVA
   in the target's corresponding module, rather than assuming equal ASLR bases. */
static DWORD remote_loadlibrary_address(DWORD pid, void **result) {
    FARPROC address = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    HMODULE owner;
    if (!address || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                       (LPCWSTR)(uintptr_t)address, &owner)) return GetLastError();
    wchar_t path[32768];
    if (!GetModuleFileNameW(owner, path, 32768)) return GetLastError();
    MODULEENTRY32W remote;
    DWORD error = find_module(pid, basename_of(path), &remote);
    if (error) return error;
    uintptr_t rva = (uintptr_t)address - (uintptr_t)owner;
    if (rva >= remote.modBaseSize) return ERROR_INVALID_ADDRESS;
    *result = remote.modBaseAddr + rva;
    return 0;
}

static DWORD local_export_rva(const wchar_t *dll, const char *name, uintptr_t *rva) {
    /* Map for export lookup without running DllMain or resolving dependencies. */
    HMODULE local = LoadLibraryExW(dll, NULL, DONT_RESOLVE_DLL_REFERENCES);
    if (!local) return GetLastError();
    FARPROC function = GetProcAddress(local, name);
    DWORD error = function ? 0 : GetLastError();
    if (function) *rva = (uintptr_t)function - (uintptr_t)local;
    FreeLibrary(local);
    return error;
}

int wmain(int argc, wchar_t **argv) {
    if (argc == 2 && !wcscmp(argv[1], L"list")) return list_targets();
    BOOL attach = argc == 5 && !wcscmp(argv[1], L"attach");
    BOOL stop = argc == 3 && !wcscmp(argv[1], L"stop");
    BOOL probe = argc == 3 && !wcscmp(argv[1], L"probe");
    if (!attach && !stop && !probe) {
        fputs("Usage:\n  CaptureLoader.exe list\n  CaptureLoader.exe probe <PID>\n"
              "  CaptureLoader.exe attach <PID> <OverParseCapture.dll> <new-output-dir>\n"
              "  CaptureLoader.exe stop <PID>\n", stderr);
        return 2;
    }
    wchar_t *end;
    unsigned long parsed = wcstoul(argv[2], &end, 10);
    if (!parsed || *end) return (int)fail("PID", ERROR_INVALID_PARAMETER);
    DWORD pid = (DWORD)parsed, error, code = 0;
    HANDLE process = NULL;
    void *remote_arg = NULL;
    BOOL argument_may_be_in_use = FALSE;
    error = check_target(pid, &process, !probe);
    if (error) goto done;
    if (!stop) { error = check_classic(pid); if (error) goto done; }
    if (probe) { puts("Read-only preflight succeeded; no DLL was loaded."); goto done; }

    MODULEENTRY32W capture;
    if (stop) {
        error = find_module(pid, DLL_NAME, &capture);
        if (error) goto done;
        uintptr_t rva;
        error = local_export_rva(capture.szExePath, "CaptureStop", &rva);
        if (error) goto done;
        error = remote_call(process, capture.modBaseAddr + rva, NULL, &code);
        if (!error) error = code;
        if (!error) puts("Recording stopped and flushed. Forwarding hook remains until game exit.");
        goto done;
    }
    /* Avoid accumulating references or enabling twice. */
    error = find_module(pid, DLL_NAME, &capture);
    if (!error) { error = ERROR_ALREADY_INITIALIZED; goto done; }
    if (error != ERROR_MOD_NOT_FOUND) goto done;
    wchar_t dll[32768];
    DWORD n = GetFullPathNameW(argv[3], 32768, dll, NULL);
    if (!n || n >= 32768) { error = ERROR_BAD_PATHNAME; goto done; }
    if (_wcsicmp(basename_of(dll), DLL_NAME)) { error = ERROR_INVALID_NAME; goto done; }
    uintptr_t start_rva;
    error = local_export_rva(dll, "CaptureStart", &start_rva);
    if (error) goto done;
    OpcConfig config = {0}; config.size = sizeof(config); config.abi = OPC_ABI;
    n = GetFullPathNameW(argv[4], OPC_PATH_CHARS, config.output_directory, NULL);
    if (!n || n >= OPC_PATH_CHARS || config.output_directory[1] != L':' ||
        config.output_directory[2] != L'\\') { error = ERROR_BAD_PATHNAME; goto done; }
    if (!CreateDirectoryW(config.output_directory, NULL)) { error = GetLastError(); goto done; }
    size_t dll_bytes = (wcslen(dll) + 1) * sizeof(wchar_t);
    size_t arg_bytes = dll_bytes > sizeof(config) ? dll_bytes : sizeof(config);
    remote_arg = VirtualAllocEx(process, NULL, arg_bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!remote_arg) { error = GetLastError(); goto done; }
    if (!WriteProcessMemory(process, remote_arg, dll, dll_bytes, NULL)) { error = GetLastError(); goto done; }
    void *loadlibrary;
    error = remote_loadlibrary_address(pid, &loadlibrary);
    if (error) goto done;
    wprintf(L"Loading: %ls\n", dll);
    error = remote_call(process, loadlibrary, remote_arg, &code);
    if (error) { argument_may_be_in_use = TRUE; goto done; }
    /* GetExitCodeThread truncates HMODULE on x64; enumerate instead. */
    error = find_module(pid, DLL_NAME, &capture);
    if (error) goto done;
    if (_wcsicmp(capture.szExePath, dll)) { error = ERROR_INVALID_NAME; goto done; }
    if (start_rva >= capture.modBaseSize) { error = ERROR_INVALID_ADDRESS; goto done; }
    if (!WriteProcessMemory(process, remote_arg, &config, sizeof(config), NULL)) { error = GetLastError(); goto done; }
    error = remote_call(process, capture.modBaseAddr + start_rva, remote_arg, &code);
    if (error) { argument_may_be_in_use = TRUE; goto done; }
    error = code;
    if (!error) wprintf(L"Recording: %ls\\capture.opcap\n", config.output_directory);
done:
    if (remote_arg && !argument_may_be_in_use) VirtualFreeEx(process, remote_arg, 0, MEM_RELEASE);
    if (process) CloseHandle(process);
    if (error) {
        fail("capture control", error);
        if (argument_may_be_in_use) fputs("Remote thread status is uncertain; its argument buffer was retained. Exit the target normally before another attempt.\n", stderr);
        fputs("No fallback or anti-cheat changes were attempted.\n", stderr);
    }
    return error ? 1 : 0;
}
