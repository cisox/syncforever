#include "clock.h"
#include "screen.h"

#define WIDTH (COL_RIGHT - COL_LEFT + 1)

/* Writes are `a[i] = v;` then `++i;`: cc65 -O can increment first in `a[i++]` (CLAUDE.md). */
static u8 x; /* the next column */

static void blank(u8 *row) {
    for (x = 0; x < SCR_W; x++) {
        row[x] = ' ';
    }
    x = COL_LEFT;
}

static void put(u8 *row, const char *s) {
    while (*s && x <= COL_RIGHT) {
        row[x] = *s;
        ++x;
        ++s;
    }
}

static void put_num(u8 *row, u16 n) {
    static u8 digits[5];
    static u8 d;

    d = 0;
    do {
        digits[d] = '0' + n % 10;
        ++d;
        n /= 10;
    } while (n);
    while (d) {
        --d;
        row[x] = digits[d];
        ++x;
    }
}

u8 clock_format(u16 secs, u8 *out) {
    static u8 digits[5];
    static u16 mins;
    static u8 n, d, rest;

    mins = secs / 60;
    rest = secs % 60;
    n = 0;
    d = 0;
    do {
        digits[d] = '0' + mins % 10;
        ++d;
        mins /= 10;
    } while (mins);
    while (d) {
        --d;
        out[n] = digits[d];
        ++n;
    }
    out[n] = ':';
    out[n + 1] = '0' + rest / 10;
    out[n + 2] = '0' + rest % 10;
    return n + 3;
}

void clock_file(u8 *row, u16 secs, u16 expect) {
    blank(row);
    put(row, "This file: ");
    x += clock_format(secs, row + x);
    if (secs / 2 > expect) {
        put(row, " (slow)");
    } else {
        put(row, " / ");
        x += clock_format(expect, row + x);
    }
}

void clock_left(u8 *row, u32 left) {
    static u16 mins;

    blank(row);
    put(row, "Time left: ");
    if (left < 60) {
        put(row, "under a minute");
        return;
    }
    mins = left >= 0xFFFFUL * 60 ? 0xFFFF : (u16)((left + 59) / 60); /* rounded up */
    put(row, "about ");
    if (mins >= 60) {
        put_num(row, mins / 60);
        put(row, " h ");
        mins %= 60;
    }
    put_num(row, mins);
    put(row, " min");
}

u8 clock_wrap(u8 *row, const u8 *text, u8 start) {
    static u8 n, end, i;

    blank(row);
    for (n = 0; text[start + n]; n++) {
    }
    if (n <= WIDTH) {
        end = start + n;
    } else {
        end = start + WIDTH;
        for (i = WIDTH; i > 0 && text[start + i] != ' '; i--) { /* the last space that fits */
        }
        if (i) {
            end = start + i;
        }
    }
    for (i = start; i < end; i++) {
        row[x] = text[i];
        ++x;
    }
    while (text[end] == ' ') {
        ++end;
    }
    return end;
}

u8 clock_percent(u32 done, u32 total) {
    static u32 p;

    if (!total) {
        return 0;
    }
    p = done * 100 / total;
    return p > 99 ? 99 : (u8)p;
}

void clock_bar(u8 *row, u8 percent) {
    static u8 fill, i;

    blank(row);
    fill = (u16)percent * BAR_W / 100;
    for (i = 0; i < BAR_W; i++) {
        row[x] = i < fill ? ' ' | HL : '.';
        ++x;
    }
    x = COL_RIGHT - 3; /* "  9%", " 48%" at the right edge */
    if (percent >= 10) {
        row[x + 1] = '0' + percent / 10;
    }
    row[x + 2] = '0' + percent % 10;
    row[x + 3] = '%';
}
