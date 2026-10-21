"""
Grid explorer: on-demand, fully traced fire-danger-index computation for any
set of AORC 800 m cells over any time window, served to the MapLibre page in
``website/explorer.html``.

Vocabulary used throughout this package (and in the web page):

    cell    one AORC grid cell: 1/120 deg x 1/120 deg (~800 m x ~600-900 m at
            46-48 N). Identified by ``point_id`` in ``points_index.csv``.
    brick   one Zarr storage chunk of the AORC archive: 144 h x 128 lat x
            256 lon. This is the smallest unit S3 actually serves, so it is
            the unit this package fetches and caches.
    packet  one cell at one hour -- the smallest *scientific* unit of the
            dataset (one row of the extracted CSVs = one cell, one column =
            one packet-time). A time window of N "packets" for a cell is N
            consecutive hours.

The science lives in ``aorc_tools.fire_indices`` and ``aorc_tools.climate_convert``
and is *called*, never re-implemented, here. This package only adds tracing
(recording every intermediate), windowing, caching and serving.
"""
