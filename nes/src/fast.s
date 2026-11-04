; Hand-written routines where C is too slow or code must not run from our ROM.

.include "zeropage.inc"
.import start
.import _exit_pkt
.importzp _zp_app

PPU_CTRL        = $2000
PPU_STATUS      = $2002
PPU_SCROLL      = $2005
PPU_ADDR        = $2006
PPU_DATA        = $2007
REG_FIFO_DATA   = $40F0
EXIT_PKT_LEN    = 55            ; must match EXIT_PKT_LEN in main.c

.segment "CODE"

; void __fastcall__ scr_copy32(const u8 *row): 32 bytes to PPU_DATA (~16 cycles/byte).
; The caller sets PPU_ADDR and must be in vblank or have rendering off.
.export _scr_copy32
_scr_copy32:
    sta ptr1
    stx ptr1+1
    ldy #0
@loop:
    lda (ptr1), y
    sta PPU_DATA
    iny
    cpy #32
    bne @loop
    rts

; void __fastcall__ scr_vblank_push(u8 ctrl): wait for vblank, push the queued rows
; (scr_q_*, screen.c), then set PPU_CTRL (nametable select) and scroll 0,0.
; ~15 cycles/byte: 3 rows take ~1500 of the ~2270 vblank cycles.
.import _scr_q_rows, _scr_q_hi, _scr_q_lo, _scr_q_n
.export _scr_vblank_push
_scr_vblank_push:
    sta tmp2                    ; ctrl
    bit PPU_STATUS
@vbl:
    bit PPU_STATUS
    bpl @vbl
    ldy #0                      ; byte offset into scr_q_rows
    sty tmp1                    ; row index
@row:
    ldx tmp1
    cpx _scr_q_n
    beq @done
    lda _scr_q_hi, x
    sta PPU_ADDR
    lda _scr_q_lo, x
    sta PPU_ADDR
    ldx #32
@byte:
    lda _scr_q_rows, y
    sta PPU_DATA
    iny
    dex
    bne @byte
    inc tmp1
    bne @row
@done:
    lda tmp2
    sta PPU_CTRL
    lda #0
    sta PPU_SCROLL
    sta PPU_SCROLL
    sta _scr_q_n
    rts

; void exit_to_menu(void): send exit_pkt (a CMD_MEM_WR of MapConfig with map_idx 255) to
; the MCU, then reset into the menu. Runs from zero page: the moment the config lands the
; FPGA maps the menu's ROM over ours (fpga/base_sv/everdrive.sv: map_rst when map_idx==255),
; so code in our ROM would vanish mid-function. Waits until the reset vector changes; if it
; never does (~0.3 s), jumps anyway, which visibly restarts this app.
.export _exit_to_menu
_exit_to_menu:
    sei
    ldx #(ram_exit_end - ram_exit - 1)
@copy:
    lda ram_exit, x
    sta _zp_app, x
    dex
    bpl @copy
    jmp _zp_app

ram_exit:
    ldx #0
@send:
    lda _exit_pkt, x
    sta REG_FIFO_DATA
    inx
    cpx #EXIT_PKT_LEN
    bne @send
    ldy #0                      ; timeout: 256 x 256 polls
    ldx #0
@wait:
    lda $FFFC
    cmp #<start
    bne @go
    lda $FFFD
    cmp #>start
    bne @go
    dex
    bne @wait
    dey
    bne @wait
@go:
    jmp ($FFFC)
ram_exit_end:

.assert (ram_exit_end - ram_exit) <= 128, error, "ram_exit does not fit in zp_app"
