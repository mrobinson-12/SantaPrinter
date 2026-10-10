# NT-8360 network capabilities

Researched 9 October 2026 for the printer at 192.168.1.100:9100.

This is the documented command inventory found in NETUM's supplied manual and Android NETDEMO SDK, plus read-only checks of this unit. It is not proof that every documented command is implemented in this unit's firmware. No printing, cutting, buzzer, cash-drawer, persistent storage, or configuration commands were tested during this research.

## Confirmed on this printer

- TCP port 9100 accepts connections and returns ESC/POS real-time status bytes.
- DLE EOT 1 returned 0x16: online; drawer connector pin 3 signal high. A high signal alone does not establish whether a drawer is open without knowing its wiring.
- DLE EOT 2 returned 0x12: cover closed, no paper-end stop, no reported error.
- DLE EOT 3 returned 0x12: no cutter, unrecoverable, or temperature/voltage error flags.
- DLE EOT 4 returned 0x12: no near-end or paper-end flags. Near-end sensing still depends on an installed sensor.
- TCP port 80 serves an Ethernet WebSet System configuration form.
- GET /printerConfig.js returned network-module version 1.0.0, IP 192.168.1.100, subnet mask 255.255.255.0, gateway 192.168.1.1, and a MAC address. This version is not necessarily the print engine firmware version.
- The web form exposes IP, subnet mask, gateway, and MAC edits through POST config.cgi. Writes were not attempted.
- Port 515 refused a connection; ports 443, 631, and 23 timed out. These observations do not conclusively establish which protocols the firmware supports. SNMP/UDP was not tested.

## Documented print and control commands

ESC=0x1B, GS=0x1D, FS=0x1C, DLE=0x10. Parameters and image data follow command prefixes; do not send incomplete commands. Consult the supplied programming manual for ranges and packet formats.

| Feature | Command family | Notes |
|---|---|---|
| Print text and line feed | LF | Text uses the selected encoding/code table. |
| Horizontal tabs and tab stops | HT, ESC D | Useful for column layouts. |
| Initialize printer | ESC @ | Clears pending print-buffer data and resets print modes; not a factory reset. |
| Character right spacing | ESC SP | Set extra spacing after characters. |
| Font and combined print modes | ESC !, ESC M | Fonts A/B; combined emphasis, width, height, underline. |
| Character scaling | GS ! | Independent width and height scaling, up to 8x according to manual. |
| Underline | ESC - | Off, one-dot, two-dot modes. |
| Emphasis / double strike | ESC E, ESC G | Bold-style output. |
| White-on-black text | GS B | Reverse printing mode. |
| Rotation | ESC V | 90-degree clockwise character rotation. |
| Upside-down printing | ESC { | Inverted text mode. |
| Code pages | ESC t | Printer's built-in character sets; not arbitrary UTF-8. |
| Custom glyphs | ESC &, ESC %, ESC ? | Define, enable/disable, and cancel user-defined characters. |
| Absolute / relative print position | ESC $, ESC backslash | Position subsequent content on a line. |
| Alignment | ESC a | Left, center, right. |
| Left margin | GS L | Set print area's left margin. |
| Print-area width | GS W | Present in SDK; not separately described in the supplied manual. |
| Line spacing | ESC 2, ESC 3 | Default or custom spacing. |
| Feed paper by distance | ESC J | Prints buffered content and feeds paper. |
| Feed paper by lines | ESC d | Prints buffered content and feeds a chosen line count. |
| Partial cut | GS V, ESC i, ESC m | Manual specifies partial cut; do not assume a full-cut mechanism. |
| Bitmap images | ESC * | Column/bit-image formats. |
| Raster images | GS v 0 | Monochrome bitmap printing; can print logos, diagrams, or rendered Unicode text. |
| Persistent logo storage | FS q | Define nonvolatile bit images. Manual cautions about limited write endurance and possible replacement of stored images. |
| Print stored logos | FS p | Print a stored NV image with supported scaling. |
| Native 1D barcodes | GS k | UPC-A/E, EAN/JAN13/8, Code39, ITF, Codabar, Code93, Code128. |
| Barcode appearance | GS H, GS f, GS h, GS w, GS x | Human-readable label placement/font, bar height, bar width, left offset. |
| QR codes | ESC Z | NETUM SDK command: 1B 5A followed by version, error correction, module size, data length, data. Do not assume Epson GS ( k support. |
| Hanzi mode | FS &, FS . | Enable/disable built-in Chinese character mode. |
| Hanzi formatting | FS !, FS -, FS S, FS W | Size, underline, spacing, quadruple size. |
| Beep | ESC B | Count and duration parameters; requires buzzer hardware. |
| Cash-drawer pulse | ESC p, DLE DC4 | Operate connector pin 2 or 5; requires compatible connected drawer. |
| Feed-button control | ESC c 5 | Enable/disable panel buttons. |
| Self-test print | 1F 11 04 | Vendor command in NETDEMO SDK, POS_Set_PrtSelfTest; not tested. |
| Real-time status | DLE EOT 1–4 | Printer, offline causes, errors, paper sensors; confirmed on this unit. |

## What applications can build from these features

- Formatted receipts, kitchen tickets, labels on continuous receipt paper, order summaries, and lists.
- Printed URLs, payment links, Wi-Fi details, or other QR payloads.
- Logos, monochrome photos, and arbitrary fonts/languages rendered into raster images on the computer. Unicode coverage depends on installed fonts; images are monochrome.
- Before/after status checks and periodic status polling. Polling can report changes without a completed-job acknowledgement.
- A software print queue, job log, or web printing API on the computer. These are application features, not confirmed printer-native services.

The existing print_nt8360.py implements only basic text, wrapping, feed, partial cut, and TCP sending. The features listed above are potential extensions, not already implemented script flags.

## Unverified or unavailable guarantees

- A status reply does not acknowledge completion of a specific receipt. DLE EOT is real-time and can respond while earlier print work is pending.
- No verified job IDs, receipt history, printed job count, cancellation of a specific queued job, or durable job-completion acknowledgement.
- No verified raw-TCP commands for changing IP, DHCP, factory reset, firmware upload, speed, darkness, print-head temperature measurement, or voltage measurement. Error flags are not numeric sensor readings.
- Automatic status back (GS a) is a common ESC/POS feature, but it was not established for this unit. Polling is confirmed; unsolicited status reporting is not.
- NETUM's product listing advertises PDF417, but the supplied manual/NETDEMO command inventory reviewed here does not provide its native command. PDF417 can alternatively be rendered as an image.
- No verified IPP/AirPrint, LPR, HTTPS, Telnet, SNMP, or direct PDF/PostScript interpretation. PDFs must be rendered to printer-compatible images or handled by a suitable installed driver.
- Web configuration controls were observed but not tested. The observed page has no DHCP, print-density, print-speed, or firmware-update field.

## Sources

- [NETUM NT-8360 product specifications](https://www.netum.net/products/nt-8360-thermal-printer).
- [NETUM official manual/SDK download page](https://support.netum.net/hc/en-us/articles/44800664000795--All-Models-Receipt-Printer-Software-Drivers).
- [Officially linked 80MM manual and SDK archive](https://cdn.shopify.com/s/files/1/2144/8019/files/80MM_Thermal_Printer_Manual_SDK-8360.zip?v=1710728732), including the programming manual and Android SDK NETDEMO Command.java / PrinterCommand.java / PrintDemo.java. The demo connects over port 9100.
- [Epson DLE EOT reference](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/dle_eot.html), used to cross-check status framing and real-time semantics.
- Direct read-only TCP/status and HTTP GET checks on 192.168.1.100.
