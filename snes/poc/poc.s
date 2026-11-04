; Sync Forever SNES channel test (docs/snes-plan.md, phase 3). LoROM, 32 KiB, 2 KiB SRAM.
;
; The box writes INBOX (in this ROM: the cart's SDRAM, via a USB PUT to SNES space) and reads
; SRAM (via a USB GET of SNES space 0xE00000). Each frame this ROM:
;   - shows INBOX's first 8 bytes and a frame counter,
;   - writes to SRAM: "SFE1", the frame counter (u16 LE), then a copy of INBOX's first 8 bytes.
; So the box sees the app alive (counter moving) and its own writes echoed back.

.p816
.smart -

INBOX = $F000          ; bank 0: file offset $7000, SNES-space address 0x007000
SRAM  = $700000        ; LoROM SRAM: bank $70; SNES-space address 0xE00000

INIDISP = $2100
BGMODE  = $2105
BG1SC   = $2107
BG12NBA = $210B
VMAIN   = $2115
VMADDL  = $2116
VMDATAL = $2118
VMDATAH = $2119
CGADD   = $2121
CGDATA  = $2122
TM      = $212C
NMITIMEN = $4200
HVBJOY  = $4212

.segment "ZEROPAGE"
frame:  .res 2
src:    .res 2
tmp:    .res 1

.segment "CODE"

reset:
    sei
    clc
    xce                 ; native mode
    rep #$10            ; 16-bit X/Y
    sep #$20            ; 8-bit A
    .a8
    .i16
    ldx #$1FFF
    txs
    lda #$00
    pha
    plb                 ; data bank 0
    ldx #$0000
    phx
    pld                 ; direct page 0

    lda #$80
    sta INIDISP         ; force blank
    stz NMITIMEN
    stz BGMODE          ; mode 0
    lda #$04
    sta BG1SC           ; BG1 tilemap at VRAM word $0400, 32x32
    stz BG12NBA         ; BG1 tiles at VRAM word $0000
    lda #$80
    sta VMAIN           ; increment after the high byte

    ; font: 256 tiles, NES 2bpp (plane 0, then plane 1) -> SNES 2bpp (rows interleaved)
    stz VMADDL
    stz VMADDL+1
    ldx #$0000
@tile:
    ldy #$0008
@row:
    lda font,x
    sta VMDATAL
    lda font+8,x
    sta VMDATAH
    inx
    dey
    bne @row
    rep #$20
    .a16
    txa
    clc
    adc #$0008
    tax
    sep #$20
    .a8
    cpx #$1000
    bne @tile

    ; tilemap: 32x32 spaces
    ldx #$0400
    stx VMADDL
    ldx #$0400
@clear:
    lda #' '
    sta VMDATAL
    stz VMDATAH
    dex
    bne @clear

    ; palette: 0 dark blue, 1-3 white
    stz CGADD
    lda #$00
    sta CGDATA
    lda #$28
    sta CGDATA
    ldy #$0003
@pal:
    lda #$FF
    sta CGDATA
    lda #$7F
    sta CGDATA
    dey
    bne @pal

    ; static text
    ldx #$0400 + 2*32 + 2
    ldy #title
    jsr print
    ldx #$0400 + 5*32 + 2
    ldy #label_in
    jsr print
    ldx #$0400 + 7*32 + 2
    ldy #label_frame
    jsr print

    lda #$01
    sta TM              ; BG1 on
    lda #$0F
    sta INIDISP         ; screen on
    ldx #$0000
    stx frame

main:
@wait_out:
    lda HVBJOY
    bmi @wait_out       ; out of the current vblank
@wait_in:
    lda HVBJOY
    bpl @wait_in        ; into the next one

    ; INBOX bytes 0-7 as hex at row 5, column 9
    ldx #$0400 + 5*32 + 9
    stx VMADDL
    ldx #$0000
@hex:
    lda INBOX,x
    jsr put_hex
    lda #' '
    sta VMDATAL
    stz VMDATAH
    inx
    cpx #$0008
    bne @hex

    ; frame counter at row 7, column 9
    ldx #$0400 + 7*32 + 9
    stx VMADDL
    lda frame+1
    jsr put_hex
    lda frame
    jsr put_hex

    ; SRAM: "SFE1", frame (LE), INBOX[0..7]
    lda #'S'
    sta f:SRAM+0
    lda #'F'
    sta f:SRAM+1
    lda #'E'
    sta f:SRAM+2
    lda #'1'
    sta f:SRAM+3
    lda frame
    sta f:SRAM+4
    lda frame+1
    sta f:SRAM+5
    ldx #$0000
@echo:
    lda INBOX,x
    sta f:SRAM+6,x
    inx
    cpx #$0008
    bne @echo

    ldx frame
    inx
    stx frame
    jmp main

; A (8-bit) as two hex digits at the VRAM address already set
put_hex:
    pha
    lsr
    lsr
    lsr
    lsr
    jsr put_nibble
    pla
    and #$0F
put_nibble:
    cmp #10
    bcc :+
    adc #'A' - 10 - 1   ; carry is set
    bra :++
:   adc #'0'
:   sta VMDATAL
    stz VMDATAH
    rts

; the NUL-terminated string at Y to VRAM word address X (forced blank only)
print:
    stx VMADDL
@next:
    lda 0,y
    beq @done
    sta VMDATAL
    stz VMDATAH
    iny
    bra @next
@done:
    rts

nothing:
    rti

.segment "RODATA"
title:       .byte "SYNC FOREVER SNES TEST", 0
label_in:    .byte "INBOX", 0
label_frame: .byte "FRAME", 0
font:        .incbin "font.chr"

.segment "INBOX"
    .res 256, 0

.segment "HEADER"
    .byte "SYNC FOREVER TEST    "   ; 21 bytes
    .byte $20                       ; LoROM, slow
    .byte $02                       ; ROM + RAM + battery
    .byte $05                       ; ROM 32 KiB
    .byte $01                       ; SRAM 2 KiB
    .byte $01                       ; North America
    .byte $00                       ; developer
    .byte $00                       ; version
    .word $FFFF, $0000              ; checksum complement, checksum (fixed by the build)

.segment "VECTORS"
    ; native: -, -, COP, BRK, ABORT, NMI, -, IRQ
    .word 0, 0, nothing, nothing, nothing, nothing, 0, nothing
    ; emulation: -, -, COP, -, ABORT, NMI, RESET, IRQ/BRK
    .word 0, 0, nothing, 0, nothing, nothing, reset, nothing
