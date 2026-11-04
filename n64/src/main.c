// Sync Forever for the EverDrive-64 X7 (docs/n64-plan.md). Two jobs over one USB link:
// - the file service: the box reads and writes the SD card through us (files.c), since the X7's
//   menu has no file commands;
// - the same screen as the NES and Genesis apps (docs/app-protocol.md): the sync plan, its mode,
//   the save backup, A to sync, plus a copy panel with a progress bar (COPY).
// File requests are answered at all times, also while we wait for the box's reply to our own
// request: the box plans and syncs through them.
#include <libdragon.h>
#include <stdio.h>
#include <string.h>
#include "files.h"
#include "link.h"
#include "ui.h"

#define BUF (LINK_MAX_DATA + 64)
static uint8_t req[BUF] __attribute__((aligned(16)));
static uint8_t rep[BUF] __attribute__((aligned(16)));

// Timings (ms), as the other apps' frame counts
#define HELLO_EVERY 2000
#define WORK_TIMEOUT 30000  // a plan or a sync without any word from the box
#define PING_EVERY 1000
#define LOST_AFTER 3000
#define CONNECT_LOST 5000

// Layout: 40 x 30 characters of 8 x 8 pixels. TVs crop the edges (overscan: the owner's cut off
// rows 28-29 and the side columns), so everything stays in columns 3-36 and rows 2-25.
#define CH 8
#define COL_LEFT 3
#define COL_RIGHT 37  // first column past the safe area
#define ROW_TITLE 2   // the title, drawn 1.5x (12 pixels)
#define ROW_RULE 4
#define ROW_STATUS 7
#define COL_STATUS (COL_LEFT + 8)
#define ROW_MODE 10
#define ROW_ADD 12
#define ROW_REPLACE 13
#define ROW_REMOVE 14
#define ROW_WARN 16
#define ROW_SAVES 19
#define ROW_FIRMWARE 20
#define ROW_HELP 23
#define ROW_FOOT 25

enum { ST_NOCARD, ST_CONNECTING, ST_WAIT, ST_READY, ST_REFUSED };
enum { PH_CHECKING, PH_PREVIEW, PH_RUNNING, PH_DONE };

static int state, phase;
static uint64_t timer_at, heard_at, ping_at;
static uint32_t status_col;
static char status[UI_TEXT_MAX + 1];
static char detail[UI_NAME_MAX + 1];  // the line under the status (a file name)
static int copying;  // a COPY arrived during this sync: show the panel
static int refreshed;  // a STATUS came unasked: the box is planning again (the library changed)
// The whole sync's time: from each STATUS, counted here (the box sends COPY only for the file).
static uint64_t sync_from, status_at, drawn_s;
static uint32_t left_at;  // everything left's seconds, as of the last STATUS
static int timed;         // a STATUS of this sync carried the times
static int plan_view;     // a slow step before the plan (a STATUS with the time left): body hidden
enum { STOP_NONE, STOP_ASKED, STOP_SENT };
static int stop;
static int dirty = 2;  // buffers still to redraw (two, flipping)
#define HINT_LOST "Is the box on and plugged in?"

static uint32_t C_BACK, C_TEXT, C_LABEL, C_RED, C_DARK_RED, C_OK, C_WAIT, C_BAD, C_BAR;

static uint64_t now(void) { return get_ticks_ms(); }
static void changed(void) { dirty = 2; }

static void show_status(const char *text, uint32_t col) {
    strncpy(status, text, UI_TEXT_MAX);
    status[UI_TEXT_MAX] = 0;
    status_col = col;
    detail[0] = 0;  // a STATUS sets its own after this
    changed();
}

// s into out (w chars + NUL), its middle cut out if too long ("Legend of...(USA).z64"):
// the start and the tags at the end both stay.
static void shorten(char *out, const char *s, int w) {
    int n = strlen(s);
    if (n <= w) {
        strcpy(out, s);
        return;
    }
    int head = (w - 3) / 2, tail = w - 3 - head;
    memcpy(out, s, head);
    memcpy(out + head, "...", 3);
    memcpy(out + head + 3, s + n - tail, tail + 1);
}

static int work_to_do(void) { return plan_add || plan_replace || plan_remove; }

static int syncing(void) { return phase == PH_RUNNING && state == ST_WAIT; }

