# Production Batch Video Transcoder

**Complete, reliable, unattended 720p H.264 transcoder for Ubuntu Linux.**

- Scans `/root/SRC_Videos` recursively
- Transcodes **only** videos taller than 720p → 720p (libx264 veryfast CRF 22)
- Preserves **exact folder structure** under `/root/ENC_Videos`
- **Never** overwrites final files (uses `.partial` + atomic rename)
- Auto-cleans abandoned `.partial` files on every start
- **Graceful Ctrl+C** — current ffmpeg jobs finish cleanly
- Rich **live dashboard** (no log spam)
- Full Unicode / CJK / Arabic / Malayalam / emoji / very long filename support
- Correctly ignores embedded PNG cover art in MKVs
- Copies AAC audio when possible, otherwise converts to AAC 192k
- Copies all subtitles + metadata
- Production-grade logging to `/root/transcoder/transcoder.log`
- Auto-tunes workers & threads based on CPU cores (no hardcoding)

## System Requirements

- Ubuntu Linux (tested on 22.04/24.04)
- Python 3.12+
- FFmpeg + ffprobe (already installed)
- `rich` Python package (already installed in your venv)
- 16+ CPU cores recommended for good throughput (auto-detects)

## Installation

```bash
# 1. Create project directory (if not already done)
sudo mkdir -p /root/transcoder
cd /root/transcoder

# 2. Copy all files from this package into /root/transcoder/
#    (transcoder.py, config.py, scanner.py, encoder.py, worker.py, dashboard.py, logger.py, utils.py, requirements.txt, README.md)

# 3. (Optional) Create/activate virtualenv if you haven't already
python3 -m venv /root/venv
source /root/venv/bin/activate

# 4. Install rich (if not already present)
pip install -r requirements.txt

# 5. Make sure FFmpeg is available
which ffmpeg
which ffprobe
```

## Running

```bash
# Activate venv
source /root/venv/bin/activate

# Run (will process everything under /root/SRC_Videos)
python /root/transcoder/transcoder.py

# Or if you installed it as a module:
cd /root/transcoder
python -m transcoder.transcoder
```

**Recommended for long runs:**
```bash
# Run in screen or tmux so it survives SSH disconnect
screen -S transcoder
python /root/transcoder/transcoder.py
# Detach with Ctrl+A then D
```

## How It Works (Architecture)

```
Main Thread
├── Scanner (pathlib rglob) → job_queue
├── Result consumer (updates counters + log)
├── Dashboard (rich Live, 2 Hz refresh)
├── Signal handler (graceful shutdown)
└── N Worker Threads (queue consumers)
    └── Encoder
        ├── ffprobe (robust, ignores cover art)
        ├── Build ffmpeg cmd (no shell=True ever)
        ├── Popen + progress parser (stdout pipe:1)
        ├── Write to Movie.mp4.partial
        └── Atomic rename on success
```

**Threading model**: `queue.Queue` + `threading.Thread` (NOT `multiprocessing`).  
This is intentional and correct for I/O-bound ffmpeg processes.

## Configuration

All settings are in `config.py` (dataclass). You can edit:

- `CRF`, `PRESET`, `SCALE_FILTER`
- `MAX_WORKERS` / `THREADS_PER_FFMPEG` (auto-computed — do not change unless you really know what you're doing)
- Paths (`SRC_DIR`, `ENC_DIR`, `LOG_FILE`)

After editing `config.py` just re-run.

## Skipping Logic (in order)

1. Height ≤ 720p → skip
2. Output `.mp4` already exists in target location → skip
3. Probe failed or no valid video stream → skip
4. Invalid duration/dimensions → skip

## Temp File Safety

- Never writes directly to final `Movie.mp4`
- Always encodes to `Movie.mp4.partial`
- Only renames after `ffmpeg` exits with code 0
- On next launch: **automatically deletes** any leftover `.partial` files

## Dashboard (what you see while running)

```
BATCH VIDEO TRANSCODER  |  2026-...  |  CPU: 4 workers × 4 threads
┌──────────────────────────────────────────────────────────────┐
│ Active Workers                                               │
├────┬──────────┬──────────────────────────────────────────────┤
│ ID │ Status   │ Current File                          │ ...  │
│ 0  │ encoding │ /root/SRC_Videos/Anime/S1/E05.mkv     │ 67.3%│
│ 1  │ idle     │ -                                     │ -    │
│ 2  │ encoding │ /root/SRC_Videos/Movies/BigFile.mp4   │ 12.1%│
│ 3  │ probing  │ /root/SRC_Videos/Series/EP03.mkv      │ 0.0% │
└────┴──────────┴──────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ Overall Progress                                             │
│ Total Found: 1247  Queued: 1247  Skipped: 312  Completed: 89 │
│ Elapsed: 01:12:34   Overall ETA: 03:45:12                    │
│ Original Size: 2.34 TB   Encoded Size: 1.12 TB   Saved: 1.22 TB (52.1%) │
└──────────────────────────────────────────────────────────────┘
```

## Logging

All events go to:
```
/root/transcoder/transcoder.log
```

Example lines:
```
2026-07-13 21:15:03 | INFO     | COMPLETED | input=/root/SRC_Videos/.../file.mkv | output=.../file.mp4 | runtime=124.7s | size=1456789012->567890123 saved=889898889 (61.0%)
2026-07-13 21:15:04 | INFO     | SKIPPED | input=... | height 1080p <= 720p
```

## Troubleshooting

**"No valid video stream found (cover art ignored)"**
- The file really has no video (or only PNG covers). Skipped safely.

**ffmpeg exits non-zero**
- Check the log file for the exact ffmpeg stderr lines.
- Common: corrupted source, out of disk space, permission on ENC_DIR.

**Dashboard freezes / looks stuck**
- It refreshes every 0.5s. If a worker is on a very long probe (rare), it may look paused for a few seconds. Normal.

**Out of disk space mid-run**
- The current `.partial` will be deleted on next start. Already completed files are safe.

**Very long filenames / Unicode**
- Fully supported. Never sanitized. Uses `pathlib` + argument lists only.

**Want to change quality?**
Edit `config.py`:
```python
CRF = 18          # better quality
PRESET = "medium" # slower but better compression
```

Then restart.

## Production Tips for 20 TB+

1. Run inside `screen` or `tmux`.
2. Mount `ENC_Videos` on a filesystem with enough space + fast writes.
3. Check `transcoder.log` occasionally with `tail -f`.
4. The transcoder is idempotent: safe to stop and restart any time.
5. After full run, you can `rsync --remove-source-files` the SRC if you want to free space.

## License / Warranty

This is production-grade code written for unattended operation on large libraries.  
It has been designed with reliability as the #1 priority.

Enjoy your smaller, still beautiful 720p library!