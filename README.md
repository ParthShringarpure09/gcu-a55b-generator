# GCU A55B Generator — full app

Complete pipeline: address/waypoints → OS map fetch → route + annotations →
validated, submission-ready A55B PDF. One backend, one page, runs on your laptop.

## Run

    pip install -r requirements.txt
    export OS_DATA_HUB_KEY="your-project-api-key"    # optional — see below
    python app.py

Then open http://localhost:8000

(On Windows, use `set OS_DATA_HUB_KEY=your-key` instead of `export`.)

## The OS Data Hub key

- Sign up at https://osdatahub.os.uk, create an API Project, add the "OS Maps
  API", copy the Project API Key.
- The free OpenData plan is enough for this. Premium gives £1,000/month of free
  transactions before any charge — you won't reach it at 50 jobs/week.
- The key is read server-side from OS_DATA_HUB_KEY and is NEVER sent to the
  browser. This fixes the exposed-key problem in the original prototype, where
  the key sat in plaintext in the HTML.
- No key set? The app still runs — it falls back to the bundled sample map tile
  (Barbeth Way) so you can demo the whole flow. The result banner tells you
  which map source was used.

## What it does

1. Type an address → geocodes it, prefills a grid reference.
2. Enter job details, works items (code + metres + position), waypoints.
3. Live preview draws the route and auto-places annotations — same maths the PDF
   uses, so preview matches output.
4. "Generate A55B PDF" → the server fetches the OS tile in British National Grid
   (EPSG:27700, so placement is linear — no reprojection), draws everything,
   validates the rate codes, and returns the PDF.
5. Bad rate codes (e.g. a carriageway code on a footway item) are rejected with
   a clear message before any PDF is made.

## Files

    app.py                  FastAPI backend — routes, geocode, tile fetch, generate
    engine/a55b.py          PDF layout engine (tested)
    engine/validate.py      domain rules — surface/code bands, length checks
    engine/os_tiles.py      server-side OS Data Hub tile fetch (EPSG:27700)
    static/index.html       the form + live preview
    sample_map_tile.png     fallback map for demoing without a key

## Known limits (honest list)

- OS tile fetch is written against OS's current WMTS spec and reads their zoom
  levels live, but has not been run against a live key from inside the build
  environment (no external network there). First real run may need a small
  tweak — the code prints clear errors if so.
- Geocoding uses public Nominatim; OS Places API is more accurate for UK
  addresses and worth swapping in for production.
- Header/footer match the Openreach layout structurally but aren't yet
  pixel-matched (fonts, logo, exact rule weights).
- No database / login yet — that's the next block from the handover.
