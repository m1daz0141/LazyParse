#include "win_util.h"
#include "MinHook.h"
#include "memory_protect_trace.h"
#include "binding_layout.h"
#include "thread_guard.h"
#include "identity_capture.h"
#include "atomic_file.h"

#if !defined(__x86_64__)
#error This capture DLL requires x64 Windows.
#endif

#define API __declspec(dllexport)
#define QUEUE_SIZE 1024
#ifdef OVERPARSE_AUTOSTART
DWORD WINAPI CaptureAutostart(void *unused);
#endif
typedef uintptr_t (*ReceiveCallback)(void *, const unsigned char *);
static ReceiveCallback original_callback;
static uintptr_t receive_hook(void *client, const unsigned char *packet);
typedef struct DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) {
    SLIST_ENTRY link;
    OpcRecord record;
} QueueEntry;
static DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) SLIST_HEADER free_entries, pending_entries;
static QueueEntry queue[QUEUE_SIZE];
typedef struct DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) {
    SLIST_ENTRY link;
    OpiRecord record;
} IdentityQueueEntry;
static DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) SLIST_HEADER identity_free, identity_pending;
static IdentityQueueEntry identity_queue[OPI_QUEUE_SIZE];
static HANDLE identity_file = INVALID_HANDLE_VALUE;
static volatile LONG64 identity_matching, identity_enqueued, identity_dropped, identity_written, identity_invalid;
static HANDLE wake_event, stop_event, writer_thread, capture_file = INVALID_HANDLE_VALUE;
static wchar_t output_dir[OPC_PATH_CHARS];
static volatile LONG lifecycle, accepting, capture_active, io_error;
static volatile LONG64 observed, matching, invalid_size, enqueued, dropped, written;
static BOOL mock_mode;
static DWORD hook_status;
static void *hook_target;
static BOOL slot_hook_mode;
static BOOL hook_installed;
static uintptr_t profile_base;
static void **fixture_slot;
static BindingSnapshot attached_binding;
static OpcThreadOps thread_ops;
static volatile LONG binding_ready, binding_error, guard_error;
static volatile LONG guard_stage, guard_native_status, guard_threads, guard_fatal;
static volatile LONG64 callbacks_total, last_callback_tick, binding_generation, connection_gaps;
static ULONGLONG retry_attach_after;
static volatile LONG damage_requested;
static DWORD wait_copiers(void);
static DWORD attach_current(void);
static DWORD verify_classic(void **target);
static LONG64 counter(volatile LONG64 *p);
#include "identity_observer.h"

#define PROTECT_TRACE_SLOTS 8
typedef struct {
    volatile LONG ready;
    void *address;
    SIZE_T size;
    DWORD requested, effective, error, query_error, old_protection;
    BOOL success, query_success;
    MEMORY_BASIC_INFORMATION region;
} ProtectTrace;
static ProtectTrace protect_trace[PROTECT_TRACE_SLOTS];
static volatile LONG protect_calls;

static BOOL protect_call(LPVOID address, SIZE_T size, DWORD protection, PDWORD old_protection,
                         BOOL adapt_mapped_target) {
    DWORD incoming_error = GetLastError();
    LONG slot = InterlockedIncrement(&protect_calls) - 1;
    ProtectTrace *trace = slot >= 0 && slot < PROTECT_TRACE_SLOTS ? &protect_trace[slot] : NULL;
    MEMORY_BASIC_INFORMATION region = {0};
    BOOL query_success = VirtualQuery(address, &region, sizeof(region)) != 0;
    DWORD query_error = query_success ? 0 : GetLastError();
    DWORD effective = protection;
    /* A read/execute section cannot become shared read/write/execute. Use
       documented private copy-on-write for this validated mapped callback.
       Decide before the call; never retry a failed protection change. */
    if (adapt_mapped_target && address == hook_target && size == 5 &&
        protection == PAGE_EXECUTE_READWRITE && query_success &&
        region.State == MEM_COMMIT && region.Type == MEM_MAPPED &&
        region.AllocationProtect == PAGE_EXECUTE_READ && region.Protect == PAGE_EXECUTE_READ &&
        (uintptr_t)address >= (uintptr_t)region.BaseAddress &&
        (uintptr_t)address - (uintptr_t)region.BaseAddress <= region.RegionSize &&
        size <= region.RegionSize - ((uintptr_t)address - (uintptr_t)region.BaseAddress))
        effective = PAGE_EXECUTE_WRITECOPY;
    if (trace) {
        trace->address = address;
        trace->size = size;
        trace->requested = protection;
        trace->effective = effective;
        trace->query_success = query_success;
        trace->query_error = query_error;
        trace->region = region;
    }
    SetLastError(incoming_error);
    BOOL success = VirtualProtect(address, size, effective, old_protection);
    DWORD call_error = GetLastError();
    if (trace) {
        trace->success = success;
        trace->error = success ? 0 : call_error;
        trace->old_protection = success ? *old_protection : 0;
        /* MinHook may have suspended the writer. Never allocate, take a
           lock, write a file or wait here. Each slot is published once. */
        InterlockedExchange(&trace->ready, 1);
    }
    SetLastError(call_error);
    return success;
}

