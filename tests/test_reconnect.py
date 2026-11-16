# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.port_identity and core.reconnect: finding a lost board again.

The port lists are built as pyserial 3.5 builds them on each system
(``serial/tools/list_ports_windows.py``, ``_linux.py``, ``_osx.py``):
the same fields, with values of the forms those modules produce. They
are replaced by lists recorded from real boards as those are tested.
"""

from __future__ import annotations

from serial.tools.list_ports_common import ListPortInfo

from core.port_identity import PortIdentity, identity_of, read_identities
from core.reconnect import Ask, Basis, Found, find_board, record_board, still_the_board

CP2102 = (0x10C4, 0xEA60)
CH340 = (0x1A86, 0x7523)
FT232 = (0x0403, 0x6001)


def _listed(device: str, vid=None, pid=None, serial=None, location=None) -> ListPortInfo:
    info = ListPortInfo(device, skip_link_detection=True)
    info.vid, info.pid, info.serial_number, info.location = vid, pid, serial, location
    return info


def _port(name: str, model=None, serial=None, location=None) -> PortIdentity:
    vid, pid = model if model else (None, None)
    return PortIdentity(name, vid, pid, serial, location)


# ── Reading identities ───────────────────────────────────────────────────────

def test_ports_are_named_as_qt_names_them_on_each_system() -> None:
    listed = [
        _listed("/dev/ttyUSB0", *CP2102, "0001", "1-1.2"),
        _listed("/dev/ttyS0"),
    ]

    identities = read_identities("linux", lambda: listed)

    assert identities == [
        PortIdentity("ttyUSB0", *CP2102, "0001", "1-1.2"),
        PortIdentity("ttyS0"),
    ]
    assert read_identities("win32", lambda: [_listed("COM7", *CH340, None, "1-1.4")])[0].port == "COM7"


def test_an_empty_serial_number_is_no_serial_number() -> None:
    (identity,) = read_identities("win32", lambda: [_listed("COM7", *CH340, "", "1-2")])

    assert identity.serial is None


def test_a_list_that_cannot_be_read_gives_no_ports() -> None:
    def broken():
        raise OSError("SetupAPI failed")

    assert read_identities("win32", broken) == []


def test_a_mac_tty_port_is_matched_to_its_cu_twin() -> None:
    present = [PortIdentity("cu.usbserial-0001", *CP2102, "0001", "20-1")]

    assert identity_of("tty.usbserial-0001", present, "darwin").serial == "0001"
    assert identity_of("tty.usbserial-0001", present, "linux") == PortIdentity("tty.usbserial-0001")


def test_the_interface_is_the_part_of_the_location_after_the_colon() -> None:
    assert _port("ttyUSB1", FT232, None, "1-1.2:1.1").interface == "1.1"
    assert _port("COM9", FT232, None, "1-3:x.0").interface == "x.0"
    assert _port("ttyUSB0", CP2102, None, "1-1.2").interface is None


# ── Rule A: serial number ────────────────────────────────────────────────────

def test_a_board_with_a_serial_number_is_found_in_any_socket_and_port() -> None:
    board = record_board(_port("COM7", CP2102, "0001A2F3", "1-1.2"), [])

    found = find_board(board, [_port("COM9", CP2102, "0001A2F3", "1-3")])

    assert found == Found(_port("COM9", CP2102, "0001A2F3", "1-3"), Basis.SERIAL_NUMBER)
    assert found.describe() == "identified by serial number 0001A2F3"


def test_another_board_of_the_same_model_is_not_the_board() -> None:
    board = record_board(_port("COM7", CP2102, "0001", "1-1.2"), [])

    assert find_board(board, [_port("COM7", CP2102, "0002", "1-1.2")]) is None


def test_a_board_another_tab_holds_is_never_taken() -> None:
    board = record_board(_port("COM7", CP2102, "0001", "1-1.2"), [])

    assert find_board(
        board, [_port("COM9", CP2102, "0001", "1-3")], held_elsewhere=frozenset({"COM9"}),
    ) is None


def test_a_serial_number_shared_at_connect_is_not_trusted() -> None:
    clones = [_port("COM7", CH340, "5", "1-1"), _port("COM8", CH340, "5", "1-2")]
    board = record_board(clones[0], clones)

    assert not board.serial_trusted
    assert find_board(board, [_port("COM9", CH340, "5", "1-2"), _port("COM7", CH340, "5", "1-1")]) \
        == Found(_port("COM7", CH340, "5", "1-1"), Basis.USB_PORT)


def test_a_serial_number_shared_now_falls_to_the_usb_port() -> None:
    board = record_board(_port("COM7", CH340, "5", "1-1"), [])

    found = find_board(board, [_port("COM9", CH340, "5", "1-2"), _port("COM7", CH340, "5", "1-1")])

    assert found == Found(_port("COM7", CH340, "5", "1-1"), Basis.USB_PORT)


# ── Rule B: USB port ─────────────────────────────────────────────────────────

def test_a_board_without_a_serial_number_is_found_in_its_socket() -> None:
    board = record_board(_port("ttyUSB0", CH340, None, "1-1.2"), [])

    found = find_board(board, [_port("ttyUSB1", CH340, None, "1-1.2")])

    assert found == Found(_port("ttyUSB1", CH340, None, "1-1.2"), Basis.USB_PORT)
    assert found.describe() == "identified by USB port 1-1.2"


def test_two_identical_boards_swapped_each_follow_their_socket() -> None:
    first = record_board(_port("ttyUSB0", CH340, None, "1-1.2"), [])
    second = record_board(_port("ttyUSB1", CH340, None, "1-1.3"), [])
    present = [_port("ttyUSB0", CH340, None, "1-1.3"), _port("ttyUSB1", CH340, None, "1-1.2")]

    assert find_board(first, present).identity.location == "1-1.2"
    assert find_board(second, present).identity.location == "1-1.3"


def test_a_board_without_a_serial_number_in_another_socket_is_asked_about() -> None:
    board = record_board(_port("COM7", CH340, None, "1-1.2"), [])

    assert find_board(board, [_port("COM9", CH340, None, "1-3")]) == Ask(_port("COM9", CH340, None, "1-3"))


def test_ports_declined_are_not_asked_about_again() -> None:
    board = record_board(_port("COM7", CH340, None, "1-1.2"), [])
    present = [_port("COM9", CH340, None, "1-3"), _port("COM10", CH340, None, "1-4")]

    assert find_board(board, present, declined=frozenset({"COM9"})) == Ask(present[1])
    assert find_board(board, present, declined=frozenset({"COM9", "COM10"})) is None


def test_a_port_present_when_the_board_was_lost_is_never_asked_about() -> None:
    board = record_board(_port("COM7", CH340, None, "1-1.2"), [])
    beside = _port("COM9", CH340, None, "1-3")
    arrived = _port("COM10", CH340, None, "1-4")

    assert find_board(board, [beside], present_at_loss=frozenset({beside})) is None
    assert find_board(board, [beside, arrived], present_at_loss=frozenset({beside})) == Ask(arrived)


def test_a_serial_number_on_a_board_present_at_the_loss_is_not_trusted() -> None:
    mine = _port("COM7", CP2102, "0001", "1-1")
    clone = _port("COM9", CP2102, "0001", "1-2")
    board = record_board(mine, [mine])

    assert find_board(board, [clone], present_at_loss=frozenset({clone})) is None
    assert find_board(board, [clone, mine], present_at_loss=frozenset({clone})) == Found(mine, Basis.USB_PORT)
    back_elsewhere = _port("COM11", CP2102, "0001", "1-4")
    assert find_board(
        board, [clone, back_elsewhere], present_at_loss=frozenset({clone}),
    ) == Ask(back_elsewhere)


def test_a_board_present_when_lost_is_still_found_in_its_own_socket() -> None:
    mine = _port("COM7", CH340, None, "1-1.2")
    board = record_board(mine, [])

    assert find_board(board, [mine], present_at_loss=frozenset({mine})) == Found(mine, Basis.USB_PORT)


def test_an_ftdi_device_without_location_or_serial_is_asked_about() -> None:
    board = record_board(_port("COM5", FT232), [])

    assert find_board(board, [_port("COM5", FT232)]) == Ask(_port("COM5", FT232))


def test_another_interface_of_the_same_device_is_another_port() -> None:
    board = record_board(_port("ttyUSB0", FT232, "FT1", "1-2:1.0"), [])

    assert find_board(board, [_port("ttyUSB1", FT232, "FT1", "1-2:1.1")]) is None


def test_a_different_model_is_never_the_board() -> None:
    board = record_board(_port("COM7", CH340, None, "1-1.2"), [])

    assert find_board(board, [_port("COM7", CP2102, None, "1-1.2")]) is None


# ── Rule C: port name ────────────────────────────────────────────────────────

def test_a_port_without_usb_identity_is_found_by_its_name_when_allowed() -> None:
    board = record_board(_port("COM1"), [])

    assert find_board(board, [_port("COM1")]) == Found(_port("COM1"), Basis.PORT_NAME)
    assert find_board(board, [_port("COM1")], by_port_name=False) is None
    assert find_board(board, [_port("COM1", CH340, None, "1-2")]) is None


# ── After opening ────────────────────────────────────────────────────────────

def test_a_port_still_shows_the_board_only_while_its_details_are_the_same() -> None:
    found = _port("COM9", CP2102, "0001", "1-3")

    assert still_the_board(found, [found])
    assert not still_the_board(found, [_port("COM9", CP2102, "0002", "1-3")])
    assert not still_the_board(found, [])
