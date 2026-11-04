/* Text screen on the NES background: 32x30 tiles, one font, a highlight bar.
 *
 * Two nametables (vertical mirroring): one shown, one hidden ("back").
 *  - Full redraw: scr_off(), scr_put_row()/scr_put_row_both(), scr_on(). One blank frame.
 *  - Live: scr_queue_row() (shown) or scr_queue_back() (hidden), up to SCR_QUEUE rows,
 *    then scr_frame() pushes them in the next vblank. scr_flip() swaps shown/hidden in the
 *    same vblank, after the push: draw a page into the back over a few frames, then flip.
 */
#ifndef SCREEN_H
#define SCREEN_H

#define SCR_W 32
#define SCR_H 30
#define SCR_QUEUE 3 /* rows per vblank: ~1500 cycles of the ~2270 available */

#define HL 0x80 /* OR into a tile to draw it on the highlight bar */

/* Layout every screen shares (scr_init sets its colour blocks and draws the header):
 *  rows 0-1   blank: TVs hide the top ~8-16 lines (overscan)
 *  rows 2-5   header art (palette 1)
 *  row 7      "Status: <status>": columns 0-7 palette 2 (label), 8-31 palette 3
 *  rows 8-25  body (palette 0)
 *  row 26     help (palette 2)
 * Palettes (scr_pal, 4 x 4 bytes, colour 0 = backdrop):
 *  0 body: bar, accent, text            1 header: stripes, stripes, title
 *  2 label: bar, label, label           3 status: bar, status, status */
#define ROW_HEADER 2
#define ROW_STATUS 7
#define ROW_BODY 10
#define ROW_HELP 26
#define COL_LEFT 1   /* where every line starts */
#define COL_RIGHT 30 /* the last column the sync screen uses */
#define COL_STATUS 9 /* the status word; column 8 (a block edge) is the space before it */

extern u8 scr_pal[16]; /* the theme; scr_init() uses it, scr_queue_pal() pushes changes */

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

/* Format one row: text left-aligned at column x, padded with spaces; hl = HL or 0. */
void scr_format(u8 *row, u8 x, const char *text, u8 hl);
void scr_format_center(u8 *row, const char *text, u8 hl);

/* Rendering must be off. */
void scr_put_row(u8 y, const u8 *row);      /* shown nametable */
void scr_put_row_both(u8 y, const u8 *row); /* both: for rows that never change */

u8 scr_queue_row(u8 y, const u8 *row);  /* 0 if the queue is full */
u8 scr_queue_back(u8 y, const u8 *row); /* 0 if the queue is full */
u8 scr_queue_free(void);
u8 scr_queue_pal(void); /* scr_pal into palette RAM in the next vblank (two queue slots: screen.c) */
void scr_flip(void);
void scr_frame(void); /* wait for vblank, push the queue, flip if asked, reset scroll */

u8 joy_read(void);

#endif
