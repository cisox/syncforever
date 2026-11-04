from syncforever.protocol import Cmd, error_name, frame, string


def test_frame_is_plus_and_command_each_with_complement():
    assert frame(Cmd.F_FOPN) == bytes([0x2B, 0xD4, 0xC9, 0x36])


def test_string_is_u16_little_endian_length_then_ascii():
    assert string("ab") == b"\x02\x00ab"


def test_error_names_cover_fatfs_cart_and_disk_codes():
    assert error_name(0x08) == "FR_EXIST"
    assert error_name(0x82) == "ERR_OUT_OF_PAGE"
    assert error_name(0xD4) == "DISK_ERR_IO_D4"
    assert error_name(0x77) == "UNKNOWN_77"


def test_everdrive_port_detection_accepts_both_usb_ids():
    from types import SimpleNamespace as P

    from syncforever.transport import is_everdrive

    assert is_everdrive(P(vid=0x38DF, pid=0x0017, product=None))
    assert is_everdrive(P(vid=0x0483, pid=0x5740, product="EverDrive N8"))
    assert not is_everdrive(P(vid=0x0483, pid=0x5740, product="Some ST board"))