BOOL WINAPI OpcVirtualProtect(LPVOID address, SIZE_T size, DWORD protection, PDWORD old_protection) {
    return protect_call(address, size, protection, old_protection, FALSE);
}

BOOL WINAPI OpcHookVirtualProtect(LPVOID address, SIZE_T size, DWORD protection, PDWORD old_protection) {
    return protect_call(address, size, protection, old_protection, TRUE);
}

static int format_protect_trace(char *buffer, size_t capacity) {
    int used = snprintf(buffer, capacity, "[");
    unsigned count = 0;
    for (unsigned i = 0; i < PROTECT_TRACE_SLOTS; ++i) {
        const ProtectTrace *trace = &protect_trace[i];
        if (!InterlockedCompareExchange(&protect_trace[i].ready, 0, 0)) continue;
        int n = snprintf(buffer + used, capacity - (size_t)used,
            "%s{\"address\":\"0x%llx\",\"size\":%llu,\"requested_protection\":%lu,\"effective_protection\":%lu,"
            "\"success\":%s,\"win32_error\":%lu,\"old_protection\":%lu,"
            "\"query_success\":%s,\"query_error\":%lu,\"region_base\":\"0x%llx\","
            "\"allocation_base\":\"0x%llx\",\"region_size\":%llu,"
            "\"state\":%lu,\"type\":%lu,\"protection\":%lu,\"allocation_protection\":%lu}",
            count++ ? "," : "", (unsigned long long)(uintptr_t)trace->address,
            (unsigned long long)trace->size, (unsigned long)trace->requested,
            (unsigned long)trace->effective,
            trace->success ? "true" : "false", (unsigned long)trace->error,
            (unsigned long)trace->old_protection, trace->query_success ? "true" : "false",
            (unsigned long)trace->query_error, (unsigned long long)(uintptr_t)trace->region.BaseAddress,
            (unsigned long long)(uintptr_t)trace->region.AllocationBase,
            (unsigned long long)trace->region.RegionSize, (unsigned long)trace->region.State,
            (unsigned long)trace->region.Type, (unsigned long)trace->region.Protect,
            (unsigned long)trace->region.AllocationProtect);
        if (n < 0 || (size_t)n >= capacity - (size_t)used) return -1;
        used += n;
    }
    int n = snprintf(buffer + used, capacity - (size_t)used, "]");
    return n == 1 && (size_t)used + 1 < capacity ? used + 1 : -1;
}

static LONG64 counter(volatile LONG64 *p) { return InterlockedCompareExchange64(p, 0, 0); }

static DWORD wait_copiers(void) {
    ULONGLONG deadline = GetTickCount64() + 5000;
    while (InterlockedCompareExchange(&capture_active, 0, 0)) {
        if (GetTickCount64() >= deadline) return ERROR_TIMEOUT;
        Sleep(1);
    }
    return ERROR_SUCCESS;
}

static DWORD inspect_current(BindingSnapshot *snapshot) {
    if (profile_base) return binding_inspect_methods(profile_base, hook_target,
                                                    (void *)(uintptr_t)&receive_hook, snapshot);
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->stage = "fixture_slot";
    snapshot->method_slot = (uintptr_t)fixture_slot;
    snapshot->listener = (uintptr_t)fixture_slot;
    if (!binding_writable_slot(fixture_slot)) return ERROR_NOACCESS;
    if (!binding_read((uintptr_t)fixture_slot, &snapshot->method, sizeof(snapshot->method))) return ERROR_PARTIAL_COPY;
    return snapshot->method == (uintptr_t)hook_target || snapshot->method == (uintptr_t)&receive_hook ? 0 : ERROR_INVALID_DATA;
}

