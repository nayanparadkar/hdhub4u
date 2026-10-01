# HDHub4U Media Downloader

A command-line tool that searches the live HDHub4U site, walks the
download gate a title puts in front of a file, and transfers the
result.

A download is only started once the bytes behind a link have been
checked. The site lists mirrors that are really further HTML pages,
so every candidate is read for a 4 KB prefix and accepted only if that
prefix is a real media container. An ad page is not a film, and this is
what stops one being saved with a `.mkv` name.

For personal use with content you have the right to download.

## Install

```bash
git clone <this repository>
cd media-downloader
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.11 or newer. No browser is required; the live path is plain
HTTP.

Pages that only exist after client-side script runs need one. Install
the optional extra and its browser:

```bash
pip install -e ".[browser]"
playwright install chromium
```

Everything keeps working without it; the browser paths just report
that a browser is needed.

## Usage

### Interactive

```bash
hdhub4u              # search the site, then pick and download
hdhub4u browse dune  # start with the query filled in
```

Type a name, pick a result, pick a quality, confirm, and the file lands
in `downloads/`. `n` goes to the next page of results, `b` starts a new
search, `q` quits.

### Scriptable

```bash
hdhub4u search "big boss"           # list matches, no prompts
hdhub4u search "big boss" --limit 5
hdhub4u status                      # where data lives, what is reachable
hdhub4u links prune                 # drop cached resolutions
```

Exit codes are `0` on success (including a deliberate quit), `1` when
nothing was found or a step failed, and `130` on interrupt, so a
wrapper script can tell the outcomes apart.

Run `hdhub4u --help` for the full list.

## JavaScript-only pages

Some pages return a shell to a plain HTTP fetch, so the title comes
back empty. With the `browser` extra installed, the indexer can render
each page in a real browser instead:

```bash
hdhub4u index --browser
hdhub4u render <url>                     # what the rendered DOM holds
hdhub4u render <url> --screenshot out.png
hdhub4u render <url> --selector ".links" # wait for a selector first
```

`render` reports what the page advertises. It does not click through a
gate, wait out a countdown, or read a token out of script state; those
pages are answered with a clear "this needs a browser" message instead.

## The offline index

`hdhub4u index` crawls the site into a local SQLite snapshot, and
`hdhub4u search --local` queries that. It is kept as a fallback and
for the crawler; it is only correct up to the moment it was built, so
it is not the default. An unbuilt index reads as "no matches" rather
than an error.

## How a download resolves

Sites gate links differently, so each gate is a separate strategy in
`unlock.py` rather than a branch in one function:

```
option -> gate page -> encoded address -> (maybe a mirror listing)
       -> probe each mirror -> first one whose bytes are media
       -> transfer with a .part file -> verify the byte count -> rename
```

Details that matter:

- **The probe is a real stream.** It reads at most 4 KB and closes the
  connection, so a mirror serving four gigabytes under a video content
  type costs one probe rather than four gigabytes of memory.
- **The bytes decide.** A container is recognised by its own layout
  (Matroska, ISO-BMFF, RIFF, ID3, or a quorum of transport-stream
  sync bytes at the 188-byte stride). An English advertisement starting
  with "G" is not a transport stream.
- **A `.part` file, verified.** The transfer is only renamed once the
  expected byte count has arrived, and an advisory lock stops two
  downloads of the same release from interleaving into one file.
- **Never the redirect target.** Signed query parameters are part of
  the authorization, so the original URL is used for the transfer.

## Layout

```
src/hdhub4u/
  __main__.py     argparse entry point and subcommands
  flow.py         the live search / pick / download flow
  catalog.py      live search, option extraction, host classification
  unlock.py       gate resolution and byte-level media verification
  downloader.py   streaming, resume, integrity checks
  browser.py      optional Playwright rendering for script-only pages
  project.py      filesystem locations
  indexer.py      crawler
  database.py     SQLite index
  search.py       fuzzy ranking over the local index
  parser.py       HTML parsing and title cleanup
  resolver.py     direct link resolution
  link_cache.py   resolved-link cache
  http.py         retry and backoff
  http_types.py   shared content-type rules
  ui.py           terminal rendering and status
  logging_setup.py stderr logging
  errors.py       error hierarchy
```

`data/media.db` holds the index, `data/links.json` the resolved-link
cache, and `downloads/` the media. Set `HDHUB_HOME` to relocate all
three.

## Development

```bash
python -m pytest                  # offline suite
python -m pytest -m live          # reaches the real site
python -m flake8 src/ tests/
python -m isort --check-only src/ tests/
```

Tests marked `live` touch the network and are excluded from the default
run. Logs go to stderr so they never corrupt the rendered tables; raise
verbosity with `--log-level DEBUG` or `HDHUB_LOG_LEVEL=DEBUG`.
