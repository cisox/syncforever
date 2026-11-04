// USB link to the box over the EverDrive-64 X7's FT245R (docs/x7-protocol.md).
#pragma once
#include <stdint.h>

#define LINK_MAX_DATA (128 * 1024)  // largest frame payload either way
#define LINK_TYPE_SFE 0x53          // UNFLoader frame type of the file service
#define LINK_TYPE_UI 0x55           // ... and of the app protocol (ui.h)

// Unlock the cart's registers and put USB in read-idle. Returns the X7's version register.
uint32_t link_init(void);

// One frame from the box into buf (cap bytes, a multiple of 4). Returns its size, 0 if nothing
// is waiting, -1 on a broken frame (it resyncs on the next DMA@), -2 if larger than cap.
int link_recv(uint8_t *buf, int cap, int *type);

// One frame to the box; 0, or the number of blocks that timed out.
int link_send(int type, const void *data, int size);

// Frames skipped while resyncing (a diagnostic for the screen).
extern int link_junk;
