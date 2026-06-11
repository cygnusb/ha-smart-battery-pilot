# Brand Icons

Since Home Assistant 2026.3.0 the integration ships its own brand images in
`custom_components/smart_battery_pilot/brand/` (icon.png 256×256,
icon@2x.png 512×512, logo variants). Local brand images override the brands
CDN — no submission to `home-assistant/brands` is needed (the brands repo no
longer accepts custom integration icons, see
https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).

Source artwork: `assets/logo.svg`. Regenerate PNGs on macOS via:
`qlmanage -t -s 512 -o . logo.svg` (512×512) and `sips -z 256 256` for resizing.