static const char *help(void) {
    if (syncing()) return stop == STOP_NONE ? "B:Stop after this file" : "Stopping after this file...";
    if (state == ST_READY) {
        if (phase == PH_DONE) return "A:Check again";
        return work_to_do() ? "A:Sync  Left/Right:Mode" : "Left/Right:Mode";
    }
    return "";
}

static void enter(int st) {
    state = st;
    timer_at = now();
    changed();
}

static void reconnect(const char *text, uint32_t col) {
    show_status(text, col);
    phase = PH_CHECKING;
    copying = 0;
    plan_view = 0;
    enter(ST_CONNECTING);
    timer_at = 0;  // hello at once
}

static void lost(void) {
    reconnect("Disconnected", C_BAD);
    strcpy(detail, HINT_LOST);
}

static void plan(uint8_t mode) {
    ui_sync_plan(mode);
    enter(ST_WAIT);
}

// -- drawing -------------------------------------------------------------------------------

static void text_at(surface_t *d, int col, int row, uint32_t fg, const char *s) {
    graphics_set_color(fg, C_BACK);
    graphics_draw_text(d, col * CH, row * CH, s);
}

// Text at 1.5x the font's size: each glyph drawn into a scratch surface, its pixel n becoming
// the box from 3n/2 to 3(n+1)/2 (1 or 2 pixels, nearest neighbour).
#define BIG(n) ((n) * 3 / 2)
static void big_text(surface_t *d, int x, int y, uint32_t fg, const char *s) {
    static surface_t glyph;
    if (!glyph.buffer) glyph = surface_alloc(FMT_RGBA16, CH, CH);
    for (; *s; s++, x += BIG(CH)) {
        graphics_set_color(0xFFFF, 0);
        graphics_fill_screen(&glyph, 0);
        graphics_draw_character(&glyph, 0, 0, *s);
        const uint16_t *px = glyph.buffer;
        for (int gy = 0; gy < CH; gy++)
            for (int gx = 0; gx < CH; gx++)
                if (px[gy * (glyph.stride / 2) + gx])
                    graphics_draw_box(d, x + BIG(gx), y + BIG(gy), BIG(gx + 1) - BIG(gx), BIG(gy + 1) - BIG(gy), fg);
    }
}

static void draw_title(surface_t *d) {
    // SYNC FOR in white, EVER in red, centred, at 1.5x
    int x = (320 - 12 * BIG(CH)) / 2;
    big_text(d, x, ROW_TITLE * CH, C_TEXT, "SYNC FOR");
    big_text(d, x + 8 * BIG(CH), ROW_TITLE * CH, C_RED, "EVER");
    graphics_draw_box(d, 0, ROW_RULE * CH + 2, 320, 2, C_DARK_RED);
    graphics_draw_box(d, 0, ROW_RULE * CH + 5, 320, 1, C_RED);
}

static void draw_plan(surface_t *d) {
    char line[48];
    snprintf(line, sizeof line, "Mode: <%s>", plan_name);
    text_at(d, COL_LEFT, ROW_MODE, C_TEXT, line);
    snprintf(line, sizeof line, "Add         %u", plan_add);
    text_at(d, COL_LEFT, ROW_ADD, C_TEXT, line);
    snprintf(line, sizeof line, "Replace     %u", plan_replace);
    text_at(d, COL_LEFT, ROW_REPLACE, C_TEXT, line);
    snprintf(line, sizeof line, "Remove      %u", plan_remove);
    text_at(d, COL_LEFT, ROW_REMOVE, C_TEXT, line);
    if (plan_mode == SYNC_STRICT) text_at(d, COL_LEFT, ROW_WARN, C_RED, "Removes ROMs not in library!");
    if (plan_saves[0]) {
        snprintf(line, sizeof line, "Saves: %s", plan_saves);
        text_at(d, COL_LEFT, ROW_SAVES, C_TEXT, line);
    }
    if (plan_fw_state != FW_NONE && plan_fw[0]) {
        text_at(d, COL_LEFT, ROW_FIRMWARE, C_TEXT, "Firmware: ");
        uint32_t col = plan_fw_state == FW_CURRENT ? C_OK : plan_fw_state == FW_WAITING ? C_BAD : C_TEXT;
        text_at(d, COL_LEFT + 10, ROW_FIRMWARE, col, plan_fw);
    }
}

