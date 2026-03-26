"""SH1106 1.3" OLED display (128x64) via I2C."""

import os

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import sh1106
    except Exception:
        SIMULATE = True

from PIL import Image, ImageDraw, ImageFont

WIDTH = 128
HEIGHT = 64


def _load_font(size):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_LARGE = _load_font(28)
FONT_MED = _load_font(16)
FONT_SMALL = _load_font(12)


class OledDisplay:
    def __init__(self):
        self.simulate = SIMULATE
        self._device = None
        if not self.simulate:
            serial = i2c(port=1, address=0x3C)
            self._device = sh1106(serial)

    def render(self, current_f: float, setpoint_f: float, heating: bool):
        """Draw the thermostat screen: current temp, setpoint, and heat status."""
        img = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(img)

        # Current temperature — large, top-left
        temp_str = f"{current_f:.0f}°"
        draw.text((0, 0), temp_str, font=FONT_LARGE, fill=1)

        # Setpoint — medium, top-right
        set_str = f"Set {setpoint_f:.0f}°"
        bbox = draw.textbbox((0, 0), set_str, font=FONT_MED)
        draw.text((WIDTH - (bbox[2] - bbox[0]) - 1, 4), set_str, font=FONT_MED, fill=1)

        # Divider line
        draw.line([(0, 38), (WIDTH, 38)], fill=1)

        # Heat status — bottom center
        status = "HEAT ON" if heating else "HEAT OFF"
        bbox = draw.textbbox((0, 0), status, font=FONT_MED)
        text_w = bbox[2] - bbox[0]
        x = (WIDTH - text_w) // 2
        draw.text((x, 44), status, font=FONT_MED, fill=1)

        if self._device:
            self._device.display(img)

        return img

    def off(self):
        if self._device:
            self._device.hide()
