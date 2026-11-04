/* Sync Forever for the Mega EverDrive PRO: preview and run a sync of the box's ROM library to
 * the cart, then go back to the menu (docs/app-protocol.md). The Genesis twin of app/app.c:
 * same states, screens and controls, on edio's text console (40x28 cells). */
#include "main.h"
#include "art.h"

#define HELLO_EVERY 120   /* frames between HELLOs while connecting */
#define WORK_TIMEOUT 1800 /* frames to wait for a plan or a sync; STATUS frames restart it */
#define PING_EVERY 60     /* frames between heartbeats while idle (READY) */
#define LOST_AFTER 180    /* idle frames without a reply before "Disconnected" */
#define CONNECT_LOST 300  /* connecting frames without a reply before "Disconnected" */
#define EXIT_DELAY 45     /* frames the "Returning to menu" screen stays up */
#define RESTART_HOLD 90   /* then frames of "Restarting..." (appserver.APP_EXIT_AFTER) */

enum { ST_NOCART, ST_CONNECTING, ST_WAIT, ST_READY, ST_REFUSED };
enum { SYNC_CHECKING, SYNC_PREVIEW, SYNC_RUNNING, SYNC_DONE };

/* Layout (cells) */
#define W G_SCREEN_W
#define COL_LEFT 2
#define ROW_TITLE 2 /* two rows: the title drawn 2x */
#define ROW_LINES 4
#define ROW_STATUS 6
#define ROW_DETAIL 7 /* under the status: the file being copied */
#define DETAIL_W (W - 2 * COL_LEFT)
#define COL_STATUS (COL_LEFT + 8)
#define ROW_MODE 9
#define ROW_ADD 11
#define ROW_REPLACE 12
#define ROW_REMOVE 13
#define ROW_WARN 15
#define ROW_SAVES 18
#define ROW_FIRMWARE 19
/* The sync screen: ROW_ADD-ROW_REMOVE the name, ROW_WARN this file's clock, ROW_BAR its bar,
   ROW_LEFT the time left, ROW_ALL_BAR the whole sync's bar. */
#define ROW_BAR 16
#define ROW_LEFT ROW_SAVES
#define ROW_ALL_BAR ROW_FIRMWARE
#define BAR_W 30
#define ROW_HELP 25

/* VRAM (edio's startup layout): plane A text at 0xC000, plane B at 0xE000, 64x64 cells;
   horizontal scroll table at 0xAC00 (word 1 = plane B); VSRAM word 1 = plane B. */
#define ART_TILE 0x200 /* our art tiles start here (VRAM 0x4000, above the font) */
#define PLANE_W 64
#define VDP_DATA16 (*(volatile u16 *)0xC00000)
#define VDP_CTRL32 (*(volatile u32 *)0xC00004)
#define VRAM_W(a) ((((u32)0x4000 + ((a) & 0x3FFF)) << 16) | ((a) >> 14))

extern u16 art_tiles[], art_tiles_end[];
void vdpVramWrite(u16 *src, u16 dst, u16 len); /* edio sys.c */

/* Palettes: a cell is its character | (palette << 13); ink is colour 15 of the palette. */
#define PAL_TEXT 0x0000
#define PAL_LABEL 0x2000
#define PAL_ACCENT 0x4000
#define PAL_STATUS 0x6000 /* its ink follows the state (status colour) */

/* CRAM colours: 0x0BGR, 3 bits each in bits 1-3. Charcoal and red, like the NES app. */
#define C_BACKDROP 0x0222  /* dark charcoal */
#define C_TEXT 0x0EEE
#define C_LABEL 0x0AAA
#define C_RED 0x066E       /* "EVER", the lower lines, warnings */
#define C_DARK_RED 0x022A  /* the first line under the title */
#define C_OK 0x02C2        /* green */
#define C_WAIT 0x02EE      /* yellow */
#define C_BAD 0x066E       /* red */

static u8 state;
static u16 timer;
static u8 phase;
static u8 refreshed; /* a STATUS came unasked: the box is planning again (the library changed) */
static u16 quiet;     /* frames since the box last sent anything */
static u8 ping_timer;
/* The sync screen (phase SYNC_RUNNING): the file's name over 3 rows, this copy's clock and the
   time left, counted by the app from each STATUS (the box sends nothing while a file copies).
   The clock also stretches the wait for the box: a big Sega CD or MSU-MD file outlasts
   WORK_TIMEOUT. */
