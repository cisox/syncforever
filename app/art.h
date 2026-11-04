/* Art tiles in the CHR slots text never uses. host/src/syncforever/chr.py draws them;
 * these numbers must match its STRIPES, TITLE and TITLE_SLOTS. */
#ifndef ART_H
#define ART_H

#define ART_STRIPES 0xFF /* header row 3: the lines under the title */

/* "SYNC FOREVER" drawn 2x: letter k (spaces skipped) is 4 slots, top-left, top-right,
   bottom-left, bottom-right, numbered from ART_TITLE_SLOT(4k). */
#define ART_TITLE "SYNC FOREVER"
#define ART_TITLE_X 4 /* 12 characters x 2 columns, centred */
#define ART_TITLE_SLOT(n) ((n) < 30 ? 0x02 + (n) : 0x82 + (n) - 30)

/* The firmware version in colour 2 (chr.VERSION_CHARS/VERSION_SLOTS): '0'-'9', 'v', '.' */
#define ART_VERSION_DIGIT(d) (0x94 + (d))
#define ART_VERSION_V 0x9E
#define ART_VERSION_DOT 0x9F

#endif
