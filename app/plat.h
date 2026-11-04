/* What each console's src/plat.c gives the shared app (app.c). */
#ifndef PLAT_H
#define PLAT_H

#include "plat_config.h"

u8 plat_init(void);                   /* at start; 0: no cart to talk to */
void plat_leave(void);                /* B: back to the menu. Doesn't return */
void plat_restart(const char *text);  /* RESTART: the box replaced this app. Doesn't return */

#endif
