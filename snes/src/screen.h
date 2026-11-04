/* Text screen on the SNES: BG1 in mode 0, 32x28 tiles of chr.py's font and art (2bpp).
 *
 * The same interface as the NES app's screen.h, so main.c ports with little change, but
 * simpler underneath: rows go into a copy of the tilemap in WRAM, and scr_frame() sends the
 * whole copy (and the palette, if it changed) by DMA in the next vblank. Nothing is ever
 * dropped, so scr_queue_* always succeed; there is one tilemap, so flipping does nothing.
 */
#ifndef SCREEN_H
#define SCREEN_H

#define SCR_W 32
#define SCR_H 28 /* 224 lines */

#define HL 0x80 /* OR into a tile to draw it on the highlight bar (chr.py's inverted glyphs) */

/* Layout every screen shares (scr_init sets its colours and draws the header). TVs crop the
 * picture's edges (overscan), so nothing is drawn within a tile of any edge:
 *  rows 0-1   blank
 *  rows 2-5   header art (palette 1)
 *  row 7      "Status: <status>": columns 0-8 palette 2 (label), 9-31 palette 3
 *  rows 8-25  body (palette 0)
 *  row 26     help (palette 2)
 *  row 27     blank
 * Palettes (scr_pal, 4 x 4 NES colour numbers, colour 0 = backdrop; nespal.h):
 *  0 body: bar, accent, text            1 header: stripes, stripes, title
 *  2 label: bar, label, label           3 status: bar, status, status */
#define ROW_HEADER 2
#define ROW_STATUS 7
#define ROW_BODY 10
#define ROW_HELP 26
#define COL_LEFT 1    /* where every line starts */
#define COL_RIGHT 30  /* the last column used */
#define COL_STATUS 9  /* the status word; column 8 is the space before it */

extern u8 scr_pal[16];

/* The NES app's joypad bits, from the SNES pad (B and A as on the NES pad). */
#define JOY_A 0x80
#define JOY_B 0x40
#define JOY_SEL 0x20
#define JOY_START 0x10
#define JOY_UP 0x08
#define JOY_DOWN 0x04
#define JOY_LEFT 0x02
#define JOY_RIGHT 0x01

void scr_init(void);
void scr_off(void);
void scr_on(void);
void scr_wait_vblank(void);

void scr_format(u8 *row, u8 x, const char *text, u8 hl);
void scr_format_center(u8 *row, const char *text, u8 hl);

void scr_put_row(u8 y, const u8 *row);
void scr_put_row_both(u8 y, const u8 *row);

u8 scr_queue_row(u8 y, const u8 *row);  /* always 1 */
u8 scr_queue_back(u8 y, const u8 *row); /* always 1: the same tilemap */
u8 scr_queue_free(void);                /* always room */
u8 scr_queue_pal(void);                 /* scr_pal goes out in the next vblank; always 1 */
void scr_flip(void);                    /* nothing to flip */
void scr_frame(void); /* wait for vblank, send what changed */

u8 joy_read(void);

#endif