static DWORD attach_current(void) {
    /* All allocations, file operations and loader calls stay outside this
       guard. Re-resolve from the live root, never write a cached object. */
    OpcThreadGuard guard;
    if (guard_fatal) return ERROR_OPERATION_ABORTED;
    DWORD error = opc_guard_enter(&guard, &thread_ops);
    InterlockedExchange(&guard_stage, (LONG)guard.stage);
    InterlockedExchange(&guard_native_status, (LONG)guard.native_status);
    InterlockedExchange(&guard_threads, (LONG)guard.suspended_count);
    if (guard.resume_error) InterlockedExchange(&guard_fatal, 1);
    InterlockedExchange(&guard_error, (LONG)error);
    if (error) return error;
    BindingSnapshot snapshot;
    error = inspect_current(&snapshot);
    BOOL exchanged = FALSE;
    if (!error && GetTickCount64() >= guard.deadline) error = ERROR_TIMEOUT;
    if (!error && snapshot.method == (uintptr_t)hook_target) {
        if (InterlockedCompareExchangePointer((void **)snapshot.method_slot,
            (void *)(uintptr_t)&receive_hook, hook_target) != hook_target) error = ERROR_RETRY;
        else exchanged = TRUE;
    }
    DWORD resume_error = opc_guard_leave(&guard);
    if (resume_error) {
        InterlockedExchange(&guard_error, (LONG)resume_error);
        InterlockedExchange(&guard_fatal, 1);
        return resume_error;
    }
    if (error) return error;
    if (exchanged || snapshot.client != attached_binding.client ||
        snapshot.network != attached_binding.network || snapshot.listener != attached_binding.listener)
        InterlockedIncrement64(&binding_generation);
    attached_binding = snapshot;
    hook_installed = TRUE;
    InterlockedExchange(&binding_error, 0);
    InterlockedExchange(&binding_ready, 1);
    return ERROR_SUCCESS;
}

static BOOL write_all(HANDLE file, const void *data, DWORD bytes) {
    const unsigned char *p = data;
    while (bytes) {
        DWORD n;
        if (!WriteFile(file, p, bytes, &n, NULL) || !n) {
            InterlockedExchange(&io_error, GetLastError() ? (LONG)GetLastError() : ERROR_WRITE_FAULT);
            InterlockedExchange(&accepting, 0);
            return FALSE;
        }
        p += n; bytes -= n;
    }
    return TRUE;
}

static BOOL output_path(wchar_t *dest, size_t capacity, const wchar_t *name) {
    int n = swprintf(dest, capacity, L"%ls\\%ls", output_dir, name);
    return n > 0 && (size_t)n < capacity;
}

static void write_status(BOOL stopped) {
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    if (!output_path(path, OPC_PATH_CHARS + 64, L"capture-status.json") ||
        !output_path(temp, OPC_PATH_CHARS + 64, L"capture-status.json.tmp")) return;
    char trace_json[6000];
    if (format_protect_trace(trace_json, sizeof(trace_json)) < 0) return;
    char buf[8000];
    int len = snprintf(buf, sizeof(buf),
        "{\n  \"abi\": 1,\n  \"mode\": \"%s\",\n  \"recording\": %s,\n"
        "  \"stopped\": %s,\n  \"observed_callbacks\": %lld,\n"
        "  \"matching_damage_packets\": %lld,\n  \"invalid_damage_size\": %lld,\n"
        "  \"enqueued\": %lld,\n  \"dropped\": %lld,\n  \"written\": %lld,\n"
        "  \"io_error\": %ld,\n  \"hook_status\": %lu,\n"
        "  \"hook_status_name\": \"%s\",\n  \"virtual_protect_calls\": %ld,\n"
        "  \"hook_method\": \"%s\",\n"
        "  \"recording_requested\": %s,\n  \"binding_ready\": %s,\n"
        "  \"binding_error\": %ld,\n  \"guard_error\": %ld,\n"
        "  \"guard_stage\": %ld,\n  \"guard_native_status\": \"0x%08lx\",\n"
        "  \"guard_threads\": %ld,\n  \"guard_fatal\": %s,\n"
        "  \"callbacks_total\": %lld,\n  \"last_callback_tick\": %lld,\n"
        "  \"binding_generation\": %lld,\n  \"connection_gaps\": %lld,\n"
        "  \"virtual_protect_trace\": %s,\n"
        "  \"source_sha256\": \"%s\",\n  \"callback_rva\": \"0xc640f0\",\n"
        "  \"body_field_meanings_live_verified\": false,\n"
        "  \"identity_matching\": %lld,\n  \"identity_enqueued\": %lld,\n"
        "  \"identity_dropped\": %lld,\n  \"identity_written\": %lld,\n"
        "  \"identity_invalid\": %lld,\n  \"identity_prefix_bytes\": 192\n}\n",
        mock_mode ? "synthetic_test" : "pso2_classic", accepting ? "true" : "false",
        stopped ? "true" : "false", counter(&observed), counter(&matching),
        counter(&invalid_size), counter(&enqueued), counter(&dropped), counter(&written),
        (long)io_error, (unsigned long)hook_status,
        slot_hook_mode ? "not_used" : MH_StatusToString((MH_STATUS)hook_status),
        (long)InterlockedCompareExchange(&protect_calls, 0, 0),
        slot_hook_mode ? "bound_callback_slot" : "minhook_code_patch",
        damage_requested ? "true" : "false",
        binding_ready ? "true" : "false", (long)binding_error, (long)guard_error,
        (long)guard_stage, (unsigned long)(DWORD)guard_native_status, (long)guard_threads, guard_fatal ? "true" : "false",
        counter(&callbacks_total), counter(&last_callback_tick), counter(&binding_generation),
        counter(&connection_gaps), trace_json,
        mock_mode ? "not_a_game_capture" : OPC_CLASSIC_SHA256,
        counter(&identity_matching), counter(&identity_enqueued), counter(&identity_dropped),
        counter(&identity_written), counter(&identity_invalid));
    if (len < 0 || (size_t)len >= sizeof(buf)) return;
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD n;
    BOOL ok = WriteFile(file, buf, (DWORD)len, &n, NULL) && n == (DWORD)len;
    CloseHandle(file);
    if (ok) (void)opc_replace_published_file(temp, path);
}

