/* Observe access checks only: no target writes, allocation, thread creation,
   privilege adjustment, security descriptor changes or DLL loading. */
#include "capture_api.h"
#include <tlhelp32.h>
#include <aclapi.h>
#include <psapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

/* Microsoft documents this ObjectBasicInformation layout. It observes the
   granted mask of our own handle; it does not request or alter target rights. */
typedef struct {
    ULONG attributes;
    ACCESS_MASK granted;
    ULONG handle_count, pointer_count, reserved[10];
} PublicObjectBasicInfo;
typedef LONG (NTAPI *QueryObject)(HANDLE, ULONG, void *, ULONG, ULONG *);

static void print_granted_access(HANDLE handle, DWORD requested, const char *label) {
    QueryObject query = (QueryObject)(uintptr_t)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryObject");
    if (!query) { puts("NtQueryObject is unavailable"); return; }
    PublicObjectBasicInfo info = {0}; ULONG needed;
    LONG status = query(handle, 0, &info, sizeof(info), &needed);
    if (status >= 0)
        printf("GrantedAccess(%s) requested=0x%lx granted=0x%lx missing=0x%lx\n",
               label, requested, info.granted, requested & ~info.granted);
    else printf("NtQueryObject(%s) NTSTATUS=0x%lx\n", label, (unsigned long)status);
}

static void psapi_diagnostics(DWORD pid) {
    HANDLE process = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
    if (!process) { printf("PSAPI OpenProcess Win32=%lu\n", GetLastError()); return; }
    HMODULE modules[1024]; DWORD needed;
    if (!EnumProcessModulesEx(process, modules, sizeof(modules), &needed, LIST_MODULES_64BIT))
        printf("EnumProcessModulesEx Win32=%lu\n", GetLastError());
    else {
        printf("EnumProcessModulesEx Win32=0 count=%lu complete=%u\n",
               needed / (DWORD)sizeof(HMODULE), needed <= sizeof(modules));
        if (needed >= sizeof(HMODULE)) {
            unsigned char dos[2]; SIZE_T bytes;
            BOOL ok = ReadProcessMemory(process, modules[0], dos, sizeof(dos), &bytes);
            printf("ReadProcessMemory(main_image_DOS_signature) Win32=%lu is_MZ=%u\n",
                   ok ? 0 : GetLastError(), ok && bytes == 2 && dos[0] == 'M' && dos[1] == 'Z');
        }
    }
    MEMORY_BASIC_INFORMATION region;
    SIZE_T size = VirtualQueryEx(process, NULL, &region, sizeof(region));
    printf("VirtualQueryEx(address_zero) Win32=%lu\n", size ? 0 : GetLastError());
    CloseHandle(process);
}

static void print_integrity(HANDLE token, const char *label) {
    unsigned char data[1024]; DWORD needed;
    if (GetTokenInformation(token, TokenIntegrityLevel, data, sizeof(data), &needed)) {
        TOKEN_MANDATORY_LABEL *m = (TOKEN_MANDATORY_LABEL *)data;
        UCHAR count = *GetSidSubAuthorityCount(m->Label.Sid);
        printf("%s integrity_rid=0x%lx\n", label, *GetSidSubAuthority(m->Label.Sid, count - 1));
    } else printf("%s GetTokenInformation(Integrity) Win32=%lu\n", label, GetLastError());
}

static void token_diagnostics(HANDLE process) {
    HANDLE self = NULL, target = NULL;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &self)) {
        print_integrity(self, "self");
        DWORD needed = 0;
        GetTokenInformation(self, TokenPrivileges, NULL, 0, &needed);
        TOKEN_PRIVILEGES *privileges = malloc(needed);
        LUID debug;
        if (privileges && LookupPrivilegeValueW(NULL, SE_DEBUG_NAME, &debug) &&
            GetTokenInformation(self, TokenPrivileges, privileges, needed, &needed)) {
            BOOL present = FALSE, enabled = FALSE;
            for (DWORD i = 0; i < privileges->PrivilegeCount; ++i) {
                LUID l = privileges->Privileges[i].Luid;
                if (l.LowPart == debug.LowPart && l.HighPart == debug.HighPart) {
                    present = TRUE;
                    enabled = (privileges->Privileges[i].Attributes & SE_PRIVILEGE_ENABLED) != 0;
                }
            }
            printf("self SeDebugPrivilege present=%u enabled=%u (unchanged)\n", present, enabled);
        }
        free(privileges);
    } else printf("self OpenProcessToken Win32=%lu\n", GetLastError());
    if (OpenProcessToken(process, TOKEN_QUERY, &target)) {
        print_integrity(target, "target");
        unsigned char a[1024], b[1024]; DWORD needed;
        if (self && GetTokenInformation(self, TokenUser, a, sizeof(a), &needed) &&
            GetTokenInformation(target, TokenUser, b, sizeof(b), &needed))
            printf("same_user_as_target=%u\n", EqualSid(((TOKEN_USER *)a)->User.Sid, ((TOKEN_USER *)b)->User.Sid));
    } else printf("target OpenProcessToken Win32=%lu\n", GetLastError());
    if (target) CloseHandle(target);
    if (self) CloseHandle(self);
}

