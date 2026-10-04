# Adding your own apps to Omnarr

Omnarr supports a set of apps out of the box (see the README). For anything else, there are
three ways to plug in your own stack. They're listed from no code to full control.

| | What it does | Needs code? |
|---|---|---|
| [Custom library](#1-custom-library) | Adds items from any URL that returns JSON | No (any script, n8n, or an app's API) |
| [Custom requests](#2-custom-requests) | Sends requests for the formats you pick to your URL | No |
| [Plug-ins](#3-plug-ins) | A Python file with its own settings, library and requests | Yes (a little Python) |

All three use the same **item** format.

## The item format

```json
{
  "id": "unique-and-stable",
  "kind": "book",
  "title": "Wool",
  "format": "audiobook",
  "authors": ["Hugh Howey"],
  "series": "Silo",
  "series_index": 1,
  "year": 2012,
  "description": "…",
  "genres": ["Science Fiction"],
  "url": "https://your-app/item/123",
  "cover_url": "https://your-app/covers/123.jpg",
  "duration": 32400,
  "added": "2026-10-01",
  "progress": 0.4,
  "finished": false,
  "rating": 4.2,
  "adult": false,
  "ids": {"isbn": "…", "asin": "…", "tmdb": "…", "imdb": "…", "tvdb": "…"}
}
```

- **Required:** `id` and `title`.
- **`kind`:** one of `book`, `movie`, `show`, `comic` or `game` (default `book`).
- **`format`:** one of `ebook`, `audiobook`, `readalong`, `movie`, `series`, `comic` or `game`. It defaults from the kind.
- **Grouping:** items are grouped with what Omnarr already knows. A book from your app with the same title and author as a Calibre book becomes one work with both formats. `ids` make matching exact (e.g. a `tmdb` id for movies and shows).
- **`url`:** where "Open in…" goes.
- **`cover_url`:** fetched by Omnarr's server (with your header, if set) and cached, so it never needs to be reachable from people's browsers.
- **`adult: true`:** puts the item behind the private-section PIN.

## 1. Custom library

In **Settings → Connections → Custom library (JSON)**, enter a URL that returns:

```json
{"items": [ITEM, ITEM, …]}
```

A plain list works too. You can add one header for authentication, for example
`Authorization` / `Bearer <token>`. Omnarr re-reads the URL on every index build (every 15
minutes by default). **Test** shows how many items it found and how many it skipped.

Example: a tiny script served by any web server, a Home Assistant template, an n8n workflow
that reshapes another app's API, or a static `items.json` file.

## 2. Custom requests

In **Settings → Connections → Custom requests (webhook)**, enter a URL and pick the formats
to send there: `movie`, `tv`, `ebook`, `audiobook`, `comic`, `game`. Requests for those
formats go to your URL instead of Seerr, Shelfmark or ROMarr. Approvals still apply first.
Omnarr sends a `POST` with JSON:

```json
{"event": "request", "format": "audiobook", "title": "Wool", "authors": ["Hugh Howey"],
 "ids": {"tmdb": "…"}, "platform": "…", "work_id": "…", "requested_by": 3}
```

- Answer with any 2xx. Optionally answer `{"message": "Queued in MyApp", "id": "abc"}`, and Omnarr shows the message.
- Books, audiobooks and comics stay on Omnarr's **Books we're looking for** list until they turn up in your library.
- **Test** sends `{"event": "test"}`.

## 3. Plug-ins

A plug-in is one Python file in Omnarr's plug-ins folder. That's `/data/plugins` by default;
set `OMNARR_PLUGINS` to use another. Mount it, drop the file in, and restart Omnarr:

```yaml
    volumes:
      - ./data:/data            # plug-ins go in ./data/plugins
```

The plug-in then appears in **Settings → Connections** as "<label> (plug-in)", with the settings
it declares and a **Test** button.

```python
PLUGIN = {
    "key": "mylibrary",                 # letters, digits, _
    "label": "My library",
    "category": "Your own apps",
    "about": "One line shown in Connections.",
    "fields": [                          # same field types as the built-in apps
        {"key": "url", "label": "Address", "type": "url", "required": True},
        {"key": "api_key", "label": "API key", "type": "secret", "required": True},
    ],
    "requests": ["audiobook"],           # optional: formats this plug-in takes requests for
}

def read(settings):                      # -> [ITEM, …]   (optional)
    ...

def test(settings):                      # -> (ok, message) (optional)
    ...

def request(settings, item):             # -> {"message": …, "id": …} (optional)
    ...

def cover(settings, cover_url):          # -> bytes | None (optional, for covers that need auth)
    ...
```

- `settings` holds the saved values of your fields. Secrets are stored on the server and never sent back to browsers.
- A plug-in that fails to load is logged and skipped. It never stops Omnarr.
- If a plug-in's `read` raises, that source shows as an error on the status page, and the rest of the index is built as usual.
- A complete, working example: [`docs/plugins/example_folder_library.py`](plugins/example_folder_library.py).

**Security:** plug-ins run inside Omnarr with its permissions, including its API keys and
files. Only install plug-ins you've read and trust. Only someone with access to the server
can add one; there's no upload button for them, on purpose.

## Where requests go

For each format, Omnarr uses the first of these that applies:

1. a plug-in that lists the format in `requests`;
2. **Custom requests**, if the format is ticked there;
3. **ReadMeABook**, for audiobooks, when it's connected and "Send audiobook requests here" isn't `no`;
4. the built-in app: Seerr (movies, TV), Shelfmark (books, audiobooks, comics) or ROMarr (games).

"Choose a copy myself" lists Shelfmark's releases, so it only appears when Shelfmark is connected.
