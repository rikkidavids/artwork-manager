Build 4.57 — Qt search and approve prototype

- Scan / Resume checks multiple album folders in parallel, which should be much faster on NAS/SMB libraries.
- Added a Settings control for how many album folders can be checked at once; the default is 8.
- Normal resume scans skip unchanged saved album folders by path/fingerprint, while still re-reading folders whose music files changed.
- Older saved queues record scan fingerprints during the next resume scan so later scans can detect changes cheaply.
- MacBook-class layouts now give more width to artwork review and allow larger cover previews.
- Removed generated Python bytecode files from version control.
- The Qt prototype now supports Find Artwork and Approve + Embed for saved artwork candidates, with progress, optional backups, and post-embed verification.

Qt prototype branch

- A PySide6 queue/review prototype is available on the qt-prototype branch.
- It can browse the queue, preview current/candidate artwork, Find Artwork for the selected album, and Approve + Embed saved candidates.
- Install prototype dependencies with: python3.11 -m pip install -r requirements-qt.txt
- Run it with: python3.11 -m artwork_manager_app.run_qt_app
- Or double-click Run Qt Prototype.command to create/update its Qt environment and launch it.
- The stable Tk app remains the full app for Scan, Convert/Save, bulk maintenance, and NAS worker settings.
