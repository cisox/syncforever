# X7 app protocol (version 1)

How the box talks to our app on an EverDrive-64 X7 (`n64/src`, `host/src/syncforever/x7.py`).
The X7's menu has no file commands, so our app serves the SD card's files; on the PRO carts the
box reads the card itself. This is that file service. The app's screen speaks the same app
protocol as the other carts (`app-protocol.md`), in its own frame type beside it (below).

## Getting the app running

The FT245R appears as a serial port (`0403:6001`, "FT245R USB FIFO"). At the menu, the box sends
UNFLoader's EverDrive commands, 16 bytes each: `cmd` + letter, then big-endian u32 address,
u32 length in 512-byte blocks, u32 argument.

1. `t`: the menu answers 16 bytes starting `cmdr`. Nothing answers while a game runs, and the X7
   has no in-game reset: the owner power-cycles to the menu.
2. `c` (address `0x10000000`, 1 MB + 4 KB) when the ROM is smaller than that.
3. `W` (address `0x10000000`, the ROM's length padded to 512), then the ROM.
4. Wait 0.5 s, then `s`: the ROM starts and sends HELLO.

If our app is already running, the box's HELLO request gets an answer and no boot is needed.
This is `sfe --x7` (`x7.connect`). `sfe serve` never boots the app: it only listens until the app,
started from the menu, says hello (`Box.x7_session`).

## Frames

UNFLoader's: `DMA@`, then a big-endian u32 whose top byte is the type and low 24 bits the size,
the data, then `CMPH`. The file service uses type `0x53`, the app protocol `0x55` (its frames
unchanged, as the data). The app answers file requests at all times, also while it waits for the
box's reply to its own request: the box plans and syncs through them.

- **Box → app:** the data padded with zeros to a multiple of 4 bytes (the app reads in 4-byte
  units); size is the unpadded length. The app finds the next `DMA@` if it gets out of step.
- **App → box:** the whole frame padded with zeros to a multiple of 512 bytes (the app writes
  whole 512-byte blocks: a short block straight after full ones froze the X7, see
  `n64-plan.md`). The box skips anything before the next `DMA@`.

At most 128 KB of data per frame either way.

## Messages

Every number is big-endian. Strings are u16 length + UTF-8. Paths have no leading slash, relative
to the card root.

Request: `op:u8 tag:u8` + arguments. Reply: `op|0x80:u8 tag:u8 status:u8` + results. The tag (1-255)
lets the box ignore stale replies; the app's unprompted HELLO at startup has tag 0. `status` is a
FatFs `FRESULT`, the codes the PRO carts send (`FR_NO_FILE` 4, `FR_NO_PATH` 5, `FR_EXIST` 8, ...);
results follow only when it's 0, except READ and WRITE as noted.

| Op | Request | Reply |
|---|---|---|
| `01` HELLO | | `proto:u8 version:str` |
| `02` STAT | `path:str` | entry |
| `03` LIST | `start:u16 path:str` | `count:u16 more:u8` + count entries, from the start-th entry; `more` = 1 means ask again from start + count |
| `04` OPEN | `mode:u8 path:str` | (FatFs `FA_*` bits: read 1, write 2, create new 4, create always 8; one file at a time, opening closes the last) |
| `05` READ | `n:u32` | up to n bytes (fewer at end of file, none past it) |
| `06` WRITE | the data | `written:u32` (always present; `FR_DENIED` when the card is full) |
| `07` CLOSE | | |
| `08` DELETE | `path:str` | (a file or empty folder) |
| `09` MKDIR | `path:str` | (`FR_EXIST` if it's there) |
| `0A` CPAK | `port:u8` (0-3) | the Controller Pak in that controller: 32 KB (its first bank, an official pak's all); `FR_NO_FILE` if none (or a Rumble/Transfer Pak). Apps before it answer `FR_INVALID_PARAMETER` |
| `0B` RENAME | `src:str, dst:str` | `f_rename` (moves between folders too). Apps before it answer `FR_INVALID_PARAMETER` |
| `0C` SEEK | `position:u32` | `f_lseek`: where the next READ starts. The box uses it to ask again for a READ whose reply was lost (`X7App._read_chunk`); apps before it answer `FR_INVALID_PARAMETER` |

An entry is `attr:u8 size:u32 fdate:u16 ftime:u16 name:str`: FatFs attributes (`0x10` folder),
size capped at 4 GB - 1, FAT date and time. Files the app creates are dated 2020-01-01 00:00 (the
N64 has no clock).

FatFs can't stat the root (`FR_INVALID_NAME`); list it instead.

## Speeds (owner's X7, 2026-11-02)

Upload through `ops.upload` (64 KB WRITE requests, SD writes in between): 443 KiB/s. Readback:
459 KiB/s.
