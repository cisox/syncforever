// The box's file requests, answered with FatFs on the SD card (docs/x7-protocol.md).
#pragma once
#include <stdint.h>

#define X7_PROTO 1
#define APP_VERSION "0.1"

enum {
    OP_HELLO = 0x01,
    OP_STAT = 0x02,
    OP_LIST = 0x03,
    OP_OPEN = 0x04,
    OP_READ = 0x05,
    OP_WRITE = 0x06,
    OP_CLOSE = 0x07,
    OP_DELETE = 0x08,
    OP_MKDIR = 0x09,
    OP_CPAK = 0x0A,  // the Controller Pak in a controller: its 32 KB
    OP_RENAME = 0x0B,  // two paths: f_rename
    OP_SEEK = 0x0C,  // position u32: where the next READ starts
    OP_REPLY = 0x80,
};

// Answer one request (req, len bytes). Writes the reply to out (cap bytes); returns its length.
int files_handle(const uint8_t *req, int len, uint8_t *out, int cap);

// The HELLO the app sends unprompted when it starts (tag 0).
int files_hello(uint8_t *out);

// The last operation, for the screen.
extern const char *files_last;
