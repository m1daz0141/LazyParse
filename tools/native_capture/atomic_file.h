#pragma once
#include <windows.h>

/* Publication runs on writers, never in the receive callback. ReplaceFileW
   supports existing SHARE_DELETE readers. MoveFileExW is used only when the
   destination is absent, without permission to overwrite a concurrent file.
   A short incompatible reader/indexer may still need bounded retry. */
static DWORD opc_replace_published_file(const wchar_t *temp, const wchar_t *path) {
    DWORD error = ERROR_SUCCESS;
    for (unsigned attempt = 0; attempt < 10; ++attempt) {
        if (ReplaceFileW(path, temp, NULL, 0, NULL, NULL)) return ERROR_SUCCESS;
        error = GetLastError();
        if (error == ERROR_FILE_NOT_FOUND) {
            if (MoveFileExW(temp, path, 0)) return ERROR_SUCCESS;
            error = GetLastError();
            if (error == ERROR_ALREADY_EXISTS || error == ERROR_FILE_EXISTS) continue;
        }
        if (error != ERROR_ACCESS_DENIED && error != ERROR_SHARING_VIOLATION && error != ERROR_LOCK_VIOLATION)
            return error;
        if (attempt != 9) Sleep(10);
    }
    return error;
}
