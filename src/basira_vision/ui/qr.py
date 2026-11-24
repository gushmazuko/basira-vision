"""A small QR code encoder for showing an address in the terminal.

Enough of ISO/IEC 18004 for a short URL and nothing more: byte mode, error correction
level L, versions 1 to 5 (up to 106 bytes), which all use a single Reed-Solomon block.
Kept in the package so that showing a QR code does not add a dependency.
"""

from typing import List, Optional

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style

Matrix = List[List[bool]]  # [row][column], True is a dark module

# version -> (data codewords, error correction codewords) at level L
_CODEWORDS = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
_FORMAT_LEVEL_L = 0b01
QUIET_ZONE = 2  # modules of light border; enough on a screen, where the code has a frame of its own colour

# GF(256) with the polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP = [0] * 510
_LOG = [0] * 256
_value = 1
for _i in range(255):
    _EXP[_i] = _EXP[_i + 255] = _value
    _LOG[_value] = _i
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D


def _error_correction(data: List[int], count: int) -> List[int]:
    """The Reed-Solomon codewords for a block of data codewords."""
    generator = [1]
    for i in range(count):  # multiply by (x - a^i)
        generator = [
            (generator[j] if j < len(generator) else 0)
            ^ (_EXP[_LOG[generator[j - 1]] + i] if j and generator[j - 1] else 0)
            for j in range(len(generator) + 1)
        ]
    remainder = [0] * count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for j in range(count):
                if generator[j + 1]:
                    remainder[j] ^= _EXP[_LOG[generator[j + 1]] + _LOG[factor]]
    return remainder


def _codewords(payload: bytes, version: int) -> List[int]:
    capacity, correction = _CODEWORDS[version]
    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(payload) >> i) & 1 for i in range(7, -1, -1)]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))  # terminator
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    for i in range(capacity - len(data)):
        data.append(0xEC if i % 2 == 0 else 0x11)
    return data + _error_correction(data, correction)


def _masked(row: int, column: int, mask: int) -> bool:
    product, total = row * column, row + column
    return (
        total % 2 == 0, row % 2 == 0, column % 3 == 0, total % 3 == 0,
        (row // 2 + column // 3) % 2 == 0, product % 2 + product % 3 == 0,
        (product % 2 + product % 3) % 2 == 0, (total % 2 + product % 3) % 2 == 0,
    )[mask]


def _penalty(matrix: Matrix) -> int:
    """The standard's score of how hard a symbol is to read; the mask with the lowest wins."""
    size = len(matrix)
    lines = matrix + [list(column) for column in zip(*matrix, strict=True)]
    score = 0
    finder = [True, False, True, True, True, False, True]
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        for i in range(size - 6):
            if line[i:i + 7] == finder:
                before, after = line[max(0, i - 4):i], line[i + 7:i + 11]
                if (i >= 4 and not any(before)) or (len(after) == 4 and not any(after)):
                    score += 40
    for row in range(size - 1):
        for column in range(size - 1):
            if matrix[row][column] == matrix[row][column + 1] == matrix[row + 1][column] == matrix[row + 1][column + 1]:
                score += 3
    dark = sum(sum(row) for row in matrix)
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def _symbol(codewords: List[int], version: int, mask: int) -> Matrix:
    size = 17 + 4 * version
    matrix = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def put(row: int, column: int, dark: bool) -> None:
        if 0 <= row < size and 0 <= column < size:
            matrix[row][column] = dark
            function[row][column] = True

    for i in range(size):  # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):  # finder patterns with their separators
        for row in range(-1, 8):
            for column in range(-1, 8):
                ring = max(abs(row - 3), abs(column - 3))
                put(top + row, left + column, ring != 2 and ring != 4)
    if version > 1:  # one alignment pattern, near the corner without a finder
        centre = size - 7
        for row in range(-2, 3):
            for column in range(-2, 3):
                put(centre + row, centre + column, max(abs(row), abs(column)) != 1)

    # Format information: level and mask, protected by a BCH(15,5) code, written twice.
    data = _FORMAT_LEVEL_L << 3 | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    fmt = (data << 10 | remainder) ^ 0x5412
    bit = [bool(fmt >> i & 1) for i in range(15)]
    for i in range(6):
        put(i, 8, bit[i])
    put(7, 8, bit[6])
    put(8, 8, bit[7])
    put(8, 7, bit[8])
    for i in range(9, 15):
        put(8, 14 - i, bit[i])
    for i in range(8):
        put(8, size - 1 - i, bit[i])
    for i in range(8, 15):
        put(size - 15 + i, 8, bit[i])
    put(size - 8, 8, True)  # the module that is always dark

    bits = [bool(byte >> i & 1) for byte in codewords for i in range(7, -1, -1)]
    index = 0
    right = size - 1
    while right >= 1:  # pairs of columns from the right, snaking up and down
        if right == 6:  # the vertical timing pattern takes a whole column
            right = 5
        upward = ((right + 1) & 2) == 0
        for step in range(size):
            row = size - 1 - step if upward else step
            for column in (right, right - 1):
                if not function[row][column]:
                    dark = index < len(bits) and bits[index]  # past the data: remainder bits, light
                    index += 1
                    matrix[row][column] = dark != _masked(row, column, mask)
        right -= 2
    return matrix


def encode(text: str) -> Optional[Matrix]:
    """The QR code for a text as rows of modules, or None when it is too long for this encoder."""
    payload = text.encode("utf-8")
    for version, (capacity, _) in _CODEWORDS.items():
        if len(payload) <= capacity - 2:  # the mode and the length take 12 bits
            codewords = _codewords(payload, version)
            return min((_symbol(codewords, version, mask) for mask in range(8)), key=_penalty)
    return None


class QRCode:
    """A QR code as a rich renderable: two modules per character cell, dark on light.

    The colours are set explicitly, so the code is dark on light on any terminal theme.
    Without colours (NO_COLOR, a dumb terminal) the blocks alone still draw it, dark on
    light where the terminal is light.
    """

    STYLE = Style(color="#000000", bgcolor="#ffffff")
    BLOCKS = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}

    def __init__(self, matrix: Matrix, indent: int = 2):
        size = len(matrix) + 2 * QUIET_ZONE
        self.rows = [[False] * size for _ in range(size)]
        for r, row in enumerate(matrix):
            self.rows[r + QUIET_ZONE][QUIET_ZONE:QUIET_ZONE + len(row)] = row
        if size % 2:
            self.rows.append([False] * size)  # the lower half of the last line of characters
        self.indent = indent

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        width = self.indent + len(self.rows[0])
        return Measurement(width, width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for top, bottom in zip(self.rows[::2], self.rows[1::2], strict=True):
            yield Segment(" " * self.indent)
            yield Segment("".join(self.BLOCKS[pair] for pair in zip(top, bottom, strict=True)), self.STYLE)
            yield Segment.line()
