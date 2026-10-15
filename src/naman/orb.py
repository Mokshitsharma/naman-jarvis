"""The orb's look: a soft gradient blob with a cute face, drawn with Pillow.

Each frame is drawn at twice the size and shrunk (smooth, anti-aliased edges Tk's canvas
can't do), into one image the widget reuses, so a frame is a single paste instead of
dozens of canvas items created and deleted.

    colour     soft lights in the state's colour and its neighbours drift inside it (slow
               while waiting, fast while thinking), lit from the top left
    shape      a squishy circle that breathes, leans in while listening and whose edge
               ripples with your voice and the assistant's
    face       eyes that blink and glance about, pink cheeks, a mouth that talks; the
               expression follows the state (see OrbRenderer._face)
    hop        `cheer()`: two little bounces with happy eyes when there's news
    plan       one dot per step under the face: finished ones solid, the running one pulsing
    wake       a quick brightening when it starts listening
"""

from __future__ import annotations

import colorsys
import math

from PIL import Image, ImageDraw, ImageFilter

PANEL = (15, 23, 42)
WHITE = (255, 255, 255)
SS = 2  # supersampling
SPLIT_S = 0.7  # how long the core takes to split into plan satellites


def rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def mix(a: tuple[float, ...], b: tuple[float, ...], t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def _ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def panel_image(w: int, h: int, radius: int, colour: str) -> Image.Image:
    """The speech bubble behind the text: a rounded card with a soft vertical gradient,
    a hairline border and a top highlight tinted with the state colour. Drawn at twice
    the size for smooth corners; made once per layout, not per frame."""
    W, H, r = w * SS, h * SS, radius * SS
    top, bottom = (23, 32, 58), (9, 14, 27)
    grad = Image.linear_gradient("L").resize((W, H))
    fill = Image.composite(Image.new("RGB", (W, H), bottom), Image.new("RGB", (W, H), top), grad)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), r, fill=255)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.paste(fill, (0, 0), mask)
    col = rgb(colour)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((SS // 2, SS // 2, W - 1 - SS // 2, H - 1 - SS // 2), r,
                        outline=mix(top, col, 0.45) + (255,), width=SS)
    # A highlight along the top edge, brightest a third of the way in, fading at the ends.
    for x in range(r, W - r):
        k = max(0.0, 1 - abs((x - W * 0.35) / (W * 0.45))) ** 1.5
        if k > 0.02:
            d.point((x, SS), fill=mix(top, col, 0.25 + 0.6 * k) + (255,))
            d.point((x, SS + 1), fill=mix(top, col, 0.1 + 0.35 * k) + (255,))
    return img.resize((w, h), Image.LANCZOS)


INK = (30, 27, 75)  # eyes and mouth: a deep navy, softer than black
PINK = (255, 120, 165)
HOP_S = 1.3  # how long the happy hop lasts


class OrbRenderer:
    def __init__(self, size: int) -> None:
        self.size = size
        self.n = n = size * SS
        self.g = max(24, n // 6)  # the colour field is painted this small, blurred, then stretched
        self.colour: tuple[float, float, float] = rgb("#94a3b8")
        self.level = 0.0
        self.swell = 0.74  # how big the blob is right now, eased towards its target
        self.mode = ""
        self.mode_since = 0.0
        self.flash_at = -10.0
        self.happy_at = -10.0
        self.plan_at = -10.0
        self.plan_total = 0
        self._shine = self._highlight()

    def cheer(self, t: float) -> None:
        """A little hop with happy eyes: something new to tell you."""
        self.happy_at = t

    # --- state ----------------------------------------------------------------------------

    def _track(self, t: float, mode: str, colour: str, level: float, plan_total: int) -> None:
        if mode != self.mode:
            if mode == "listening":
                self.flash_at = t
            self.mode, self.mode_since = mode, t
        if plan_total != self.plan_total:
            self.plan_total, self.plan_at = plan_total, t
        target = rgb(colour)
        self.colour = tuple(c + (g - c) * 0.12 for c, g in zip(self.colour, target))  # type: ignore[assignment]
        # Fast attack, slow release: reacts to a syllable, doesn't flicker between them.
        self.level += (level - self.level) * (0.6 if level > self.level else 0.12)

    # --- drawing --------------------------------------------------------------------------

    def render(self, t: float, mode: str, colour: str, level: float = 0.0, hover: bool = False,
               plan_total: int = 0, plan_done: int = 0) -> Image.Image:
        """One frame (RGBA, `size` square). `plan_done` counts steps finished; the step
        after them is the one running."""
        self._track(t, mode, colour, level, plan_total)
        n, c = self.n, self.n / 2
        muted = mode == "muted"
        busy = mode in {"thinking", "heard", "working", "starting"}
        lv = 0.0 if muted else self.level
        breath = 0.5 + 0.5 * math.sin(t * (1.4 if mode == "sleeping" else 2.6))

        # Size: breathes while waiting, leans in while listening, swells with a voice. Kept
        # under the frame so a hop has room.
        target = 0.72 + 0.03 * breath + 0.05 * hover + 0.05 * (mode == "listening") + 0.1 * lv
        self.swell += (target - self.swell) * 0.25
        R = n / 2 * min(0.88, self.swell)

        # Hop: two bounces, squashing a little each time it lands.
        p = (t - self.happy_at) / HOP_S
        hop, sx, sy = 0.0, 1.0, 1.0
        if 0 <= p < 1:
            bounce = abs(math.sin(p * 2 * math.pi)) * (1 - p)
            hop = n * 0.09 * bounce
            squash = max(0.0, 0.25 - bounce) * (1 - p) * 0.5
            sx, sy = 1 + squash, 1 - squash
        cy = c + n / 2 * 0.06 - hop  # sits a touch low so it can jump

        # Colour field: soft lights drifting inside the blob (the Meta AI swirl).
        pal = palette(self.colour, muted)
        speed = 0.25 if muted else 0.5 if mode == "sleeping" else 1.6 if busy else 1.0
        g = self.g
        field = Image.new("RGB", (g, g), pal[3])
        fd = ImageDraw.Draw(field)
        for k, (col, orbit, size) in enumerate(((pal[0], 0.22, 0.42), (pal[1], 0.28, 0.36),
                                                (pal[2], 0.25, 0.34), (pal[0], 0.12, 0.22))):
            a = t * speed * (0.7 + 0.25 * k) * (-1 if k % 2 else 1) + k * 1.9
            x = g / 2 + g * orbit * math.cos(a)
            y = g / 2 + g * orbit * math.sin(a * 1.3 + k)
            r = g * size * (1 + 0.25 * lv)
            fd.ellipse((x - r, y - r, x + r, y + r), fill=col)
        img = field.filter(ImageFilter.GaussianBlur(g / 7)).resize((n, n), Image.BILINEAR)
        img.paste(WHITE, mask=self._shine)  # soft light from the top left: it reads as a sphere
        age = t - self.flash_at
        if 0 <= age < 0.5:  # wake: a quick brightening
            img = Image.blend(img, Image.new("RGB", (n, n), WHITE), 0.35 * (1 - age / 0.5))

        # Shape: a squishy circle whose edge ripples with the voice (barely at rest).
        wob = 0.012 + 0.045 * lv + 0.015 * busy
        pts = []
        for i in range(96):
            a = i * math.pi / 48
            rr = R * (1 + wob * (math.sin(3 * a + t * 2.3) * 0.6 + math.sin(5 * a - t * 3.1) * 0.4))
            pts.append((c + rr * sx * math.cos(a), cy + rr * sy * math.sin(a)))
        shape = Image.new("L", (n, n), 0)
        ImageDraw.Draw(shape).polygon(pts, fill=255)

        d = ImageDraw.Draw(img)
        happy = 0 <= p < 1
        self._face(d, t, c, cy, R, mode, lv, hover, happy)
        if self.plan_total > 1:
            self._plan(d, t, c, cy + R * 0.62, R, plan_done)

        frame = img.reduce(SS).convert("RGBA")
        frame.putalpha(shape.reduce(SS))  # outside the blob is see-through
        return frame

    def _highlight(self) -> Image.Image:
        n, g = self.n, self.g
        small = Image.new("L", (g, g), 0)
        ImageDraw.Draw(small).ellipse((g * 0.22, g * 0.2, g * 0.54, g * 0.46), fill=70)
        return small.filter(ImageFilter.GaussianBlur(g / 9)).resize((n, n), Image.BILINEAR)

    def _face(self, d: ImageDraw.ImageDraw, t: float, cx: float, cy: float, R: float, mode: str, lv: float,
              hover: bool, happy: bool) -> None:
        """Eyes that blink and look around, pink cheeks, and a mouth that talks.

        waiting    gentle smile, blinks every few seconds, glances about now and then
        listening  bigger eyes, a little "o"
        thinking   eyes up and to the side, a small sideways mouth
        speaking   the mouth opens with the voice
        happy      ^ ^ eyes and an open grin (a new update, or the mouse on it)
        muted      eyes closed, mouth flat
        error      > < eyes
        """
        line = max(2, int(2.1 * SS))
        # Where the eyes look: a slow wander while waiting, up and aside while thinking.
        if mode in {"thinking", "heard", "working", "starting"}:
            lx, ly = R * (0.07 + 0.02 * math.sin(t * 1.3)), -R * 0.07
        else:
            glance = math.sin(t * 0.31) * math.sin(t * 0.17 + 1)
            lx, ly = R * 0.08 * max(-1.0, min(1.0, glance * 2.2)), R * 0.015 * math.sin(t * 0.23)
        ex, ey = R * 0.32, cy - R * 0.1 + ly
        big = 1.18 if mode == "listening" else 1.0
        w, h = R * 0.1 * big, R * 0.145 * big
        beat = t % 4.6  # blink every few seconds; sometimes twice
        blink = beat < 0.12 or (int(t / 4.6) % 3 == 0 and 0.22 < beat < 0.34)

        for side in (-1, 1):
            x = cx + side * ex + lx
            if mode == "muted" or (happy or hover) and mode not in {"error"}:
                # Closed (muted, curving down) or happy (^, curving up).
                down = mode == "muted"
                box = (x - w * 1.1, ey - h * 0.55, x + w * 1.1, ey + h * 0.55)
                d.arc(box, 20 if down else 200, 160 if down else 340, fill=INK, width=line)
            elif mode == "error":
                s = w * 0.9
                d.line((x - s * side, ey - s, x + s * side, ey), fill=INK, width=line)
                d.line((x + s * side, ey, x - s * side, ey + s), fill=INK, width=line)
            elif blink:
                d.line((x - w, ey, x + w, ey), fill=INK, width=line)
            else:
                d.ellipse((x - w, ey - h, x + w, ey + h), fill=INK)
                gw = w * 0.42  # a glint, so they look alive
                d.ellipse((x - w * 0.55 - gw / 2 + w * 0.3, ey - h * 0.55 - gw / 2, x - w * 0.55 + gw / 2 + w * 0.3,
                           ey - h * 0.55 + gw / 2), fill=WHITE)

        # Cheeks: pinker when happy or hovered.
        blush = mix(self.colour, PINK, 0.75 if happy or hover else 0.55)
        bw, bh = R * 0.13, R * 0.065
        for side in (-1, 1):
            x, y = cx + side * R * 0.47 + lx * 0.5, cy + R * 0.1
            d.ellipse((x - bw, y - bh, x + bw, y + bh), fill=blush)

        # Mouth.
        mx, my = cx + lx * 0.5, cy + R * 0.22
        if mode == "speaking":
            k = 0.55 + 0.45 * abs(math.sin(t * 13) * math.sin(t * 5.1))
            mw, mh = R * 0.095, R * (0.03 + 0.13 * lv * k)
            d.ellipse((mx - mw, my - mh, mx + mw, my + mh), fill=INK)
            if mh > R * 0.06:  # a tongue when it's wide open
                d.chord((mx - mw * 0.7, my, mx + mw * 0.7, my + mh * 0.95), 0, 180, fill=PINK)
        elif happy or hover:
            mw = R * 0.14
            d.chord((mx - mw, my - mw * 0.75, mx + mw, my + mw * 0.75), 0, 180, fill=INK)
            d.chord((mx - mw * 0.55, my + mw * 0.2, mx + mw * 0.55, my + mw * 0.72), 0, 180, fill=PINK)
        elif mode == "listening":
            s = R * 0.05
            d.ellipse((mx - s, my - s * 0.8, mx + s, my + s * 1.1), fill=INK)
        elif mode in {"thinking", "heard", "working", "starting"}:
            s = R * 0.075
            d.line((mx - s + R * 0.04, my + R * 0.01, mx + s + R * 0.04, my - R * 0.012), fill=INK, width=line)
        elif mode == "muted":
            s = R * 0.05
            d.line((mx - s, my, mx + s, my), fill=INK, width=line)
        elif mode == "error":
            s = R * 0.07
            d.line([(mx - s, my), (mx - s / 2, my - s / 3), (mx, my), (mx + s / 2, my - s / 3), (mx + s, my)],
                   fill=INK, width=line)
        else:  # waiting: a small smile
            s = R * 0.11
            d.arc((mx - s, my - s * 0.8, mx + s, my + s * 0.5), 25, 155, fill=INK, width=line)

    def _plan(self, d: ImageDraw.ImageDraw, t: float, cx: float, y: float, R: float, done: int) -> None:
        """A multi-step command: one dot per step, sliding out of the middle; finished ones
        solid, the running one pulsing, the rest faint."""
        total = self.plan_total
        grow = _ease((t - self.plan_at) / SPLIT_S)
        gap = R * min(0.16, 0.9 / total) * grow
        for i in range(total):
            x = cx + (i - (total - 1) / 2) * gap
            if i < done:
                s, fill = R * 0.04, WHITE
            elif i == done:
                pulse = 0.5 + 0.5 * math.sin(t * 8)
                s, fill = R * (0.045 + 0.015 * pulse), WHITE
            else:
                s, fill = R * 0.03, mix(self.colour, WHITE, 0.55)
            d.ellipse((x - s, y - s, x + s, y + s), fill=fill)


def palette(colour: tuple[float, ...], muted: bool = False) -> list[tuple[int, int, int]]:
    """The state colour and two neighbours on the colour wheel (blue, violet and teal for
    listening), plus a deeper shade behind the swirl."""
    h, l, s = colorsys.rgb_to_hls(*(v / 255 for v in colour))
    if muted:
        s *= 0.25

    def shade(dh: float, dl: float) -> tuple[int, int, int]:
        rgb_ = colorsys.hls_to_rgb((h + dh) % 1, min(0.9, max(0.1, l + dl)), s)
        return tuple(round(v * 255) for v in rgb_)  # type: ignore[return-value]

    return [shade(0, 0.02), shade(0.09, -0.02), shade(-0.08, 0.08), shade(0.04, -0.22)]
