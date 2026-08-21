# Brand Icons

Since Home Assistant 2026.3.0 the integration ships its own brand images in
`custom_components/smart_battery_pilot/brand/` (icon.png 256×256,
icon@2x.png 512×512, logo.png 256×80, logo@2x.png 512×160). Local brand images
override the brands CDN — no submission to `home-assistant/brands` is needed
(the brands repo no longer accepts custom integration icons, see
https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).

Source artwork: `assets/icon.svg` (square battery mark) and `assets/logo.svg`
(mark + wordmark). Regenerate with `rsvg-convert`:

```sh
cd assets
rsvg-convert -w 256 -h 256 icon.svg -o icon.png
rsvg-convert -w 512 -h 512 icon.svg -o icon@2x.png
rsvg-convert -w 256 -h  80 logo.svg -o logo.png
rsvg-convert -w 512 -h 160 logo.svg -o logo@2x.png
cp icon.png icon@2x.png logo.png logo@2x.png ../custom_components/smart_battery_pilot/brand/
```
