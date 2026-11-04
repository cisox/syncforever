/* The sync screen's lines (main.c): the file's name wrapped, this copy's clock, the time left.
 * The box can't speak to the app during an FXPAK upload, so the app counts the seconds itself
 * against the box's estimates (STATUS). Every function fills a whole row (SCR_W tiles), its
 * text from COL_LEFT. */
#ifndef CLOCK_H
#define CLOCK_H

#include "main.h"

/* secs as m:ss into out (no terminator); its length. */
u8 clock_format(u16 secs, u8 *out);

/* "This file: 0:30 / 2:28", or "This file: 7:12 (slow)" once past twice expect. */
void clock_file(u8 *row, u16 secs, u16 expect);

/* "Time left: about 42 min", "about 1 h 12 min", "under a minute". */
void clock_left(u8 *row, u32 left);

/* done of total as a percent, never 100 until it's done (an estimate that's too short holds at
 * 99). */
u8 clock_percent(u32 done, u32 total);

/* A progress bar: BAR_W cells, filled (the highlight's solid cell) or dotted, then "48%". */
#define BAR_W 25
void clock_bar(u8 *row, u8 percent);

/* One line of text wrapped to the row's width, from text[start]: breaks at the last space that
 * fits (mid-word only if a word is longer than the line). Returns where the next line starts. */
u8 clock_wrap(u8 *row, const u8 *text, u8 start);

#endif
