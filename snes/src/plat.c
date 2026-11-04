/* The SNES app's own part of the shared app (app/app.c), for the FXPAK PRO (docs/snes-plan.md): a
 * ROM can't hand the SNES back to the FXPAK's menu, so B asks the box to (EXIT: it sends the cart
 * MENU_RESET), and an updated app is restarted by the box (BOOT). If the box doesn't, the screen
 * asks for RESET (the console's reset button goes to the menu). */
#include "main.h"
#include "plat.h"
#include "link_io.h"

#define EXIT_WAIT 180 /* frames to wait for the box to reset us before asking for RESET */

static u8 row[SCR_W];

/* One line on the blank screen. */
static void centre_screen(const char *text, u8 y) {
    scr_format_center(row, text, 0);
    scr_put_row(y, row);
}

/* text while the box resets (EXIT) or restarts (RESTART) us; if it doesn't, ask for RESET. */
static void exit_screen(const char *text) {
    u16 i;

    scr_off();
    scr_init();
    centre_screen(text, 13);
    scr_on();
    for (i = 0; i < EXIT_WAIT; i++) {
        scr_wait_vblank();
    }
    centre_screen("Press RESET for the menu", 15);
    scr_frame();
    for (;;) {
        scr_wait_vblank();
    }
}

u8 plat_init(void) {
    link_init();
    return 1;
}

void plat_leave(void) {
    link_exit();
    exit_screen("Returning to menu...");
}

void plat_restart(const char *text) {
    exit_screen(text);
}
