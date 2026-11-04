/* Sync Forever, the NES and SNES apps' shared part: preview and run a sync of the box's ROM
 * library to the cart, then go back to the menu (docs/app-protocol.md). The box backs up every
 * save as the app opens: shown, not asked for. What differs per console is in its src/plat.c and
 * src/plat_config.h; its screen and link transport are in src/ too. */
#include "main.h"
#include "art.h"
#include "clock.h"
#include "plat.h"

#define HELLO_EVERY 120   /* frames between HELLOs while connecting */
#define PING_EVERY 60     /* frames between heartbeats while idle (READY) */
#define LOST_AFTER 180    /* idle frames without a reply before "Disconnected" */
#define CONNECT_LOST 300  /* connecting frames without a reply before "Disconnected" */

enum { ST_NOCART, ST_CONNECTING, ST_WAIT, ST_READY, ST_REFUSED };
enum { SYNC_CHECKING, SYNC_PREVIEW, SYNC_RUNNING, SYNC_DONE };

/* Body rows */
#define ROW_DETAIL (ROW_STATUS + 1) /* under the status: the file being copied */
#define DETAIL_W (SCR_W - 2 * COL_LEFT)
#define ROW_MODE ROW_BODY
#define ROW_ADD (ROW_BODY + 2)
#define ROW_REPLACE (ROW_BODY + 3)
#define ROW_REMOVE (ROW_BODY + 4)
#define ROW_WARN (ROW_BODY + 6)
#define ROW_SAVES (ROW_BODY + 9)
#define ROW_FIRMWARE (ROW_BODY + 10)
/* The sync screen: ROW_ADD-ROW_REMOVE the name, ROW_WARN this file's clock, ROW_BAR its bar,
   ROW_SAVES the time left, ROW_FIRMWARE the whole sync's bar. */
#define ROW_BAR (ROW_BODY + 7)
#define NAME_ROWS 3
#define CLOCK_GRACE 30 /* seconds past twice the estimate before "Disconnected" */

/* Colours (charcoal and red). */
#define COL_BACKDROP 0x2D
#define COL_BAR 0x16
#define COL_STRIPE1 0x26 /* lower stripes and "EVER" (chr.ACCENT) */
#define COL_LABEL 0x10
#define COL_OK 0x29   /* status: green */
#define COL_WAIT 0x38 /* status: yellow */
#define COL_BAD 0x26  /* status: red */
#define COL_WHITE 0x30

static u8 row[SCR_W];
static u8 status_row[SCR_W];
static u8 detail_row[SCR_W];
static u8 status_dirty; /* status_row changed: queue it when there's room */
static u8 detail_dirty; /* detail_row changed: likewise */
static u8 detail_shown;
static u8 state;
static u16 timer; /* frames in the current state */
static u8 phase;  /* SYNC_* */
static u8 refreshed; /* a STATUS came unasked: the box is planning again (the library changed) */
static u16 quiet; /* frames since the box last sent anything */
static u8 ping_timer;
static u8 job;    /* body rows still to rewrite; 0 = idle */
static u8 pal_dirty; /* scr_pal changed: push it when the queue has room */
static u8 fw_col = COL_WHITE; /* the firmware version's colour (body palette colour 2) */

/* The sync screen (phase SYNC_RUNNING): counted by the app from each STATUS. */
static u8 sync_name[LINK_DETAIL_MAX + 1];
static u8 name_at[NAME_ROWS]; /* where each row of the wrapped name starts */
static u16 clock_expect;      /* this copy's seconds (0: a removal) */
static u16 clock_secs;
static u32 left_at;           /* everything left's seconds, as of the last STATUS */
static u32 sync_secs;         /* since the sync started: the whole sync's bar */
static u8 clock_on;           /* counting: a STATUS of the running sync arrived */
static u8 clock_frames;
static u8 pend_rows;          /* sync_list rows still to queue, from the end (3 rows a frame) */
static u8 sync_drawn;         /* the sync screen is up: later STATUSes redraw only what changes */
/* B during a sync: STOP goes when the next STATUS arrives, between files. */
enum { STOP_NONE, STOP_ASKED, STOP_SENT };
static u8 stop;
static u8 fps; /* frames in a second: the sync screen's clock */
#define HINT_LOST ((const u8 *)"Is the box on and plugged in?")
static u8 plan_view; /* a slow step before the plan (a STATUS with the time left): the sync screen */

