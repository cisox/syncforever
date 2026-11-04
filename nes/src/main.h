/* Shared header. edio/everdrive.c includes "main.h", so it must define the edio types. */
#ifndef MAIN_H
#define MAIN_H

#define u8 unsigned char
#define u16 unsigned short
#define u32 unsigned long

#define vu8 volatile unsigned char
#define vu16 volatile unsigned short
#define vu32 volatile unsigned long

#include "everdrive.h"
#include "errors.h"
#include "screen.h"
#include "link.h"

#endif
