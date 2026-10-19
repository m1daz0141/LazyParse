/* Compiled only for the isolated remote-loader test. Not used by the game. */
#include "capture_api.h"
#include <stdio.h>
#include <wchar.h>

__declspec(dllexport) DWORD WINAPI CaptureStart(void *argument) {
    OpcConfig *config = argument;
    if (!config || config->size != sizeof(*config) || config->abi != OPC_ABI)
        return ERROR_INVALID_PARAMETER;
    wchar_t path[OPC_PATH_CHARS + 64];
    if (swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\fixture-started.txt", config->output_directory) < 0)
        return ERROR_BAD_PATHNAME;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    const char text[] = "synthetic remote loader test\n";
    DWORD bytes;
    BOOL ok = WriteFile(file, text, sizeof(text)-1, &bytes, NULL);
    CloseHandle(file);
    return ok ? 0 : ERROR_WRITE_FAULT;
}

__declspec(dllexport) DWORD WINAPI CaptureStop(void *argument) {
    (void)argument;
    wchar_t name[100];
    swprintf(name, 100, L"Local\\OverParseRemoteTest-%lu", GetCurrentProcessId());
    HANDLE event = OpenEventW(EVENT_MODIFY_STATE, FALSE, name);
    if (!event) return GetLastError();
    BOOL ok = SetEvent(event);
    CloseHandle(event);
    return ok ? 0 : ERROR_FUNCTION_FAILED;
}
