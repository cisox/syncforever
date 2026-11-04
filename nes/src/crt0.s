; Sync Forever startup. Structure follows Krikzz's edio sample crt0.s (MIT).

NES_MAPPER      = 0     ; NROM
NES_PRG_BANKS   = 2     ; 2 x 16 KB
NES_CHR_BANKS   = 1     ; 1 x 8 KB
NES_MIRRORING   = 1     ; vertical

PPU_CTRL        = $2000
PPU_MASK        = $2001
PPU_STATUS      = $2002
APU_FRAMECNT    = $4017
DMC_FREQ        = $4010

.export start, __STARTUP__:absolute=1
.import __RAM_START__, __RAM_SIZE__
.import initlib, zerobss, copydata, _main

.include "zeropage.inc"

.segment "HEADER"
    .byte $4e, $45, $53, $1a
    .byte NES_PRG_BANKS
    .byte NES_CHR_BANKS
    .byte NES_MIRRORING | ((NES_MAPPER & 15) << 4)
    .byte NES_MAPPER & $f0
    .res 8, 0

.segment "STARTUP"
start:
    sei
    cld
    ldx #$ff
    txs
    inx                 ; x = 0
    stx PPU_CTRL        ; no NMI
    stx PPU_MASK        ; rendering off
    stx DMC_FREQ        ; no DMC IRQ
    lda #$40
    sta APU_FRAMECNT    ; no frame IRQ

    bit PPU_STATUS      ; let the PPU warm up: two vblanks
@vb1:
    bit PPU_STATUS
    bpl @vb1

    lda #0
    tay
@clram:
    sta $0000, y
    sta $0100, y
    sta $0200, y
    sta $0300, y
    sta $0400, y
    sta $0500, y
    sta $0600, y
    sta $0700, y
    iny
    bne @clram

@vb2:
    bit PPU_STATUS
    bpl @vb2

    jsr zerobss
    jsr copydata

    lda #<$0800         ; cc65 parameter stack grows down from $0800
    sta sp
    lda #>$0800
    sta sp+1

    jsr initlib
    jmp _main

nmi:
irq:
    rti

.segment "VECTORS"
    .word nmi
    .word start
    .word irq

.segment "CHARS"
    .incbin "font.chr"
