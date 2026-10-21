#define OVERPARSE_TEST_HOST
#include "win_util.h"

typedef uintptr_t (*Callback)(void *, const unsigned char *);
static unsigned calls, altered;
static const unsigned char *expected_packet;
static unsigned char expected_copy[262148];
static unsigned expected_size;
static uintptr_t original(void *owner, const unsigned char *packet) {
    ++calls;
    if (owner != (void *)(uintptr_t)42 || packet != expected_packet || memcmp(packet, expected_copy, expected_size)) ++altered;
    return 123;
}
static BOOL emit(Callback *slot, unsigned minor, unsigned count, unsigned stride) {
    unsigned size = stride ? 12 + count * stride : count;
    unsigned char *packet = calloc(size + 16, 1);
    if (!packet) return FALSE;
    memcpy(packet, &size, 4); packet[5] = minor == 0x99 ? 0x07 : 0x0b; packet[6] = (unsigned char)minor;
    if (stride) {
        uint32_t encoded = (count + (minor == 0x18 ? 0xc5 : 0x5b)) ^ (minor == 0x18 ? 0x1db0 : 0x292c);
        memcpy(packet + 8, &encoded, 4);
        for (unsigned i = 0; i < count; ++i) {
            unsigned char *item = packet + 12 + i * stride;
            uint32_t id = i ? 1130 + i - 1 : 475, name = i ? 130030 + i - 1 : 20010;
            memcpy(item + 32, &id, 4); item[40] = 11; memcpy(item + 44, &name, 4);
        }
    }
    expected_packet = packet; expected_size = size; memcpy(expected_copy, packet, size);
    BOOL ok = (*slot)((void *)(uintptr_t)42, packet) == 123 && !memcmp(packet, expected_copy, size);
    free(packet); return ok;
}
int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    HMODULE dll = LoadLibraryW(argv[1]); if (!dll) return 3;
    OpcControl start = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStartTest");
    OpcControl stop = (OpcControl)(uintptr_t)GetProcAddress(dll, "CaptureObserverStop");
    if (!start || !stop) return 4;
    Callback *slot = VirtualAlloc(NULL, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!slot) return 5;
    *slot = original;
    OpcTestConfig config = {0};
    config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    wcscpy(config.config.output_directory, argv[2]);
    config.callback = (void *)(uintptr_t)&original; config.method_slot = (void **)slot;
    DWORD error = start(&config);
    if (error) { fprintf(stderr, "observer start: %lu\n", error); return 6; }
    if (!emit(slot, 0x18, 2, 1076) || !emit(slot, 0x18, 166, 1076) ||
        !emit(slot, 0x1a, 2, 404) || !emit(slot, 0x1b, 8, 0) || !emit(slot, 0x1c, 8, 0) ||
        !emit(slot, 0x99, 64, 0) || !emit(slot, 0x18, 10, 0) || !emit(slot, 0x18, 262148, 0)) return 7;
    error = stop(NULL);
    if (error || calls != 8 || altered) { fprintf(stderr, "stop=%lu calls=%u altered=%u\n", error, calls, altered); return 8; }
    puts("PASS: original callback gets unchanged pointers, bytes, context, and return; large list + difficulty + terminators captured");
    return 0;
}
