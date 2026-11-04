/* How the SNES app's bytes travel (app/link.c): two rings in cart memory, link_io.c. */
#define IN_HEAD (*(const vu8 *)0xF100)

extern u8 in_tail; /* next byte of the box's ring to read */

#define link_empty() (in_tail == IN_HEAD)
u8 link_get(void);
void link_put(const u8 *data, u8 n); /* the box sees all of it or none. Waits (briefly) for room */
void link_consumed(void);            /* tell the box how far we've read */
void link_init(void);                /* once, at start: the rings, and "we (re)started" for the box */
