#pragma once
/* Included after capture.c's private state declarations. The run-scoped
   identity journal remains alive while damage recording is paused. */
static DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) SLIST_HEADER observer_free, observer_pending;
static IdentityQueueEntry observer_queue[OPI_QUEUE_SIZE];
static volatile LONG observing, observer_accepting, observer_error, observer_reset_pending;
static volatile LONG64 observer_matching, observer_written, observer_dropped, observer_invalid, observer_watermark;
static HANDLE observer_file = INVALID_HANDLE_VALUE, observer_wake, observer_stop, observer_thread;
static wchar_t observer_directory[OPC_PATH_CHARS];
static uint64_t observer_started;

static BOOL observer_write(HANDLE file, const void *data, DWORD size) {
    const unsigned char *p = data;
    while (size) {
        DWORD n;
        if (!WriteFile(file, p, size, &n, NULL) || !n) {
            InterlockedExchange(&observer_error, GetLastError() ? (LONG)GetLastError() : ERROR_WRITE_FAULT);
            InterlockedExchange(&observer_accepting, 0);
            return FALSE;
        }
        p += n; size -= n;
    }
    return TRUE;
}

static void observer_enqueue(const unsigned char *packet, unsigned limit) {
    uint32_t size;
    memcpy(&size, packet, 4);
    if (size < 8 || size > 0xffffff || (size & 3)) { InterlockedIncrement64(&observer_invalid); return; }
    uint64_t sequence = (uint64_t)InterlockedIncrement64(&observer_matching);
    PSLIST_ENTRY link = InterlockedPopEntrySList(&observer_free);
    if (!link) { InterlockedIncrement64(&observer_dropped); return; }
    IdentityQueueEntry *entry = CONTAINING_RECORD(link, IdentityQueueEntry, link);
    OpiRecord *record = &entry->record;
    memset(record, 0, sizeof(*record));
    LARGE_INTEGER qpc;
    QueryPerformanceCounter(&qpc);
    record->sequence = sequence; record->damage_sequence = (uint64_t)counter(&matching);
    record->filetime = opc_filetime(); record->qpc = (uint64_t)qpc.QuadPart;
    record->packet_size = size; record->stored_size = size < limit ? size : limit;
    memcpy(record->prefix, packet, record->stored_size);
    InterlockedPushEntrySList(&observer_pending, &entry->link);
    SetEvent(observer_wake);
}

static void observer_packet(const unsigned char *packet) {
    if (!packet) return;
    qcat_packet(packet, observer_wake);
    unsigned limit = identity_prefix_limit(packet[5], packet[6] | ((unsigned)packet[7] << 8));
    if (limit) observer_enqueue(packet, limit);
}

static void observer_reset(void) {
    /* A synthetic context boundary, never sent to the game's callback.
       Caller disabled observation and waited for copying sections to end. */
    static const unsigned char reset[8] = {8,0,0,0,0,0xff,1,0};
    observer_enqueue(reset, 8);
}

static DWORD observer_status(BOOL stopped) {
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\context-status.json", observer_directory);
    swprintf(temp, OPC_PATH_CHARS + 64, L"%ls\\context-status.json.tmp", observer_directory);
    char text[900];
    int n = snprintf(text, sizeof(text),
        "{\"schema_version\":1,\"pid\":%lu,\"source_sha256\":\"%s\",\"start_filetime\":%llu,"
        "\"stopped\":%s,\"written\":%lld,\"matching\":%lld,\"dropped\":%lld,\"invalid\":%lld,"
        "\"io_error\":%ld,\"published_through_qpc\":%lld,\"binding_generation\":%lld,\"connection_gaps\":%lld}",
        GetCurrentProcessId(), mock_mode ? "not_a_game_capture" : OPC_CLASSIC_SHA256,
        (unsigned long long)observer_started, stopped ? "true" : "false", counter(&observer_written),
        counter(&observer_matching), counter(&observer_dropped), counter(&observer_invalid), (long)observer_error,
        counter(&observer_watermark), counter(&binding_generation), counter(&connection_gaps));
    if (n <= 0 || (size_t)n >= sizeof(text)) return ERROR_INSUFFICIENT_BUFFER;
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    BOOL ok = observer_write(file, text, (DWORD)n);
    CloseHandle(file);
    if (!ok) return (DWORD)InterlockedCompareExchange(&observer_error, 0, 0);
    return opc_replace_published_file(temp, path);
}

