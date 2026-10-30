"""Minimal QR Code encoder: byte mode, error correction level L, versions 1-6.

Enough for the status page URL (up to 134 bytes), stdlib only. Follows the
structure of Project Nayuki's reference implementation. encode() returns a
square list of rows of booleans (True = dark) without the quiet zone.
"""

# level L: version -> (data codewords, EC codewords per block, blocks)
_LEVEL_L = {1: (19, 7, 1), 2: (34, 10, 1), 3: (55, 15, 1), 4: (80, 20, 1),
            5: (108, 26, 1), 6: (136, 18, 2)}
_ALIGN = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}  # second alignment coordinate

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _ec_codewords(data, n):
    """Reed-Solomon remainder of data with n EC codewords."""
    gen = [1]
    for i in range(n):  # gen *= (x + a^i), highest degree first
        nxt = gen + [0]
        for j, g in enumerate(gen):
            nxt[j + 1] ^= _mul(g, _EXP[i])
        gen = nxt
    msg = list(data) + [0] * n
    for i in range(len(data)):
        coef = msg[i]
        if coef:
            for j in range(1, len(gen)):
                msg[i + j] ^= _mul(gen[j], coef)
    return msg[len(data):]


def _codewords(payload):
    """Pick the smallest version; return (version, final codeword list)."""
    for version, (ndata, nec, nblocks) in sorted(_LEVEL_L.items()):
        if len(payload) + 2 <= ndata:  # 4-bit mode + 8-bit count fit in 2
            break
    else:
        raise ValueError("text too long for a version 1-6 QR code")
    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(payload) >> i) & 1 for i in range(7, -1, -1)]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, ndata * 8 - len(bits))  # terminator
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2)
            for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < ndata:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    size = ndata // nblocks  # equal block sizes for every level-L version <= 6
    blocks = [data[i * size:(i + 1) * size] for i in range(nblocks)]
    ecs = [_ec_codewords(b, nec) for b in blocks]
    out = [b[i] for i in range(size) for b in blocks]
    out += [e[i] for i in range(nec) for e in ecs]
    return version, out


_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: r * c % 2 + r * c % 3 == 0,
    lambda r, c: (r * c % 2 + r * c % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + r * c % 3) % 2 == 0,
]


class _Matrix:
    def __init__(self, version):
        self.size = n = version * 4 + 17
        self.dark = [[False] * n for _ in range(n)]
        self.function = [[False] * n for _ in range(n)]
        for i in range(n):  # timing patterns
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (n - 4, 3), (3, n - 4)):  # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= x + dx < n and 0 <= y + dy < n:
                        self.set(x + dx, y + dy,
                                 max(abs(dx), abs(dy)) not in (2, 4))
        if version in _ALIGN:
            a = _ALIGN[version]
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.set(a + dx, a + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # reserve the format areas
        self.set(8, n - 8, True)  # dark module

    def set(self, x, y, dark):
        self.dark[y][x] = dark
        self.function[y][x] = True

    def format_bits(self, mask):
        data = (1 << 3) | mask  # level L is 0b01
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        n = self.size

        def bit(i):
            return (bits >> i) & 1 == 1

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit(i))

    def place(self, codewords):
        n, i = self.size, 0
        right = n - 1
        while right >= 1:
            if right == 6:  # skip the vertical timing column
                right = 5
            upward = (right + 1) & 2 == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.function[y][x] and i < len(codewords) * 8:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        fn = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and fn(y, x):
                    self.dark[y][x] = not self.dark[y][x]


def _penalty(m):
    n = len(m)
    score = 0
    lines = m + [[m[y][x] for y in range(n)] for x in range(n)]
    finder = ([True, False, True, True, True, False, True, False, False, False, False],
              [False, False, False, False, True, False, True, True, True, False, True])
    for line in lines:
        run = 1
        for i in range(1, n + 1):
            if i < n and line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        for i in range(n - 10):
            if line[i:i + 11] in finder:
                score += 40
    for y in range(n - 1):
        for x in range(n - 1):
            c = m[y][x]
            if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    total = n * n
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def encode(text, mask=None):
    """QR matrix for text; mask None picks the lowest-penalty mask."""
    version, codewords = _codewords(text.encode("utf-8"))
    best = None
    for candidate in range(8) if mask is None else (mask,):
        m = _Matrix(version)
        m.place(codewords)
        m.apply_mask(candidate)
        m.format_bits(candidate)
        score = _penalty(m.dark) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, m.dark)
    return best[1]
