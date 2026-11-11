"""Browser policy for untrusted artwork, including top-level navigation."""

# Artwork can come from uploads, scrapers, peers, or existing files. Do not
# inherit the application document's script permissions, even for cache hits
# or files whose extension claims they are passive images. Sandbox without
# allow-scripts/allow-same-origin also isolates active SVG/HTML documents.
# Inline styles and embedded images preserve ordinary SVG rendering; this
# policy does not change image bytes used by the viewer or canvas cropper.
MEDIA_CONTENT_SECURITY_POLICY = (
    "sandbox; default-src 'none'; script-src 'none'; "
    "style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
