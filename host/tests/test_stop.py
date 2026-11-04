"""Stopping the box never cuts a write short: a partial F_FWR payload wedges the cart."""

import os
import signal
import threading

import pytest

from syncforever import n8pro
from syncforever.protocol import FA_CREATE_ALWAYS, FA_WRITE


def test_stop_waits_for_a_write_in_another_thread_then_refuses_new_ones():
    started, release = threading.Event(), threading.Event()

    def writer():
        with n8pro._payload():
            started.set()
            release.wait(5)

    t = threading.Thread(target=writer)
    t.start()
    assert started.wait(5)
    assert n8pro.writes_in_flight() == 1
    assert n8pro.stop_writes(timeout=0.05) is False  # still writing
    release.set()
    assert n8pro.stop_writes(timeout=5) is True
    t.join(5)
    with pytest.raises(KeyboardInterrupt), n8pro._payload():
        pass  # nothing is sent once stopping
    n8pro.allow_writes()
    with n8pro._payload():
        assert n8pro.writes_in_flight() == 1


@pytest.mark.parametrize("sig", n8pro.STOP_SIGNALS)
def test_a_stop_signal_waits_until_the_payload_is_sent(sig):
    previous = signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    sent = []
    try:
        with pytest.raises(KeyboardInterrupt), n8pro._defer_sigint():
            os.kill(os.getpid(), sig)
            sent.append("rest of the payload")
        assert sent == ["rest of the payload"]
        assert signal.getsignal(sig) is not signal.SIG_IGN
    finally:
        signal.signal(sig, previous)


def test_write_refuses_to_start_once_stopping(dev, sd):
    dev.open("/new.bin", FA_WRITE | FA_CREATE_ALWAYS)
    n8pro.stop_writes(timeout=1)
    with pytest.raises(KeyboardInterrupt):
        dev.write(b"x" * 100)
    n8pro.allow_writes()
    dev.write(b"x" * 100)
    dev.close()
    assert (sd / "new.bin").read_bytes() == b"x" * 100