static BOOL drain_identity(void) {
    PSLIST_ENTRY entries = InterlockedFlushSList(&identity_pending), ordered = NULL;
    while (entries) {
        PSLIST_ENTRY next = entries->Next;
        entries->Next = ordered; ordered = entries; entries = next;
    }
    BOOL had_entries = ordered != NULL;
    while (ordered) {
        IdentityQueueEntry *entry = CONTAINING_RECORD(ordered, IdentityQueueEntry, link);
        ordered = ordered->Next;
        if (!io_error && write_all(identity_file, &entry->record, sizeof(entry->record)))
            InterlockedIncrement64(&identity_written);
        InterlockedPushEntrySList(&identity_free, &entry->link);
    }
    return had_entries;
}

static DWORD WINAPI writer(void *unused) {
    (void)unused;
    HANDLE events[2] = { stop_event, wake_event };
    BOOL stopping = FALSE;
    for (;;) {
        OpcRecord batch[64];
        unsigned n = 0;
        PSLIST_ENTRY entries = InterlockedFlushSList(&pending_entries);
        /* A flush gives a private LIFO chain; reverse it into enqueue order. */
        PSLIST_ENTRY ordered = NULL;
        while (entries) {
            PSLIST_ENTRY next = entries->Next;
            entries->Next = ordered; ordered = entries; entries = next;
        }
        BOOL had_entries = ordered != NULL;
        while (ordered) {
            QueueEntry *entry = CONTAINING_RECORD(ordered, QueueEntry, link);
            ordered = ordered->Next;
            batch[n++] = entry->record;
            InterlockedPushEntrySList(&free_entries, &entry->link);
            if (n == 64 || !ordered) {
                if (!io_error && write_all(capture_file, batch, n * sizeof(OpcRecord)))
                    InterlockedAdd64(&written, n);
                n = 0;
            }
        }
        BOOL had_identity = drain_identity();
        if (had_entries || had_identity) {
            continue;
        }
        if (!FlushFileBuffers(capture_file)) {
            InterlockedExchange(&io_error, GetLastError());
            InterlockedExchange(&accepting, 0);
        }
        if (!FlushFileBuffers(identity_file)) {
            InterlockedExchange(&io_error, GetLastError());
            InterlockedExchange(&accepting, 0);
        }
        write_status(stopping);
        if (stopping) break;
        if (WaitForMultipleObjects(2, events, FALSE, 1000) == WAIT_OBJECT_0) stopping = TRUE;
    }
    CloseHandle(capture_file);
    capture_file = INVALID_HANDLE_VALUE;
    CloseHandle(identity_file);
    identity_file = INVALID_HANDLE_VALUE;
    return 0;
}

static void capture_identity(const unsigned char *packet) {
    if (!packet) return;
    unsigned minor = packet[6] | ((unsigned)packet[7] << 8);
    unsigned limit = identity_prefix_limit(packet[5], minor);
    if (!limit) return;
    uint32_t size;
    memcpy(&size, packet, sizeof(size));
    if (size < 8 || size > 0xffffff || (size & 3)) {
        InterlockedIncrement64(&identity_invalid);
        return;
    }
    uint64_t sequence = (uint64_t)InterlockedIncrement64(&identity_matching);
    PSLIST_ENTRY link = InterlockedPopEntrySList(&identity_free);
    if (!link) { InterlockedIncrement64(&identity_dropped); return; }
    IdentityQueueEntry *entry = CONTAINING_RECORD(link, IdentityQueueEntry, link);
    OpiRecord *record = &entry->record;
    memset(record, 0, sizeof(*record));
    LARGE_INTEGER qpc;
    QueryPerformanceCounter(&qpc);
    record->sequence = sequence;
    record->damage_sequence = (uint64_t)counter(&matching);
    record->filetime = opc_filetime();
    record->qpc = (uint64_t)qpc.QuadPart;
    record->packet_size = size;
    record->stored_size = size < limit ? size : limit;
    memcpy(record->prefix, packet, record->stored_size);
    InterlockedPushEntrySList(&identity_pending, &entry->link);
    InterlockedIncrement64(&identity_enqueued);
    SetEvent(wake_event);
}

