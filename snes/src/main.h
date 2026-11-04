/* Shared header of the Sync Forever SNES app. */
#ifndef MAIN_H
#define MAIN_H

#define u8 unsigned char
#define u16 unsigned short
#define u32 unsigned long

#define vu8 volatile unsigned char
#define vu16 volatile unsigned short

/* sram.s: SRAM ($70:0000) for code that can't reach other banks */
extern u8 sram_val;
#pragma zpsym("sram_val")
void __fastcall__ sram_store(u16 offset); /* writes sram_val */
u8 __fastcall__ sram_load(u16 offset);

#include "screen.h"
#include "link.h"

#endif
