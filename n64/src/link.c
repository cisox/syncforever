// The X7's USB registers, reached only by 4-byte PI DMA: direct CPU loads and stores (libdragon's
// io_read/io_write, used by its usb.c) froze the N64 on the PI bus mid-transfer (2026-11-02), and
// Krikzz's menu does it by DMA too. Frames are UNFLoader's: "DMA@", type:u8 size:u24, data, "CMPH".
// The box pads its frames to 4 bytes; ours are padded with zeros to whole 512-byte blocks, so a
// short block never follows a full one (the box skips to the next "DMA@").
#include <libdragon.h>
#include <string.h>
#include "link.h"

#define REG_CFG 0x1F800004
#define REG_VER 0x1F800014
#define REG_DAT 0x1F800400
#define REG_SYSCFG 0x1F808000
#define REG_KEY 0x1F808004

#define CFG_RD_NOP 0xC400
#define CFG_RD 0xC600
#define CFG_WR_NOP 0xC000
#define CFG_WR 0xC200
#define ST_ACT 0x0200
#define ST_RXF 0x0400
#define ST_PWR 0x1000
#define BUSY_MS 100
// A reply to a file request: the box has just asked, so it's reading; wait out a pause. Giving up
// after 100 ms lost blocks and cut the box's frame short (64,088 of 65,539 bytes, 2026-11-05).
#define SENDING_MS 2000

int link_junk;

static uint32_t regbuf[4] __attribute__((aligned(16)));
static uint8_t blk[512] __attribute__((aligned(16)));

static void reg_wr(uint32_t addr, uint32_t val) {
    regbuf[0] = val;
    data_cache_hit_writeback(regbuf, sizeof regbuf);
    dma_write_raw_async(regbuf, addr, 4);
    dma_wait();
}

static uint32_t reg_rd(uint32_t addr) {
    data_cache_hit_writeback_invalidate(regbuf, sizeof regbuf);
    dma_read_raw_async(regbuf, addr, 4);
    dma_wait();
    return regbuf[0];
}

// Wait for the FPGA to finish a USB transfer; on timeout put it back in read-idle.
static int busy_wait(uint64_t ms) {
    uint64_t t0 = get_ticks_ms();
    while (reg_rd(REG_CFG) & ST_ACT) {
        if (get_ticks_ms() - t0 > ms) {
            reg_wr(REG_CFG, CFG_RD_NOP);
            return -1;
        }
    }
    return 0;
}

uint32_t link_init(void) {
    reg_wr(REG_KEY, 0xAA55);
    uint32_t ver = reg_rd(REG_VER);
    reg_wr(REG_SYSCFG, 0);
    reg_wr(REG_CFG, CFG_RD_NOP);
    return ver;
}

static int rx_waiting(void) {
    return (reg_rd(REG_CFG) & (ST_PWR | ST_RXF)) == ST_PWR;
}

// n bytes (a multiple of 4) from USB; -1 on timeout.
static int rx_bytes(uint8_t *dst, int n) {
    while (n > 0) {
        int b = n < 512 ? n : 512;
        int addr = 512 - b;
        reg_wr(REG_CFG, CFG_RD | addr);
        if (busy_wait(BUSY_MS)) return -1;
        data_cache_hit_writeback_invalidate(blk, sizeof blk);
        dma_read_raw_async(blk, REG_DAT + addr, b);
        dma_wait();
        memcpy(dst, blk, b);
        dst += b;
        n -= b;
    }
    return 0;
}

int link_recv(uint8_t *buf, int cap, int *type) {
    uint8_t head[8] __attribute__((aligned(8)));
    if (!rx_waiting()) return 0;
    if (rx_bytes(head, 4)) return -1;
    while (memcmp(head, "DMA@", 4) != 0) {  // out of step: look for the next frame
        link_junk++;
        if (!rx_waiting() || rx_bytes(head, 4)) return -1;
    }
    if (rx_bytes(head + 4, 4)) return -1;
    *type = head[4];
    int size = head[5] << 16 | head[6] << 8 | head[7];
    int padded = (size + 3) & ~3;
    if (padded > cap) {
        for (int left = padded; left > 0; left -= cap) {
            if (rx_bytes(buf, left < cap ? left : cap)) return -1;
        }
        rx_bytes(head, 4);
        return -2;
    }
    if (rx_bytes(buf, padded)) return -1;
    if (rx_bytes(head, 4) || memcmp(head, "CMPH", 4) != 0) return -1;
    return size;
}

static int tx_block(uint64_t wait_ms) {
    data_cache_hit_writeback(blk, sizeof blk);
    reg_wr(REG_CFG, CFG_WR_NOP);
    dma_write_raw_async(blk, REG_DAT, sizeof blk);
    dma_wait();
    reg_wr(REG_CFG, CFG_WR);  // buffer address 0: the whole block
    return busy_wait(wait_ms);
}

int link_send(int type, const void *data, int size) {
    uint32_t header = (uint32_t)type << 24 | (size & 0xFFFFFF);
    uint8_t head[8] = {'D', 'M', 'A', '@', header >> 24, header >> 16, header >> 8, header};
    static const uint8_t cmph[4] = {'C', 'M', 'P', 'H'};
    const uint8_t *parts[3] = {head, data, cmph};
    int lens[3] = {8, size, 4};
    int fill = 0;
    // App messages: maybe no box is there, and the app shouldn't stall for a frame nobody wants.
    uint64_t wait_ms = type == LINK_TYPE_SFE ? SENDING_MS : BUSY_MS;
    for (int i = 0; i < 3; i++) {
        for (int off = 0; off < lens[i];) {
            int n = lens[i] - off < 512 - fill ? lens[i] - off : 512 - fill;
            memcpy(blk + fill, parts[i] + off, n);
            fill += n;
            off += n;
            if (fill == 512) {
                if (tx_block(wait_ms)) return -1;  // the rest is no use: the box asks again
                fill = 0;
            }
        }
    }
    if (fill) {
        memset(blk + fill, 0, 512 - fill);
        if (tx_block(wait_ms)) return -1;
    }
    return 0;
}
