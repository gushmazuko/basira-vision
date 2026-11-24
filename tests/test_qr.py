"""The QR code shown by `basira capture`: basira's own small encoder (no dependency)."""

import io

import pytest
from rich.console import Console

from basira_vision.ui.qr import QUIET_ZONE, QRCode, encode

# "basira" as a version 1 symbol. The encoder was checked module by module against the
# `qrcode` package (every mask, versions 1 to 5) and its symbols were read back with
# OpenCV's detector; this one pins the result.
BASIRA = [
    "#######.##..#.#######",
    "#.....#..#..#.#.....#",
    "#.###.#.#.#.#.#.###.#",
    "#.###.#.#..#..#.###.#",
    "#.###.#.###...#.###.#",
    "#.....#.......#.....#",
    "#######.#.#.#.#######",
    ".........##..........",
    "####..#.#.#..#..###.#",
    "#.##...#....#..#..#.#",
    "####..#..#..#####..##",
    "..####...#..##...#.##",
    "##..#.#.#.##.#..##...",
    "........###..#...#...",
    "#######......##.###..",
    "#.....#..######..##.#",
    "#.###.#..#.#..#..####",
    "#.###.#.#.#..#....##.",
    "#.###.#.#..#.#...##..",
    "#.....#.##.##.#.#...#",
    "#######.#.###...###..",
]

FINDER = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"]


def drawn(matrix):
    return ["".join("#" if dark else "." for dark in row) for row in matrix]


def test_a_known_symbol():
    assert drawn(encode("basira")) == BASIRA


@pytest.mark.parametrize("length, size", [(1, 21), (17, 21), (18, 25), (32, 25), (33, 29), (53, 29), (54, 33),
                                          (78, 33), (79, 37), (106, 37)])
def test_the_symbol_grows_with_the_text_and_keeps_its_fixed_patterns(length, size):
    rows = drawn(encode("x" * length))
    assert len(rows) == size and all(len(row) == size for row in rows)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert [row[left:left + 7] for row in rows[top:top + 7]] == FINDER
    assert rows[6][8:size - 8] == "".join("#." [i % 2] for i in range(size - 16))  # the timing pattern
    assert rows[size - 8][8] == "#"  # the module that is always dark


def test_a_text_that_is_too_long_gets_no_code_and_utf8_counts_in_bytes():
    assert encode("x" * 107) is None
    assert encode("Ӏ" * 53) is not None and encode("Ӏ" * 54) is None  # two bytes each


def test_the_code_is_drawn_two_modules_per_line_with_a_light_border():
    matrix = encode("http://192.168.1.20:8765/k7mqx2hd/")
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, color_system="truecolor", width=80)
    console.print(QRCode(matrix))
    modules = len(matrix) + 2 * QUIET_ZONE
    # Its own colours, so it is dark on light on a dark terminal too.
    assert buffer.getvalue().count("\x1b[38;2;0;0;0;48;2;255;255;255m") == (modules + 1) // 2

    plain = io.StringIO()
    Console(file=plain, force_terminal=False, color_system=None, width=80).print(QRCode(matrix))
    lines = plain.getvalue().splitlines()
    assert len(lines) == (modules + 1) // 2 and all(len(line) == 2 + modules for line in lines)
    # Read the blocks back: the upper and lower half of every character are two rows of modules.
    upper = {" ": ".", "▀": "#", "▄": ".", "█": "#"}
    lower = {" ": ".", "▀": ".", "▄": "#", "█": "#"}
    rows = []
    for line in lines:
        rows += ["".join(upper[c] for c in line[2:]), "".join(lower[c] for c in line[2:])]
    inner = [row[QUIET_ZONE:-QUIET_ZONE] for row in rows[QUIET_ZONE:QUIET_ZONE + len(matrix)]]
    assert inner == drawn(matrix)
    assert set("".join(rows[:QUIET_ZONE])) == {"."} and all(row[:QUIET_ZONE] == "." * QUIET_ZONE for row in rows)
