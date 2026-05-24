from pathlib import Path
from urllib.parse import quote

try:
    from .route_config import API_PREFIX, api_url
except ImportError:
    from route_config import API_PREFIX, api_url  # type: ignore

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


UI_HTML = load_template("index.html")
SWAGGER_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ROM API Swagger</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.addEventListener("load", function () {{
      SwaggerUIBundle({{
        url: "{API_PREFIX}/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis]
      }});
    }});
  </script>
</body>
</html>
"""


class UiRoutesMixin:
    def _handle_root_html(self) -> None:
        self._send_html(200, UI_HTML)

    def _handle_static_file(self, relative_path: str) -> None:
        rel = str(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise FileNotFoundError()
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.exists() or not target.is_file():
            raise FileNotFoundError()
        self._stream_file(target, self._guess_content_type(target))

    def _handle_swagger_html(self) -> None:
        self._send_html(200, SWAGGER_HTML)

    def _handle_openapi_json(self) -> None:
        self._send_json(200, self.openapi_spec)

    def _handle_download_sitemap(self) -> None:
        if not self.settings.downloads_enabled:
            self._send_html(
                200,
                """<!doctype html><html><head><meta charset="utf-8"><title>ROM Download Sitemap</title></head>
<body><h1>ROM Download Sitemap</h1><p>Downloads are currently disabled by server configuration.</p></body></html>""",
            )
            return
        systems = self.repository.list_systems()

        sections = []
        total_links = 0
        for system in systems:
            system_name = system["name"]
            if str(system_name).strip().lower() == "steam":
                continue
            _, roms = self.repository.list_assets(system_name, "roms")
            downloadable = [rom for rom in roms if rom.get("is_downloadable", True)]
            if not downloadable:
                continue

            total_links += len(downloadable)
            links_html = "\n".join(
                (
                    f'<li><a href="{api_url("/systems/" + quote(system_name, safe="") + "/" + quote(rom["unique_id"], safe=""))}">{rom["name"]}</a></li>'
                )
                for rom in downloadable
            )
            sections.append(
                f"""
                <section class="system">
                  <h2>{system_name} <span class="count">({len(downloadable)})</span></h2>
                  <ul>
                    {links_html}
                  </ul>
                </section>
                """
            )

        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ROM Download Sitemap</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f6f7fb; color: #1f2937; }}
    .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .top {{ margin-bottom: 18px; }}
    h1 {{ margin: 0 0 6px; font-size: 1.7rem; }}
    .meta {{ color: #6b7280; }}
    .system {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; margin: 0 0 14px; }}
    .system h2 {{ margin: 0 0 10px; font-size: 1.1rem; }}
    .count {{ color: #6b7280; font-weight: 500; }}
    ul {{ margin: 0; padding-left: 20px; column-count: 2; column-gap: 24px; }}
    li {{ break-inside: avoid; margin: 0 0 6px; }}
    a {{ color: #0d6efd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    @media (max-width: 840px) {{ ul {{ column-count: 1; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>ROM Download Sitemap</h1>
      <div class="meta">Systems: {len(sections)} · Download links: {total_links}</div>
    </div>
    {"".join(sections) if sections else "<p>No downloadable ROM links found.</p>"}
  </div>
</body>
</html>"""
        self._send_html(200, body)
