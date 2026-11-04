; Sync Forever (SNES app) startup: LoROM 32 KiB, 2 KiB SRAM.
;
; Resets the PPU and DMA state the menu leaves behind (it left garbage on screen in the
; channel test), clears WRAM, then runs cc65's 6502 code with the 65816 in emulation mode:
; direct page 0, stack in page 1, data bank 0, all of which cc65 code assumes.

.p816
.smart -

.export start, __STARTUP__:absolute=1
.import zerobss, copydata, initlib, _main

.include "zeropage.inc"

.segment "STARTUP"
start:
    sei
    clc
    xce                 ; native, to set what emulation mode can't
    rep #$38            ; 16-bit A/X/Y, binary mode
    .a16
    .i16
    ldx #$01FF
    txs
    lda #$0000
    tcd                 ; direct page 0
    sep #$20
    .a8
    pha
    plb                 ; data bank 0

    lda #$80
    sta $2100           ; force blank
    stz $4200           ; no NMI, IRQ or auto-joypad yet
    stz $420B           ; no DMA
    stz $420C           ; no HDMA (the menu's would keep running)
    stz $420D           ; slow ROM

    ; PPU registers $2101-$2133 to their power-on values
    ldx #$2101
@ppu:
    stz $00,x
    inx
    cpx #$210D
    bne @ppu
@scroll:                ; $210D-$2114: write twice
    stz $00,x
    stz $00,x
    inx
    cpx #$2115
    bne @scroll
    lda #$80
    sta $2115           ; VRAM: increment after the high byte
    stz $2116
    stz $2117
    stz $211A
    ldx #$211B
@mode7:                 ; $211B-$2120: write twice; $211B/$211E = 1.0
    stz $00,x
    stz $00,x
    inx
    cpx #$2121
    bne @mode7
    lda #$01
    stz $211B
    sta $211B
    stz $211E
    sta $211E
    ldx #$2121
@rest:
    stz $00,x
    inx
    cpx #$2134
    bne @rest
    lda #$30
    sta $2130           ; no colour math
    lda #$E0
    sta $2132           ; fixed colour black
    lda #$FF
    sta $4201

    ; WRAM $0000-$1FFF to zero (the stack is empty)
    rep #$20
    .a16
    ldx #$0000
@wram:
    stz $00,x
    inx
    inx
    cpx #$2000
    bne @wram
    sep #$30
    .a8
    .i8

    sec
    xce                 ; emulation mode for cc65's code
    ldx #$FF
    txs

    jsr zerobss
    jsr copydata
    lda #<$2000         ; cc65 parameter stack grows down from $2000
    sta sp
    lda #>$2000
    sta sp+1
    jsr initlib
    jmp _main

nothing:
    rti

.segment "HEADER"
    .byte "SYNC FOREVER         "   ; 21 bytes
    .byte $20                       ; LoROM, slow
    .byte $02                       ; ROM + RAM + battery
    .byte $05                       ; ROM 32 KiB
    .byte $01                       ; SRAM 2 KiB
    .byte $01                       ; North America
    .byte $00                       ; developer
    .byte $00                       ; version
    .word $FFFF, $0000              ; checksum complement, checksum (tools/checksum.py)

.segment "VECTORS"
    ; native: -, -, COP, BRK, ABORT, NMI, -, IRQ
    .word 0, 0, nothing, nothing, nothing, nothing, 0, nothing
    ; emulation: -, -, COP, -, ABORT, NMI, RESET, IRQ/BRK
    .word 0, 0, nothing, 0, nothing, nothing, start, nothing