static u8 sync_name[LINK_DETAIL_MAX + 1];
static u16 clock_expect; /* this copy's seconds (0: a removal) */
static u16 clock_secs;
static u32 left_at;      /* everything left's seconds, as of the last STATUS */
static u8 clock_on;      /* counting: a STATUS of the running sync arrived */
static u32 sync_secs;    /* since the sync started: the whole sync's bar */
static u8 clock_frames;
/* B during a sync: STOP goes when the next STATUS arrives, between files (as on the SNES). */
enum { STOP_NONE, STOP_ASKED, STOP_SENT };
static u8 stop;
static u8 fps; /* 60, or 50 on a PAL console */
#define CLOCK_GRACE 30 /* seconds past twice the estimate before "Disconnected" */
#define HINT_LOST ((const u8 *)"Is the box on and plugged in?")
static u8 plan_view; /* a slow step before the plan (a STATUS with the time left): the sync screen */

static void set_ink(u16 pal, u16 color) {
    gSetColor((pal >> 13) * 16 + 15, color);
}

/* Row y: text at column x in palette pal, the rest of the row blank. */
static void row(u16 y, u16 x, u16 pal, const char *text) {
    u16 i = 0;

    gSetXY(0, y);
    gSetPal(pal);
    for (; i < x; i++) {
        gAppendString((u8 *)" ");
    }
    for (; i < W && *text; i++) {
        u8 c[2] = {(u8)*text++, 0};
        gAppendString(c);
    }
    for (; i < W; i++) {
        gAppendString((u8 *)" ");
    }
}

static void put(u16 x, u16 y, u16 pal, const char *text) {
    gSetXY(x, y);
    gSetPal(pal);
    gAppendString((u8 *)text);
}

static void show_status(const char *text, u16 color) {
    row(ROW_STATUS, COL_STATUS, PAL_STATUS, text);
    put(COL_LEFT, ROW_STATUS, PAL_LABEL, "Status:");
    set_ink(PAL_STATUS, color);
}

/* The line under the status: text, or blank. A name too long for it loses its middle
 * ("Phantasy Star...(USA, Europe).md"): the start and the tags at the end both stay. */
static void show_detail(const u8 *text) {
    static u8 shown;
    char line[DETAIL_W + 1];
    u16 n = 0, i, x = 0, head = (DETAIL_W - 3) / 2, tail = DETAIL_W - 3 - head;

    if (!text[0] && !shown) {
        return;
    }
    shown = text[0] != 0;
    while (text[n]) {
        n++;
    }
    if (n <= DETAIL_W) {
        for (i = 0; i < n; i++) {
            line[x++] = text[i];
        }
    } else {
        for (i = 0; i < head; i++) {
            line[x++] = text[i];
        }
        for (i = 0; i < 3; i++) {
            line[x++] = '.';
        }
        for (i = n - tail; i < n; i++) {
            line[x++] = text[i];
        }
    }
    line[x] = 0;
    row(ROW_DETAIL, COL_LEFT, PAL_TEXT, line);
}

/* secs as m:ss into out; its length. */
static u16 format_time(u16 secs, char *out) {
    char digits[5];
    u16 mins = secs / 60, rest = secs % 60, n = 0, d = 0;

    do {
        digits[d++] = '0' + mins % 10;
        mins /= 10;
    } while (mins);
    while (d) {
        out[n++] = digits[--d];
    }
    out[n++] = ':';
    out[n++] = '0' + rest / 10;
    out[n++] = '0' + rest % 10;
    out[n] = 0;
    return n;
}

static u16 append(char *line, u16 n, const char *s) {
    while (*s) {
        line[n++] = *s++;
    }
    line[n] = 0;
    return n;
}

static u16 append_num(char *line, u16 n, u16 v) {
    char digits[6];
    u16 d = 0;

    do {
        digits[d++] = '0' + v % 10;
        v /= 10;
    } while (v);
    while (d) {
        line[n++] = digits[--d];
    }
    line[n] = 0;
    return n;
}

/* done of total as a percent, never 100 until it's done (an estimate that's too short holds). */
static u16 percent(u32 done, u32 total) {
    u32 p = total ? done * 100 / total : 0;
    return p > 99 ? 99 : (u16)p;
}

