Build 4.68 — Qt scan and settings migration

- Scan / Resume checks multiple album folders in parallel, which should be much faster on NAS/SMB libraries.
- Added a Settings control for how many album folders can be checked at once; the default is 8.
- Normal resume scans skip unchanged saved album folders by path/fingerprint, while still re-reading folders whose music files changed.
- Older saved queues record scan fingerprints during the next resume scan so later scans can detect changes cheaply.
- MacBook-class layouts now give more width to artwork review and allow larger cover previews.
- Removed generated Python bytecode files from version control.
- The Qt review app now supports Find Artwork and Approve + Embed for saved artwork candidates, with progress, optional backups, and post-embed verification.
- Polished the Qt filter dropdown and scrollbars so the modern UI does not fall back to chunky default controls.
- Replaced old stock action icons with custom line icons, added clearer button hierarchy, and softened the queue/candidate list styling.
- Cleaned label backgrounds in the queue header and added draggable, remembered album-list column widths with a roomier Qt split.
- After a successful Qt approve/embed, the review pane advances to the next actionable queue item.
- Long selected album names now truncate instead of pushing the queue/artwork layout around.
- The Qt queue/review divider is remembered, and Backup before embed moved out of the bottom action row.
- Added clickable queue count filters for the main review buckets, with saved search/filter state.
- Added Qt keyboard flow for Cmd-F queue search, F Find Artwork, A Approve + Embed, and N next actionable album.
- Artwork preview canvases now stay square, and review copy/log output is quieter for a cleaner modern layout.
- Removed the duplicate Qt queue filter dropdown; all queue views now use the count chips.
- Simplified Qt queue filters to four workflow views: Needs Work, Review, Done, and All.
- Removed old visible chrome from the Qt review window and simplified the toolbar around direct workflow actions.
- Empty queue views now clear stale album details and show a plain hint instead of leaving old review text behind.
- Searching from Needs Work now follows albums into Review when artwork options are found, so the find/check/approve flow stays continuous.
- Search logs now stay attached to the album they came from, and queue status labels use shorter text without badge backgrounds.
- Added native Qt Scan Library, using the same threaded scanner and incremental NAS-friendly resume logic as the existing app.
- Added a native Qt Settings dialog for artwork rules, provider switches, approval defaults, scan concurrency, and NAS worker mapping.
- The Qt toolbar now uses direct Refresh, Scan Library, and Settings actions instead of handing routine work to the old window.

Qt review branch

- A PySide6 queue/review app is available on the qt-prototype branch.
- It can scan the library, manage core settings, browse the queue, preview current/candidate artwork, Find Artwork for the selected album, and Approve + Embed saved candidates.
- Install Qt review dependencies with: python3.11 -m pip install -r requirements-qt.txt
- Run it with: python3.11 -m artwork_manager_app.run_qt_app
- Or double-click Run Qt Prototype.command to create/update its Qt environment and launch it.
- Or double-click Update and Run Artwork Manager.command to pull the latest qt-prototype changes, update dependencies, and launch.
- The stable Tk app remains available for deeper maintenance and any tools not yet migrated.
