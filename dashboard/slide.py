"""Horizontal page-slide transition on an appbase FrameBuffer back buffer.

appbase reports a swipe only once the finger lifts, so the transition plays
after the gesture: the old page moves out and the new one moves in. Frames
are composed by copying whole columns between two full-frame snapshots, which
is far cheaper than drawing both pages again for every frame.

This reads appbase internals (_buf, stride, rotate, phys_w, phys_h); can()
says whether they are there, otherwise pages just switch without animation.
"""

import time

DURATION = 0.2  # seconds
FRAMES = 6


def can(fb):
    return all(hasattr(fb, a) for a in ("_buf", "stride", "rotate", "phys_w",
                                        "phys_h", "flush")) \
        and fb.rotate in (0, 90, 180, 270)


def copy_cols(fb, dst, src, sx, dx, n):
    """Copy visual columns [sx, sx+n) of src into [dx, dx+n) of dst.

    src and dst are full-frame byte buffers laid out like fb._buf.
    """
    if n <= 0:
        return
    r, stride = fb.rotate, fb.stride
    if r in (90, 270):  # a visual column is one physical row
        if r == 270:  # visual x -> physical row phys_h-1-x
            sx, dx = fb.phys_h - sx - n, fb.phys_h - dx - n
        dst[dx * stride:(dx + n) * stride] = src[sx * stride:(sx + n) * stride]
        return
    if r == 180:  # visual x -> physical x phys_w-1-x
        sx, dx = fb.phys_w - sx - n, fb.phys_w - dx - n
    sb, db, length = sx * 2, dx * 2, n * 2
    for row in range(0, stride * fb.phys_h, stride):
        dst[row + db:row + db + length] = src[row + sb:row + sb + length]


def compose(fb, dst, old, new, shift, forward):
    """Frame with the pages moved by shift pixels.

    forward: the old page leaves to the left and the new one comes in from
    the right (next page); otherwise the other way round.
    """
    w = fb.width
    shift = max(0, min(w, shift))
    if forward:
        copy_cols(fb, dst, old, shift, 0, w - shift)
        copy_cols(fb, dst, new, 0, w - shift, shift)
    else:
        copy_cols(fb, dst, new, w - shift, 0, shift)
        copy_cols(fb, dst, old, 0, shift, w - shift)


def play(fb, old, new, forward):
    """Animate from the old frame to the new one and leave the new frame in fb."""
    start = time.monotonic()
    for i in range(1, FRAMES):
        t = i / FRAMES
        ease = 1 - (1 - t) ** 3  # ease-out: fast start, gentle stop
        compose(fb, fb._buf, old, new, round(fb.width * ease), forward)
        fb.flush()
        wait = start + DURATION * t - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    fb._buf[:] = new