/* A progress bar on row y: BAR_W cells (art.py's), then "48%"; an empty row if !on. */
static void draw_bar(u16 y, u8 on, u16 pct) {
    char num[6];
    u16 i, fill = pct * BAR_W / 100, n;

    row(y, 0, PAL_TEXT, "");
    if (!on) {
        return;
    }
    VDP_CTRL32 = VRAM_W(0xC000 + (y * PLANE_W + COL_LEFT) * 2);
    for (i = 0; i < BAR_W; i++) {
        VDP_DATA16 = (ART_TILE + (i < fill ? ART_BAR_FULL : ART_BAR_EMPTY)) | PAL_TEXT;
    }
    n = append_num(num, 0, pct);
    append(num, n, "%");
    put(COL_LEFT + BAR_W + 5 - n, y, PAL_TEXT, num);
}

/* "This file: 0:30 / 2:28", "(slow)" past twice the estimate, and its bar; "Time left: about
   42 min" and the whole sync's bar. */
static void draw_clock(void) {
    char line[DETAIL_W + 1];
    u16 n = 0, mins;
    u32 left = left_at > clock_secs ? left_at - clock_secs : 0;

    line[0] = 0;
    if (clock_on && clock_expect) {
        n = append(line, 0, "This file: ");
        n += format_time(clock_secs, line + n);
        if (clock_secs / 2 > clock_expect) {
            append(line, n, " (slow)");
        } else {
            n = append(line, n, " / ");
            format_time(clock_expect, line + n);
        }
    }
    row(ROW_WARN, COL_LEFT, PAL_TEXT, line);
    line[0] = 0;
    if (clock_on) {
        n = append(line, 0, "Time left: ");
        if (left < 60) {
            append(line, n, "under a minute");
        } else {
            mins = left >= 0xFFFFUL * 60 ? 0xFFFF : (u16)((left + 59) / 60);
            n = append(line, n, "about ");
            if (mins >= 60) {
                n = append_num(line, n, mins / 60);
                n = append(line, n, " h ");
                mins %= 60;
            }
            n = append_num(line, n, mins);
            append(line, n, " min");
        }
    }
    row(ROW_LEFT, COL_LEFT, PAL_TEXT, line);
    draw_bar(ROW_BAR, clock_on && clock_expect, percent(clock_secs, clock_expect));
    draw_bar(ROW_ALL_BAR, clock_on, percent(sync_secs, sync_secs + left));
}

/* The name over ROW_ADD..ROW_REMOVE, wrapped at the last space that fits. */
static void draw_name(void) {
    char line[DETAIL_W + 1];
    u16 start = 0, y, n, end, i, x;

    for (y = ROW_ADD; y <= ROW_REMOVE; y++) {
        for (n = 0; sync_name[start + n]; n++) {
        }
        if (n <= DETAIL_W) {
            end = start + n;
        } else {
            end = start + DETAIL_W;
            for (i = DETAIL_W; i > 0 && sync_name[start + i] != ' '; i--) {
            }
            if (i) {
                end = start + i;
            }
        }
        for (x = 0, i = start; i < end; i++) {
            line[x++] = sync_name[i];
        }
        line[x] = 0;
        row(y, COL_LEFT, PAL_TEXT, line);
        while (sync_name[end] == ' ') {
            end++;
        }
        start = end;
    }
}

static void count_row(u16 y, const char *label, u16 n) {
    char digits[6];
    u8 i = 0;

    row(y, COL_LEFT, PAL_TEXT, label);
    do {
        digits[i++] = '0' + n % 10;
        n /= 10;
    } while (n);
    digits[i] = 0;
    {
        char out[6];
        u8 k = 0;
        while (i) {
            out[k++] = digits[--i];
        }
        out[k] = 0;
        put(COL_LEFT + 12, y, PAL_TEXT, out);
    }
}

static u8 work_to_do(void) {
    return (plan_add | plan_replace | plan_remove) != 0;
}

static u8 syncing(void) {
    return phase == SYNC_RUNNING && state == ST_WAIT;
}

/* The sync screen is up. */
static u8 working(void) {
    return syncing() || plan_view;
}

static const char *help(void) {
    if (syncing()) {
        return stop == STOP_NONE ? "B:Stop after this file" : "Stopping after this file...";
    }
    if (state == ST_READY) {
        if (phase == SYNC_DONE) {
            return "A:Check again  B:Exit";
        }
        return work_to_do() ? "A:Sync  Left/Right:Mode  B:Exit" : "Left/Right:Mode  B:Exit";
    }
    return state == ST_CONNECTING || state == ST_REFUSED ? "B:Exit" : "";
}

