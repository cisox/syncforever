// File requests from the box, answered with libdragon's FatFs (volume "0:", the SD card mounted by
// debug_init_sdfs). Status bytes are FatFs FRESULT codes, the same the PRO carts send.
// Every number is big-endian (the N64's own order).
#include <libdragon.h>
#include <fatfs/ff.h>
#include <cpak.h>
#include <string.h>
#include "files.h"

const char *files_last = "-";

static FIL file;
static int file_open;

static void put16(uint8_t *p, uint32_t v) { p[0] = v >> 8; p[1] = v; }
static void put32(uint8_t *p, uint32_t v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }
static uint32_t get16(const uint8_t *p) { return p[0] << 8 | p[1]; }
static uint32_t get32(const uint8_t *p) { return (uint32_t)p[0] << 24 | p[1] << 16 | p[2] << 8 | p[3]; }

// "0:/" + the box's path (no leading slash). 0 if the string doesn't fit the request.
static int fs_path(char *dst, int cap, const uint8_t *p, const uint8_t *end) {
    if (end - p < 2) return 0;
    int n = get16(p);
    if (n > end - p - 2 || n + 4 > cap) return 0;
    memcpy(dst, "0:/", 3);
    memcpy(dst + 3, p + 2, n);
    dst[3 + n] = 0;
    return 1;
}

// One entry: attr u8, size u32, fdate u16, ftime u16, name (u16 length + UTF-8).
static int put_entry(uint8_t *o, const FILINFO *fi) {
    int n = strlen(fi->fname);
    o[0] = fi->fattrib;
    put32(o + 1, fi->fsize > 0xFFFFFFFFu ? 0xFFFFFFFFu : (uint32_t)fi->fsize);
    put16(o + 5, fi->fdate);
    put16(o + 7, fi->ftime);
    put16(o + 9, n);
    memcpy(o + 11, fi->fname, n);
    return 11 + n;
}

int files_hello(uint8_t *out) {
    out[0] = OP_HELLO | OP_REPLY;
    out[1] = 0;
    out[2] = FR_OK;
    out[3] = X7_PROTO;
    int n = strlen(APP_VERSION);
    put16(out + 4, n);
    memcpy(out + 6, APP_VERSION, n);
    return 6 + n;
}

int files_handle(const uint8_t *req, int len, uint8_t *out, int cap) {
    static char path[1100];
    const uint8_t *end = req + len;
    if (len < 2) return 0;
    int op = req[0];
    out[0] = op | OP_REPLY;
    out[1] = req[1];  // the box's tag, echoed
    uint8_t *st = out + 2;
    int n = 3;
    FRESULT r = FR_INVALID_PARAMETER;
    const uint8_t *a = req + 2;

    switch (op) {
    case OP_HELLO:
        files_last = "hello";
        n = files_hello(out);
        out[1] = req[1];
        return n;

    case OP_STAT: {
        files_last = "stat";
        FILINFO fi;
        if (!fs_path(path, sizeof path, a, end)) break;
        r = f_stat(path, &fi);
        if (r == FR_OK) n += put_entry(out + n, &fi);
        break;
    }

    case OP_LIST: {  // start u16, path -> count u16, more u8, entries
        files_last = "list";
        DIR dir;
        FILINFO fi;
        if (end - a < 2 || !fs_path(path, sizeof path, a + 2, end)) break;
        int start = get16(a), count = 0, more = 0, at = n + 3;
        r = f_opendir(&dir, path);
        if (r != FR_OK) break;
        for (int i = 0;; i++) {
            r = f_readdir(&dir, &fi);
            if (r != FR_OK || !fi.fname[0]) break;
            if (i < start) continue;
            if (at + 11 + (int)strlen(fi.fname) > cap || count == 0xFFFF) {
                more = 1;
                break;
            }
            at += put_entry(out + at, &fi);
            count++;
        }
        f_closedir(&dir);
        put16(out + n, count);
        out[n + 2] = more;
        n = at;
        break;
    }

    case OP_OPEN: {  // mode u8 (FatFs FA_* bits), path
        files_last = "open";
        if (end - a < 1 || !fs_path(path, sizeof path, a + 1, end)) break;
        if (file_open) f_close(&file);
        r = f_open(&file, path, a[0]);
        file_open = r == FR_OK;
        break;
    }

    case OP_READ: {  // n u32 -> data
        files_last = "read";
        if (end - a < 4) break;
        r = FR_INVALID_OBJECT;
        if (!file_open) break;
        UINT want = get32(a), got = 0;
        if (want > (UINT)(cap - n)) want = cap - n;
        r = f_read(&file, out + n, want, &got);
        n += got;
        break;
    }

    case OP_SEEK: {  // position u32: where the next READ starts
        files_last = "seek";
        if (end - a < 4) break;
        r = FR_INVALID_OBJECT;
        if (file_open) r = f_lseek(&file, get32(a));
        break;
    }

    case OP_WRITE: {  // data -> written u32
        files_last = "write";
        r = FR_INVALID_OBJECT;
        if (!file_open) break;
        UINT put = 0;
        r = f_write(&file, a, end - a, &put);
        if (r == FR_OK && put != (UINT)(end - a)) r = FR_DENIED;  // the card is full
        put32(out + n, put);
        n += 4;
        break;
    }

    case OP_CLOSE:
        files_last = "close";
        r = file_open ? f_close(&file) : FR_OK;
        file_open = 0;
        break;

    case OP_DELETE:
        files_last = "delete";
        if (!fs_path(path, sizeof path, a, end)) break;
        r = f_unlink(path);
        break;

    case OP_MKDIR:
        files_last = "mkdir";
        if (!fs_path(path, sizeof path, a, end)) break;
        r = f_mkdir(path);
        break;

    case OP_RENAME: {  // src path, dst path
        files_last = "rename";
        static char dst[sizeof path];
        if (!fs_path(path, sizeof path, a, end)) break;
        const uint8_t *b = a + 2 + get16(a);
        if (!fs_path(dst, sizeof dst, b, end)) break;
        r = f_rename(path, dst);
        break;
    }

    case OP_CPAK: {  // port u8 (0-3) -> the pak's first bank (32 KB, an official pak's all)
        files_last = "cpak";
        if (end - a < 1 || a[0] > 3) break;
        r = FR_NO_FILE;  // no Controller Pak there (nothing, or a Rumble/Transfer Pak)
        if (joypad_get_accessory_type(a[0]) != JOYPAD_ACCESSORY_TYPE_CONTROLLER_PAK) break;
        if (cap - n < 0x8000) break;
        r = cpak_read(a[0], 0, 0, out + n, 0x8000) == 0x8000 ? FR_OK : FR_DISK_ERR;
        if (r == FR_OK) n += 0x8000;
        break;
    }

    default:
        files_last = "unknown";
        break;
    }
    *st = r;
    return n;
}