static uintptr_t receive_hook(void *client, const unsigned char *packet) {
    InterlockedIncrement(&capture_active);
    InterlockedIncrement64(&callbacks_total);
    InterlockedExchange64(&last_callback_tick, (LONG64)GetTickCount64());
    if (InterlockedCompareExchange(&observer_accepting, 0, 0)) observer_packet(packet);
    if (InterlockedCompareExchange(&accepting, 0, 0)) {
        InterlockedIncrement64(&observed);
        capture_identity(packet);
        /* The original callback also requires a valid 8-byte packet header.
           Only a fixed 84-byte damage frame is copied; no disk I/O or heap
           allocation is performed on the game's receive thread. */
        if (packet && packet[5] == 4 && packet[6] == 0x52 && packet[7] == 0) {
            uint32_t size;
            memcpy(&size, packet, 4);
            if (size != 84) InterlockedIncrement64(&invalid_size);
            else {
                uint64_t sequence = (uint64_t)InterlockedIncrement64(&matching);
                PSLIST_ENTRY link = InterlockedPopEntrySList(&free_entries);
                if (!link) InterlockedIncrement64(&dropped);
                else {
                    QueueEntry *entry = CONTAINING_RECORD(link, QueueEntry, link);
                    OpcRecord *r = &entry->record;
                    LARGE_INTEGER qpc;
                    QueryPerformanceCounter(&qpc);
                    r->sequence = sequence;
                    r->filetime = opc_filetime();
                    r->qpc = (uint64_t)qpc.QuadPart;
                    memcpy(r->packet, packet, 84);
                    InterlockedPushEntrySList(&pending_entries, &entry->link);
                    InterlockedIncrement64(&enqueued);
                    SetEvent(wake_event);
                }
            }
        }
    }
    InterlockedDecrement(&capture_active);
    return original_callback(client, packet);
}

static DWORD verify_classic(void **target) {
    HMODULE module = GetModuleHandleW(L"pso2classic.dll");
    if (!module) return ERROR_MOD_NOT_FOUND;
    wchar_t path[32768];
    DWORD n = GetModuleFileNameW(module, path, 32768);
    if (!n || n >= 32768) return ERROR_INSUFFICIENT_BUFFER;
    char hash[65];
    DWORD error = opc_hash_file(path, hash);
    if (error) return error;
    if (strcmp(hash, OPC_CLASSIC_SHA256)) return ERROR_REVISION_MISMATCH;
    unsigned char *base = (unsigned char *)module;
    if (!opc_readable(base, 4096, FALSE)) return ERROR_INVALID_ADDRESS;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 || dos->e_lfanew > 3072)
        return ERROR_BAD_EXE_FORMAT;
    IMAGE_NT_HEADERS64 *nt = (IMAGE_NT_HEADERS64 *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        nt->OptionalHeader.SizeOfImage <= 0x3cead00) return ERROR_BAD_EXE_FORMAT;
    static const unsigned char expected[] = {
        0x33,0xc0,0x48,0x89,0x81,0xcc,0x14,0,0,0x44,0x0f,0xb6,0x42,2,
        0x0f,0xb7,2,0x41,0xc1,0xe0,0x10,0x44,0x0b,0xc0,0x44,0x01,
        0x81,0xbc,0x14,0,0,0xe9,0xdc,0xfd,0xff,0xff
    };
    *target = base + OPC_CALLBACK_RVA;
    if (!opc_readable(*target, sizeof(expected), TRUE) || memcmp(*target, expected, sizeof(expected)))
        return ERROR_INVALID_DATA;
    static const unsigned char descriptor[8] = {3,0,0x40,0,0x54,0,0,0};
    if (nt->OptionalHeader.SizeOfImage < 0x38768b8 ||
        !opc_readable(base + 0x38768b0, 8, FALSE) || memcmp(base + 0x38768b0, descriptor, 8))
        return ERROR_INVALID_DATA;
    return ERROR_SUCCESS;
}

