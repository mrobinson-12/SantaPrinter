# NT-8360 Santa receipt printing

The script now defaults to printing the Santa border with your text inside it as a monochrome image. Layout: 576 x 800 dots at 8 dots/mm, or 72 mm wide x 100 mm tall, for the printable area of 80 mm receipt paper. Additional paper feed for the cutter makes the total paper used longer than 100 mm.

Your installed copy is `/Users/michael/SantaPrinter/print_nt8360.py`, with the border image beside it. It is executable and configured to use the local virtual environment containing Pillow. Run it directly:

```sh
~/SantaPrinter/print_nt8360.py 192.168.1.100 --text "Dear Alex, Merry Christmas! Love, Santa" --font-size 12
~/SantaPrinter/print_nt8360.py 192.168.1.100 --file letter.txt --font-size 14
~/SantaPrinter/print_nt8360.py 192.168.1.100 --file letter.txt --preview ~/SantaPrinter/santa-preview.png
~/SantaPrinter/print_nt8360.py 192.168.1.100 --file letter.txt --font-size 12 --fixed-size
~/SantaPrinter/print_nt8360.py 192.168.1.100 --text "Ordinary receipt" --plain
```

`--preview` saves an image and sends nothing to the printer. `--dry-run job.bin` saves ESC/POS bytes and also sends nothing. Both options can be used together. The printer IP is still a required argument for these offline modes but no connection is made.

## Text layout

- `--font-size`: requested size in typographic points, 8 through 18; default 12. Values above the maximum 18 are rejected.
- Text wraps using actual font measurements, preserving explicit newlines and blank paragraphs. Long words are split when necessary.
- The script starts at the requested font size and automatically reduces it in 1-point steps until it fits, stopping at 8 pt. It never enlarges text above your requested size.
- `--fixed-size` disables shrinking. Text that cannot fit fails before connecting; it is not truncated and does not create extra pages.
- The text area leaves room for Santa and the decorations. Text begins at the top of the blank area.
- Default font is Georgia on macOS, with other platform fallbacks. `--font /path/to/font.ttf` selects another font. Use a font with glyphs for your language; missing glyphs can appear as boxes. Emoji support is not guaranteed.
- Control characters are rejected. Input files must be UTF-8. Literal backslash-n in `--text` is not expanded; use a file for multiline letters.
- `--border /path/to/image.png` accepts another border, but uses the same text area, so replacement artwork must leave that area blank.

## Network and printer behavior

The image is resized and thresholded to black and white, then transmitted using `GS v 0` in 128-row strips on TCP port 9100. This raster command is documented in NETUM's SDK; physical output on your particular firmware has not yet been verified. The complete layout is built before any network connection.

`--no-cut` disables partial cutting; `--feed 4` controls trailing paper feed. A successful send does not confirm physical printing. Network sends are not retried automatically because a failed send can print part of a job.

## Portable installation

For another computer, keep `print_nt8360.py` and `santa-receipt-border.png` together, create a Python 3.8+ virtual environment, and install Pillow:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install Pillow
.venv/bin/python print_nt8360.py 192.168.1.100 --file letter.txt
```

On Windows use `.venv\Scripts\python.exe`. The portable source uses the standard Python shebang; the home-directory copy is configured to this computer's virtual environment.

`--plain` retains the original PC437 text mode without Pillow or the border. `--columns` and `--replace` apply only in plain mode.

## Mobile web app

A mobile visitor app and Cloudflare Access protected event-team panel are now included. See [WEB-APP.md](WEB-APP.md) for setup, tunnel configuration, background services, queue recovery, and tests. Start with `.venv/bin/python run_web.py` after configuring `.env`. The existing printing commands above remain available.

## Event reliability and recovery

See [OPERATIONS.md](OPERATIONS.md) for Linux deployment, scheduled SMS alerts, safe queue recovery, backups/restore, and the event rehearsal.
