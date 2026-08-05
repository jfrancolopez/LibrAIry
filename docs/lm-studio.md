# Using LM Studio on your LAN

LM Studio turns a machine with a good GPU into a local AI server. LibrAIry talks
to it over your network, so **no data leaves your LAN** — LM Studio is treated as
a local provider, exactly like Ollama, with no API key and no cloud opt-in.

## 1. On the GPU machine (the one with the RTX 5080)

1. Install **LM Studio** from [lmstudio.ai](https://lmstudio.ai).
2. Go to the **Discover** (search) tab and download a model — see the table below.
3. Open the **Developer** tab (older builds call it *Local Server*).
4. Load the model, then **Start Server**.
5. Turn on **Serve on Local Network**. This is the step people miss — without it
   the server only listens on `localhost` and LibrAIry cannot reach it.
6. Note the port (default **1234**) and the machine's IP. On Windows run
   `ipconfig`; on macOS/Linux run `ip addr` or `ifconfig`. It looks like
   `192.168.1.50`.

### Which model to use on a 16 GB card

A 5080 has 16 GB of VRAM, which fits a 14B model comfortably at 4-bit. LibrAIry
only asks the model to return a small JSON object describing one file, so
instruction-following matters far more than size.

| Model (search this in LM Studio) | Quant | Why |
| --- | --- | --- |
| **`qwen2.5-14b-instruct`** | Q4_K_M | **Recommended.** Best accuracy that still fits 16 GB with room for context. |
| `qwen2.5-7b-instruct` | Q4_K_M / Q5_K_M | Noticeably faster, still very good at structured JSON. Pick this if you want to churn through a big inbox. |
| `llama-3.1-8b-instruct` | Q4_K_M | Fine alternative if you already have it. |

Avoid "thinking"/reasoning-heavy variants — they burn tokens narrating before
answering, and LibrAIry only wants a short JSON verdict.

Copy the model's identifier exactly as LM Studio shows it (for example
`qwen2.5-14b-instruct`) — that string is what you paste into LibrAIry.

## 2. In LibrAIry

Open **Settings → AI providers → LM Studio (local, on your LAN)** and fill in:

- **IP or URL** — just the IP is enough: `192.168.1.50`. LibrAIry fills in
  `http://` and `:1234` for you. Use `192.168.1.50:5000` if you changed the port.
- **Model** — the identifier from LM Studio, e.g. `qwen2.5-14b-instruct`. You do
  not have to know it in advance: press **Test connection** and LibrAIry lists
  the models that server has actually loaded, so you can click one.

Press **Test connection** first, then **Save LM Studio**. No restart is needed.

Prefer environment variables instead? Set these in `.env` and recreate the
container:

```
LMSTUDIO_HOST=192.168.1.50
LMSTUDIO_MODEL=qwen2.5-14b-instruct
```

The Settings value wins over the environment variable when both are set.

## 3. Test it

Press **Test connection**, right under the two boxes in Settings. It probes the
address you have typed *without saving it*, so a wrong IP never has to be
committed as configuration before you find out it is wrong.

- **reachable** — the loaded models are listed as buttons. Click one to fill the
  model box. If the box already names a model that server has not loaded,
  LibrAIry says so: that combination looks healthy but makes every single file
  wait out the full AI timeout and return nothing.
- **unreachable** — the message names the likely cause:
  - *Connection refused* → nothing is listening there. Start the server in the
    Developer tab, and check the port.
  - *timed out* → most often **Serve on Local Network** is off, so the server is
    running but only answers its own machine. Otherwise a firewall is blocking
    the port; on Windows, allow LM Studio through for **Private** networks.
  - *does not resolve* → use an IP rather than a hostname.
  - *no route* → if LibrAIry is in Docker, check the container can see your LAN.

The `lmstudio` row on the **Health** page still works and tests the *saved*
configuration, which is the one analysis actually uses.

## 4. Make it the preferred provider

AI runs only when catalogs and heuristics are not confident enough, and
providers are tried in order. To try LM Studio before anything else, set the
order in **Settings → AI providers → Provider kind order**:

```
lmstudio,ollama,openai,anthropic,gemini
```

## What LibrAIry sends

The same redacted view every provider gets: file name, extension, size bucket,
media details, embedded tags, sibling file names, and folder hints. **Never**
absolute paths, GPS coordinates, API keys, or file contents. Because LM Studio
runs on your own network, none of it leaves your home.