static DWORD start_capture(const OpcConfig *config, void *target, BOOL is_mock, void **method_slot, void *mock_base) {
    if (!config || config->size != sizeof(OpcConfig) || config->abi != OPC_ABI ||
        !config->output_directory[0] ||
        !wmemchr(config->output_directory, 0, OPC_PATH_CHARS)) return ERROR_INVALID_PARAMETER;
    LONG previous = InterlockedCompareExchange(&lifecycle, 0, 0);
    if ((previous != 0 && previous != 3) || InterlockedCompareExchange(&lifecycle, 1, previous) != previous)
        return ERROR_ALREADY_INITIALIZED;
    DWORD error = ERROR_SUCCESS;
    InterlockedExchange(&accepting, 0);
    error = wait_copiers();
    if (error) { InterlockedExchange(&lifecycle, previous); return error; }
    io_error = 0;
    observed = matching = invalid_size = enqueued = dropped = written = 0;
    identity_matching = identity_enqueued = identity_dropped = identity_written = identity_invalid = 0;
    memset(protect_trace, 0, sizeof(protect_trace));
    InterlockedExchange(&protect_calls, 0);
    InitializeSListHead(&free_entries);
    InitializeSListHead(&pending_entries);
    InitializeSListHead(&identity_free);
    InitializeSListHead(&identity_pending);
    for (unsigned i = 0; i < QUEUE_SIZE; ++i) InterlockedPushEntrySList(&free_entries, &queue[i].link);
    for (unsigned i = 0; i < OPI_QUEUE_SIZE; ++i) InterlockedPushEntrySList(&identity_free, &identity_queue[i].link);
    mock_mode = is_mock;
    slot_hook_mode = method_slot != NULL;
    wcscpy(output_dir, config->output_directory);
    /* Absolute local output path, prepared by the caller. */
    if (wcslen(output_dir) < 3 || output_dir[1] != L':' || output_dir[2] != L'\\') {
        error = ERROR_BAD_PATHNAME; goto fail;
    }
    if (!is_mock) {
        error = verify_classic(&target);
        if (error) goto fail;
#ifdef OVERPARSE_CALLBACK_SLOT
        profile_base = (uintptr_t)GetModuleHandleW(L"pso2classic.dll");
        slot_hook_mode = TRUE;
#endif
    } else profile_base = (uintptr_t)mock_base;
    if (hook_installed && target != hook_target) { error = ERROR_REVISION_MISMATCH; goto fail; }
    hook_target = target;
    fixture_slot = method_slot;
    if (profile_base) slot_hook_mode = TRUE;
    if (method_slot) {
        void *current;
        if (!binding_writable_slot(method_slot)) { error = ERROR_NOACCESS; goto fail; }
        if (!binding_read((uintptr_t)method_slot, &current, sizeof(current)) ||
            (current != target && current != (void *)(uintptr_t)&receive_hook)) {
            error = ERROR_INVALID_DATA; goto fail;
        }
    }
    wchar_t path[OPC_PATH_CHARS + 64];
    if (!output_path(path, OPC_PATH_CHARS + 64, L"capture.opcap")) { error = ERROR_BAD_PATHNAME; goto fail; }
    capture_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (capture_file == INVALID_HANDLE_VALUE) { error = GetLastError(); goto fail; }
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency);
    OpcHeader header = {{'O','P','C','A','P','0','1',0},1,sizeof(OpcRecord),
                         (uint64_t)frequency.QuadPart,opc_filetime()};
    if (!write_all(capture_file, &header, sizeof(header))) { error = io_error; goto fail; }
    if (!output_path(path, OPC_PATH_CHARS + 64, L"identity.opmeta")) { error = ERROR_BAD_PATHNAME; goto fail; }
    identity_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (identity_file == INVALID_HANDLE_VALUE) { error = GetLastError(); goto fail; }
    memcpy(header.magic, "OPMETA02", 8);
    header.version = 2;
    header.record_size = sizeof(OpiRecord);
    if (!write_all(identity_file, &header, sizeof(header))) { error = io_error; goto fail; }
    wake_event = CreateEventW(NULL, FALSE, FALSE, NULL);
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!wake_event || !stop_event) { error = GetLastError(); goto fail; }
    hook_status = MH_OK;
    if (slot_hook_mode) {
        original_callback = (ReceiveCallback)target;
        error = opc_thread_ops_init(&thread_ops);
        if (error) goto fail;
    } else if (!hook_installed) {
        hook_status = MH_Initialize();
        if (hook_status != MH_OK) { error = ERROR_FUNCTION_FAILED; goto fail; }
        hook_status = MH_CreateHook(target, receive_hook, (void **)&original_callback);
        if (hook_status != MH_OK) { error = ERROR_NOT_SUPPORTED; MH_Uninitialize(); goto fail; }
    }
    /* Pin before enabling: forwarded calls can still be in flight after stop. */
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           (LPCWSTR)(uintptr_t)&receive_hook, &pinned)) {
        error = GetLastError(); goto fail_hook;
    }
    writer_thread = CreateThread(NULL, 0, writer, NULL, 0, NULL);
    if (!writer_thread) { error = GetLastError(); goto fail_hook; }
    if (slot_hook_mode) {
        InterlockedExchange(&binding_ready, 0);
        error = attach_current();
        InterlockedExchange(&binding_error, (LONG)error);
    } else if (!hook_installed) {
        hook_status = MH_EnableHook(target);
        if (hook_status != MH_OK) error = ERROR_NOT_SUPPORTED;
        else { hook_installed = TRUE; InterlockedExchange(&binding_ready, 1); }
    }
    if (error) {
        InterlockedExchange(&accepting, 0);
        SetEvent(stop_event);
        WaitForSingleObject(writer_thread, INFINITE);
        CloseHandle(writer_thread); writer_thread = NULL;
        goto fail_hook;
    }
    InterlockedExchange(&lifecycle, 2);
    InterlockedExchange(&damage_requested, 1);
    InterlockedExchange(&accepting, 1);
    return ERROR_SUCCESS;
