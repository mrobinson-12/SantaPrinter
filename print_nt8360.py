#!/Users/michael/SantaPrinter/.venv/bin/python
"""Send plain text to an NT-8360 LAN printer using ESC/POS over TCP.

Santa border mode requires Pillow. Run with --help for options.
"""

import argparse
import math
from pathlib import Path
import socket
import sys
import textwrap

MAX_FONT_PT = 18
MIN_FONT_PT = 8
WIDTH_DOTS = 576
HEIGHT_DOTS = 800
TEXT_BOX = (81, 144, 495, 680)


def clean_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    if any((ord(c) < 32 and c != "\n") or 127 <= ord(c) < 160 for c in text):
        raise ValueError("Text contains control characters. Only newlines and tabs are allowed.")
    if not text.strip():
        raise ValueError("There is no text to print.")
    return text


def build_job(text, columns=48, feed=4, cut=True, replace=False):
    """Build a Font A / PC437 job; never interpret input as printer commands."""
    text = clean_text(text)
    lines = []
    for line in text.split("\n"):
        lines.extend(textwrap.wrap(line, width=columns, replace_whitespace=False,
                                   drop_whitespace=False) or [""])
    body = "\n".join(lines).rstrip("\n") + "\n"
    encoded = body.encode("cp437", errors="replace" if replace else "strict")
    # Initialize, disable Chinese mode, select Font A, PC437 table 0, left align.
    job = b"\x1b@\x1c.\x1bM\x00\x1bt\x00\x1ba\x00" + encoded
    job += b"\x1bd" + bytes([feed])
    if cut:
        job += b"\x1dV\x01"  # Partial cut.
    return job


def find_font(explicit=None):
    if explicit:
        if not Path(explicit).is_file():
            raise ValueError("Font file does not exist: {}".format(explicit))
        return str(explicit)
    candidates = [
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise ValueError("No suitable font found. Supply --font /path/to/font.ttf")


def wrap_pixels(text, font, width):
    """Wrap by measured glyph width, including splitting overlong words."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        line = ""
        for word in paragraph.split():
            candidate = line + (" " if line else "") + word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            for char in word:
                if font.getlength(char) > width:
                    raise ValueError("A character is wider than the text area.")
                if font.getlength(line + char) > width:
                    lines.append(line)
                    line = char
                else:
                    line += char
        if line:
            lines.append(line)
    return lines


def render_letter(text, border, font_path=None, font_size=12, auto_size=True):
    """Compose one 72 x 100 mm raster at 8 dots/mm; no text over artwork."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError("Santa mode requires Pillow. Install it in a virtual environment: "
                         "python3 -m pip install Pillow")
    if not MIN_FONT_PT <= font_size <= MAX_FONT_PT:
        raise ValueError("Font size must be between {} and {} pt.".format(MIN_FONT_PT, MAX_FONT_PT))
    text = clean_text(text).strip("\n")
    font_path = find_font(font_path)
    left, top, right, bottom = TEXT_BOX
    width, height = right - left, bottom - top
    sizes = range(font_size, MIN_FONT_PT - 1, -1) if auto_size else [font_size]
    for points in sizes:
        # 8 dots/mm = 203.2 dpi; point size is 1/72 inch.
        font = ImageFont.truetype(font_path, round(points * 203.2 / 72))
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + max(3, round(font.size * 0.15))
        lines = wrap_pixels(text, font, width - 4)
        if len(lines) * line_height <= height:
            break
    else:
        raise ValueError("Text will not fit inside the 10 cm border at {} pt. "
                         "Shorten the letter{}".format(points, "; the minimum is 8 pt." if points == MIN_FONT_PT else " or use a smaller --font-size." ))
    with Image.open(border) as original:
        image = original.convert("L").resize((WIDTH_DOTS, HEIGHT_DOTS), Image.Resampling.LANCZOS)
    # Render into a separate bounded panel so italic overhangs cannot touch the border.
    panel = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(panel)
    for i, line in enumerate(lines):
        draw.text((2, i * line_height), line, fill=0, font=font, anchor="lt")
    image.paste(panel, (left, top))
    # Threshold rather than dither: crisp text and reduced thermal dot noise.
    image = image.point(lambda value: 255 if value >= 180 else 0, mode="1")
    image.info["dpi"] = (203.2, 203.2)
    return image, points


