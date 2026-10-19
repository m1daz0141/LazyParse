#pragma once
/* Bounded prefixes of selected identity/lifecycle notifications, separate from
   OPCAP damage records. No generic packet dump or chat/inventory body logging. */
#define OPI_PREFIX_BYTES 192
#define OPI_QUEUE_SIZE 512
#pragma pack(push, 1)
typedef struct {
    uint64_t sequence, damage_sequence, filetime, qpc;
    uint32_t packet_size, stored_size;
    unsigned char prefix[OPI_PREFIX_BYTES];
} OpiRecord;
#pragma pack(pop)
_Static_assert(sizeof(OpiRecord) == 232, "identity record ABI v2");

static unsigned identity_prefix_limit(unsigned major, unsigned minor) {
    if (major == 6 && minor == 0) return 20;
    if (major == 8 && minor == 4) return 0x90; /* UTF-16 character name ends here. */
    if (major == 8 && minor == 0x11) return OPI_PREFIX_BYTES; /* Pet owner candidate. */
    if (major == 8 && (minor == 0xb || minor == 0xc || minor == 0xd || minor == 0x10)) return 128;
    if (major == 4 && (minor == 6 || minor == 0x3b || minor == 0x15)) return 128;
    if (major == 3 && minor == 0) return 128;
    /* Quest investigation only. Receive-side IDs/timestamps, without quest
       result rewards or list bodies; no automatic reset from these candidates. */
    if (major == 3 && minor == 0x24) return 128; /* Level/world/quest headers. */
    if (major == 0x0b) return 8;
    return 0;
}
