/* The app side of docs/app-protocol.md on the FXPAK: the link in app/link.c over two rings in cart memory, which the box reads and writes over USB
 * (docs/snes-plan.md, host fxapp.py):
 *   box -> app: 256 bytes in this ROM at $F000 (the box writes the cart's copy of the ROM);
 *               the box's head at $F100, written after the bytes; its read position in the
 *               app -> box ring at $F101.
 *   app -> box: SRAM ($70:0000): "SFE1", our read position in the box -> app ring (+4),
 *               our head (+5), a start counter (+6), then the 256-byte ring from +8.
 * Positions are single bytes, so each is written in one go. */
#include "main.h"
#include "link_io.h"

#define IN_RING ((const vu8 *)0xF000)
#define IN_ACK (*(const vu8 *)0xF101) /* how far the box has read our ring */
#define S_TAIL 4
#define S_HEAD 5
#define S_EPOCH 6
#define S_RING 8

u8 in_tail;         /* next byte of the box's ring to read */
static u8 out_head; /* next byte of our ring to write */

/* Not `IN_RING[in_tail++]`: cc65 (2.18, -O) increments in_tail before it indexes, so every
   read took the byte after (the app ate each WELCOME without seeing its 'S'). */
u8 link_get(void) {
    u8 c = IN_RING[in_tail];
    ++in_tail;
    return c;
}

static void sram_put(u16 off, u8 v) {
    sram_val = v;
    sram_store(off);
}

void link_consumed(void) {
    sram_put(S_TAIL, in_tail);
}

void link_init(void) {
    u8 epoch = sram_load(S_EPOCH);

    in_tail = IN_HEAD; /* anything the box sent before we started is stale */
    out_head = IN_ACK; /* our ring starts where the box has read up to */
    link_consumed();
    sram_put(S_HEAD, out_head);
    sram_put(S_EPOCH, epoch + 1); /* the box sees we (re)started */
    sram_put(0, 'S');
    sram_put(1, 'F');
    sram_put(2, 'E');
    sram_put(3, '1');
}

void link_put(const u8 *data, u8 n) {
    u8 i;
    u16 spin = 0;

    while ((u8)(out_head - IN_ACK) > (u8)(255 - n)) { /* full: the box hasn't read enough */
        if (++spin == 0) {
            return; /* no box: drop it, as the NES app's USB write would */
        }
    }
    for (i = 0; i < n; i++) {
        sram_put(S_RING + (u8)(out_head + i), data[i]);
    }
    out_head += n;
    sram_put(S_HEAD, out_head);
}