/* "Firmware: v26.0925", the version in colour 14 of the accent palette: green or red. */
static void draw_firmware(void) {
    u16 i, c, t;

    row(ROW_FIRMWARE, COL_LEFT, PAL_TEXT, plan_fw_state != FW_NONE ? "Firmware: " : "");
    if (plan_fw_state == FW_NONE) {
        return;
    }
    gSetColor(2 * 16 + 14, plan_fw_state == FW_CURRENT ? C_OK : C_BAD);
    VDP_CTRL32 = VRAM_W(0xC000 + (ROW_FIRMWARE * PLANE_W + COL_LEFT + 10) * 2);
    for (i = 0; plan_fw[i] && COL_LEFT + 10 + i < W; i++) {
        c = plan_fw[i];
        t = c >= '0' && c <= '9' ? ART_TILE + ART_VERSION + c - '0'
            : c == 'v'           ? ART_TILE + ART_VERSION + 10
            : c == '.'           ? ART_TILE + ART_VERSION + 11
                                 : c;
        VDP_DATA16 = t | PAL_ACCENT;
    }
}

/* "Mode: <Mirror STRICT>  MSU off" (MSU only on a cart with MSU games). */
static void draw_mode(void) {
    char line[64]; /* row() cuts it at the screen's edge */
    u16 n = append(line, 0, "Mode: <");

    n = append(line, n, (const char *)plan_name);
    n = append(line, n, ">");
    if (plan_msu != MSU_NONE) {
        append(line, n, plan_msu == MSU_ON ? "  MSU on" : "  MSU off");
    }
    row(ROW_MODE, COL_LEFT, PAL_TEXT, line);
}

static void draw_body(void) {
    if (phase == SYNC_CHECKING && !plan_view) {
        u16 y;
        for (y = ROW_MODE; y <= ROW_FIRMWARE; y++) {
            row(y, 0, PAL_TEXT, "");
        }
    } else if (working()) {
        if (syncing()) {
            draw_mode();
        } else {
            row(ROW_MODE, 0, PAL_TEXT, "");
        }
        row(ROW_MODE + 1, 0, PAL_TEXT, "");
        draw_name();
        row(ROW_REMOVE + 1, 0, PAL_TEXT, "");
        row(ROW_BAR + 1, 0, PAL_TEXT, "");
        draw_clock();
    } else {
        draw_mode();
        row(ROW_BAR, 0, PAL_TEXT, "");
        row(ROW_BAR + 1, 0, PAL_TEXT, "");
        count_row(ROW_ADD, "Add", plan_add);
        count_row(ROW_REPLACE, "Replace", plan_replace);
        count_row(ROW_REMOVE, "Remove", plan_remove);
        row(ROW_WARN, COL_LEFT, PAL_ACCENT, plan_mode == SYNC_STRICT ? "Removes ROMs not in library!" : "");
        row(ROW_SAVES, COL_LEFT, PAL_TEXT, plan_saves[0] ? "Saves: " : "");
        if (plan_saves[0]) {
            put(COL_LEFT + 7, ROW_SAVES, PAL_TEXT, (const char *)plan_saves);
        }
        draw_firmware();
    }
    row(ROW_HELP, COL_LEFT, PAL_LABEL, help());
    put(W - 4, ROW_HELP, PAL_LABEL, "v");
    {
        char v[2] = {'0' + LINK_VERSION, 0};
        put(W - 3, ROW_HELP, PAL_LABEL, v);
    }
}

static void vram_fill_start(u16 addr) {
    VDP_CTRL32 = VRAM_W(addr);
}

/* The title (art tiles: EVER already red) and the lines under it. Plane B stays empty: a
   background of our own (grid + SF monogram) is planned. */
static void draw_art(void) {
    static const char title[] = ART_TITLE;
    u16 x, k, n = 0, x0 = (W - 2 * (sizeof(title) - 1)) / 2;

    vdpVramWrite(art_tiles, ART_TILE * 32, (u16)((u8 *)art_tiles_end - (u8 *)art_tiles));
    for (k = 0; title[k]; k++) {
        if (title[k] == ' ') {
            continue;
        }
        x = x0 + 2 * k;
        vram_fill_start(0xC000 + (ROW_TITLE * PLANE_W + x) * 2);
        VDP_DATA16 = ART_TILE + ART_TITLE_TILES + n;
        VDP_DATA16 = ART_TILE + ART_TITLE_TILES + n + 1;
        vram_fill_start(0xC000 + ((ROW_TITLE + 1) * PLANE_W + x) * 2);
        VDP_DATA16 = ART_TILE + ART_TITLE_TILES + n + 2;
        VDP_DATA16 = ART_TILE + ART_TITLE_TILES + n + 3;
        n += 4;
    }
    vram_fill_start(0xC000 + ROW_LINES * PLANE_W * 2);
    for (x = 0; x < W; x++) {
        VDP_DATA16 = ART_TILE + ART_LINES;
    }
}

