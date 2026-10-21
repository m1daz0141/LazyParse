#pragma once
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>

#define OPC_ABI 1
#define OPC_PATH_CHARS 1024
/* Legacy development builds keep their investigated layout. Portable builds
   include a complete, explicit profile generated from the checked JSON. */
#ifndef OPC_CLASSIC_SHA256
#define OPC_CLASSIC_SHA256 "b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332"
#define OPC_CALLBACK_RVA 0x00c640f0u
#define OPC_CLIENT_GLOBAL_RVA 0x3ceacf8u
#define OPC_CLIENT_VTABLE_RVA 0x35a5680u
#define OPC_NETWORK_VTABLE_RVA 0x35a5560u
#define OPC_SOCKET_VTABLE_RVA 0x35a55e0u
#define OPC_LISTENER_VTABLE_RVA 0x34c19f0u
#define OPC_LISTENER_ADAPTER_RVA 0x1770e0u
#define OPC_DAMAGE_DESCRIPTOR_RVA 0x38768b0u
#define OPC_REQUIRED_IMAGE_SIZE 0x3cead00u
#endif

typedef struct {
    uint32_t size;
    uint32_t abi;
    wchar_t output_directory[OPC_PATH_CHARS];
} OpcConfig;

/* Thread-compatible exports: success is zero, failure is a Win32 error code.
   Start/stop are called outside DllMain. Stop ends recording, but the forwarding
   hook and pinned DLL remain until process exit. Do not call FreeLibrary. */
typedef DWORD (WINAPI *OpcControl)(void *);

typedef struct {
    uint32_t size, abi;
    uint32_t requested, recording, binding_ready, io_error, binding_error, guard_error, draining;
    uint32_t guard_stage, guard_native_status, guard_threads, guard_fatal;
    uint64_t callbacks_total, last_callback_tick, binding_generation, connection_gaps;
    uint32_t observing, context_error;
    uint64_t context_written, context_dropped, context_watermark_qpc, context_start_filetime;
} OpcHealth;

#pragma pack(push, 1)
typedef struct {
    char magic[8];              /* OPCAP01\0 */
    uint32_t version;           /* 1 */
    uint32_t record_size;       /* 108 */
    uint64_t qpc_frequency;
    uint64_t start_filetime;    /* UTC, 100 ns since 1601-01-01 */
} OpcHeader;
typedef struct {
    uint64_t sequence;          /* matching packet sequence, gaps mean drops */
    uint64_t filetime;
    uint64_t qpc;
    unsigned char packet[84];
} OpcRecord;
#pragma pack(pop)

_Static_assert(sizeof(OpcHeader) == 32, "capture header ABI");
_Static_assert(sizeof(OpcRecord) == 108, "capture record ABI");

#ifdef OVERPARSE_TEST_HOST
typedef struct {
    OpcConfig config;
    void *callback;
    void **method_slot;
    void *profile_base;
} OpcTestConfig;
#endif
