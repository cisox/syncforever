/* How the NES app's bytes travel (app/link.c): the EverDrive's FIFO to the app, its USB write back. */
#define FIFO_DATA (*(vu8 *)0x40F0)
#define FIFO_STAT (*(vu8 *)0x40F1) /* bit 7 set: nothing for the CPU to read */

#define link_empty() (FIFO_STAT & 0x80)
#define link_get() FIFO_DATA
#define link_put(data, n) ed_cmd_usb_wr(data, n)
#define link_consumed() /* the FIFO needs no telling */
