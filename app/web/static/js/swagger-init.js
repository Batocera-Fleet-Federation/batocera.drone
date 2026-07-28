"use strict";

window.addEventListener("load", () => {
  const script = document.currentScript || Array.from(document.scripts).find((item) => item.dataset.openapiUrl);
  const openapiUrl = script?.dataset.openapiUrl || "/v1/api/openapi.json";
  window.SwaggerUIBundle({
    url: openapiUrl,
    dom_id: "#swagger-ui",
    deepLinking: true,
    presets: [window.SwaggerUIBundle.presets.apis],
  });
});
