#define OVERPARSE_TEST_HOST
#include "win_util.h"

typedef uintptr_t (*Callback)(void *, const unsigned char *);
static unsigned calls;
static uintptr_t original(void *owner, const unsigned char *packet) {
    (void)owner; (void)packet;
    ++calls;
    return 123456;
}
static int send(Callback *slot, unsigned char *data, unsigned size, unsigned major, unsigned minor) {
    memcpy(data, &size, 4); data[4] = 0; data[5] = (unsigned char)major;
    data[6] = (unsigned char)minor; data[7] = (unsigned char)(minor >> 8);
    return (*slot)(NULL, data) != 123456;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 3) return 2;
    HMODULE module = LoadLibraryW(argv[1]);
    if (!module) return 3;
    OpcControl start = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureStartTest");
    OpcControl stop = (OpcControl)(uintptr_t)GetProcAddress(module, "CaptureStop");
    Callback *slot = VirtualAlloc(NULL, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    unsigned char *pages = VirtualAlloc(NULL, 8192, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    DWORD old;
    if (!start || !stop || !slot || !pages || !VirtualProtect(pages + 4096, 4096, PAGE_NOACCESS, &old)) return 4;
    *slot = original;
    if (!CreateDirectoryW(argv[2], NULL)) return 5;
    OpcTestConfig config = {0};
    config.config.size = sizeof(OpcConfig); config.config.abi = OPC_ABI;
    config.callback = (void *)(uintptr_t)&original; config.method_slot = (void **)slot;
    swprintf(config.config.output_directory, OPC_PATH_CHARS, L"%ls\\part-1", argv[2]);
    if (!CreateDirectoryW(config.config.output_directory, NULL) || start(&config)) return 6;
    unsigned char packet[84] = {0};
    uint32_t self = 42, enemy = 20000001, damage = 100, attack = 99;
    memcpy(packet + 8, &self, 4);
    if (send(slot, packet, 20, 6, 0)) return 7;
    memset(packet, 0, sizeof(packet)); memcpy(packet + 8, &enemy, 4); packet[16] = 6; packet[18] = 8;
    if (send(slot, packet, 84, 8, 0xd)) return 8;
    memset(packet, 0, sizeof(packet));
    memcpy(packet + 8, &self, 4); packet[16] = 4;
    memcpy(packet + 0x14, &enemy, 4); packet[0x1c] = 6; packet[0x1e] = 8;
    memcpy(packet + 0x20, &self, 4); packet[0x28] = 4;
    memcpy(packet + 0x2c, &attack, 4); memcpy(packet + 0x30, &damage, 4);
    if (send(slot, packet, 84, 4, 0x52) || send(slot, packet, 84, 7, 0)) return 9;
    /* Guard page proves a 2176-byte spawn copies only its 144-byte prefix. */
    unsigned char *prefix = pages + 4096 - 144;
    memset(prefix, 0xab, 144);
    if (send(slot, prefix, 2176, 8, 4)) return 10;
    /* Pet owner lies beyond the old 128-byte bound; still never copy the tail. */
    prefix = pages + 4096 - 192;
    memset(prefix, 0, 192);
    uint32_t pet = 77;
    memcpy(prefix + 8, &pet, 4); prefix[16] = 6; prefix[18] = 8;
    memcpy(prefix + 0x24, "PetFixture", 10); memcpy(prefix + 0xac, &self, 4);
    prefix[0xb4] = 4; /* Owner ObjectHeader is a player. */
    if (send(slot, prefix, 256, 8, 0x11)) return 16;
    if (send(slot, packet, 19, 8, 0xd)) return 11;
    if (stop(NULL) || send(slot, packet, 20, 6, 0)) return 12;
    swprintf(config.config.output_directory, OPC_PATH_CHARS, L"%ls\\part-2", argv[2]);
    if (!CreateDirectoryW(config.config.output_directory, NULL) || start(&config)) return 13;
    memset(packet, 0, sizeof(packet)); memcpy(packet + 8, &self, 4);
    if (send(slot, packet, 20, 6, 0) || stop(NULL)) return 14;
    if (calls != 9) return 15;
    puts("PASS: bounded identity prefixes, guarded tail, forwarding, stop and resume");
    return 0;
}
