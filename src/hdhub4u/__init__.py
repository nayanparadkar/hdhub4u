"""HDHub4U media downloader.

Searches the live site for a title and transfers the file behind the
link it picks.

Entry points:
    hdhub4u                    interactive search
    hdhub4u dune               the same, with the query filled in
    hdhub4u search "title"     non-interactive search, as a table or
                               as plain records or JSON
    hdhub4u status             data locations and what is reachable

Maintenance commands (``index``, ``links``, ``render``) are hidden from
``--help`` and documented in the README.
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