/* --- colours and the status line ---------------------------------------------------- */

/* The palettes (screen.h); col is the status word's colour. */
static void make_pal(u8 col) {
    scr_pal[0] = scr_pal[4] = scr_pal[8] = scr_pal[12] = COL_BACKDROP;
    scr_pal[1] = scr_pal[9] = scr_pal[13] = COL_BAR;
    scr_pal[2] = fw_col;
    scr_pal[3] = scr_pal[7] = COL_WHITE;
    scr_pal[5] = COL_STRIPE1;
    scr_pal[6] = COL_STRIPE2;
    scr_pal[10] = scr_pal[11] = COL_LABEL;
    scr_pal[14] = scr_pal[15] = col;
    pal_dirty = 1;
}

/* "Status: <text>" with the text in colour col (COL_LABEL for plain progress messages). */
static void set_status(const char *text, u8 col) {
    static const char label[] = "Status:";
    u8 i;

    scr_format(status_row, COL_STATUS, text, 0);
    for (i = 0; label[i]; i++) {
        status_row[COL_LEFT + i] = label[i];
    }
    make_pal(col);
}

static void show_status(const char *text, u8 col) {
    set_status(text, col);
    status_dirty = 1; /* queued by the main loop: the queue may be full now */
}

/* The line under the status: text, or blank. A name too long for it loses its middle
 * ("Dragon Quest...III (Japan).nes"): the start and the tags at the end both stay. */
static void show_detail(const u8 *text) {
    static u8 n, i, x, tail;

    if (!text[0] && !detail_shown) {
        return;
    }
    detail_shown = text[0] != 0;
    scr_format(detail_row, 0, "", 0);
    for (n = 0; text[n]; n++) {
    }
    x = COL_LEFT;
    if (n <= DETAIL_W) {
        for (i = 0; i < n; i++) {
            detail_row[x++] = text[i];
        }
    } else {
        tail = DETAIL_W - 3 - (DETAIL_W - 3) / 2; /* the end gets the odd character */
        for (i = 0; i < (DETAIL_W - 3) / 2; i++) {
            detail_row[x++] = text[i];
        }
        for (i = 0; i < 3; i++) {
            detail_row[x++] = '.';
        }
        for (i = n - tail; i < n; i++) {
            detail_row[x++] = text[i];
        }
    }
    detail_dirty = 1;
}

/* --- the screen --------------------------------------------------------------------- */

/* A character of the firmware version as its colour-2 tile (art.h); others as text. */
static u8 version_tile(u8 c) {
    if (c >= '0' && c <= '9') {
        return ART_VERSION_DIGIT(c - '0');
    }
    if (c == 'v') {
        return ART_VERSION_V;
    }
    return c == '.' ? ART_VERSION_DOT : c;
}

/* "Label     123" into `row`. */
static void format_count(const char *label, u16 n) {
    u8 digits[5], i = 0, x = COL_LEFT + 13;

    scr_format(row, COL_LEFT, label, 0);
    do {
        digits[i++] = '0' + n % 10;
        n /= 10;
    } while (n);
    while (i) {
        row[x++] = digits[--i];
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
        return work_to_do() ? "A:Sync  <>:Mode  B:Exit" : "<>:Mode  B:Exit";
    }
    return state == ST_CONNECTING || state == ST_REFUSED ? "B:Exit" : "";
}

/* Rows that change, in update order. */
#define ROWS 9
static const u8 rows[ROWS] = {ROW_MODE, ROW_ADD, ROW_REPLACE, ROW_REMOVE, ROW_WARN, ROW_BAR, ROW_SAVES, ROW_FIRMWARE, ROW_HELP};
/* What changes on the sync screen: the name for each file (all 7), the clock every second
   (the last 4). Queued as there's room, while messages keep being read. */
#define SYNC_ROWS 7
#define CLOCK_ROWS 4
static const u8 sync_list[SYNC_ROWS] = {ROW_ADD, ROW_REPLACE, ROW_REMOVE, ROW_WARN, ROW_BAR, ROW_SAVES, ROW_FIRMWARE};

