/* link.c in cc65's 6502 simulator (`make test`): frames in the box -> app ring parse, the
 * app's frames land in its SRAM ring, both wrap. sim65 has flat RAM, so the ring at $F000 is
 * plain memory here; SRAM is an array. Exit code 0 = pass. */
#include <stdio.h>
#include "main.h"
#include "link_io.h"

u8 sram_val;
static u8 sram[512];
void __fastcall__ sram_store(u16 off) { sram[off] = sram_val; }
u8 __fastcall__ sram_load(u16 off) { return sram[off]; }

#define RING ((volatile u8 *)0xF000)
static u8 head;
static int fails;

static void box_send(const u8 *f, u8 n) {
    u8 i;
    for (i = 0; i < n; i++) { /* not RING[head++]: cc65 increments first (see link.c) */
        RING[head] = f[i];
        ++head;
    }
    RING[0x100] = head;
}

static void check(int ok, const char *what) {
    if (!ok) {
        printf("FAIL: %s\n", what);
        fails++;
    }
}

int main(void) {
    static const u8 welcome[] = {'S', 'F', 0x81, 10, 0, 9, 'C', 'o', 'n', 'n', 'e', 'c', 't', 'e', 'd'};
    static const u8 pong[] = {'S', 'F', 0x8A, 1, 0, 1};
    u8 round, m;

    RING[0x100] = RING[0x101] = 0;
    link_init();
    check(sram[0] == 'S' && sram[3] == '1', "SRAM magic");
    for (round = 0; round < 30; round++) { /* 30 x 21 bytes: the ring wraps twice */
        box_send(welcome, sizeof welcome);
        box_send(pong, sizeof pong);
        m = link_poll();
        check(m == 0x81, "WELCOME parsed");
        check(msg_text[0] == 'C' && msg_text[8] == 'd' && !msg_text[9], "WELCOME text");
        m = link_poll();
        check(m == 0x8A && pong_known == 1, "PONG parsed");
        check(link_poll() == 0, "then nothing");
        check(sram[4] == head, "read position published");
        link_ping();
        RING[0x101] = sram[5]; /* the box read it */
    }
    check(sram[8 + (u8)(sram[5] - 5)] == 'S' && sram[8 + (u8)(sram[5] - 3)] == 0x0A, "PING in SRAM ring");
    { /* STATUS: text, detail, and a long copy's expected seconds (u16), or not */
        static const u8 timed[] = {'S', 'F', 0x83, 6, 0, 1, 'a', 1, 'b', 0xB9, 0x00};
        static const u8 plain[] = {'S', 'F', 0x83, 4, 0, 1, 'a', 1, 'b'};
        static const u8 left[] = {'S', 'F', 0x83, 10, 0, 1, 'a', 1, 'b', 0xB9, 0x00, 0x70, 0x11, 0x01, 0x00};
        box_send(timed, sizeof timed);
        m = link_poll();
        check(m == 0x83 && msg_detail[0] == 'b' && msg_expect == 185, "STATUS with an expected time");
        box_send(plain, sizeof plain);
        m = link_poll();
        check(m == 0x83 && msg_detail[0] == 'b' && msg_expect == 0, "STATUS without one");
        box_send(left, sizeof left);
        m = link_poll();
        check(m == 0x83 && msg_expect == 185 && msg_left == 70000UL, "STATUS with the time left");
    }
    if (!fails) {
        printf("link_test: ok\n");
    }
    return fails != 0;
}
