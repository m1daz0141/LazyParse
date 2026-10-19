#pragma once
#include "capture_api.h"
#include <bcrypt.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

static inline DWORD opc_hash_file(const wchar_t *path, char out[65]) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                              OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    BCRYPT_ALG_HANDLE alg = NULL;
    BCRYPT_HASH_HANDLE hash = NULL;
    unsigned char digest[32], buffer[65536];
    DWORD n = 0, error = ERROR_INVALID_DATA;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, NULL, 0) < 0)
        goto done;
    if (BCryptCreateHash(alg, &hash, NULL, 0, NULL, 0, 0) < 0) goto done;
    for (;;) {
        if (!ReadFile(file, buffer, sizeof(buffer), &n, NULL)) {
            error = GetLastError(); goto done;
        }
        if (!n) break;
        if (BCryptHashData(hash, buffer, n, 0) < 0) goto done;
    }
    if (BCryptFinishHash(hash, digest, sizeof(digest), 0) < 0) goto done;
    for (unsigned i = 0; i < 32; ++i) sprintf(out + i * 2, "%02x", digest[i]);
    error = ERROR_SUCCESS;
done:
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    CloseHandle(file);
    return error;
}

static inline uint64_t opc_filetime(void) {
    FILETIME t;
    GetSystemTimeAsFileTime(&t);
    return ((uint64_t)t.dwHighDateTime << 32) | t.dwLowDateTime;
}

static inline BOOL opc_readable(const void *address, size_t size, BOOL executable) {
    uintptr_t p = (uintptr_t)address, end = p + size;
    if (!p || end < p) return FALSE;
    while (p < end) {
        MEMORY_BASIC_INFORMATION m;
        if (VirtualQuery((void *)p, &m, sizeof(m)) != sizeof(m) ||
            m.State != MEM_COMMIT || (m.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return FALSE;
        DWORD prot = m.Protect & 0xff;
        if (executable) {
            if (prot != PAGE_EXECUTE_READ && prot != PAGE_EXECUTE_READWRITE &&
                prot != PAGE_EXECUTE_WRITECOPY) return FALSE;
        } else if (prot != PAGE_READONLY && prot != PAGE_READWRITE &&
                   prot != PAGE_WRITECOPY && prot != PAGE_EXECUTE_READ &&
                   prot != PAGE_EXECUTE_READWRITE && prot != PAGE_EXECUTE_WRITECOPY) return FALSE;
        uintptr_t next = (uintptr_t)m.BaseAddress + m.RegionSize;
        if (next <= p) return FALSE;
        p = next;
    }
    return TRUE;
}
