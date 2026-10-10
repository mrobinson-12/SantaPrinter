# AI Santa replies on your receipt printer

Installed script: `~/SantaPrinter/santa_reply.py`. It uses exactly `gpt-6-luna` through the OpenAI Responses API, then prints the generated reply inside your Santa border on `192.168.1.100:9100`.

```sh
~/SantaPrinter/santa_reply.py "I am Michael and I want a cheeseburger for Christmas"
```

Or just run `~/SantaPrinter/santa_reply.py` and enter your message at the prompt. If `OPENAI_API_KEY` is not set, interactive runs ask for your API key privately without displaying or saving it. You can also set the environment variable in your terminal before running. Never put the key inside either Python script.

Preview only (still calls the API, but does not print):

```sh
~/SantaPrinter/santa_reply.py "I am Michael and I want a cheeseburger for Christmas" --preview ~/SantaPrinter/santa-preview.png
```

Read a visitor message from a file:

```sh
~/SantaPrinter/santa_reply.py --message-file wishes.txt --font-size 14
```

Every reply is saved as `~/SantaPrinter/santa-last-response.txt` by default, before the printer connection. This file is overwritten on the next successful generation. Use `--save-reply another-file.txt` to retain separate replies. Reprint without another API call:

```sh
~/SantaPrinter/santa_reply.py --reply-file ~/SantaPrinter/santa-last-response.txt
```

The prompt targets 35–50 words including greeting and signature, refers to the person's name and wish, adds a small relevant festive detail, and asks for warm, varied, gentle humour. It avoids invented facts and promises of specific presents. A random style hint helps vary the replies. Responses over 60 words or 600 characters are rejected before printing rather than truncated.

Font size defaults to 12 pt with the existing automatic shrinking and 18 pt maximum. Your original `~/SantaPrinter/print_nt8360.py` remains available for printing your own exact text. Keep both scripts and `santa-receipt-border.png` together on another machine; install Pillow in a Python virtual environment as described in README.md.

No live API generation was tested during setup because this environment did not have an API key configured. Request construction, simulated response parsing, error handling, preview generation, and a local TCP print transfer were tested offline. Physical raster printing remains unverified.

References: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) and [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create).

## Response-only testing

Use `--output-only` to show the model response without rendering, printing, or saving a default letter. This mode bypasses the letter-length checks to expose prompt failures. See SANTA-STRESS-TESTS.md for the 28-case test suite and runner. Ordinary printing and preview still enforce the short-letter limit.