/* "Mode: <Mirror STRICT>  MSU off" (MSU only on a cart with MSU games). */
static void format_mode(void) {
    static u8 x, i;
    static const char *msu;

    scr_format(row, COL_LEFT, "Mode: <", 0);
    for (x = COL_LEFT + 7; x < COL_RIGHT && plan_name[x - COL_LEFT - 7]; x++) {
        row[x] = plan_name[x - COL_LEFT - 7];
    }
    row[x] = '>';
    ++x;
    msu = plan_msu == MSU_ON ? "  MSU on" : plan_msu == MSU_OFF ? "  MSU off" : "";
    for (i = 0; msu[i] && x <= COL_RIGHT; i++) {
        row[x] = msu[i];
        ++x;
    }
}

/* A row of the sync screen. */
static void format_sync_row(u8 r) {
    static u32 left;

    left = left_at > clock_secs ? left_at - clock_secs : 0;
    switch (r) {
    case ROW_MODE:
        if (syncing()) {
            format_mode();
        }
        break;
    case ROW_ADD:
    case ROW_REPLACE:
    case ROW_REMOVE:
        clock_wrap(row, sync_name, name_at[r - ROW_ADD]);
        break;
    case ROW_WARN:
        if (clock_on && clock_expect) {
            clock_file(row, clock_secs, clock_expect);
        }
        break;
    case ROW_BAR:
        if (clock_on && clock_expect) {
            clock_bar(row, clock_percent(clock_secs, clock_expect));
        }
        break;
    case ROW_SAVES:
        if (clock_on) {
            clock_left(row, left);
        }
        break;
    case ROW_FIRMWARE:
        if (clock_on) {
            clock_bar(row, clock_percent(sync_secs, sync_secs + left));
        }
        break;
    }
}

/* Screen row r of the body or the help line into `row`. */
static const u8 *format_row(u8 r) {
    u8 x;

    scr_format(row, 0, "", 0);
    if (r == ROW_HELP) {
        scr_format(row, COL_LEFT, help(), 0);
        row[COL_RIGHT - 1] = 'v'; /* the protocol version: which build is on the cart */
        row[COL_RIGHT] = '0' + LINK_VERSION;
    } else if (working()) {
        format_sync_row(r);
    } else if (phase != SYNC_CHECKING) { /* the plan's rows only once it has arrived */
        switch (r) {
        case ROW_MODE:
            format_mode();
            break;
        case ROW_ADD:
            format_count("Add", plan_add);
            break;
        case ROW_REPLACE:
            format_count("Replace", plan_replace);
            break;
        case ROW_REMOVE:
            format_count("Remove", plan_remove);
            break;
        case ROW_WARN:
            if (plan_mode == SYNC_STRICT) {
                scr_format(row, COL_LEFT, "Removes ROMs not in library!", 0);
            }
            break;
        case ROW_SAVES:
            if (plan_saves[0]) {
                scr_format(row, COL_LEFT, "Saves: ", 0);
                for (x = COL_LEFT + 7; x <= COL_RIGHT && plan_saves[x - COL_LEFT - 7]; x++) {
                    row[x] = plan_saves[x - COL_LEFT - 7];
                }
            }
            break;
        case ROW_FIRMWARE: /* the version in colour 2: green or red (fw_col) */
            if (plan_fw_state != FW_NONE) {
                scr_format(row, COL_LEFT, "Firmware: ", 0);
                for (x = COL_LEFT + 10; x <= COL_RIGHT && plan_fw[x - COL_LEFT - 10]; x++) {
                    row[x] = version_tile(plan_fw[x - COL_LEFT - 10]);
                }
            }
            break;
        }
    }
    return row;
}

/* The whole screen (rendering off: only at start). */
static void draw_all(void) {
    u8 k;

    scr_off();
    scr_put_row(ROW_STATUS, status_row);
    for (k = 0; k < ROWS; k++) {
        scr_put_row(rows[k], format_row(rows[k]));
    }
    scr_on();
}

/* Rewrite the changing rows in place over the next frames, no blanking. */
static void redraw(void) {
    job = ROWS;
}

static void step(void) {
    u8 k;

    while (job && scr_queue_free()) {
        k = ROWS - job;
        scr_queue_row(rows[k], format_row(rows[k]));
        job--;
    }
}

/* --- states ------------------------------------------------------------------------ */

static void enter(u8 st) {
    state = st;
    timer = 0;
    redraw(); /* the help follows the state */
}

