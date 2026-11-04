#include "main.h"
#include "art.h"
#include "nespal.h"

#define REG(a) (*(vu8 *)(a))
#define INIDISP REG(0x2100)
#define BGMODE REG(0x2105)
#define BG1SC REG(0x2107)
#define BG12NBA REG(0x210B)
#define VMAIN REG(0x2115)
#define VMADDL REG(0x2116)
#define VMADDH REG(0x2117)
#define CGADD REG(0x2121)
#define TM REG(0x212C)
#define NMITIMEN REG(0x4200)
#define MDMAEN REG(0x420B)
#define HVBJOY REG(0x4212)
#define JOY1L REG(0x4218)
#define JOY1H REG(0x4219)
#define DMAP0 REG(0x4300)
#define BBAD0 REG(0x4301)
#define A1T0L REG(0x4302)
#define A1T0H REG(0x4303)
#define A1B0 REG(0x4304)
#define DAS0L REG(0x4305)
#define DAS0H REG(0x4306)

#define MAP_VRAM 0x1000 /* tilemap at VRAM word $1000; the font fills $0000-$07FF */
#define MAP_BYTES (SCR_W * 32 * 2)

extern const u8 font[]; /* font.s: 256 tiles, 2bpp */

u8 scr_pal[16];
static u8 map[MAP_BYTES]; /* tile, attribute (palette << 2) per cell; rows 28-31 unused */
static u8 cgram[40]; /* palettes 0-3 (scr_pal), and 4: the stripes' (palette 1, colour 1 dimmed) */
static u8 map_dirty, pal_dirty;

/* channel 0: `size` bytes from bank 0 `src` to B-bus register `reg` (mode: 0 one register,
   1 two: $2118/$2119) */
static void dma(const void *src, u16 size, u8 reg, u8 mode) {
    DMAP0 = mode;
    BBAD0 = reg;
    A1T0L = (u16)src & 0xFF;
    A1T0H = (u16)src >> 8;
    A1B0 = 0;
    DAS0L = size & 0xFF;
    DAS0H = size >> 8;
    MDMAEN = 1;
}

/* A BGR555 colour (2 bytes, low first) at 3/4: the stripes' thin lines, which a CRT's
   bloom ran together at full red (2026-11-03). */
static void dim(u8 *c) {
    u16 v = c[0] | (u16)c[1] << 8;
    u16 r = v & 31, g = (v >> 5) & 31, b = (v >> 10) & 31;

    v = (r * 3 / 4) | (g * 3 / 4) << 5 | (b * 3 / 4) << 10;
    c[0] = v & 0xFF;
    c[1] = v >> 8;
}

static void vram_addr(u16 word) {
    VMADDL = word & 0xFF;
    VMADDH = word >> 8;
}

/* scr_pal as CGRAM colours, ready for send(). Done outside vblank: dim()'s 16-bit maths took
   long enough there that the map's DMA ran past the vblank's end, and the console drops VRAM
   writes during the picture: rows kept old letters ("Donkey KCre", 2026-11-03). */
static void build_cgram(void) {
    u8 i;

    for (i = 0; i < 16; i++) {
        cgram[2 * i] = nes_rgb[scr_pal[i] & 0x3F] & 0xFF;
        cgram[2 * i + 1] = nes_rgb[scr_pal[i] & 0x3F] >> 8;
    }
    for (i = 0; i < 8; i++) {
        cgram[32 + i] = cgram[8 + i];
    }
    dim(cgram + 34);
    pal_dirty = 1;
}

/* In vblank: DMA only, the map first. */
static void send(void) {
    if (map_dirty) {
        vram_addr(MAP_VRAM);
        dma(map, MAP_BYTES, 0x18, 1);
        map_dirty = 0;
    }
    if (pal_dirty) {
        CGADD = 0;
        dma(cgram, sizeof cgram, 0x22, 0);
        pal_dirty = 0;
    }
}

void scr_wait_vblank(void) {
    while (HVBJOY & 0x80) /* out of the current one */
        ;
    while (!(HVBJOY & 0x80))
        ;
}

void scr_off(void) {
    scr_wait_vblank();
    INIDISP = 0x80;
}

void scr_on(void) {
    scr_wait_vblank();
    send();
    INIDISP = 0x0F;
}

static u8 attr(u8 y, u8 x) {
    if (y == ROW_HEADER + 3) {
        return 4 << 2; /* the stripes */
    }
    if (y >= ROW_HEADER && y < ROW_HEADER + 3) {
        return 1 << 2;
    }
    if (y == ROW_STATUS) {
        return (x < COL_STATUS ? 2 : 3) << 2;
    }
    return y == ROW_HELP ? 2 << 2 : 0;
}

static void put(u8 y, const u8 *row) {
    u8 *dst = map + (u16)y * SCR_W * 2;
    u8 x;

    for (x = 0; x < SCR_W; x++) {
        dst[2 * x] = row[x];
        dst[2 * x + 1] = attr(y, x);
    }
    map_dirty = 1;
}

/* Header rows (ROW_HEADER to +3): the double-size title and the stripes under it. */
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
        put(ROW_HEADER + r, row); /* the art's first row is blank */
    }
}

void scr_init(void) {
    u8 y;
    u8 blank[SCR_W];

    INIDISP = 0x80;
    BGMODE = 0;
    BG1SC = MAP_VRAM >> 8; /* (address / $400) << 2, 32x32 */
    BG12NBA = 0;
    TM = 0x01;
    VMAIN = 0x80;
    vram_addr(0);
    dma(font, 256 * 16, 0x18, 1);
    for (y = 0; y < SCR_W; y++) {
        blank[y] = ' ';
    }
    for (y = 0; y < 32; y++) {
        put(y, blank);
    }
    put_header();
    build_cgram();
    send();
    NMITIMEN = 0x01; /* auto-joypad */
}

u8 scr_queue_pal(void) {
    build_cgram();
    return 1;
}

void scr_format(u8 *row, u8 x, const char *text, u8 hl) {
    u8 i;

    for (i = 0; i < x; i++) {
        row[i] = ' ' | hl;
    }
    for (; i <= COL_RIGHT && *text; i++) {
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
    put(y, row);
}

void scr_put_row_both(u8 y, const u8 *row) {
    put(y, row);
}

u8 scr_queue_row(u8 y, const u8 *row) {
    put(y, row);
    return 1;
}

u8 scr_queue_back(u8 y, const u8 *row) {
    put(y, row);
    return 1;
}

u8 scr_queue_free(void) {
    return 3;
}

void scr_flip(void) {
}

void scr_frame(void) {
    scr_wait_vblank();
    send();
}

u8 joy_read(void) {
    u8 hi, joy;

    while (HVBJOY & 0x01) /* auto-read still running */
        ;
    hi = JOY1H; /* B Y Select Start Up Down Left Right */
    joy = hi & 0x3F;
    if (hi & 0x80) {
        joy |= JOY_B;
    }
    if (JOY1L & 0x80) { /* A X L R */
        joy |= JOY_A;
    }
    return joy;
}
