#pragma once
/* Optional investigation stream. Only quest category/difficulty responses;
   no changes to OPMETA/OPCAP, callbacks, or normal-build capture policy. */
#ifdef OVERPARSE_QUEST_CATALOG_CAPTURE
#define QCAT_PACKET_MAX 262144u
#define QCAT_QUEUE_SIZE 32u
#pragma pack(push, 1)
typedef struct {
    uint64_t sequence, filetime, qpc;
    uint32_t packet_size, reserved;
} QcatRecord;
#pragma pack(pop)
_Static_assert(sizeof(QcatRecord) == 32, "quest catalog record ABI");
typedef struct DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) {
    SLIST_ENTRY link;
    QcatRecord record;
    unsigned char packet[QCAT_PACKET_MAX];
} QcatEntry;
static DECLSPEC_ALIGN(MEMORY_ALLOCATION_ALIGNMENT) SLIST_HEADER qcat_free, qcat_pending;
static QcatEntry qcat_queue[QCAT_QUEUE_SIZE];
static HANDLE qcat_file = INVALID_HANDLE_VALUE;
static volatile LONG qcat_error;
static volatile LONG64 qcat_seen, qcat_enqueued, qcat_written, qcat_dropped, qcat_invalid, qcat_oversize;
static wchar_t qcat_directory[OPC_PATH_CHARS];

static BOOL qcat_write(HANDLE file, const void *data, DWORD size) {
    const unsigned char *bytes = data;
    while (size) {
        DWORD written_bytes = 0;
        if (!WriteFile(file, bytes, size, &written_bytes, NULL) || !written_bytes) {
            DWORD error = GetLastError();
            InterlockedCompareExchange(&qcat_error, error ? (LONG)error : ERROR_WRITE_FAULT, 0);
            return FALSE;
        }
        bytes += written_bytes; size -= written_bytes;
    }
    return TRUE;
}

static void qcat_close(void) {
    if (qcat_file != INVALID_HANDLE_VALUE) CloseHandle(qcat_file);
    qcat_file = INVALID_HANDLE_VALUE;
}

static DWORD qcat_start(const wchar_t *directory, uint64_t frequency, uint64_t started) {
    wcscpy(qcat_directory, directory);
    qcat_error = 0;
    qcat_seen = qcat_enqueued = qcat_written = qcat_dropped = qcat_invalid = qcat_oversize = 0;
    InitializeSListHead(&qcat_free); InitializeSListHead(&qcat_pending);
    for (unsigned i = 0; i < QCAT_QUEUE_SIZE; ++i) InterlockedPushEntrySList(&qcat_free, &qcat_queue[i].link);
    wchar_t path[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\quest-catalog.opcat", directory);
    qcat_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_NEW, 0, NULL);
    if (qcat_file == INVALID_HANDLE_VALUE) return GetLastError();
    OpcHeader header = {{'Q','C','A','T','0','0','0','1'}, 1, sizeof(QcatRecord), frequency, started};
    if (!qcat_write(qcat_file, &header, sizeof(header))) { qcat_close(); return (DWORD)qcat_error; }
    return ERROR_SUCCESS;
}

static void qcat_packet(const unsigned char *packet, HANDLE wake) {
    if (!packet || packet[5] != 0x0b || packet[7] ||
        (packet[6] != 0x18 && packet[6] != 0x1a && packet[6] != 0x1b && packet[6] != 0x1c)) return;
    uint64_t sequence = (uint64_t)InterlockedIncrement64(&qcat_seen);
    if (InterlockedCompareExchange(&qcat_error, 0, 0)) return;
    uint32_t size; memcpy(&size, packet, 4);
    if (size > QCAT_PACKET_MAX) { InterlockedIncrement64(&qcat_oversize); return; }
    unsigned minimum = packet[6] == 0x1b || packet[6] == 0x1c ? 8u : 12u;
    if (size < minimum || (size & 3) || !opc_readable(packet, size, FALSE)) {
        InterlockedIncrement64(&qcat_invalid); return;
    }
    PSLIST_ENTRY link = InterlockedPopEntrySList(&qcat_free);
    if (!link) { InterlockedIncrement64(&qcat_dropped); return; }
    QcatEntry *entry = CONTAINING_RECORD(link, QcatEntry, link);
    LARGE_INTEGER now; QueryPerformanceCounter(&now);
    entry->record = (QcatRecord){sequence, opc_filetime(), (uint64_t)now.QuadPart, size, 0};
    memcpy(entry->packet, packet, size);
    InterlockedPushEntrySList(&qcat_pending, &entry->link);
    InterlockedIncrement64(&qcat_enqueued);
    SetEvent(wake);
}

static void qcat_drain(void) {
    PSLIST_ENTRY pending = InterlockedFlushSList(&qcat_pending), ordered = NULL;
    while (pending) {
        PSLIST_ENTRY next = pending->Next;
        pending->Next = ordered; ordered = pending; pending = next;
    }
    while (ordered) {
        QcatEntry *entry = CONTAINING_RECORD(ordered, QcatEntry, link);
        ordered = ordered->Next;
        if (!qcat_error && qcat_write(qcat_file, &entry->record, sizeof(entry->record)) &&
            qcat_write(qcat_file, entry->packet, entry->record.packet_size))
            InterlockedIncrement64(&qcat_written);
        InterlockedPushEntrySList(&qcat_free, &entry->link);
    }
    if (!qcat_error && !FlushFileBuffers(qcat_file)) InterlockedExchange(&qcat_error, (LONG)GetLastError());
}

static void qcat_status(BOOL stopped) {
    wchar_t path[OPC_PATH_CHARS + 64], temp[OPC_PATH_CHARS + 64];
    swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\quest-catalog-status.json", qcat_directory);
    swprintf(temp, OPC_PATH_CHARS + 64, L"%ls\\quest-catalog-status.json.tmp", qcat_directory);
    char json[1000];
    int size = snprintf(json, sizeof(json),
        "{\"schema_version\":1,\"source_sha256\":\"%s\",\"stopped\":%s,\"max_packet_bytes\":%u,"
        "\"seen\":%lld,\"enqueued\":%lld,\"written\":%lld,\"dropped\":%lld,"
        "\"invalid\":%lld,\"oversize\":%lld,\"io_error\":%ld}",
        mock_mode ? "not_a_game_capture" : OPC_CLASSIC_SHA256, stopped ? "true" : "false", QCAT_PACKET_MAX,
        counter(&qcat_seen), counter(&qcat_enqueued), counter(&qcat_written), counter(&qcat_dropped),
        counter(&qcat_invalid), counter(&qcat_oversize), (long)qcat_error);
    if (size <= 0 || (size_t)size >= sizeof(json)) { InterlockedExchange(&qcat_error, ERROR_INSUFFICIENT_BUFFER); return; }
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) { InterlockedCompareExchange(&qcat_error, (LONG)GetLastError(), 0); return; }
    BOOL ok = qcat_write(file, json, (DWORD)size);
    CloseHandle(file);
    if (ok) {
        DWORD error = opc_replace_published_file(temp, path);
        if (error) InterlockedCompareExchange(&qcat_error, (LONG)error, 0);
    }
}
#else
#define qcat_start(directory, frequency, started) ERROR_SUCCESS
#define qcat_packet(packet, wake) ((void)0)
#define qcat_drain() ((void)0)
#define qcat_status(stopped) ((void)0)
#define qcat_close() ((void)0)
#endif
