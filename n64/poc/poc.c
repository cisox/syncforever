// Sync Forever X7 proof of concept (phase 1 of docs/n64-plan.md).
// Booted over USB by the box: mounts the SD card, sends the ED64/gamedata listing,
// then answers text commands so the box can measure USB and SD speed.
#include <libdragon.h>
#include <usb.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define CHUNK (128 * 1024)

// The X7's USB registers (docs/n64-plan.md): the same as libdragon's usb.c uses.
#define ED_USBCFG 0x1F800004
#define ED_USBDAT 0x1F800400
#define ED_WR_NOP 0xC000
#define ED_WR 0xC200
#define ED_ACT 0x0200

static uint8_t block[512] __attribute__((aligned(16)));
static int pad_mode = 1;

// Register access by 4-byte PI DMA, never CPU loads/stores: Krikzz's menu does it this way, and
// direct io_write/io_read during USB transfers froze the cart on the PI bus (2026-11-02).
static uint32_t reg_buf[4] __attribute__((aligned(16)));

static void reg_wr(uint32_t addr, uint32_t val) {
    reg_buf[0] = val;
    data_cache_hit_writeback(reg_buf, 16);
    dma_write_raw_async(reg_buf, addr, 4);
    dma_wait();
}

static uint32_t reg_rd(uint32_t addr) {
    data_cache_hit_writeback_invalidate(reg_buf, 16);
    dma_read_raw_async(reg_buf, addr, 4);
    dma_wait();
    return reg_buf[0];
}

// Send one 512-byte block; 0, or -1 when the FPGA stays busy (then it goes back to read idle).
static int tx_block(void) {
    data_cache_hit_writeback(block, sizeof block);
    reg_wr(ED_USBCFG, ED_WR_NOP);
    dma_write_raw_async(block, ED_USBDAT, sizeof block);
    dma_wait();
    reg_wr(ED_USBCFG, ED_WR);  // buffer address 0: all 512 bytes
    uint64_t t0 = get_ticks_ms();
    while (reg_rd(ED_USBCFG) & ED_ACT)
        if (get_ticks_ms() - t0 > 100) {
            reg_wr(ED_USBCFG, 0xC400);
            return -1;
        }
    return 0;
}

// A UNFLoader frame (DMA@, type:u8 size:u24, data, CMPH), zero-padded to whole 512-byte blocks,
// so no short block ever follows a full one (that froze the cart on the PI bus, 2026-11-02).
static int tx_frame(int type, const void *data, int size) {
    const uint8_t *p = data;
    uint32_t header = (uint32_t)type << 24 | (size & 0xFFFFFF);
    uint8_t head[8] = {'D', 'M', 'A', '@', header >> 24, header >> 16, header >> 8, header};
    static const uint8_t cmph[4] = {'C', 'M', 'P', 'H'};
    int fill = 0, errs = 0;
    const uint8_t *parts[3] = {head, p, cmph};
    int lens[3] = {8, size, 4};
    for (int i = 0; i < 3; i++) {
        int off = 0;
        while (off < lens[i]) {
            int n = lens[i] - off < 512 - fill ? lens[i] - off : 512 - fill;
            memcpy(block + fill, parts[i] + off, n);
            fill += n;
            off += n;
            if (fill == 512) {
                errs += tx_block() != 0;
                fill = 0;
            }
        }
    }
    if (fill) {
        memset(block + fill, 0, 512 - fill);
        errs += tx_block() != 0;
    }
    return errs;
}

static int usb_send(int type, const void *data, int size) {
    if (pad_mode) return tx_frame(type, data, size);
    usb_write(type, data, size);
    return usb_timedout();
}

static char out[4096];
static uint8_t *buf;

static void say(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(out, sizeof out, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof out - 1) n = sizeof out - 1;
    usb_send(DATATYPE_TEXT, out, n);
    printf("> %.60s\n", out);
    console_render();
}

static void list_gamedata(void) {
    dir_t d;
    int len = 0, count = 0;
    len += snprintf(out, sizeof out, "LIST ED64/gamedata\n");
    if (dir_findfirst("sd:/ED64/gamedata", &d) == 0) {
        do {
            if (len < (int)sizeof out - 300)
                len += snprintf(out + len, sizeof out - len, "%c %lld %s\n",
                                d.d_type == DT_DIR ? 'd' : 'f', (long long)d.d_size, d.d_name);
            count++;
        } while (dir_findnext("sd:/ED64/gamedata", &d) == 0);
    }
    len += snprintf(out + len, sizeof out - len, "END %d", count);
    usb_send(DATATYPE_TEXT, out, len);
    printf("listed %d gamedata entries\n", count);
    console_render();
}

// "ls PATH": every entry with its raw d_type, so the box sees what the filesystem reports.
static void ls(const char *path) {
    dir_t d;
    int len = snprintf(out, sizeof out, "LS %s\n", path), count = 0;
    int r = dir_findfirst(path, &d);
    while (r == 0) {
        if (len < (int)sizeof out - 300)
            len += snprintf(out + len, sizeof out - len, "%d %lld %s\n", d.d_type, (long long)d.d_size, d.d_name);
        count++;
        r = dir_findnext(path, &d);
    }
    len += snprintf(out + len, sizeof out - len, "END %d (last %d)", count, r);
    usb_send(DATATYPE_TEXT, out, len);
}

