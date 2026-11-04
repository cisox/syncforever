// The app protocol (docs/app-protocol.md, v7, the NES and Genesis apps' too), carried over the
// X7 link in frames of type LINK_TYPE_UI. Plus COPY, the copy panel's progress (X7 only).
#pragma once
#include <stdint.h>

// The app protocol's version, not the build's: it must equal the box's PROTO_VERSION or the box refuses
// the app. A change that keeps the protocol keeps the number.
#define UI_VERSION 7
#define UI_TEXT_MAX 30
#define UI_NAME_MAX 63

// box -> app message types (ui_parse's result)
#define MSG_NONE 0x00
#define MSG_BROKEN 0xFF
#define MSG_WELCOME 0x81
#define MSG_STATUS 0x83
#define MSG_RESTART 0x84
#define MSG_ERROR 0x85
#define MSG_PLAN 0x86
#define MSG_SYNC_DONE 0x87
#define MSG_COPY 0x89
#define MSG_PONG 0x8A

#define SYNC_DEFAULT 0xFF
#define SYNC_MODES 4
#define SYNC_STRICT 3

#define FW_NONE 0
#define FW_CURRENT 1  // Krikzz's newest: green
#define FW_WAITING 2  // a newer OS exists: red
#define FW_UNKNOWN 3  // the newest isn't known: plain

#define COPY_COPYING 0
#define COPY_CHECKING 1

extern char msg_text[UI_TEXT_MAX + 1];  // WELCOME, STATUS, ERROR, SYNC_DONE, RESTART
extern char msg_detail[UI_NAME_MAX + 1];  // a STATUS's second line (a file name), or ""

extern uint8_t plan_mode;
extern char plan_name[UI_TEXT_MAX + 1];
extern char plan_saves[UI_TEXT_MAX + 1];
extern char plan_fw[UI_TEXT_MAX + 1];  // the card's OS version ("v3.11"), or ""
extern uint8_t plan_fw_state;          // FW_*
extern uint16_t plan_add, plan_replace, plan_remove;

extern uint8_t pong_known;
extern uint8_t plan_number;   // PLAN's number, sent back with SYNC_RUN (the box may plan again)
extern uint16_t msg_expect;   // STATUS during a sync: this copy's seconds (0: a removal)
extern uint32_t msg_left;     // and everything left's (this one included)

extern uint16_t copy_n, copy_total;
extern uint8_t copy_stage;
extern uint32_t copy_done, copy_size;
extern char copy_name[UI_NAME_MAX + 1];

void ui_hello(void);
void ui_sync_plan(uint8_t mode);
void ui_sync_run(uint8_t mode);
void ui_ping(void);
void ui_stop(void);  // STOP: end the running sync before its next file (sent as a STATUS arrives)

// One frame from the box (the payload of a LINK_TYPE_UI frame): its message type, or MSG_BROKEN.
uint8_t ui_parse(const uint8_t *data, int len);
