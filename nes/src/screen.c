#include "main.h"
#include "art.h"

#define PPU_CTRL (*(vu8 *)0x2000)
#define PPU_MASK (*(vu8 *)0x2001)
#define PPU_STAT (*(vu8 *)0x2002)
#define PPU_SCROLL (*(vu8 *)0x2005)
#define PPU_ADDR (*(vu8 *)0x2006)
#define PPU_DATA (*(vu8 *)0x2007)
#define JOY1 (*(vu8 *)0x4016)

#define MASK_BG_ON 0x0A /* background on, including the leftmost 8 pixels */

/* Shared with fast.s (scr_vblank_push). */
u8 scr_q_rows[SCR_QUEUE * SCR_W];
u8 scr_q_hi[SCR_QUEUE];
u8 scr_q_lo[SCR_QUEUE];
u8 scr_q_n;

u8 scr_pal[16];
static u8 pal32[32]; /* scr_pal twice: $3F10 mirrors the backdrop, so the copy keeps it */

static u8 shown;    /* 0: $2000 shown, 1: $2400 shown */
static u8 flip_req; /* swap at the next scr_frame */

void __fastcall__ scr_copy32(const u8 *row);  /* fast.s */
void __fastcall__ scr_vblank_push(u8 ctrl);  /* fast.s: wait, push queue, ctrl, scroll 0 */

static u8 queue_addr(u16 addr, const u8 *row);

static u16 nt_base(u8 nt) {
    return nt ? 0x2400 : 0x2000;
}

static void set_addr(u16 addr) {
    PPU_ADDR = addr >> 8;
    PPU_ADDR = addr & 0xff;
}

static void reset_scroll(void) {
    PPU_CTRL = shown; /* nametable select, background tiles from $0000, no NMI */
    PPU_SCROLL = 0;
    PPU_SCROLL = 0;
}

void scr_wait_vblank(void) {
    (void)PPU_STAT;
    while (!(PPU_STAT & 0x80))
        ;
}

void scr_off(void) {
    scr_wait_vblank();
    PPU_MASK = 0;
}

void scr_on(void) {
    scr_wait_vblank();
    reset_scroll();
    PPU_MASK = MASK_BG_ON;
}

/* Colour blocks for the layout in screen.h: one byte per 32x32 area (2x2 blocks of 16x16,
   palette numbers TL | TR << 2 | BL << 4 | BR << 6). */
static const u8 attrs[64] = {
    0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, /* rows 0-3: (blank), header (1) */
    0xA5, 0xA5, 0xF5, 0xF5, 0xF5, 0xF5, 0xF5, 0xF5, /* rows 4-7: header (1); label (2) | status (3) */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* rows 8-11: body (0) */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* rows 12-15 */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* rows 16-19 */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* rows 20-23 */
    0xA0, 0xA0, 0xA0, 0xA0, 0xA0, 0xA0, 0xA0, 0xA0, /* rows 24-27: help (2) at 26-27 */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* rows 28-29 */
};

static void fill_pal32(void) {
    u8 i;

    for (i = 0; i < 16; i++) {
        pal32[i] = scr_pal[i];
        pal32[i + 16] = scr_pal[i];
    }
}

/* Header rows 0-3 into `row` one at a time, written at the current PPU address. */
static void put_header(void) {
    static const char title[] = ART_TITLE;
    u8 r, x, k, n, t;
    u8 row[SCR_W];

    for (r = 0; r < 4; r++) {
        for (x = 0; x < SCR_W; x++) {
            row[x] = r == 3 ? ART_STRIPES : ' ';
        }
        if (r == 1 || r == 2) {
            n = 0;
            for (k = 0; title[k]; k++) {
                x = ART_TITLE_X + 2 * k;
                if (title[k] == ' ') {
                    row[x] = row[x + 1] = ' ';
                    continue;
                }
                t = r == 1 ? 0 : 2;
                row[x] = ART_TITLE_SLOT(n + t);
                row[x + 1] = ART_TITLE_SLOT(n + t + 1);
                n += 4;
            }
        }
        scr_copy32(row);
    }
}