/* Back to saying hello (at once), the plan cleared: it may be stale. */
static void reconnect(const char *text, u8 col) {
    show_status(text, col);
    plan_view = 0;
    phase = SYNC_CHECKING;
    enter(ST_CONNECTING);
    timer = HELLO_EVERY - 1;
}

static void plan(u8 mode) {
    link_sync_plan(mode);
    enter(ST_WAIT); /* a mode change keeps the old plan up until the new one arrives */
}

static void input(u8 pressed) {
    if (pressed & JOY_LEFT) {
        plan((plan_mode + SYNC_MODES - 1) % SYNC_MODES);
    } else if (pressed & JOY_RIGHT) {
        plan((plan_mode + 1) % SYNC_MODES);
    } else if (pressed & JOY_A) {
        if (phase == SYNC_DONE) {
            plan(plan_mode); /* the last plan has run: look again */
        } else if (work_to_do()) {
            phase = SYNC_RUNNING;
            stop = STOP_NONE;
            sync_drawn = 0;
            sync_secs = 0;
            sync_name[0] = 0;
            name_at[0] = name_at[1] = name_at[2] = 0;
            show_status("Starting...", COL_LABEL);
            link_sync_run(plan_mode);
            enter(ST_WAIT);
        }
    } else if (pressed & JOY_B) {
        plat_leave();
    }
}

static void on_message(u8 msg) {
    quiet = 0;
    if (msg != MSG_PONG) {
        clock_on = 0;
        pend_rows = 0;
        if (msg == MSG_STATUS && (syncing() || msg_left || msg_expect)) {
            if (!syncing() && !plan_view) { /* a slow step of a plan: it gets the same screen */
                plan_view = 1;
                sync_drawn = 0;
                sync_secs = 0;
            }
            if (syncing() && stop == STOP_ASKED) { /* now, between files: the box listens right after a STATUS */
                link_stop();
                stop = STOP_SENT;
            }
            name_at[0] = 0;
            for (clock_frames = 0; msg_detail[clock_frames]; clock_frames++) {
                sync_name[clock_frames] = msg_detail[clock_frames];
            }
            sync_name[clock_frames] = 0;
            for (clock_frames = 1; clock_frames < NAME_ROWS; clock_frames++) {
                name_at[clock_frames] = clock_wrap(row, sync_name, name_at[clock_frames - 1]);
            }
            clock_expect = msg_expect;
            left_at = msg_left;
            clock_secs = 0;
            clock_frames = 0;
            clock_on = 1;
            show_detail((const u8 *)"");
            if (sync_drawn) {
                pend_rows = SYNC_ROWS;
            } else {
                sync_drawn = 1;
                redraw();
            }
        } else {
            if (plan_view) { /* that step is over */
                plan_view = 0;
                redraw();
            }
            show_detail(msg == MSG_STATUS || msg == MSG_ERROR || msg == MSG_PLAN ? (const u8 *)msg_detail : (const u8 *)"");
        }
    }
    switch (msg) {
    case MSG_PONG:
        if (state == ST_READY && !pong_known) { /* the box restarted: it has no plan */
            reconnect("Connecting...", COL_WAIT);
        }
        break;
    case MSG_WELCOME:
        if (state == ST_CONNECTING) {
            show_status((const char *)msg_text, COL_OK);
            plan(SYNC_DEFAULT);
        }
        break;
    case MSG_PLAN:
        fw_col = plan_fw_state == FW_CURRENT ? COL_OK : plan_fw_state == FW_UNKNOWN ? COL_WHITE : COL_BAD;
        scr_pal[2] = fw_col;
        pal_dirty = 1;
        if (state == ST_WAIT && phase != SYNC_RUNNING) {
            if (refreshed) {
                show_status("Library changed", COL_OK);
                refreshed = 0;
            } else if (phase != SYNC_DONE || work_to_do()) { /* after a sync: "Sync done" stays */
                show_status(work_to_do() ? "Ready to sync" : "Nothing to do", COL_OK);
            }
            phase = SYNC_PREVIEW;
            enter(ST_READY);
        } else if (state == ST_READY) { /* unasked: the box saw the library change */
            show_status("Library changed", COL_OK);
            phase = SYNC_PREVIEW;
            redraw();
        }
        break;
    case MSG_SYNC_DONE: /* the counts are the old plan's: plan again, "Sync done" stays up */
        phase = SYNC_DONE;
        show_status((const char *)msg_text, COL_OK);
        plan(plan_mode);
        break;
    case MSG_RESTART:
        plat_restart((const char *)msg_text); /* the box starts the new copy */
        break;
    case MSG_STATUS:
        show_status((const char *)msg_text, COL_LABEL);
        timer = 0; /* still working */
        if (state == ST_READY) { /* unasked: the box saw the library change and is planning */
            refreshed = 1;
            state = ST_WAIT; /* its PLAN follows (and redraws): no pings meanwhile, no "Disconnected" */
            timer = 0;
        }
        break;
    case MSG_ERROR:
        show_status((const char *)msg_text, COL_BAD);
        if (state == ST_CONNECTING) { /* HELLO refused (app replaced, too old): stop asking */
            enter(ST_REFUSED);
        } else if (state == ST_WAIT) {
            if (phase != SYNC_PREVIEW) {
                phase = SYNC_DONE; /* A plans again, B goes back to the menu */
            }
            enter(ST_READY);
        }
        break;
    case MSG_BROKEN:
        show_status("Garbled reply", COL_BAD);
        if (state == ST_WAIT) {
            phase = SYNC_CHECKING;
            enter(ST_CONNECTING);
        }
        break;
    }
}