// The copy panel: what's being copied, n of N, MB done, and a bar.
static void draw_copy(surface_t *d) {
    char line[48];
    snprintf(line, sizeof line, "%s %u of %u", copy_stage == COPY_CHECKING ? "Checking" : "Copying", copy_n,
             copy_total);
    text_at(d, COL_LEFT, ROW_MODE, C_LABEL, line);
    char name[COL_RIGHT - COL_LEFT + 1];
    strncpy(name, copy_name, COL_RIGHT - COL_LEFT);
    name[COL_RIGHT - COL_LEFT] = 0;
    text_at(d, COL_LEFT, ROW_MODE + 2, C_TEXT, name);
    unsigned done10 = (unsigned)((uint64_t)copy_done * 10 / (1024 * 1024));
    unsigned size10 = (unsigned)((uint64_t)copy_size * 10 / (1024 * 1024));
    snprintf(line, sizeof line, "%u.%u / %u.%u MB", done10 / 10, done10 % 10, size10 / 10, size10 % 10);
    text_at(d, COL_LEFT, ROW_MODE + 4, C_TEXT, line);
    int x = COL_LEFT * CH, y = (ROW_MODE + 6) * CH, w = (COL_RIGHT - COL_LEFT) * CH, h = 10;
    graphics_draw_box(d, x, y, w, h, C_LABEL);
    graphics_draw_box(d, x + 1, y + 1, w - 2, h - 2, C_BACK);
    int fill = copy_size ? (int)((uint64_t)(w - 4) * copy_done / copy_size) : 0;
    if (fill > 0) graphics_draw_box(d, x + 2, y + 2, fill, h - 4, copy_stage == COPY_CHECKING ? C_OK : C_BAR);
}

// "Time left: about 42 min" and the whole sync's bar (time so far against time so far + left,
// never full until the box says it's done).
static void draw_left(surface_t *d) {
    if (!timed) return;
    uint32_t since = (uint32_t)((now() - status_at) / 1000), left = left_at > since ? left_at - since : 0;
    uint32_t so_far = (uint32_t)((now() - sync_from) / 1000);
    char line[48];
    if (left < 60) {
        snprintf(line, sizeof line, "Time left: under a minute");
    } else {
        uint32_t mins = (left + 59) / 60;
        if (mins >= 60)
            snprintf(line, sizeof line, "Time left: about %lu h %lu min", (unsigned long)(mins / 60), (unsigned long)(mins % 60));
        else
            snprintf(line, sizeof line, "Time left: about %lu min", (unsigned long)mins);
    }
    text_at(d, COL_LEFT, ROW_SAVES, C_TEXT, line);
    uint32_t pct = so_far + left ? (uint32_t)((uint64_t)so_far * 100 / (so_far + left)) : 0;
    if (pct > 99) pct = 99;
    int x = COL_LEFT * CH, y = (ROW_SAVES + 1) * CH + 2, w = (COL_RIGHT - COL_LEFT - 5) * CH, h = 8;
    graphics_draw_box(d, x, y, w, h, C_LABEL);
    graphics_draw_box(d, x + 1, y + 1, w - 2, h - 2, C_BACK);
    int fill = (int)((w - 4) * pct / 100);
    if (fill > 0) graphics_draw_box(d, x + 2, y + 2, fill, h - 4, C_BAR);
    snprintf(line, sizeof line, "%lu%%", (unsigned long)pct);
    text_at(d, COL_RIGHT - (int)strlen(line), ROW_SAVES + 1, C_TEXT, line);
}

