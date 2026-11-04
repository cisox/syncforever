; SRAM ($70:0000, LoROM) for cc65 code, which can't reach other banks: long indirect
; addressing through a 3-byte pointer, which works in emulation mode too.
;   void __fastcall__ sram_store(u16 offset);   writes sram_val
;   u8   __fastcall__ sram_load(u16 offset);

.p816
.smart -

.export _sram_store, _sram_load, _sram_val

SRAM_BANK = $70

.segment "ZEROPAGE"
ptr:      .res 3
_sram_val: .res 1

.segment "CODE"

_sram_store:
    sta ptr
    stx ptr+1
    lda #SRAM_BANK
    sta ptr+2
    lda _sram_val
    ldy #0
    sta [ptr],y
    rts

_sram_load:
    sta ptr
    stx ptr+1
    lda #SRAM_BANK
    sta ptr+2
    ldy #0
    lda [ptr],y
    ldx #0
    rts