/* One line in the middle of a blank screen. */
static void centre_screen(const char *text) {
    u16 y, n = 0;

    for (y = 0; y < G_SCREEN_H; y++) {
        row(y, 0, PAL_TEXT, "");
    }
    while (text[n]) {
        n++;
    }
    put((W - n) / 2, 14, PAL_TEXT, text);
}

/* text, then (if any) `then` for RESTART_HOLD frames, then the menu. */
static void exit_screen(const char *text, const char *then) {
    u16 i;

    centre_screen(text);
    for (i = 0; i < EXIT_DELAY; i++) {
        gVsync();
    }
    if (then) { /* an update: say it comes back (the box relaunches it from the menu) */
        centre_screen(then);
        for (i = 0; i < RESTART_HOLD; i++) {
            gVsync();
        }
    }
    ed_cmd_reboot(); /* back to the menu */
}

static void enter(u8 st) {
    state = st;
    timer = 0;
    draw_body();
}

/* Back to saying hello (at once), the plan cleared: it may be stale. */
static void reconnect(const char *text, u16 color) {
    show_status(text, color);
    plan_view = 0;
    phase = SYNC_CHECKING;
    enter(ST_CONNECTING);
    timer = HELLO_EVERY - 1;
}

static void plan(u8 mode) {
    link_sync_plan(mode);
    enter(ST_WAIT);
}

static void input(u16 pressed) {
    if (pressed & JOY_L) {
        plan((plan_mode + SYNC_MODES - 1) % SYNC_MODES);
    } else if (pressed & JOY_R) {
        plan((plan_mode + 1) % SYNC_MODES);
    } else if (pressed & JOY_A) {
        if (phase == SYNC_DONE) {
            plan(plan_mode);
        } else if (work_to_do()) {
            phase = SYNC_RUNNING;
            stop = STOP_NONE;
            sync_secs = 0;
            sync_name[0] = 0;
            show_status("Starting...", C_LABEL);
            link_sync_run(plan_mode);
            enter(ST_WAIT);
        }
    } else if (pressed & JOY_B) {
        exit_screen("Returning to menu...", 0);
    }
}

static void on_message(u8 msg) {
    quiet = 0;
    if (msg != MSG_PONG) {
        clock_on = 0;
        if (msg == MSG_STATUS && (syncing() || msg_left || msg_expect)) {
            u16 i = 0;
            if (!syncing() && !plan_view) { /* a slow step of a plan: it gets the same screen */
                plan_view = 1;
                sync_secs = 0;
            }
            if (syncing() && stop == STOP_ASKED) { /* now, between files: the box listens right after a STATUS */
                link_stop();
                stop = STOP_SENT;
            }
            for (; msg_detail[i]; i++) {
                sync_name[i] = msg_detail[i];
            }
            sync_name[i] = 0;
            clock_expect = msg_expect;
            left_at = msg_left;
            clock_secs = 0;
            clock_frames = 0;
            clock_on = 1;
            show_detail((const u8 *)"");
            draw_body();
        } else {
            if (plan_view) { /* that step is over */
                plan_view = 0;
                draw_body();
            }
            show_detail(msg == MSG_STATUS || msg == MSG_ERROR || msg == MSG_PLAN ? msg_detail : (const u8 *)"");
        }
    }
    switch (msg) {
    case MSG_PONG:
        if (state == ST_READY && !pong_known) { /* the box restarted: it has no plan */
            reconnect("Connecting...", C_WAIT);
        }
        break;
    case MSG_WELCOME:
        if (state == ST_CONNECTING) {
            show_status((const char *)msg_text, C_OK);
            plan(SYNC_DEFAULT);
        }
        break;
    case MSG_PLAN:
        if (state == ST_WAIT && phase != SYNC_RUNNING) {
            if (refreshed) {
                show_status("Library changed", C_OK);
                refreshed = 0;
            } else if (phase != SYNC_DONE || work_to_do()) { /* after a sync: "Sync done" stays */
                show_status(work_to_do() ? "Ready to sync" : "Nothing to do", C_OK);
            }
            phase = SYNC_PREVIEW;
            enter(ST_READY);
        } else if (state == ST_READY) { /* unasked: the box saw the library change */
            show_status("Library changed", C_OK);
            phase = SYNC_PREVIEW;
            draw_body();
        }
        break;
    case MSG_SYNC_DONE: /* the counts are the old plan's: plan again, "Sync done" stays up */
        phase = SYNC_DONE;
        show_status((const char *)msg_text, C_OK);
        plan(plan_mode);
        break;
    case MSG_RESTART:
        exit_screen((const char *)msg_text, "Restarting..."); /* the box starts the new copy */
        break;
    case MSG_STATUS:
        show_status((const char *)msg_text, C_LABEL);
        timer = 0;
        if (state == ST_READY) { /* unasked: the box saw the library change and is planning */
            refreshed = 1;
            state = ST_WAIT; /* its PLAN follows (and redraws): no pings meanwhile, no "Disconnected" */
            timer = 0;
        }
        break;
    case MSG_ERROR:
        show_status((const char *)msg_text, C_BAD);
        if (state == ST_CONNECTING) {
            enter(ST_REFUSED);
        } else if (state == ST_WAIT) {
            if (phase != SYNC_PREVIEW) {
                phase = SYNC_DONE;
            }
            enter(ST_READY);
        }
        break;
    case MSG_BROKEN:
        show_status("Garbled reply", C_BAD);
        if (state == ST_WAIT) {
            phase = SYNC_CHECKING;
            enter(ST_CONNECTING);
        }
        break;
    }
}

