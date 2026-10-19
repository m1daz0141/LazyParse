#ifndef OPC_MEMORY_PROTECT_TRACE_H
#define OPC_MEMORY_PROTECT_TRACE_H
#include <windows.h>

BOOL WINAPI OpcVirtualProtect(LPVOID address, SIZE_T size, DWORD protection, PDWORD old_protection);
BOOL WINAPI OpcHookVirtualProtect(LPVOID address, SIZE_T size, DWORD protection, PDWORD old_protection);

/* Only MinHook's hook.c is compiled with this definition. Include Windows
   declarations first so the real API remains available to our wrapper. */
#ifdef OPC_INTERPOSE_PROTECT
#define VirtualProtect OpcHookVirtualProtect
#endif
#endif
