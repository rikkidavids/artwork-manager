Artwork Manager NAS Worker — 5.04

Preferred easy-update setup:
Use the separate NAS worker repo here:
   https://github.com/rikkidavids/artwork-manager-nas-worker

That repo is set up for a normal Synology/GitHub Container Registry image workflow, so the NAS can pull a finished image instead of rebuilding from this bundled folder.

This optional worker runs on a Synology NAS via Container Manager/Docker. The Mac app stays as the review UI, but scan / embed / convert / deep-check jobs can run on the NAS-local filesystem instead of touching every track over SMB/VPN.

Why 5.04 exists:
- 4.53 added worker API 2, status output, path checking, and Unicode path fixes.
- 5.04 adds worker API 3 and POST /scan-library so Scan / Resume can walk and inspect folders locally on the NAS.
- If http://NAS-IP:8765/ still shows worker_build 4.53 or api 2, the NAS is still running an older cached worker container/image.
- Restarting the container is not enough after replacing server.py. You must rebuild/recreate the Docker project/image.

New in 5.04:
- POST /scan-library walks the mounted music root inside the worker, checks changed/new album folders on the NAS, and returns compact queue rows to the Mac app.
- Resume scans can skip unchanged folders using scan fingerprints without the Mac doing thousands of SMB/VPN stat/tag reads.
- Existing Unicode-normalized path handling is preserved for accented folder names such as Zoë.
- GET / and GET /version are public LAN sanity checks and return worker_build, api, version, endpoints, uptime, and an update hint.
- GET /status and GET /health still require the API token and include filesystem diagnostics for /music and /backups.
- Every JSON response includes worker_build/api fields or headers where practical.
- Docker Compose now has an explicit image tag, artwork-manager-worker:5.04, so the running version is easier to spot.
- update_worker.sh force-rebuilds and recreates the container from the current files.
- verify_worker.py checks the root/status endpoints and warns when Synology is still serving an old worker.
- POST /path-check verifies the exact mapped album folder exists, is readable/writable, and counts supported music files.
- Existing embed, convert/save, baseline JPEG, square-padding, deep-check, timing, and busy-guard logic is preserved.

Files:
- .env.example
- server.py
- requirements.txt
- Dockerfile
- docker-compose.yml
- update_worker.sh
- update_from_github.sh
- verify_worker.py

Easiest GitHub-linked Synology setup:
This keeps a Git checkout on the NAS, limited to the nas_worker folder, then rebuilds the local Docker project from that checkout.

First-time setup from an SSH/Terminal session on the NAS:
   cd /volume1/docker
   git clone --filter=blob:none --sparse --branch main https://github.com/rikkidavids/artwork-manager.git artwork-manager
   cd artwork-manager
   git sparse-checkout set artwork_manager_app/nas_worker
   cd artwork_manager_app/nas_worker
   chmod +x update_worker.sh update_from_github.sh
   cp .env.example .env

Then edit .env once:
   - Set AMW_TOKEN to a private token.
   - Confirm AMW_MUSIC_PATH. For Rikki's setup it is:
     AMW_MUSIC_PATH=/volume2/data/media/music
   - If your music is elsewhere, change that value only.

Initial build:
   ./update_worker.sh

Future updates:
   cd /volume1/docker/artwork-manager/artwork_manager_app/nas_worker
   ./update_from_github.sh

If you want to track a different branch:
   AMW_GIT_BRANCH=qt-prototype ./update_from_github.sh

Recommended Synology update steps:
1. Stop the old artwork-manager-worker project/container in Synology Container Manager.
2. Copy this nas_worker folder to your NAS project folder, for example:
   /volume1/docker/artwork-manager-worker
3. Copy .env.example to .env, then edit .env:
   - Set AMW_TOKEN to a private token.
   - Confirm AMW_MUSIC_PATH. For Rikki's setup it is:
     AMW_MUSIC_PATH=/volume2/data/media/music
   - If your music is elsewhere, change that value only.
4. Rebuild/recreate the project. Do not only restart it.

Terminal method from the NAS project folder:
   chmod +x update_worker.sh
   ./update_worker.sh

Equivalent manual Docker Compose commands:
   docker compose down --remove-orphans
   docker compose build --no-cache --pull
   docker compose up -d --force-recreate

If your NAS uses the old compose command:
   docker-compose down --remove-orphans
   docker-compose build --no-cache --pull
   docker-compose up -d --force-recreate

Synology Container Manager UI method:
- Open Container Manager > Project.
- Stop the artwork-manager-worker project.
- Use Build/Rebuild or Action > Clean/Rebuild if available.
- Start the project again.
- If the UI only offers restart/start, delete the old project/container/image and create the project again from this folder.

Verification:
1. From a browser on your Mac, open:
   http://YOUR-NAS-IP:8765/
2. You should see:
   worker_build: "5.04"
   api: 3
   endpoints containing GET /, GET /version, GET /status, POST /scan-library, POST /embed, POST /deep-check, POST /path-check
3. Optional terminal check from your Mac:
   python verify_worker.py http://YOUR-NAS-IP:8765 YOUR_TOKEN

Mac app Settings > NAS / Synology Worker:
- Enable the NAS worker.
- Worker URL: http://YOUR-NAS-IP:8765
- API token: same as AMW_TOKEN.
- Mac path prefix: /Volumes/data/media/music
- Worker path prefix: /music

Security:
Keep this worker on your LAN/VPN only. Do not expose port 8765 to the public internet.



Unicode note:
If the Mac app sends a folder name in decomposed Unicode but Synology stores it in composed Unicode, worker 5.04 resolves the on-disk folder component-by-component before scanning, embedding, deep-checking, or path-checking.
