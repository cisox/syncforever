/* The NES app's settings for the shared app (app/app.c). */
#define WORK_TIMEOUT 1800 /* frames to wait for a plan, a sync or a backup; STATUS restarts it */
#define COL_STRIPE2 0x16  /* the first line under the title: dark red */
#define PLAT_FPS 60 /* an NTSC console's; on PAL the clock runs slow, and nothing depends on it but the display */
#define SYNC_WAITS_FOREVER 0 /* a copy gets twice its estimate, then "Disconnected" */
