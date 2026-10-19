#include "capture_api.h"
#include <stdio.h>

int main(void) {
    wchar_t name[100];
    swprintf(name, 100, L"Local\\OverParseRemoteTest-%lu", GetCurrentProcessId());
    HANDLE event = CreateEventW(NULL, TRUE, FALSE, name);
    if (!event) return 1;
    DWORD result = WaitForSingleObject(event, 60000);
    Sleep(300); /* let the remote stop export return before process exit */
    CloseHandle(event);
    return result == WAIT_OBJECT_0 ? 0 : 1;
}
