"""HDHub4U media downloader.

A CLI that crawls a media catalog into a local SQLite index, searches
it, and downloads selected qualities from the results.

Entry points:
    hdhub4u                    interactive search
    hdhub4u search "title"     non-interactive search
    hdhub4u index              rebuild the local index
    hdhub4u status             index size and data locations
    hdhub4u links prune        drop expired cached links
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