fail_hook:
    if (!slot_hook_mode && !hook_installed) { MH_RemoveHook(target); MH_Uninitialize(); }
fail:
    if (identity_file != INVALID_HANDLE_VALUE) { CloseHandle(identity_file); identity_file = INVALID_HANDLE_VALUE; }
    if (capture_file != INVALID_HANDLE_VALUE) { CloseHandle(capture_file); capture_file = INVALID_HANDLE_VALUE; }
    if (wake_event) { CloseHandle(wake_event); wake_event = NULL; }
    if (stop_event) { CloseHandle(stop_event); stop_event = NULL; }
    InterlockedExchange(&lifecycle, hook_installed ? 3 : 0);
    return error;
}

API DWORD WINAPI CaptureStart(void *config) { return start_capture(config, NULL, FALSE, NULL, NULL); }

API DWORD WINAPI CaptureMaintain(void *unused) {
    (void)unused;
    LONG previous = InterlockedCompareExchange(&lifecycle, 0, 0);
    if (previous != 2 && !((previous == 0 || previous == 3) && observing)) return ERROR_INVALID_STATE;
    if (InterlockedCompareExchange(&lifecycle, 6, previous) != previous) return ERROR_BUSY;
    DWORD error = ERROR_SUCCESS;
    if (io_error && damage_requested) { error = (DWORD)io_error; goto done; }
    if (slot_hook_mode) {
        BindingSnapshot current;
        error = inspect_current(&current);
        if (error || current.client != attached_binding.client || current.network != attached_binding.network ||
            current.listener != attached_binding.listener || current.method != (uintptr_t)&receive_hook) {
            DWORD snapshot_error = error;
            InterlockedExchange(&accepting, 0);
            InterlockedExchange(&observer_accepting, 0);
            if (InterlockedExchange(&binding_ready, 0)) {
                InterlockedIncrement64(&connection_gaps);
                if (observing) InterlockedExchange(&observer_reset_pending, 1);
            }
            if (observer_reset_pending) {
                DWORD reset_error = wait_copiers();
                if (reset_error) { error = reset_error; goto done; }
                observer_reset();
                InterlockedExchange(&observer_reset_pending, 0);
            }
            InterlockedExchange(&binding_error, (LONG)error);
            if (!snapshot_error) {
                if (GetTickCount64() < retry_attach_after) error = guard_error ? (DWORD)guard_error : ERROR_RETRY;
                else {
                    error = wait_copiers();
                    if (!error) error = attach_current();
                    retry_attach_after = error && guard_error ? GetTickCount64() + 5000 : 0;
                }
            }
            InterlockedExchange(&binding_error, (LONG)error);
        }
        if (!error) {
            if (damage_requested && !io_error) InterlockedExchange(&accepting, 1);
            if (observing && !observer_error) InterlockedExchange(&observer_accepting, 1);
        }
    }
done:
    InterlockedExchange(&lifecycle, previous);
    return error;
}

API DWORD WINAPI CaptureGetHealth(void *argument) {
    OpcHealth *h = argument;
    if (!h || h->size != sizeof(*h) || h->abi != OPC_ABI) return ERROR_INVALID_PARAMETER;
    LONG state = InterlockedCompareExchange(&lifecycle, 0, 0);
    h->requested = (uint32_t)InterlockedCompareExchange(&damage_requested, 0, 0);
    h->recording = (uint32_t)InterlockedCompareExchange(&accepting, 0, 0);
    h->binding_ready = (uint32_t)InterlockedCompareExchange(&binding_ready, 0, 0);
    h->io_error = (uint32_t)InterlockedCompareExchange(&io_error, 0, 0);
    h->binding_error = (uint32_t)InterlockedCompareExchange(&binding_error, 0, 0);
    h->guard_error = (uint32_t)InterlockedCompareExchange(&guard_error, 0, 0);
    h->draining = state == 4;
    h->guard_stage = (uint32_t)InterlockedCompareExchange(&guard_stage, 0, 0);
    h->guard_native_status = (uint32_t)InterlockedCompareExchange(&guard_native_status, 0, 0);
    h->guard_threads = (uint32_t)InterlockedCompareExchange(&guard_threads, 0, 0);
    h->guard_fatal = (uint32_t)InterlockedCompareExchange(&guard_fatal, 0, 0);
    h->callbacks_total = (uint64_t)counter(&callbacks_total);
    h->last_callback_tick = (uint64_t)counter(&last_callback_tick);
    h->binding_generation = (uint64_t)counter(&binding_generation);
    h->connection_gaps = (uint64_t)counter(&connection_gaps);
    h->observing = (uint32_t)InterlockedCompareExchange(&observing, 0, 0);
    h->context_error = (uint32_t)InterlockedCompareExchange(&observer_error, 0, 0);
    h->context_written = (uint64_t)counter(&observer_written);
    h->context_dropped = (uint64_t)counter(&observer_dropped);
    h->context_watermark_qpc = (uint64_t)counter(&observer_watermark);
    h->context_start_filetime = observer_started;
    return ERROR_SUCCESS;
}

