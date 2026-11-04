// 'S' 'F' type:u8 len:u16le payload; str = len:u8 + ASCII. Numbers are little-endian here, as
// on the NES (the box packs PLAN's counts with "<HHH").
#include <string.h>
#include "link.h"
#include "ui.h"

char msg_text[UI_TEXT_MAX + 1];
char msg_detail[UI_NAME_MAX + 1];
uint8_t plan_mode;
char plan_name[UI_TEXT_MAX + 1];
char plan_saves[UI_TEXT_MAX + 1];
char plan_fw[UI_TEXT_MAX + 1];
uint8_t plan_fw_state;
uint16_t plan_add, plan_replace, plan_remove;
uint8_t pong_known;
uint8_t plan_number;
uint16_t msg_expect;
uint32_t msg_left;
uint16_t copy_n, copy_total;
uint8_t copy_stage;
uint32_t copy_done, copy_size;
char copy_name[UI_NAME_MAX + 1];

static void send(uint8_t type, const uint8_t *payload, int len) {
    uint8_t f[16];  // every request carries at most 1 byte
    f[0] = 'S';
    f[1] = 'F';
    f[2] = type;
    f[3] = len;
    f[4] = len >> 8;
    for (int i = 0; i < len; i++) f[5 + i] = payload[i];
    link_send(LINK_TYPE_UI, f, 5 + len);
}

void ui_hello(void) {
    uint8_t v = UI_VERSION;
    send(0x01, &v, 1);
}

void ui_sync_plan(uint8_t mode) { send(0x06, &mode, 1); }
void ui_sync_run(uint8_t mode) {
    uint8_t p[2] = {mode, plan_number};  // which plan the user saw
    send(0x07, p, 2);
}
void ui_ping(void) { send(0x0A, 0, 0); }
void ui_stop(void) { send(0x0C, 0, 0); }

// A str at *p into dst (cap chars + NUL); advances *p. 0 if it runs past end.
static int str(const uint8_t **p, const uint8_t *end, char *dst, int cap) {
    if (*p >= end) return 0;
    int n = **p;
    if (*p + 1 + n > end) return 0;
    int k = n < cap ? n : cap;
    for (int i = 0; i < k; i++) {
        uint8_t c = (*p)[1 + i];
        dst[i] = c >= 0x20 && c < 0x7F ? c : '?';
    }
    dst[k] = 0;
    *p += 1 + n;
    return 1;
}

static uint16_t le16(const uint8_t *p) { return p[0] | p[1] << 8; }
static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

uint8_t ui_parse(const uint8_t *data, int len) {
    if (len < 5 || data[0] != 'S' || data[1] != 'F') return MSG_BROKEN;
    uint8_t type = data[2];
    int n = le16(data + 3);
    if (5 + n > len) return MSG_BROKEN;
    const uint8_t *p = data + 5, *end = p + n;
    switch (type) {
    case MSG_WELCOME:
    case MSG_STATUS:
    case MSG_RESTART:
    case MSG_ERROR:
    case MSG_SYNC_DONE:
        if (!str(&p, end, msg_text, UI_TEXT_MAX)) return MSG_BROKEN;
        msg_detail[0] = 0;
        msg_expect = 0;
        msg_left = 0;
        if ((type == MSG_STATUS || type == MSG_ERROR) && p < end && !str(&p, end, msg_detail, UI_NAME_MAX)) return MSG_BROKEN;
        if (type == MSG_STATUS && end - p >= 2) {  // during a sync: the copy's and the sync's seconds
            msg_expect = le16(p);
            p += 2;
            if (end - p >= 4) msg_left = le32(p);
        }
        break;
    case MSG_PLAN:
        if (p >= end) return MSG_BROKEN;
        plan_mode = *p++;
        if (!str(&p, end, plan_name, UI_TEXT_MAX) || end - p < 6) return MSG_BROKEN;
        plan_add = le16(p);
        plan_replace = le16(p + 2);
        plan_remove = le16(p + 4);
        p += 6;
        if (!str(&p, end, plan_saves, UI_TEXT_MAX)) return MSG_BROKEN;
        plan_fw[0] = 0;
        plan_fw_state = FW_NONE;
        if (p < end && str(&p, end, plan_fw, UI_TEXT_MAX) && p < end) plan_fw_state = *p++;
        if (p < end) p++;  // msu: this cart plays no MSU games
        plan_number = 0;
        if (p < end) plan_number = *p++;
        msg_detail[0] = 0;  // the line under the status: a finished sync's summary, say
        if (p < end && !str(&p, end, msg_detail, UI_NAME_MAX)) return MSG_BROKEN;
        break;
    case MSG_COPY:
        if (end - p < 13) return MSG_BROKEN;
        copy_n = le16(p);
        copy_total = le16(p + 2);
        copy_stage = p[4];
        copy_done = le32(p + 5);
        copy_size = le32(p + 9);
        p += 13;
        if (!str(&p, end, copy_name, UI_NAME_MAX)) return MSG_BROKEN;
        break;
    case MSG_PONG:
        pong_known = p < end ? *p : 0;
        break;
    default:
        return MSG_NONE;
    }
    return type;
}
