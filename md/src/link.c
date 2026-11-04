/* Port of app/link.c to the Mega EverDrive: the same frames, its FIFO registers. */
#include "main.h"

#define FIFO_DATA (EDIO->FIFODATA)
#define FIFO_EMPTY() (EDIO->FIFOSTAT & FIFO_CPU_RXF) /* set: nothing for the CPU to read */
#define RX_SPIN 400000UL /* raw() wait: ~0.5 s on the 7.6 MHz 68000 */

#define HUNT_BUDGET 64 /* junk bytes skipped per poll before giving the frame back */

u8 msg_text[LINK_TEXT_MAX + 1];
u8 msg_detail[LINK_DETAIL_MAX + 1];
u8 plan_mode;
u8 plan_name[LINK_TEXT_MAX + 1]; /* its own buffer: msg_text changes with every STATUS */
u8 plan_saves[LINK_TEXT_MAX + 1];
u8 plan_fw[LINK_TEXT_MAX + 1];
u8 plan_fw_state;
u16 plan_add, plan_replace, plan_remove;
u8 pong_known;
u16 msg_expect;
u32 msg_left;
u8 plan_msu;
u8 plan_number;

static u8 tx[5 + 2];
static u8 rx_ok;   /* cleared by a timeout or a short payload */
static u16 remain; /* payload bytes left in the current frame */

/* --- send ---------------------------------------------------------------------------- */

static void send(u8 type, u8 len) {
    tx[0] = 'S';
    tx[1] = 'F';
    tx[2] = type;
    tx[3] = len;
    tx[4] = 0;
    ed_cmd_usb_wr(tx, 5 + len);
}

void link_hello(void) {
    tx[5] = LINK_VERSION;
    send(0x01, 1);
}

void link_sync_plan(u8 mode) {
    tx[5] = mode;
    send(0x06, 1);
}

void link_sync_run(u8 mode) {
    tx[5] = mode;
    tx[6] = plan_number; /* which plan the user saw: the box may have planned again */
    send(0x07, 2);
}

void link_ping(void) {
    send(0x0A, 0);
}

void link_stop(void) {
    send(0x0C, 0);
}



/* --- receive ------------------------------------------------------------------------- */

/* One byte, waiting up to ~0.5 s (the box writes a whole frame at once). */
static u8 raw(void) {
    u32 spin = 0;

    if (!rx_ok) {
        return 0;
    }
    while (FIFO_EMPTY()) {
        if (++spin == RX_SPIN) {
            rx_ok = 0;
            return 0;
        }
    }
    return FIFO_DATA;
}

static u8 rx(void) {
    if (!remain) {
        rx_ok = 0;
        return 0;
    }
    remain--;
    return raw();
}

static u16 rx16(void) {
    u8 lo = rx();
    return lo | ((u16)rx() << 8);
}

/* str field into dst (max chars, NUL-terminated); the excess is read and dropped. */
static void rx_str(u8 *dst, u8 max) {
    u8 n = rx(), i, c;

    for (i = 0; i < n; i++) {
        c = rx();
        if (i < max) {
            dst[i] = c;
        }
    }
    if (n > max) {
        n = max;
    }
    dst[n] = 0;
}

u8 link_poll(void) {
    u8 budget = HUNT_BUDGET, type, lo;

    for (;;) { /* find 'S' without waiting */
        if (FIFO_EMPTY()) {
            return MSG_NONE;
        }
        if (FIFO_DATA == 'S') {
            break;
        }
        if (!--budget) {
            return MSG_NONE;
        }
    }
    rx_ok = 1;
    do { /* "SSF": a stray 'S' right before a frame must not swallow its 'S' */
        type = raw();
    } while (type == 'S' && rx_ok);
    if (type != 'F') {
        return rx_ok ? MSG_NONE : MSG_BROKEN; /* stray 'S' */
    }
    type = raw();
    lo = raw();
    remain = lo | ((u16)raw() << 8);
    if (!rx_ok) {
        return MSG_BROKEN;
    }

    switch (type) {
    case MSG_WELCOME:
    case MSG_STATUS:
    case MSG_RESTART:
    case MSG_ERROR:
    case MSG_SYNC_DONE:
        rx_str(msg_text, LINK_TEXT_MAX);
        msg_detail[0] = 0;
        msg_expect = 0;
        msg_left = 0;
        if ((type == MSG_STATUS || type == MSG_ERROR) && remain) { /* older boxes send no detail */
            rx_str(msg_detail, LINK_DETAIL_MAX);
            if (remain >= 2) { /* during a sync: this copy's seconds (0: a removal) */
                msg_expect = rx16();
            }
            if (remain >= 4) { /* and everything left's */
                msg_left = rx16();
                msg_left |= (u32)rx16() << 16;
            }
        }
        break;
    case MSG_PLAN:
        plan_mode = rx();
        rx_str(plan_name, LINK_TEXT_MAX);
        plan_add = rx16();
        plan_replace = rx16();
        plan_remove = rx16();
        rx_str(plan_saves, LINK_TEXT_MAX);
        plan_fw[0] = 0;
        plan_fw_state = FW_NONE;
        if (remain) { /* boxes before the firmware line send no more */
            rx_str(plan_fw, LINK_TEXT_MAX);
            plan_fw_state = rx();
        }
        plan_msu = MSU_NONE;
        if (remain) { /* boxes before the sync screen send no more */
            plan_msu = rx();
        }
        plan_number = 0;
        if (remain) { /* boxes that send it check it against SYNC_RUN */
            plan_number = rx();
        }
        msg_detail[0] = 0;
        if (remain) { /* the line under the status: a finished sync's summary, say */
            rx_str(msg_detail, LINK_DETAIL_MAX);
        }
        break;
    case MSG_PONG:
        pong_known = rx();
        break;
    }
    while (remain && rx_ok) { /* unknown types, and anything newer than we parse */
        rx();
    }
    return rx_ok ? type : MSG_BROKEN;
}