def build_raster_job(image, feed=4, cut=True):
    """GS v 0 raster rows, MSB first, 1 means black; send in 128-row strips."""
    width, height = image.size
    if width != WIDTH_DOTS or height != HEIGHT_DOTS or image.mode != "1":
        raise ValueError("Expected a 576 x 800 monochrome letter image.")
    width_bytes = (width + 7) // 8
    job = bytearray(b"\x1b@\x1ba\x00")
    for y in range(0, height, 128):
        rows = min(128, height - y)
        # Pillow 1-bit bytes use white=1; ESC/POS uses black=1.
        packed = image.crop((0, y, width, y + rows)).tobytes()
        job += b"\x1dv0\x00" + width_bytes.to_bytes(2, "little") + rows.to_bytes(2, "little")
        job += bytes(value ^ 0xff for value in packed)
    job += b"\x1bd" + bytes([feed])
    if cut:
        job += b"\x1dV\x01"
    return bytes(job)


def send_job(host, port, job, timeout):
    """Send once. Retrying a partial send could print duplicate receipts."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Printer IP address or hostname")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="Text to print (quote it in your shell)")
    source.add_argument("--file", type=Path, help="Read a UTF-8 text file")
    parser.add_argument("--port", type=int, default=9100, help="TCP port (default: 9100)")
    parser.add_argument("--timeout", type=float, default=10, help="Network timeout in seconds")
    parser.add_argument("--columns", type=int, default=48, help="Wrap width (default: 48 for 80mm Font A)")
    parser.add_argument("--feed", type=int, default=4, help="Trailing feed lines, 0–255 (default: 4)")
    parser.add_argument("--no-cut", action="store_true", help="Do not cut the paper")
    parser.add_argument("--replace", action="store_true", help="Replace characters outside PC437 with ?")
    parser.add_argument("--dry-run", type=Path, metavar="OUTPUT", help="Save printer bytes instead of sending")
    parser.add_argument("--plain", action="store_true", help="Print old-style plain text without the Santa border")
    parser.add_argument("--border", type=Path, default=Path(__file__).resolve().with_name("santa-receipt-border.png"), help="Border image (default: beside this script)")
    parser.add_argument("--font-size", type=int, default=12, help="Requested size in points, 8–18 (default: 12); automatically shrinks to fit")
    parser.add_argument("--font", type=Path, help="Optional TTF/OTF font; default: Georgia on macOS")
    parser.add_argument("--fixed-size", action="store_true", help="Disable automatic font shrinking")
    parser.add_argument("--preview", type=Path, metavar="PNG", help="Save a preview PNG without sending to the printer")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if not 1 <= args.columns <= 48:
        parser.error("--columns must be between 1 and 48")
    if not 0 <= args.feed <= 255:
        parser.error("--feed must be between 0 and 255")
    if not MIN_FONT_PT <= args.font_size <= MAX_FONT_PT:
        parser.error("--font-size must be between 8 and 18 pt")
    if args.plain and args.preview:
        parser.error("--preview is available only in Santa border mode")
    try:
        if args.text is not None:
            text = args.text
        elif args.file is not None:
            text = args.file.read_text(encoding="utf-8-sig")
        else:
            if sys.stdin.isatty():
                parser.error("Supply --text, --file, or pipe text into the script")
            text = sys.stdin.read()
        if args.plain:
            job = build_job(text, args.columns, args.feed, not args.no_cut, args.replace)
        else:
            image, actual_size = render_letter(text, args.border, args.font, args.font_size, not args.fixed_size)
            job = build_raster_job(image, args.feed, not args.no_cut)
            print("Santa letter: 72 x 100 mm, {} pt text.".format(actual_size))
            if args.preview is not None:
                image.save(args.preview, format="PNG", dpi=(203.2, 203.2))
                print("Saved preview to {}; nothing sent to the printer.".format(args.preview))
        if args.dry_run is not None:
            args.dry_run.write_bytes(job)
            print("Saved {} bytes to {}".format(len(job), args.dry_run))
        elif args.preview is None:
            send_job(args.host, args.port, job, args.timeout)
            print("Sent {} bytes to {}:{}; physical printing is not confirmed.".format(
                len(job), args.host, args.port))
        return 0
    except UnicodeEncodeError:
        print("Error: text contains characters outside PC437 (e.g. emoji). "
              "Use --replace to print ? in their place.", file=sys.stderr)
    except (OSError, ValueError) as error:
        print("Error: {}".format(error), file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
