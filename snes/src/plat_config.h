/* The SNES app's settings for the shared app (app/app.c). */
#define WORK_TIMEOUT 7200 /* frames (2 min) to wait for a plan or a backup; STATUS restarts it */
#define COL_STRIPE2 0x06  /* the first line under the title: dark red ($16 merged with $26 on the SNES) */
#define PLAT_FPS ((*(volatile u8 *)0x213F & 0x10) ? 50 : 60) /* STAT78 bit 4: a PAL console */
/* Never give up mid-sync: saying hello writes save RAM, and the cart's autosave that follows hangs
   its USB if it meets an upload (one MSU-1 track can take half an hour, and the box can't speak
   during it; it shows the file's size instead). */
#define SYNC_WAITS_FOREVER 1
