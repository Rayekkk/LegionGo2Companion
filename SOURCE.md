# Frontend source and relinking

The generated `dist/index.js` includes code from `@decky/api` 1.1.3 under
LGPL-2.1. `npm run package` includes its full package source in
`THIRD_PARTY_SOURCES/decky-api-1.1.3/` and its license in
`THIRD_PARTY_LICENSES/decky-api-LGPL-2.1.txt` inside the ZIP archive. These
generated directories are not part of the Git checkout.

The source and build inputs for the combined frontend are included in the
release archive as `src/`, `rollup.config.js`, `tsconfig.json`, `package.json`
and `package-lock.json`. With Node.js 18 or newer, rebuild the bundle with:

```bash
npm ci
npm run build
```

This permits replacing or modifying `@decky/api` and relinking the frontend.
The combined project source is available at
https://github.com/Rayekkk/LegionGo2Companion. Its public standalone source bases
are available at https://github.com/Rayekkk/LeGoTDP,
https://github.com/Rayekkk/LeGo-Vibe-Control and
https://github.com/Rayekkk/LeGo2BrightnessFix. The Wi-Fi module also incorporates
Rayekkk's unpublished WiFi Optimizer Go 2 work; its source is included here.

The RGB page and Go 2 lighting interface were developed with reference to
HueSync 3.9.0 at commit `575fba4acd054f00629e5998eafef30169ee2005`:
https://github.com/honjow/HueSync. HueSync is BSD-3-Clause; see
`LICENSE.HUESYNC`. The joystick-ring ABI is provided by Linux's
`hid-lenovo-go` driver and is not redistributed by this archive.

The Button Remapper communicates with the InputPlumber D-Bus interface shipped
by SteamOS. InputPlumber is not bundled or modified by this archive. Its source,
profile schema and D-Bus documentation are available under GPL-3.0-or-later at
https://github.com/ShadowBlip/InputPlumber. The live target used version 0.78.0.
