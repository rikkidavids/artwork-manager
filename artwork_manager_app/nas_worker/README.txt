Artwork Manager NAS Worker — 4.53

This optional worker runs on a Synology NAS via Container Manager/Docker. The Mac app stays as the review UI, but embed / convert / deep-check jobs run on the NAS-local filesystem instead of rewriting every track over SMB/VPN.

Why 4.53 exists:
- 4.48 already added worker API 2 and GET / status output.
- If http://NAS-IP:8765/ still returns {"ok": false, "error": "not found"}, the NAS is still running an older cached worker container/image.
- Restarting the container is not enough after replacing server.py. You must rebuild/recreate the Docker project/image.

New in 4.53:
- Fixes album paths with accented Unicode characters such as Zoë by resolving NFC/NFD-equivalent folder names inside the worker.
- GET / and GET /version are public LAN sanity checks and return worker_build, api, version, endpoints, uptime, and an update hint.
- GET /status and GET /health still require the API token and include filesystem diagnostics for /music and /backups.
- Every JSON response includes worker_build/api fields or headers where practical.
- Docker Compose now has an explicit image tag, artwork-manager-worker:4.53, so the running version is easier to spot.
- update_worker.sh force-rebuilds and recreates the container from the current files.
- verify_worker.py checks the root/status endpoints and warns when Synology is still serving an old worker.
- POST /path-check verifies the exact mapped album folder exists, is readable/writable, and counts supported music files.
- Existing embed, convert/save, baseline JPEG, square-padding, deep-check, timing, and busy-guard logic is preserved.

Files:
- server.py
- requirements.txt
- Dockerfile
- docker-compose.yml
- update_worker.sh
- verify_worker.py

Recommended Synology update steps:
1. Stop the old artwork-manager-worker project/container in Synology Container Manager.
2. Copy this nas_worker folder to your NAS project folder, for example:
   /volume1/docker/artwork-manager-worker
3. Edit docker-compose.yml:
   - Set AMW_TOKEN to a private token.
   - Confirm the music mapping. For Rikki's setup it is:
     /volume2/data/media/music:/music:rw
   - If your music is elsewhere, change only the left side of that mapping.
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
   worker_build: "4.53"
   api: 2
   endpoints containing GET /, GET /version, GET /status, POST /embed, POST /deep-check, POST /path-check
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
If the Mac app sends a folder name in decomposed Unicode but Synology stores it in composed Unicode, worker 4.53 resolves the on-disk folder component-by-component before embedding, deep-checking, or path-checking.