static DWORD WINAPI observer_writer(void *unused) {
    (void)unused;
    HANDLE events[] = {observer_stop, observer_wake};
    BOOL stopping = FALSE;
    for (;;) {
        LARGE_INTEGER through;
        QueryPerformanceCounter(&through);
        /* All callbacks timestamped before 'through' must have completed
           copying before the journal publishes that time as complete. */
        DWORD barrier_error = wait_copiers();
        if (barrier_error) {
            InterlockedExchange(&observer_error, (LONG)barrier_error);
            InterlockedExchange(&observer_accepting, 0);
        }
        PSLIST_ENTRY entries = InterlockedFlushSList(&observer_pending), ordered = NULL;
        while (entries) {
            PSLIST_ENTRY next = entries->Next;
            entries->Next = ordered; ordered = entries; entries = next;
        }
        while (ordered) {
            IdentityQueueEntry *entry = CONTAINING_RECORD(ordered, IdentityQueueEntry, link);
            ordered = ordered->Next;
            if (!observer_error && observer_write(observer_file, &entry->record, sizeof(entry->record)))
                InterlockedIncrement64(&observer_written);
            InterlockedPushEntrySList(&observer_free, &entry->link);
        }
        if (!observer_error && !FlushFileBuffers(observer_file)) InterlockedExchange(&observer_error, GetLastError());
        qcat_drain();
        qcat_status(stopping);
        if (!observer_error) InterlockedExchange64(&observer_watermark, through.QuadPart);
        else InterlockedExchange(&observer_accepting, 0);
        DWORD publish_error = observer_status(stopping);
        if (publish_error) {
            InterlockedCompareExchange(&observer_error, (LONG)publish_error, 0);
            InterlockedExchange(&observer_accepting, 0);
        }
        if (stopping) break;
        if (WaitForMultipleObjects(2, events, FALSE, 100) == WAIT_OBJECT_0) stopping = TRUE;
    }
    CloseHandle(observer_file); observer_file = INVALID_HANDLE_VALUE;
    qcat_close();
    return 0;
}