static void draw(surface_t *d) {
    graphics_fill_screen(d, C_BACK);
    draw_title(d);
    text_at(d, COL_LEFT, ROW_STATUS, C_LABEL, "Status:");
    char shown[COL_RIGHT - COL_STATUS + 1];  // the box's texts are short; cut at the safe edge
    strncpy(shown, status, COL_RIGHT - COL_STATUS);
    shown[COL_RIGHT - COL_STATUS] = 0;
    text_at(d, COL_STATUS, ROW_STATUS, status_col, shown);
    if (detail[0] && !(phase == PH_RUNNING && copying)) {  // the copy panel shows its own name
        char line[COL_RIGHT - COL_LEFT + 1];
        shorten(line, detail, COL_RIGHT - COL_LEFT);
        text_at(d, COL_LEFT, ROW_STATUS + 1, C_TEXT, line);
    }
    if (phase == PH_RUNNING && copying) {
        draw_copy(d);
        draw_left(d);
    } else if (phase == PH_RUNNING || plan_view) {
        draw_left(d);
    } else if (phase != PH_CHECKING) {
        draw_plan(d);
    }
    text_at(d, COL_LEFT, ROW_HELP, C_LABEL, help());
    text_at(d, COL_LEFT, ROW_FOOT, C_LABEL, "Power off to exit");
    char v[8];
    snprintf(v, sizeof v, "v%d", UI_VERSION);
    text_at(d, COL_RIGHT - 2, ROW_FOOT, C_LABEL, v);
}

// -- the box's messages ----------------------------------------------------------------------

static void on_message(uint8_t msg) {
    heard_at = now();
    switch (msg) {
    case MSG_PONG:
        if (state == ST_READY && !pong_known) reconnect("Connecting...", C_WAIT);
        break;
    case MSG_WELCOME:
        if (state == ST_CONNECTING) {
            show_status(msg_text, C_OK);
            plan(SYNC_DEFAULT);
        }
        break;
    case MSG_PLAN:
        plan_view = 0;
        if (state == ST_WAIT && phase != PH_RUNNING) {
            if (refreshed) {
                show_status("Library changed", C_OK);
                refreshed = 0;
            } else if (phase != PH_DONE || work_to_do()) {  // after a sync: "Sync done" stays
                show_status(work_to_do() ? "Ready to sync" : "Nothing to do", C_OK);
            }
            phase = PH_PREVIEW;
            enter(ST_READY);
        } else if (state == ST_READY) {  // unasked: the box saw the library change
            show_status("Library changed", C_OK);
            phase = PH_PREVIEW;
            changed();
        }
        strcpy(detail, msg_detail);
        break;
    case MSG_SYNC_DONE:  // the counts are the old plan's: plan again, "Sync done" stays up
        phase = PH_DONE;
        copying = 0;
        show_status(msg_text, C_OK);
        plan(plan_mode);
        break;
    case MSG_COPY:
        copying = 1;
        timer_at = now();
        changed();
        break;
    case MSG_RESTART:  // an update (later): the box restarts us
        show_status(msg_text, C_WAIT);
        break;
    case MSG_STATUS:
        show_status(msg_text, C_LABEL);
        strcpy(detail, msg_detail);
        if (!strncmp(msg_text, "Remov", 5)) copying = 0;  // removals: no copy panel
        timer_at = now();
        if (state == ST_READY) {  // unasked: the box saw the library change and is planning
            refreshed = 1;
            state = ST_WAIT;  // its PLAN follows: no pings meanwhile, no "Disconnected"
        }
        if (syncing()) {
            if (stop == STOP_ASKED) {  // now, between files: the box listens right after a STATUS
                ui_stop();
                stop = STOP_SENT;
            }
            if (msg_expect || msg_left) {
                left_at = msg_left;
                status_at = now();
                timed = 1;
            }
        } else if (msg_left) {  // a slow step of a plan: its time left instead of the old plan
            if (!plan_view) sync_from = now();
            plan_view = timed = 1;
            left_at = msg_left;
            status_at = now();
        } else {
            plan_view = 0;
        }
        break;
    case MSG_ERROR:
        show_status(msg_text, C_BAD);
        strcpy(detail, msg_detail);
        copying = 0;
        plan_view = 0;
        if (state == ST_CONNECTING) {
            enter(ST_REFUSED);
        } else if (state == ST_WAIT) {
            if (phase != PH_PREVIEW) phase = PH_DONE;
            enter(ST_READY);
        }
        break;
    case MSG_BROKEN:
        show_status("Garbled reply", C_BAD);
        if (state == ST_WAIT) {
            phase = PH_CHECKING;
            enter(ST_CONNECTING);
        }
        break;
    }
}

