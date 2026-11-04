/* The NES app's own part of the shared app (app/app.c): starting up and leaving, on the EverDrive. */
#include "main.h"
#include "plat.h"

#define EXIT_DELAY 45   /* frames the "Returning to menu" screen stays up */
#define RESTART_HOLD 90 /* then frames of "Restarting..." (appserver.APP_EXIT_AFTER) */

void exit_to_menu(void); /* fast.s */

static u8 row[SCR_W];

/* CMD_MEM_WR of a MapConfig with map_idx 255 to ADDR_CFG, sent by exit_to_menu() from
 * RAM (see fast.s). Same config as edio's ed_exit_game(). */
#define EXIT_PKT_LEN (4 + 4 + 4 + 1 + sizeof(MapConfig)) /* must match fast.s */
u8 exit_pkt[EXIT_PKT_LEN];

static void build_exit_pkt(void) {
    MapConfig *cfg = (MapConfig *)(exit_pkt + 13);
    u8 i;

    for (i = 0; i < EXIT_PKT_LEN; i++) {
        exit_pkt[i] = 0;
    }
    exit_pkt[0] = '+';
    exit_pkt[1] = '+' ^ 0xFF;
    exit_pkt[2] = 0x1A; /* CMD_MEM_WR */
    exit_pkt[3] = 0x1A ^ 0xFF;
    *(u32 *)(exit_pkt + 4) = ADDR_CFG;
    *(u32 *)(exit_pkt + 8) = sizeof(MapConfig);
    exit_pkt[12] = 0; /* ack: plain write, no DMA handshake */
    cfg->map_idx = 255;
    cfg->map_ctrl = MAP_CTRL_UNLOCK;
}

/* One line in the middle of a blank screen, rendering off while it's written. */
static void centre_screen(const char *text) {
    scr_off();
    scr_init();
    scr_format_center(row, text, 0);
    scr_put_row(14, row);
    scr_on();
}

/* text, then (if any) `then` for RESTART_HOLD frames, then the menu. */
static void exit_screen(const char *text, const char *then) {
    u8 i;

    centre_screen(text);
    for (i = 0; i < EXIT_DELAY; i++) { /* long enough to read: the jump isn't a crash */
        scr_wait_vblank();
    }
    if (then) { /* an update: say it comes back (the box relaunches it from the menu) */
        centre_screen(then);
        for (i = 0; i < RESTART_HOLD; i++) {
            scr_wait_vblank();
        }
    }
    build_exit_pkt();
    exit_to_menu();
}

u8 plat_init(void) {
    /* edio's wait_status only accepts mapper status bytes $Ax; open bus reads $40. */
    if ((REG_MSTAT & 0xF0) != 0xA0) {
        return 0;
    }
    ed_init();
    return 1;
}

void plat_leave(void) {
    exit_screen("Returning to menu...", 0);
}

void plat_restart(const char *text) {
    exit_screen(text, "Restarting...");
}
