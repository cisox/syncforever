# SNES channel test ROM. Needs cc65 (ca65/ld65) and python3.
#   make    build/poc.sfc

B := build

$(B)/poc.sfc: $(B)/poc.o poc.cfg checksum.py
	ld65 -C poc.cfg -o $@ $(B)/poc.o
	python3 -I checksum.py $@

$(B)/poc.o: poc.s $(B)/font.chr
	ca65 --cpu 65816 --bin-include-dir $(B) -o $@ poc.s

$(B)/font.chr: ../../nes/edio/font.chr | $(B)
	cp $< $@

$(B):
	mkdir -p $@

clean:
	rm -rf $(B)