static void input(joypad_buttons_t b) {
    if (b.d_left || b.l || b.c_left) {
        plan((plan_mode + SYNC_MODES - 1) % SYNC_MODES);
    } else if (b.d_right || b.r || b.c_right) {
        plan((plan_mode + 1) % SYNC_MODES);
    } else if (b.a) {
        if (phase == PH_DONE) {
            plan(plan_mode);
        } else if (work_to_do()) {
            phase = PH_RUNNING;
            copying = 0;
            stop = STOP_NONE;
            timed = 0;
            sync_from = now();
            show_status("Starting...", C_LABEL);
            ui_sync_run(plan_mode);
            enter(ST_WAIT);
        }
    }
}

// Everything the box sent that's waiting: file requests answered, app messages handled.
static void serve(void) {
    for (int i = 0; i < 16; i++) {
        int type = 0;
        int n = link_recv(req, sizeof req, &type);
        if (n <= 0) return;
        if (type == LINK_TYPE_SFE) {
            int len = files_handle(req, n, rep, LINK_MAX_DATA + 16);
            if (len > 0) link_send(LINK_TYPE_SFE, rep, len);
            heard_at = now();
            timer_at = state == ST_WAIT ? now() : timer_at;  // the box is working for us
        } else if (type == LINK_TYPE_UI) {
            on_message(ui_parse(req, n));
        }
    }
}

static void step(void) {
    uint64_t t = now();
    switch (state) {
    case ST_CONNECTING:
        if (t - timer_at >= HELLO_EVERY) {
            timer_at = t;
            ui_hello();
        }
        if (t - heard_at >= CONNECT_LOST && strcmp(status, "Disconnected") != 0) {
            show_status("Disconnected", C_BAD);
            strcpy(detail, HINT_LOST);
        }
        break;
    case ST_WAIT:
        if (t - timer_at >= WORK_TIMEOUT) lost();
        if ((syncing() || plan_view) && timed && t / 1000 != drawn_s) {  // the time left, once a second
            drawn_s = t / 1000;
            changed();
        }
        break;
    case ST_READY:
        if (t - heard_at >= LOST_AFTER) {
            lost();
        } else if (t - ping_at >= PING_EVERY) {
            ping_at = t;
            ui_ping();
        }
        break;
    }
}

int main(void) {
    display_init(RESOLUTION_320x240, DEPTH_16_BPP, 2, GAMMA_NONE, FILTERS_RESAMPLE);
    joypad_init();
    C_BACK = graphics_make_color(0x38, 0x38, 0x38, 0xFF);  // the Genesis's 0x222 is level 1 of 7, not #222
    C_TEXT = graphics_make_color(0xEE, 0xEE, 0xEE, 0xFF);
    C_LABEL = graphics_make_color(0xAA, 0xAA, 0xAA, 0xFF);
    C_RED = graphics_make_color(0xE0, 0x60, 0x60, 0xFF);
    C_DARK_RED = graphics_make_color(0xA0, 0x20, 0x20, 0xFF);
    C_OK = graphics_make_color(0x20, 0xC0, 0x20, 0xFF);
    C_WAIT = graphics_make_color(0xE0, 0xE0, 0x20, 0xFF);
    C_BAD = graphics_make_color(0xE0, 0x60, 0x60, 0xFF);
    C_BAR = graphics_make_color(0x60, 0x90, 0xE0, 0xFF);

    link_init();
    phase = PH_CHECKING;
    heard_at = now();
    if (!debug_init_sdfs("sd:/", -1)) {
        show_status("No SD card", C_BAD);
        state = ST_NOCARD;
    } else {
        link_send(LINK_TYPE_SFE, rep, files_hello(rep));  // for `sfe --x7`
        show_status("Connecting...", C_WAIT);
        state = ST_CONNECTING;
        timer_at = 0;
    }

    while (1) {
        if (state != ST_NOCARD) {
            serve();
            step();
        }
        if (!dirty && state != ST_READY && !syncing()) continue;  // input: when ready, or B mid-sync
        surface_t *d = display_try_get();
        if (!d) continue;
        joypad_poll();
        if (state == ST_READY) input(joypad_get_buttons_pressed(JOYPAD_PORT_1));
        if (syncing() && stop == STOP_NONE && joypad_get_buttons_pressed(JOYPAD_PORT_1).b) {
            stop = STOP_ASKED;  // sent with the next STATUS, between files
            changed();
        }
        if (dirty) dirty--;
        draw(d);
        display_show(d);
    }
}
