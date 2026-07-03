Build 4.53 — Unicode-safe NAS paths

- Fixes NAS worker embedding for album/artist folders containing accented characters such as Zoë.
- The Mac app normalizes mapped worker paths to Unicode NFC before sending requests.
- The bundled NAS worker resolves path components by Unicode-equivalent names, so composed/decomposed folder names work across macOS SMB and Synology/Linux.

