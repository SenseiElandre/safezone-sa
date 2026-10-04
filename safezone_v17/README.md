SafeZone SA v15

Updates:
- Fixed Safety Map tile layout by adding a Leaflet CSS fallback and hardened map sizing.
- Kept v14 Trusted Circle and live-location features.
- Mobile map height is increased for reliable rendering.


## v16 Map rendering fix
The Safety Map now uses self-contained Leaflet layout CSS with fixed 256px tile dimensions and protection against global image/CSS rules resizing Leaflet tiles. It also refreshes map size after mobile resize/orientation/visibility changes.


## v17 Map rebuild
Replaced Leaflet tile rendering with an OpenStreetMap embed to avoid tile-layout conflicts. GPS still updates the embedded map to the current position.
