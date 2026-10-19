#pragma once
#include "win_util.h"

typedef enum { OPC_COMMAND_START = 1, OPC_COMMAND_STOP = 2, OPC_COMMAND_SHUTDOWN = 3 } OpcCommandKind;
typedef struct { uint64_t id; OpcCommandKind kind; } OpcCommand;

static inline const char *opc_command_name(OpcCommandKind kind) {
    return kind == OPC_COMMAND_START ? "start" : kind == OPC_COMMAND_STOP ? "stop" :
           kind == OPC_COMMAND_SHUTDOWN ? "shutdown" : "none";
}

static inline DWORD opc_parse_command(const char *data, size_t size, OpcCommand *out) {
    static const char prefix[] = "OPC-CONTROL-1\n";
    if (size < sizeof(prefix) || memcmp(data, prefix, sizeof(prefix) - 1)) return ERROR_INVALID_DATA;
    size_t pos = sizeof(prefix) - 1;
    uint64_t id = 0;
    unsigned digits = 0;
    while (pos < size && data[pos] >= '0' && data[pos] <= '9') {
        unsigned value = (unsigned)(data[pos++] - '0');
        if (id > (UINT64_MAX - value) / 10) return ERROR_INVALID_DATA;
        id = id * 10 + value; ++digits;
    }
    if (!digits || !id || pos >= size || data[pos++] != '\n') return ERROR_INVALID_DATA;
    size_t remaining = size - pos;
    OpcCommandKind kind;
    if (remaining == 6 && !memcmp(data + pos, "start\n", 6)) kind = OPC_COMMAND_START;
    else if (remaining == 5 && !memcmp(data + pos, "stop\n", 5)) kind = OPC_COMMAND_STOP;
    else if (remaining == 9 && !memcmp(data + pos, "shutdown\n", 9)) kind = OPC_COMMAND_SHUTDOWN;
    else return ERROR_INVALID_DATA;
    out->id = id; out->kind = kind;
    return ERROR_SUCCESS;
}

static inline DWORD opc_read_command(const wchar_t *directory, OpcCommand *out) {
    wchar_t path[OPC_PATH_CHARS + 64];
    int n = swprintf(path, OPC_PATH_CHARS + 64, L"%ls\\control.command", directory);
    if (n <= 0 || n >= OPC_PATH_CHARS + 64) return ERROR_BAD_PATHNAME;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    char data[128]; DWORD bytes;
    BOOL ok = ReadFile(file, data, sizeof(data), &bytes, NULL);
    DWORD error = ok ? 0 : GetLastError();
    CloseHandle(file);
    if (error) return error;
    return opc_parse_command(data, bytes, out);
}

static inline BOOL opc_json_path(char *out, size_t capacity, const wchar_t *path) {
    char utf8[OPC_PATH_CHARS * 4];
    int n = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path, -1, utf8, sizeof(utf8), NULL, NULL);
    if (!n) return FALSE;
    size_t used = 0;
    for (int i = 0; i < n - 1; ++i) {
        unsigned char c = (unsigned char)utf8[i];
        if (c < 0x20) return FALSE;
        if (c == '\\' || c == '"') { if (used + 1 >= capacity) return FALSE; out[used++] = '\\'; }
        if (used + 1 >= capacity) return FALSE;
        out[used++] = (char)c;
    }
    out[used] = 0;
    return TRUE;
}