int main(void) {
    u16 joy, prev = 0, pressed;
    u8 msg, resp;

    sysInit();
    fps = (*(volatile u16 *)0xC00004 & 1) ? 50 : 60; /* VDP status bit 0: a PAL console */
    gSetColor(0, C_BACKDROP);
    set_ink(PAL_TEXT, C_TEXT);
    gSetColor(14, C_RED);       /* palette 0: the title's EVER, the lines */
    gSetColor(13, C_DARK_RED);
    set_ink(PAL_LABEL, C_LABEL);
    set_ink(PAL_ACCENT, C_RED);
    gCleanPlan();

    draw_art();
    phase = SYNC_CHECKING;
    resp = ed_init();
    if (resp) {
        show_status("No EverDrive found", C_BAD);
        state = ST_NOCART;
    } else {
        show_status("Connecting...", C_WAIT);
        state = ST_CONNECTING;
        timer = HELLO_EVERY - 1; /* say hello on the first frame */
    }
    draw_body();

    for (;;) {
        gVsync();
        joy = sysJoyRead();
        pressed = joy & ~prev;
        prev = joy;
        timer++;
        if (quiet != 0xFFFF) {
            quiet++;
        }

        if (state == ST_NOCART) {
            continue;
        }
        if (working() && ++clock_frames >= fps) {
            clock_frames = 0;
            sync_secs++;
            if (clock_on) {
                clock_secs++;
                draw_clock();
            }
        }
        msg = link_poll();
        if (msg != MSG_NONE) {
            on_message(msg);
            continue;
        }

        switch (state) {
        case ST_CONNECTING:
            if (timer >= HELLO_EVERY) {
                timer = 0;
                link_hello();
            }
            if (quiet == CONNECT_LOST) {
                show_status("Disconnected", C_BAD);
                show_detail(HINT_LOST);
            }
            if (pressed & JOY_B) {
                exit_screen("Returning to menu...", 0);
            }
            break;
        case ST_WAIT:
            if (clock_on ? clock_secs > 2 * (u32)clock_expect + CLOCK_GRACE : timer >= WORK_TIMEOUT) {
                clock_on = 0;
                reconnect("Disconnected", C_BAD);
                show_detail(HINT_LOST);
                break;
            }
            if (syncing() && (pressed & JOY_B) && stop == STOP_NONE) {
                stop = STOP_ASKED; /* sent with the next STATUS, between files */
                draw_body();
            }
            break;
        case ST_READY:
            if (quiet >= LOST_AFTER) {
                reconnect("Disconnected", C_BAD);
                show_detail(HINT_LOST);
                break;
            }
            if (++ping_timer >= PING_EVERY) {
                ping_timer = 0;
                link_ping();
            }
            input(pressed);
            break;
        case ST_REFUSED:
            if (pressed & JOY_B) {
                exit_screen("Returning to menu...", 0);
            }
            break;
        }
    }
    return 0;
}