static void snapshot_diagnostics(DWORD pid, DWORD flags, const char *name) {
    HANDLE snap = CreateToolhelp32Snapshot(flags, pid);
    if (snap == INVALID_HANDLE_VALUE) {
        printf("CreateToolhelp32Snapshot(%s) Win32=%lu\n", name, GetLastError()); return;
    }
    printf("CreateToolhelp32Snapshot(%s) Win32=0\n", name);
    MODULEENTRY32W entry = {0}; entry.dwSize = sizeof(entry);
    if (!Module32FirstW(snap, &entry)) printf("Module32FirstW(%s) Win32=%lu\n", name, GetLastError());
    else {
        unsigned count = 0; BOOL classic = FALSE;
        do {
            ++count;
            if (!_wcsicmp(entry.szModule, L"pso2classic.dll")) classic = TRUE;
        } while (Module32NextW(snap, &entry));
        printf("Module32FirstW(%s) Win32=0 count=%u classic=%u next_error=%lu\n",
               name, count, classic, GetLastError());
    }
    CloseHandle(snap);
}

static void rights_diagnostics(DWORD pid, DWORD rights, const char *name) {
    HANDLE process = OpenProcess(rights, FALSE, pid);
    if (!process) printf("OpenProcess(%s,0x%lx) Win32=%lu\n", name, rights, GetLastError());
    else {
        printf("OpenProcess(%s,0x%lx) Win32=0 (handle only; no operation performed)\n", name, rights);
        print_granted_access(process, rights, name);
        CloseHandle(process);
    }
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 2) { fputs("Usage: DiagnoseAccess.exe <PSO2 PID|--self>\n", stderr); return 2; }
    BOOL self_test = !wcscmp(argv[1], L"--self");
    wchar_t *end = NULL;
    DWORD pid = self_test ? GetCurrentProcessId() : wcstoul(argv[1], &end, 10);
    if (!pid || (!self_test && *end)) return 2;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) { printf("OpenProcess(QUERY_LIMITED) Win32=%lu\n", GetLastError()); return 1; }
    wchar_t path[32768]; DWORD n = 32768;
    if (!QueryFullProcessImageNameW(process, 0, path, &n)) {
        printf("QueryFullProcessImageNameW Win32=%lu\n", GetLastError()); CloseHandle(process); return 1;
    }
    const wchar_t *name = wcsrchr(path, L'\\'); name = name ? name + 1 : path;
    if (!self_test && _wcsicmp(name, L"pso2.exe")) { CloseHandle(process); return 2; }
    wprintf(L"Target PID=%lu path=%ls\n", pid, path);
    token_diagnostics(process);
    PROCESS_PROTECTION_LEVEL_INFORMATION protection;
    if (GetProcessInformation(process, ProcessProtectionLevelInfo, &protection, sizeof(protection)))
        printf("GetProcessInformation(ProtectionLevel) Win32=0 level=0x%lx is_none=%u\n",
               protection.ProtectionLevel, protection.ProtectionLevel == PROTECTION_LEVEL_NONE);
    else printf("GetProcessInformation(ProtectionLevel) Win32=%lu\n", GetLastError());
    CloseHandle(process);
    rights_diagnostics(pid, PROCESS_QUERY_INFORMATION, "QUERY_INFORMATION");
    rights_diagnostics(pid, PROCESS_VM_READ, "VM_READ");
    rights_diagnostics(pid, PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, "QUERY_INFORMATION|VM_READ");
    rights_diagnostics(pid, PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_CREATE_THREAD |
                            PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE |
                            PROCESS_VM_READ, "loader_required_rights");
    HANDLE security = OpenProcess(READ_CONTROL, FALSE, pid);
    if (!security) printf("OpenProcess(READ_CONTROL) Win32=%lu\n", GetLastError());
    else {
        PACL dacl = NULL; PSECURITY_DESCRIPTOR descriptor = NULL;
        DWORD error = GetSecurityInfo(security, SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
                                      NULL, NULL, &dacl, NULL, &descriptor);
        printf("GetSecurityInfo(DACL) Win32=%lu ace_count=%u null_dacl=%u\n",
               error, !error && dacl ? dacl->AceCount : 0, !error && !dacl);
        if (descriptor) LocalFree(descriptor);
        CloseHandle(security);
    }
    snapshot_diagnostics(pid, TH32CS_SNAPMODULE, "SNAPMODULE");
    snapshot_diagnostics(pid, TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, "SNAPMODULE|SNAPMODULE32");
    psapi_diagnostics(pid);
    puts("Diagnosis finished. No target write or privilege/security change performed.");
    return 0; /* Individual failed probes are observations, not a failed run. */
}
