/* clock.c in cc65's 6502 simulator (`make test`): the sync screen's lines. */
#include <stdio.h>
#include <string.h>
#include "clock.h"
#include "screen.h"

static int fails;
static u8 row[SCR_W + 1];

static void same(const char *what, const char *want) {
    row[SCR_W] = 0;
    if (strcmp((const char *)row, want)) {
        printf("FAIL: %s\n got  [%s]\n want [%s]\n", what, row, want);
        fails++;
    }
}

static void check_file(u16 secs, u16 expect, const char *want) {
    clock_file(row, secs, expect);
    same("file", want);
}

static void check_left(u32 left, const char *want) {
    clock_left(row, left);
    same("left", want);
}

static void check_wrap(const char *text, const char **want, u8 lines) {
    u8 start = 0, i;
    for (i = 0; i < lines; i++) {
        start = clock_wrap(row, (const u8 *)text, start);
        same(text, want[i]);
    }
    if (text[start]) {
        printf("FAIL: %s: more than %u lines\n", text, lines);
        fails++;
    }
}

int main(void) {
    check_file(30, 148, " This file: 0:30 / 2:28         ");
    check_file(400, 180, " This file: 6:40 (slow)         ");
    check_file(3725, 3600, " This file: 62:05 / 60:00       ");
    check_file(0, 5, " This file: 0:00 / 0:05         ");
    check_left(30UL, " Time left: under a minute      ");
    check_left(60UL, " Time left: about 1 min         ");
    check_left(61UL, " Time left: about 2 min         ");
    check_left(2520UL, " Time left: about 42 min        ");
    check_left(4321UL, " Time left: about 1 h 13 min    ");
    check_left(196000UL, " Time left: about 54 h 27 min   ");
    { static const char *want[] = {" Super Smash T.V. (USA)         ", " (MSU1)-2.pcm (44 MB)           "}; check_wrap("Super Smash T.V. (USA) (MSU1)-2.pcm (44 MB)", want, 2); }
    { static const char *want[] = {" Mario.sfc                      "}; check_wrap("Mario.sfc", want, 1); }
    { static const char *want[] = {" Dragon Quest III (Japan) [T-En ", " by DQ Translations v1.1] [Add  ", " by Rod M.rida v1.0c] [n].sfc   "}; check_wrap("Dragon Quest III (Japan) [T-En by DQ Translations v1.1] [Add by Rod M.rida v1.0c] [n].sfc", want, 3); }
    { static const char *want[] = {" AVeryLongNameWithNoSpacesAtAll ", " ThatGoesOnAndOn.sfc            "}; check_wrap("AVeryLongNameWithNoSpacesAtAllThatGoesOnAndOn.sfc", want, 2); }
    clock_bar(row, 0); same("bar 0", " .........................   0% ");
    clock_bar(row, 9); same("bar 9", " \240\240.......................   9% ");
    clock_bar(row, 48); same("bar 48", " \240\240\240\240\240\240\240\240\240\240\240\240.............  48% ");
    clock_bar(row, 99); same("bar 99", " \240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240\240.  99% ");
    if (clock_percent(150, 148) != 99 || clock_percent(74, 148) != 50 || clock_percent(5, 0) != 0) { printf("FAIL: percent\n"); fails++; }
    if (!fails) {
        printf("clock_test: ok\n");
    }
    return fails != 0;
}
