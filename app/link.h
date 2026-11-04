/* The app side of docs/app-protocol.md. */
#ifndef LINK_H
#define LINK_H

#define LINK_VERSION 7
#define LINK_TEXT_MAX 30
#define LINK_DETAIL_MAX 63 /* a STATUS detail: a file name, shortened to fit when shown */

/* box -> app message types (link_poll's return value), plus: */
#define MSG_NONE 0x00
#define MSG_BROKEN 0xFF /* a frame started but didn't finish in time */
#define MSG_WELCOME 0x81
#define MSG_STATUS 0x83
#define MSG_RESTART 0x84 /* the box replaced this app: show the text, exit; it starts the new one */
#define MSG_ERROR 0x85
#define MSG_PLAN 0x86
#define MSG_SYNC_DONE 0x87
#define MSG_PONG 0x8A /* answers link_ping; pong_known: the box knows this app */

#define SYNC_DEFAULT 0xFF /* link_sync_plan: the box's default mode */
#define SYNC_MODES 4      /* add, update, mirror, strict */
#define SYNC_STRICT 3

#define FW_NONE 0
#define FW_CURRENT 1
#define FW_WAITING 2
#define FW_UNKNOWN 3

#define MSU_NONE 0
#define MSU_OFF 1
#define MSU_ON 2

/* Last text message (WELCOME, STATUS, ERROR, SYNC_DONE), NUL-terminated. */
extern u8 msg_text[LINK_TEXT_MAX + 1];
/* The line under the status from the last STATUS, ERROR or PLAN (a file name, a summary), or "". */
extern u8 msg_detail[LINK_DETAIL_MAX + 1];

/* Last PLAN. */
extern u8 plan_mode;
extern u8 plan_name[LINK_TEXT_MAX + 1];
extern u8 plan_saves[LINK_TEXT_MAX + 1]; /* the box's save backup when the app opened */
extern u8 plan_fw[LINK_TEXT_MAX + 1];    /* the newest firmware ("v26.0925"), or "" */
extern u8 plan_fw_state;                 /* FW_NONE, FW_CURRENT (green), FW_WAITING (red), FW_UNKNOWN (plain) */
extern u8 plan_msu;                      /* MSU_NONE (no MSU games on this cart), MSU_OFF, MSU_ON */
extern u16 plan_add, plan_replace, plan_remove;

/* Last PONG: 0 = the box doesn't know this app (it restarted): say hello again. */
extern u8 pong_known;

void link_hello(void);
void link_sync_plan(u8 mode);
void link_sync_run(u8 mode);
void link_ping(void); /* the heartbeat while idle */
void link_stop(void); /* STOP: end the running sync before its next file (sent as a STATUS arrives) */
void link_exit(void); /* EXIT: the box resets the console to the menu (the SNES: MENU_RESET) */
extern u16 msg_expect; /* STATUS during a sync: seconds this copy should take (0: a removal) */
extern u32 msg_left;   /* and everything left (this one included) */
u8 link_poll(void); /* MSG_NONE, MSG_BROKEN or the type of a message now parsed */

#endif
