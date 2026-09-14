# BookMyStay backend — the debug panel + all 3 flows (panel/CLI paste,
# Chrome extension page_data, WhatsApp webhook) share this one process.
#
# playwright's browser + its OS-level deps are the reason this is a real
# Dockerfile rather than relying on a PaaS's auto-detected build — those
# system libraries (fonts, X11 bits, etc.) are exactly what tends to be
# missing/unreliable with buildpack-style auto-detection.
FROM python:3.12-slim

WORKDIR /app

# System deps for lxml (libxml2/libxslt) — playwright's OWN system deps
# are installed separately below via `playwright install --with-deps`,
# which knows the exact package list for whatever Chromium version ships
# with the pinned playwright version.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + its OS-level dependencies (fonts, codecs, etc.) — must match
# the playwright version pinned in requirements.txt.
RUN playwright install --with-deps chromium

COPY yta/ ./yta/

# data/hotels.db (~1.2GB, gitignored) is NOT baked into the image — it's
# far too large for a container build and would bloat every deploy. It
# needs to live on a persistent Volume mounted at /app/data instead,
# uploaded once separately. Without it, TripJack hotel-name resolution
# degrades gracefully (packet.log("TripJack lookup skipped — data/hotels.db
# not built")) rather than crashing — the bot still replies, just always
# with "couldn't get a live rate" until the volume is attached.

# Railway injects PORT at runtime; yta/web.py already reads it (falls
# back to 8765 if unset, e.g. running this image anywhere else).
# -u (unbuffered) — without it, print()'d log lines sit in a buffer and
# never reach `docker logs` until it fills or the process exits, the
# exact same issue this project already hit once running locally.
CMD ["python", "-u", "-m", "yta.web"]