static DWORD start_observer(const OpcConfig *config, void *target, BOOL is_mock, void **method_slot, void *mock_base) {
    if (!config || config->size != sizeof(*config) || config->abi != OPC_ABI ||
        !wmemchr(config->output_directory, 0, OPC_PATH_CHARS) || wcslen(config->output_directory) < 3 ||
        config->output_directory[1] != L':' || config->output_directory[2] != L'\\') return ERROR_INVALID_PARAMETER;
    if (observing || observer_thread) return ERROR_ALREADY_INITIALIZED;
    if (hook_installed && !slot_hook_mode) return ERROR_NOT_SUPPORTED;
    LONG previous = InterlockedCompareExchange(&lifecycle, 0, 0);
    if ((previous != 0 && previous != 3) || InterlockedCompareExchange(&lifecycle, 1, previous) != previous)
        return ERROR_BUSY;
    DWORD error = 0;
    uintptr_t candidate_base;
    if (!is_mock) {
        error = verify_classic(&target);
        if (error) goto fail;
        candidate_base = (uintptr_t)GetModuleHandleW(L"pso2classic.dll");
    } else candidate_base = (uintptr_t)mock_base;
    if (hook_installed && hook_target != target) { error = ERROR_REVISION_MISMATCH; goto fail; }
    mock_mode = is_mock;
    profile_base = candidate_base;
    if (!profile_base && !method_slot) { error = ERROR_INVALID_PARAMETER; goto fail; }
    hook_target = target; fixture_slot = method_slot; slot_hook_mode = TRUE;
    original_callback = (ReceiveCallback)target;
    if ((error = opc_thread_ops_init(&thread_ops))) goto fail;
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
        (LPCWSTR)(uintptr_t)&receive_hook, &pinned)) { error = GetLastError(); goto fail; }
    error = attach_current();
    if (error) goto fail;
    wcscpy(observer_directory, config->output_directory);
    if (!CreateDirectoryW(observer_directory, NULL)) { error = GetLastError(); goto fail; }
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\identity.opmeta", observer_directory);
    observer_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (observer_file == INVALID_HANDLE_VALUE) { error = GetLastError(); goto fail; }
    observer_error = 0;
    observer_matching = observer_written = observer_dropped = observer_invalid = observer_watermark = 0;
    observer_reset_pending = 0;
    InitializeSListHead(&observer_free); InitializeSListHead(&observer_pending);
    for (unsigned i = 0; i < OPI_QUEUE_SIZE; ++i) InterlockedPushEntrySList(&observer_free, &observer_queue[i].link);
    LARGE_INTEGER frequency;
    QueryPerformanceFrequency(&frequency); observer_started = opc_filetime();
    OpcHeader header = {{'O','P','M','E','T','A','0','2'},2,sizeof(OpiRecord),(uint64_t)frequency.QuadPart,observer_started};
    if (!observer_write(observer_file, &header, sizeof(header))) { error = (DWORD)observer_error; goto fail; }
    error = qcat_start(observer_directory, (uint64_t)frequency.QuadPart, observer_started);
    if (error) goto fail;
    observer_wake = CreateEventW(NULL, FALSE, FALSE, NULL); observer_stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!observer_wake || !observer_stop) { error = GetLastError(); goto fail; }
    observer_thread = CreateThread(NULL, 0, observer_writer, NULL, 0, NULL);
    if (!observer_thread) { error = GetLastError(); goto fail; }
    InterlockedExchange(&observing, 1); InterlockedExchange(&observer_accepting, 1);
    InterlockedExchange(&lifecycle, previous);
    return 0;
fail:
    qcat_close();
    if (observer_file != INVALID_HANDLE_VALUE) { CloseHandle(observer_file); observer_file = INVALID_HANDLE_VALUE; }
    if (observer_wake) { CloseHandle(observer_wake); observer_wake = NULL; }
    if (observer_stop) { CloseHandle(observer_stop); observer_stop = NULL; }
    InterlockedExchange(&observer_error, (LONG)error);
    InterlockedExchange(&lifecycle, previous);
    return error;
}

API DWORD WINAPI CaptureObserverStart(void *config) { return start_observer(config, NULL, FALSE, NULL, NULL); }

API DWORD WINAPI CaptureObserverStop(void *unused) {
    (void)unused;
    if (!observing) return 0;
    InterlockedExchange(&observer_accepting, 0);
    DWORD error = wait_copiers();
    if (error) return error;
    SetEvent(observer_stop);
    DWORD waited = WaitForSingleObject(observer_thread, 5000);
    if (waited != WAIT_OBJECT_0) return waited == WAIT_TIMEOUT ? ERROR_TIMEOUT : GetLastError();
    CloseHandle(observer_thread); observer_thread = NULL;
    CloseHandle(observer_wake); observer_wake = NULL;
    CloseHandle(observer_stop); observer_stop = NULL;
    InterlockedExchange(&observing, 0);
    return (DWORD)observer_error;
}

#ifdef OVERPARSE_TEST_HOST
API DWORD WINAPI CaptureObserverStartTest(void *argument) {
    OpcTestConfig *test = argument;
    if (!test || !test->callback || !opc_readable(test->callback, 16, TRUE)) return ERROR_INVALID_PARAMETER;
    return start_observer(&test->config, test->callback, TRUE, test->method_slot, test->profile_base);
}
#endif
