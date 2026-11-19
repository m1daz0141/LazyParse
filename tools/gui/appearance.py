"""Local appearance settings and portable, copied background images."""
import hashlib
from io import BytesIO
from pathlib import Path
import re

DEFAULT_BACKGROUND = '#0a0a0a'
DEFAULT_ACCENT = '#76d5cf'
COLOR_ROLES = ('#0a0a0a', '#15191c', '#e4e8eb', '#939da5', '#343c41', '#76d5cf',
    '#205c61', '#1b6467', '#29353b', '#426068', '#29383f', '#586168', '#24575d',
    '#102b2d', '#111619', '#19565a', '#103c40', '#252c31', '#4a878a', '#252d32', 'white')
BACKGROUND_NAME = re.compile(r'background-[0-9a-f]{16}\.png')


def valid_color(value):
    return isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) is not None


def rgb(value):
    if not valid_color(value):
        raise ValueError('RGBは0〜255で指定してください。')
    return tuple(int(value[i:i+2], 16) for i in (1, 3, 5))


def rgb_color(values):
    if len(values) != 3 or any(not re.fullmatch(r'[0-9]{1,3}', str(v)) for v in values):
        raise ValueError('RGBは0〜255の整数で指定してください。')
    channels = tuple(int(v) for v in values)
    if any(v > 255 for v in channels):
        raise ValueError('RGBは0〜255の整数で指定してください。')
    return '#' + ''.join(f'{v:02x}' for v in channels)


def mix(first, second, weight):
    return '#' + ''.join(f'{round(a*(1-weight)+b*weight):02x}' for a, b in zip(rgb(first), rgb(second)))


def palette(background=DEFAULT_BACKGROUND, accent=DEFAULT_ACCENT):
    if background == DEFAULT_BACKGROUND and accent == DEFAULT_ACCENT:
        return {color: color for color in COLOR_ROLES}
    light = sum(c*w for c, w in zip(rgb(background), (.2126, .7152, .0722))) > 150
    text = '#161a1d' if light else '#e4e8eb'
    panel = mix(background, text, .08)
    own = mix(background, accent, .17)
    return dict(zip(COLOR_ROLES, (background, panel, text, mix(background, text, .62),
        mix(background, text, .24), accent, mix(background, accent, .42),
        mix(background, accent, .46), mix(background, text, .16), mix(background, accent, .44),
        mix(background, text, .19), mix(background, text, .34), mix(background, accent, .4),
        own, mix(background, text, .035), mix(background, accent, .36),
        mix(background, accent, .24), mix(background, text, .13), mix(background, accent, .65),
        panel, text)))


def status_band_color(label, color, accent):
    """Footer band: the user's accent for normal states, built-in colors otherwise."""
    if label in {'待機中', '計測中'} and valid_color(accent) and accent.lower() != DEFAULT_ACCENT:
        return accent.lower()
    return color


def import_background(source, directory):
    """Decode first frame/orientation, strip metadata and copy a bounded PNG."""
    from PIL import Image, ImageOps
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert('RGBA')
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            image.info.clear()
            encoded = BytesIO()
            image.save(encoded, format='PNG')
    except Image.DecompressionBombError as error:
        raise ValueError('画像のサイズが大きすぎます。縮小した画像を選択してください。') from error
    data = encoded.getvalue()
    name = f'background-{hashlib.sha256(data).hexdigest()[:16]}.png'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    temporary = target.with_suffix('.tmp')
    temporary.write_bytes(data)
    temporary.replace(target)
    return name


def remove_background(name, directory):
    if BACKGROUND_NAME.fullmatch(name):
        try:
            (Path(directory) / name).unlink(missing_ok=True)
        except OSError:
            pass  # A stale owned image must not prevent restoring appearance.


class Background:
    def __init__(self):
        self.source = self.key = self.photo = None
        self.overlays = []

    def load(self, name, directory):
        self.source = self.key = self.photo = None
        self.overlays.clear()
        if name:
            from PIL import Image
            try:
                with Image.open(Path(directory) / name) as image:
                    self.source = image.convert('RGBA')
            except Image.DecompressionBombError as error:
                raise ValueError('背景画像のサイズが大きすぎます。') from error

    def image(self, master, size, color):
        if self.source is None:
            return None
        key = (size, color)
        if self.key != key:
            from PIL import Image, ImageOps, ImageTk
            fitted = ImageOps.fit(self.source, size, method=Image.Resampling.LANCZOS)
            base = Image.new('RGBA', size, color)
            composited = Image.alpha_composite(base, fitted).convert('RGB')
            # Readable labels while preserving the selected image underneath bars.
            composited = Image.blend(composited, base.convert('RGB'), .30)
            self.photo = ImageTk.PhotoImage(composited, master=master)
            self.key = key
        return self.photo

    def overlay(self, master, size, color, opacity):
        from PIL import Image, ImageTk
        photo = ImageTk.PhotoImage(Image.new('RGBA', size, (*rgb(color), round(255*opacity))), master=master)
        self.overlays.append(photo)  # Keep this frame's canvas images alive.
        return photo
