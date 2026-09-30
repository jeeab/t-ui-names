# T-UI place names

Offline place-name search for the [T-UI](https://jeeab.github.io/t-ui/) launcher on the LilyGo T-Deck.

- `names/<lat>/<lon>.tnm` - one file per 1x1 degree square of the USA and Europe (lat/lon = the
  south-west corner). The T-Deck fetches the squares covering an area whenever you download maps
  for it on the device, then searches them with no signal at all.
- `names/cities.tnm` - world cities of 15,000+ people, searchable from anywhere.
- Whole-region downloads (USA, Europe) for copying straight onto the SD card are on the
  [Releases](https://github.com/jeeab/t-ui-names/releases) page: unzip into the root of the card so
  you get a `names` folder.

Built by `build_names.py` in the T-UI project. Format: TNM1 (documented in that script).

## Sources and licences

- **USA:** [USGS Geographic Names Information System](https://www.usgs.gov/tools/geographic-names-information-system-gnis) - public domain.
- **Europe and world cities:** [GeoNames](https://www.geonames.org/) - licensed under
  [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/). Converted to
  this format; no other changes to the data.