// "sdread PATH": time reading up to 8 MB of one file.
static void sd_read_test(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { say("ERR sdread: open %s", path); return; }
    int64_t done = 0;
    uint64_t t0 = get_ticks_ms();
    while (done < 8 * 1024 * 1024) {
        size_t n = fread(buf, 1, CHUNK, f);
        if (n == 0) break;
        done += n;
    }
    uint64_t ms = get_ticks_ms() - t0;
    fclose(f);
    say("SDREAD %lld bytes in %llu ms (%s)", (long long)done, (unsigned long long)ms, path);
}

static void sd_write_test(int total) {
    const char *path = "sd:/sfe-speed.bin";
    FILE *f = fopen(path, "wb");
    if (!f) { say("ERR sdwrite: open"); return; }
    memset(buf, 0x5A, CHUNK);
    int done = 0;
    uint64_t t0 = get_ticks_ms();
    while (done < total) {
        int n = total - done < CHUNK ? total - done : CHUNK;
        if (fwrite(buf, 1, n, f) != (size_t)n) break;
        done += n;
    }
    fclose(f);
    uint64_t ms = get_ticks_ms() - t0;
    int removed = remove(path) == 0;
    say("SDWRITE %d bytes in %llu ms (removed %d)", done, (unsigned long long)ms, removed);
}

// Cart -> box: total bytes in frames of `frame` bytes, with progress on screen.
static void send_test(int total, int frame) {
    if (frame <= 0 || frame > CHUNK) frame = CHUNK;
    for (int i = 0; i < CHUNK; i++) buf[i] = (uint8_t)i;
    uint64_t t0 = get_ticks_ms();
    int done = 0, k = 0, timeouts = 0;
    printf("send %d in %d\n", total, frame);
    while (done < total) {
        int n = total - done < frame ? total - done : frame;
        uint32_t st = reg_rd(ED_USBCFG);
        printf("\r#%d st %04lx to %d ", k, (unsigned long)(st & 0xFFFF), timeouts);
        console_render();
        timeouts += usb_send(DATATYPE_RAWBINARY, buf, n);
        done += n;
        k++;
    }
    printf("\n");
    say("SENT %d bytes in %d frames, %llu ms, timeouts %d", done, k,
        (unsigned long long)(get_ticks_ms() - t0), timeouts);
}

static void handle(uint32_t header) {
    int type = USBHEADER_GETTYPE(header), size = USBHEADER_GETSIZE(header);
    if (type == DATATYPE_RAWBINARY) {
        uint64_t t0 = get_ticks_ms();
        uint32_t sum = 0;
        int left = size;
        while (left > 0) {
            int n = left < CHUNK ? left : CHUNK;
            usb_read(buf, n);
            for (int i = 0; i < n; i++) sum += buf[i];
            left -= n;
        }
        say("GOT %d bytes sum %lu, read out in %llu ms", size, (unsigned long)sum,
            (unsigned long long)(get_ticks_ms() - t0));
        return;
    }
    char cmd[300] = {0};
    int n = size < (int)sizeof cmd - 1 ? size : (int)sizeof cmd - 1;
    usb_read(cmd, n);
    if (size > n) usb_skip(size - n);
    if (strcmp(cmd, "ping") == 0) say("pong");
    else if (strncmp(cmd, "pad ", 4) == 0) { pad_mode = atoi(cmd + 4); say("pad %d", pad_mode); }
    else if (strcmp(cmd, "list") == 0) list_gamedata();
    else if (strncmp(cmd, "sdread ", 7) == 0) sd_read_test(cmd + 7);
    else if (strncmp(cmd, "ls ", 3) == 0) ls(cmd + 3);
    else if (strncmp(cmd, "sdwrite ", 8) == 0) sd_write_test(atoi(cmd + 8));
    else if (strncmp(cmd, "send ", 5) == 0) {
        char *rest = strchr(cmd + 5, ' ');
        send_test(atoi(cmd + 5), rest ? atoi(rest + 1) : CHUNK);
    }
    else say("ERR unknown: %s", cmd);
}

int main(void) {
    console_init();
    console_set_render_mode(RENDER_MANUAL);
    printf("Sync Forever X7 test\n");
    console_render();

    buf = malloc_uncached(CHUNK);
    usb_initialize();
    printf("usb cart %d\n", usb_getcart());
    console_render();

    int sd = debug_init_sdfs("sd:/", -1);
    say("HELLO cart %d sd %d", usb_getcart(), sd);
    if (sd) list_gamedata();

    // Diagnostics: bytes waiting that usb_poll() rejected (not a DMA@ header).
    int junk = 0, frames = 0;
    uint64_t shown = 0;
    while (1) {
        uint32_t header = usb_poll();
        if (header) {
            frames++;
            handle(header);
        } else if ((reg_rd(ED_USBCFG) & 0x1400) == 0x1000) {
            junk++;  // powered, and RXF low: data waiting that poll didn't take as a frame
        }
        if (get_ticks_ms() - shown > 1000) {
            shown = get_ticks_ms();
            printf("\rframes %d junk %d   ", frames, junk);
            console_render();
        }
    }
}