#ifdef OVERPARSE_CALLBACK_SLOT
DWORD CaptureProbeBinding(const wchar_t *directory) {
    static HMODULE verified_module;
    static void *verified_target;
    HMODULE module = GetModuleHandleW(L"pso2classic.dll");
    void *target = verified_target;
    BindingSnapshot snapshot = {0};
    snapshot.stage = "verify_classic";
    DWORD error = ERROR_SUCCESS;
    if (!module || module != verified_module) {
        error = verify_classic(&target);
        if (!error) { verified_module = module; verified_target = target; }
    }
    if (!error) error = binding_inspect_methods((uintptr_t)GetModuleHandleW(L"pso2classic.dll"), target,
                                               (void *)(uintptr_t)&receive_hook, &snapshot);
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\binding-probe.json", directory);
    swprintf(temp, OPC_PATH_CHARS + 64, L"%ls\\binding-probe.json.tmp", directory);
    char text[900];
    int n = snprintf(text, sizeof(text),
        "{\"stage\":\"%s\",\"win32_error\":%lu,\"read_only\":true,"
        "\"client\":\"0x%llx\",\"network\":\"0x%llx\",\"listener\":\"0x%llx\","
        "\"method_slot\":\"0x%llx\",\"method_slot_requires_protection_change\":false}\n",
        snapshot.stage, (unsigned long)error, (unsigned long long)snapshot.client,
        (unsigned long long)snapshot.network, (unsigned long long)snapshot.listener,
        (unsigned long long)snapshot.method_slot);
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    DWORD bytes;
    BOOL ok = n > 0 && (size_t)n < sizeof(text) && WriteFile(file, text, (DWORD)n, &bytes, NULL) && bytes == (DWORD)n;
    CloseHandle(file);
    if (!ok) return ERROR_WRITE_FAULT;
    DWORD publish_error = opc_replace_published_file(temp, path);
    return publish_error ? publish_error : error;
}
#endif

#ifdef OVERPARSE_TEST_HOST
API DWORD WINAPI CaptureStartTest(void *argument) {
    OpcTestConfig *test = argument;
    if (!test || !test->callback || !opc_readable(test->callback, 16, TRUE)) return ERROR_INVALID_PARAMETER;
    return start_capture(&test->config, test->callback, TRUE, test->method_slot, test->profile_base);
}

API DWORD WINAPI CaptureProbeProtectTest(void *address) {
    DWORD old;
    SetLastError(0x12345678);
    return OpcVirtualProtect(address, 4096, PAGE_EXECUTE_READWRITE, &old) ? 0 : GetLastError();
}
#endif

API DWORD WINAPI CaptureStop(void *unused) {
    (void)unused;
    LONG state = InterlockedCompareExchange(&lifecycle, 0, 0);
    if (state == 3) return ERROR_SUCCESS;
    if (state != 2 && state != 4) return ERROR_INVALID_STATE;
    if (InterlockedCompareExchange(&lifecycle, 4, state) != state) return ERROR_BUSY;
    InterlockedExchange(&damage_requested, 0);
    InterlockedExchange(&accepting, 0);
    DWORD error = wait_copiers();
    if (error) return error;
    SetEvent(stop_event);
    DWORD result = WaitForSingleObject(writer_thread, 5000);
    if (result != WAIT_OBJECT_0) return result == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
    CloseHandle(writer_thread); writer_thread = NULL;
    CloseHandle(wake_event); wake_event = NULL;
    CloseHandle(stop_event); stop_event = NULL;
    InterlockedExchange(&lifecycle, 3);
    return (DWORD)io_error;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
#ifdef OVERPARSE_AUTOSTART
        /* No waiting under the loader lock. This DLL remains loaded; the
           capture worker starts after DLL initialization has completed. */
        HANDLE thread = CreateThread(NULL, 0, CaptureAutostart, NULL, 0, NULL);
        if (!thread) return FALSE;
        CloseHandle(thread);
#endif
    }
    return TRUE;
}