/* --- main -------------------------------------------------------------------------- */

void main(void) {
    u8 joy, prev = 0, pressed, msg;

    make_pal(COL_WAIT);
    phase = SYNC_CHECKING;
    fps = PLAT_FPS;
    if (plat_init()) {
        set_status("Connecting...", COL_WAIT);
        state = ST_CONNECTING;
        timer = HELLO_EVERY - 1; /* say hello on the first frame */
    } else {
        set_status("No EverDrive found", COL_BAD);
        state = ST_NOCART;
    }
    scr_init();
    pal_dirty = 0;
    draw_all();

    for (;;) {
        scr_frame();
        joy = joy_read();
        pressed = joy & ~prev;
        prev = joy;
        timer++;
        if (quiet != 0xFFFF) {
            quiet++;
        }
        if (pal_dirty && scr_queue_pal()) {
            pal_dirty = 0;
        }
        if (status_dirty && scr_queue_row(ROW_STATUS, status_row)) {
            status_dirty = 0;
        }
        if (detail_dirty && scr_queue_row(ROW_DETAIL, detail_row)) {
            detail_dirty = 0;
        }

        if (state == ST_NOCART) {
            continue;
        }
        if (working() && ++clock_frames >= fps) {
            clock_frames = 0;
            sync_secs++;
            if (clock_on) {
                clock_secs++;
                if (pend_rows < CLOCK_ROWS) {
                    pend_rows = CLOCK_ROWS;
                }
            }
        }
        while (pend_rows && !job && scr_queue_free()) { /* the rest next frame: 3 rows a vblank */
            scr_queue_row(sync_list[SYNC_ROWS - pend_rows], format_row(sync_list[SYNC_ROWS - pend_rows]));
            pend_rows--;
        }
        if (job) { /* finish redrawing before anything else */
            step();
            continue;
        }
        msg = link_poll();
        if (msg != MSG_NONE) {
            on_message(msg);
            step();
            continue;
        }

        switch (state) {
        case ST_CONNECTING:
            if (timer >= HELLO_EVERY) {
                timer = 0;
                link_hello();
            }
            if (quiet == CONNECT_LOST) {
                show_status("Disconnected", COL_BAD);
                show_detail(HINT_LOST);
            }
            if (pressed & JOY_B) {
                plat_leave();
            }
            break;
        case ST_WAIT:
#if SYNC_WAITS_FOREVER
            if (phase != SYNC_RUNNING && timer >= WORK_TIMEOUT) {
#else
            if (clock_on ? clock_secs > 2 * (u32)clock_expect + CLOCK_GRACE : timer >= WORK_TIMEOUT) {
#endif
                clock_on = 0;
                reconnect("Disconnected", COL_BAD);
                show_detail(HINT_LOST);
                break;
            }
            if (syncing() && (pressed & JOY_B) && stop == STOP_NONE) {
                stop = STOP_ASKED; /* sent with the next STATUS, between files */
                scr_queue_row(ROW_HELP, format_row(ROW_HELP));
            }
            break;
        case ST_READY:
            if (quiet >= LOST_AFTER) {
                reconnect("Disconnected", COL_BAD);
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
                plat_leave();
            }
            break;
        }
    }
}