void scr_init(void) {
    u16 i;
    u8 t;

    PPU_MASK = 0;
    fill_pal32();
    set_addr(0x3F00);
    for (i = 0; i < 32; i++) {
        PPU_DATA = pal32[i];
    }
    for (t = 0; t < 2; t++) { /* both nametables: header, spaces, then the colour blocks */
        set_addr(nt_base(t));
        for (i = 0; i < ROW_HEADER * SCR_W; i++) {
            PPU_DATA = ' ';
        }
        put_header();
        for (i = (ROW_HEADER + 4) * SCR_W; i < SCR_W * SCR_H; i++) {
            PPU_DATA = ' ';
        }
        for (i = 0; i < 64; i++) {
            PPU_DATA = attrs[i];
        }
    }
    shown = 0;
    flip_req = 0;
    scr_q_n = 0;
}

/* The palette twice in one vblank: on hardware the first palette bytes written in a vblank
   don't all stick (entry 2 kept its old colour while 14-15 changed; the second copy lands whole;
   N8 PRO on an NTSC NES, 2026-11-02). Needs two free slots; 0 if not now. */
u8 scr_queue_pal(void) {
    if (scr_queue_free() < 2) {
        return 0;
    }
    fill_pal32();
    queue_addr(0x3F00, pal32);
    return queue_addr(0x3F00, pal32);
}

void scr_format(u8 *row, u8 x, const char *text, u8 hl) {
    u8 i;

    for (i = 0; i < x; i++) {
        row[i] = ' ' | hl;
    }
    for (; i < SCR_W && *text; i++) {
        row[i] = (*text++ & 0x7F) | hl;
    }
    for (; i < SCR_W; i++) {
        row[i] = ' ' | hl;
    }
}

void scr_format_center(u8 *row, const char *text, u8 hl) {
    u8 len = 0;

    while (text[len] && len < SCR_W) {
        len++;
    }
    scr_format(row, (SCR_W - len) >> 1, text, hl);
}

void scr_put_row(u8 y, const u8 *row) {
    set_addr(nt_base(shown) + (u16)y * SCR_W);
    scr_copy32(row);
}

void scr_put_row_both(u8 y, const u8 *row) {
    set_addr(0x2000 + (u16)y * SCR_W);
    scr_copy32(row);
    set_addr(0x2400 + (u16)y * SCR_W);
    scr_copy32(row);
}

static u8 queue_addr(u16 addr, const u8 *row) {
    u8 *dst, i;

    if (scr_q_n == SCR_QUEUE) {
        return 0;
    }
    dst = scr_q_rows + scr_q_n * SCR_W;
    for (i = 0; i < SCR_W; i++) {
        dst[i] = row[i];
    }
    scr_q_hi[scr_q_n] = addr >> 8;
    scr_q_lo[scr_q_n] = addr & 0xff;
    scr_q_n++;
    return 1;
}

static u8 queue(u8 nt, u8 y, const u8 *row) {
    return queue_addr(nt_base(nt) + (u16)y * SCR_W, row);
}

u8 scr_queue_row(u8 y, const u8 *row) {
    return queue(shown, y, row);
}

u8 scr_queue_back(u8 y, const u8 *row) {
    return queue(shown ^ 1, y, row);
}

u8 scr_queue_free(void) {
    return SCR_QUEUE - scr_q_n;
}

void scr_flip(void) {
    flip_req = 1;
}

void scr_frame(void) {
    if (flip_req) {
        shown ^= 1;
        flip_req = 0;
    }
    scr_vblank_push(shown);
}

u8 joy_read(void) {
    u8 i, joy = 0;

    JOY1 = 1;
    JOY1 = 0;
    for (i = 0; i < 8; i++) {
        joy <<= 1;
        if (JOY1 & 3) {
            joy |= 1;
        }
    }
    return joy;
}
