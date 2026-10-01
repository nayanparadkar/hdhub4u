# HDHub4U Media Downloader

A command-line tool that crawls a media catalog into a local SQLite
index, searches it, and downloads a chosen quality.

Only links that already serve a direct media file are downloaded. This
tool does not bypass interstitials, paywalls, or access controls on any
site.

## Features

- Local SQLite index, so searches are instant and work offline
- Fuzzy ranking across substring, prefix, and edit-distance matches
- Quality selection, including an automatic "smallest available" pick
- Resumable downloads with `.part` files and byte-count verification
- Redirect following with media content-type validation
- Pluggable link resolution behind a `LinkResolver` protocol
- On-disk cache of resolved links with a one-hour TTL
- Retry with exponential backoff on transient failures
- Interactive menu plus scriptable subcommands

## Install

```bash
cd hdhub4u
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

For development tooling:

```bash
pip install -e ".[dev]"
```

## Usage

### Interactive

```bash
hdhub4u
```

The menu offers search, browse, index rebuild, status, and quit.

### Scriptable

```bash
hdhub4u search "big boss" --limit 10
hdhub4u index
hdhub4u status
hdhub4u links prune
```

Run `hdhub4u --help` for the full list.

## Layout

```
src/hdhub4u/
  __main__.py     argparse entry point and subcommands
  cli.py          interactive flow
  project.py      filesystem locations
  indexer.py      crawler
  database.py     SQLite index
  search.py       fuzzy ranking
  parser.py       HTML parsing and title cleanup
  quality.py      quality parsing and selection
  resolver.py     link resolution strategies
  link_cache.py   resolved-link cache
  downloader.py   streaming, resume, integrity checks
  http.py         retry and backoff
  http_types.py   shared content-type rules
  inspector.py    arbitrary URL inspection
  ui.py           terminal rendering
  logging_setup.py stderr logging
  errors.py       error hierarchy
```

`data/media.db` holds the index, `data/links.json` the resolved-link
cache, and `downloads/` the media. Set `HDHUB_HOME` to relocate all
three.

## Extending resolution

Sites gate links differently, so strategies are separate classes
rather than branches in one function:

```python
from hdhub4u.resolver import build_resolver

resolver = build_resolver(strategies=[MyStrategy()])
result = resolver.resolve(url)
```

A strategy returns a `ResolveResult` with `is_media=False` when it
cannot help; it should not raise. The chain returns the first success,
or the first result when all strategies decline.

Two rules worth keeping:

- Never download from a resolved redirect target. Signed query
  parameters are part of the authorization and are lost once a
  redirect is followed. Use the original URL.
- Treat a `200` carrying `text/html` as a failure. That response is an
  interstitial, not a media file.

## Development

```bash
python -m pytest
python -m flake8 --max-line-length=79 src/ tests/
python -m isort --check-only src/ tests/
```

Logs go to stderr so they never corrupt the rendered tables. Raise the
verbosity with `HDHUB_LOG_LEVEL=DEBUG` or `hdhub4u --log-level DEBUG`.

## License

For personal use with content you have the right to download.
