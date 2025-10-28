#!/usr/bin/env python3
import os
import time
import json
import requests
import threading
import traceback
import xml.etree.ElementTree as ET
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ==============================
# LOGGING SETUP
# ==============================
# I want decent debug output from my code, but I turn down some very noisy libs.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] (%(threadName)s): %(message)s",
)
logging.getLogger("urllib3").setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.INFO)

log = logging.getLogger(__name__)

try:
    from plexapi.server import PlexServer
    PLEXAPI_AVAILABLE = True
except ImportError:
    # I don't strictly need plexapi for this flow, but I keep a flag for future expansion.
    PLEXAPI_AVAILABLE = False

"""
High-level goal:

This script acts like my personal "music brain."

Here's what I do:
- Poll Plex to see what I'm currently listening to (usually via Plexamp).
- When a track finishes, give Plex a moment to save my final rating (thumbs up/down).
- Re-fetch metadata for that track, grab the final userRating, and normalize it to:
    "like", "dislike", or None.
- If I liked it, record it in liked.json.
- If I disliked it:
    - record it in disliked.json
    - tell Lidarr (via API) to delete that track from disk.

I also spin up a tiny Flask API on port 7000 so I can hit endpoints like
/liked, /disliked, /now, /health to inspect what the system is doing.
"""

# ==============================
# ENV / CONFIG
# ==============================
# All sensitive values come from environment variables which are injected
# by docker-compose using my private .env (not committed to GitHub).

LB_USER  = os.getenv("LB_USER")
LB_TOKEN = os.getenv("LB_TOKEN")

LIDARR_URL         = os.getenv("LIDARR_URL")
LIDARR_API_KEY     = os.getenv("LIDARR_API_KEY")
LIDARR_ROOT_ID     = int(os.getenv("LIDARR_ROOT_FOLDER_ID", "1"))
LIDARR_QUALITY_ID  = int(os.getenv("LIDARR_QUALITY_PROFILE_ID", "2"))
LIDARR_METADATA_ID = int(os.getenv("LIDARR_METADATA_PROFILE_ID", "1"))

# How long I sleep between major sync cycles (not super critical in current logic)
SLEEP_BETWEEN_SYNC_SEC = 3600

# This is how the container sees my music library; docker-compose bind-mounts
# my actual host music directory to this path.
MUSIC_LIBRARY_ROOT = "/media/Plex/Music"

# I persist state into /app/config (which is a volume mount to the host).
CONFIG_DIR        = "/app/config"
LIKED_PATH        = os.path.join(CONFIG_DIR, "liked.json")
DISLIKED_PATH     = os.path.join(CONFIG_DIR, "disliked.json")
NOW_PLAYING_PATH  = os.path.join(CONFIG_DIR, "now_playing.json")

# Plex connectivity. PLEX_URL is my Plex server base URL.
# PLEX_TOKEN is my Plex auth token.
# PLEX_CLIENT_FILTER is optional: I can filter to a specific Plex client name
# (ex: "Plexamp") if I only want those sessions to count.
PLEX_URL           = os.getenv("PLEX_URL")
PLEX_TOKEN         = os.getenv("PLEX_TOKEN")
PLEX_CLIENT_FILTER = os.getenv("PLEX_CLIENT_FILTER", "").strip()

# I consider "track finished" at basically any fraction right now.
# If I wanted "only count if I listened to at least 80%", I'd bump this.
PLAY_THRESHOLD_FRACTION = 0.0

# After Plex says the track ended, I wait this long (seconds) before checking
# the final rating. This gives PlexAmp time to persist my thumb rating.
FINISH_GRACE_SECONDS = 30

# Headers for external APIs
LB_API_BASE = "https://api.listenbrainz.org/1"
LB_HEADERS = {
    "Accept": "application/json",
    "Authorization": f"Token {LB_TOKEN}"
}
LIDARR_HEADERS = {
    "X-Api-Key": LIDARR_API_KEY,
    "Content-Type": "application/json"
}

os.makedirs(CONFIG_DIR, exist_ok=True)

# These dicts track what's currently playing and what just finished.
CURRENT_TRACKS = {}
RECENTLY_FINISHED = {}
_FINISHED_ALREADY_SCHEDULED = set()

# ==============================
# JSON LOAD/SAVE HELPERS
# ==============================
# I store likes/dislikes in simple JSON so it's easy to browse/edit outside Docker.
def _load_json_file(path):
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception(f"Failed loading {path}")
        return {}

def _save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        log.exception(f"Failed saving {path}")

# ==============================
# RATING NORMALIZATION
# ==============================
# Plex gives "userRating" values like 10, 5, 2, 1, etc.
# I don't care about half-stars. I just bucket:
#   10 / 5 -> "like"
#   2 / 1  -> "dislike"
# anything else -> None
def _normalize_rating(val):
    if val is None:
        return None
    try:
        f = float(val)
    except Exception:
        return None

    # I treat "5 stars" and "10/10" as a like
    if abs(f - 10.0) < 0.1 or abs(f - 5.0) < 0.1:
        return "like"

    # I treat "1 star" / "2/10" as a dislike
    if abs(f - 2.0) < 0.1 or abs(f - 1.0) < 0.1:
        return "dislike"

    return None

# ==============================
# LIDARR HELPER FUNCTIONS
# ==============================
# Here's the flow:
# - I try to match the finished track (artist/title/album) against Lidarr.
# - If I find a confident match, I tell Lidarr to delete that track,
#   including removing the file from disk.
def _score_lidarr_candidate(hit, artist_lower, track_lower, album_lower):
    hit_title  = str(hit.get("title", "")).lower()
    hit_artist = str(hit.get("artistName", "")).lower()
    hit_album  = str(hit.get("album", {}).get("title", "")).lower()

    score = 0
    if artist_lower and artist_lower in hit_artist:
        score += 2
    if track_lower and track_lower in hit_title:
        score += 2
    if album_lower and album_lower in hit_album:
        score += 2
    if hit.get("hasFile"):
        score += 1

    return score

def lidarr_track_lookup_multi(artist_name, track_title, album_title, guid_raw):
    if not LIDARR_URL or not LIDARR_API_KEY:
        log.warning("lidarr_track_lookup_multi(): Missing LIDARR_URL or LIDARR_API_KEY; cannot talk to Lidarr.")
        return None

    # I generate multiple search terms to boost match accuracy:
    search_terms = []
    if artist_name and track_title:
        search_terms.append(f"{artist_name} {track_title}")
    if artist_name and album_title:
        search_terms.append(f"{artist_name} {album_title}")
    if track_title:
        search_terms.append(track_title)
    if album_title and artist_name:
        search_terms.append(f"{album_title} {artist_name}")

    # Plex gives a GUID like plex://track/XYZ, which can help sometimes
    if guid_raw:
        guid_tail = guid_raw.split("/")[-1]
        if guid_tail and guid_tail not in search_terms:
            search_terms.append(guid_tail)

    artist_lower = (artist_name or "").lower()
    track_lower  = (track_title or "").lower()
    album_lower  = (album_title or "").lower()

    for term in search_terms:
        url = f"{LIDARR_URL}/api/v1/track/lookup"
        params = {"term": term}
        log.debug(f"lidarr_track_lookup_multi(): querying Lidarr with term='{term}'")
        try:
            resp = requests.get(url, headers=LIDARR_HEADERS, params=params, timeout=10)
        except Exception as e:
            log.warning(f"lidarr_track_lookup_multi(): lookup request failed for '{term}': {e}")
            continue

        if resp.status_code != 200:
            log.warning(f"lidarr_track_lookup_multi(): HTTP {resp.status_code} for term '{term}'")
            continue

        try:
            results = resp.json()
        except Exception:
            log.exception("lidarr_track_lookup_multi(): Failed to decode JSON from Lidarr.")
            continue

        if not isinstance(results, list) or not results:
            log.debug(f"lidarr_track_lookup_multi(): no results for '{term}'")
            continue

        best = max(results, key=lambda h: _score_lidarr_candidate(h, artist_lower, track_lower, album_lower))
        best_score = _score_lidarr_candidate(best, artist_lower, track_lower, album_lower)

        log.debug(
            f"lidarr_track_lookup_multi(): best candidate for term='{term}' "
            f"is track_id={best.get('id')} title='{best.get('title')}' "
            f"artist='{best.get('artistName')}' score={best_score}"
        )

        # I consider score >=2 "good enough" to try to delete
        if best_score >= 2 and best.get("id") is not None:
            return {
                "id": best.get("id"),
                "title": best.get("title"),
                "artistName": best.get("artistName"),
            }

    return None

def lidarr_delete_track(track_id):
    """
    I tell Lidarr to delete a specific track, including the media file itself.
    """
    if not LIDARR_URL or not LIDARR_API_KEY:
        log.warning("lidarr_delete_track(): Missing LIDARR_URL or LIDARR_API_KEY; cannot talk to Lidarr.")
        return False

    url = f"{LIDARR_URL}/api/v1/track/{track_id}"
    params = {"deleteFiles": "true"}

    try:
        resp = requests.delete(url, headers=LIDARR_HEADERS, params=params, timeout=10)
    except Exception as e:
        log.warning(f"lidarr_delete_track(): DELETE failed for track_id={track_id}: {e}")
        return False

    if resp.status_code not in (200, 202, 204):
        log.warning(
            f"lidarr_delete_track(): Unexpected HTTP {resp.status_code} for track_id={track_id}: {resp.text}"
        )
        return False

    return True

def purge_with_lidarr(artist_name, track_title, album_title, guid_raw):
    """
    High-level "nuke this song" step:
    1. Try to match the track that I just disliked to a Lidarr track_id.
    2. Tell Lidarr to delete it.
    """
    match = lidarr_track_lookup_multi(artist_name, track_title, album_title, guid_raw)
    if not match:
        log.info(
            f"purge_with_lidarr(): Couldn't confidently match '{track_title}' "
            f"by '{artist_name}' for deletion."
        )
        return

    track_id = match["id"]
    log.info(
        f"purge_with_lidarr(): Requesting Lidarr delete for track_id={track_id} "
        f"('{match.get('title')}' by '{match.get('artistName')}')"
    )

    ok = lidarr_delete_track(track_id)
    if ok:
        log.info(f"purge_with_lidarr(): Lidarr delete succeeded for track_id={track_id}")
    else:
        log.warning(f"purge_with_lidarr(): Lidarr delete may have failed for track_id={track_id}")

# ==============================
# LIKE / DISLIKE STORAGE
# ==============================
# I store likes/dislikes in JSON, plus mark artist ban state if I ever want that.
def add_like(artist, track):
    liked = _load_json_file(LIKED_PATH)
    entry = liked.get(artist, {"tracks": [], "last_seen": None})
    if track not in entry["tracks"]:
        entry["tracks"].append(track)
    entry["last_seen"] = datetime.now(timezone.utc).isoformat()
    liked[artist] = entry
    _save_json_file(LIKED_PATH, liked)
    log.info(f"Stored LIKE for '{track}' by '{artist}'")

def add_dislike(artist, track, ban_artist=False):
    disliked = _load_json_file(DISLIKED_PATH)
    entry = disliked.get(artist, {"tracks": [], "ban_artist": False, "last_seen": None})
    if track not in entry["tracks"]:
        entry["tracks"].append(track)
    if ban_artist:
        entry["ban_artist"] = True
    entry["last_seen"] = datetime.now(timezone.utc).isoformat()
    disliked[artist] = entry
    _save_json_file(DISLIKED_PATH, disliked)
    log.info(f"Stored DISLIKE for '{track}' by '{artist}'")

def _act_on_rating(artist, track, album, guid_raw, normalized_bucket):
    """
    Decide what to do based on my rating bucket:
    - "like"    -> record in liked.json
    - "dislike" -> record in disliked.json and ask Lidarr to delete
    - None      -> do nothing
    """
    if normalized_bucket == "like":
        add_like(artist, track)
        return True

    if normalized_bucket == "dislike":
        add_dislike(artist, track)
        purge_with_lidarr(artist, track, album, guid_raw)
        return True

    return False

# ==============================
# POST-FINISH RATING HARVEST
# ==============================
# When Plex says a session ended, I don't immediately trust the mid-play rating.
# I wait FINISH_GRACE_SECONDS, then I pull track metadata again from Plex to
# grab the final userRating (the "real" thumb up/down I care about).
def _harvest_final_rating_for_finished_track(session_id, finished_snap):
    try:
        rating_key = finished_snap.get("ratingKey")
        artist     = finished_snap.get("artist")
        track      = finished_snap.get("track")
        album      = finished_snap.get("album")
        guid_raw   = finished_snap.get("guid")
        frac       = finished_snap.get("progress_fraction", 0.0)

        if not rating_key:
            log.warning(f"[{session_id}] No ratingKey, skipping final harvest.")
            return

        log.debug(f"[{session_id}] Sleeping {FINISH_GRACE_SECONDS}s before checking final rating...")
        time.sleep(FINISH_GRACE_SECONDS)

        meta_url = f"{PLEX_URL}/library/metadata/{rating_key}"
        headers = {"X-Plex-Token": PLEX_TOKEN}
        r = requests.get(meta_url, headers=headers, timeout=10)
        log.debug(f"[{session_id}] Metadata fetch -> HTTP {r.status_code}")
        if r.status_code != 200:
            return

        root = ET.fromstring(r.text)
        track_node = root.find(".//Track")
        if track_node is None:
            log.warning(f"[{session_id}] No <Track> node in metadata XML.")
            return

        raw_final_rating = track_node.attrib.get("userRating")
        normalized_bucket = _normalize_rating(raw_final_rating)

        log.info(
            f"Final rating for '{track}' by '{artist}': "
            f"raw={raw_final_rating}, bucket={normalized_bucket}, listened={frac:.2f}"
        )

        acted = _act_on_rating(
            artist,
            track,
            album,
            guid_raw,
            normalized_bucket
        )

        if acted:
            log.info(
                f"Action stored for '{track}' by '{artist}' "
                f"(session {session_id}, bucket={normalized_bucket})"
            )
        else:
            log.info(
                f"No action taken for '{track}' by '{artist}' "
                f"(session {session_id}, bucket={normalized_bucket})"
            )

    except Exception:
        log.exception(f"Error in _harvest_final_rating_for_finished_track ({session_id})")

def finished_watcher_loop():
    """
    I run forever in a background thread.
    I look at RECENTLY_FINISHED for sessions that ended, and for each one
    I schedule a worker (only once) to harvest the final rating.
    """
    log.info("finished_watcher_loop started.")
    while True:
        try:
            for session_id, snap in list(RECENTLY_FINISHED.items()):
                if session_id in _FINISHED_ALREADY_SCHEDULED:
                    continue

                log.info(
                    f"Scheduling post-finish check for session {session_id} "
                    f"({snap.get('artist')} - {snap.get('track')}) "
                    f"(progress={snap.get('progress_fraction'):.2f})"
                )

                t = threading.Thread(
                    target=_harvest_final_rating_for_finished_track,
                    args=(session_id, snap),
                    daemon=True
                )
                t.start()
                _FINISHED_ALREADY_SCHEDULED.add(session_id)

        except Exception:
            log.exception("Error in finished_watcher_loop")

        time.sleep(5)

# ==============================
# PLEX POLLER
# ==============================
# This thread polls Plex (/status/sessions), figures out what is currently being
# played, and stores that info. Anything that was playing last poll but not
# playing this poll is considered "finished" and queued for rating harvest.
def poll_plex_sessions():
    log.info("Plex poller started.")
    last_active = set()

    while True:
        try:
            r = requests.get(
                f"{PLEX_URL}/status/sessions",
                headers={"X-Plex-Token": PLEX_TOKEN},
                timeout=10
            )
            if r.status_code != 200:
                log.warning(f"Plex poll error HTTP {r.status_code}")
                time.sleep(5)
                continue

            root = ET.fromstring(r.text)
            seen_now = set()

            for track_el in root.findall(".//Track"):
                a = track_el.attrib
                artist         = a.get("grandparentTitle")
                album_title    = a.get("parentTitle")
                title          = a.get("title")
                guid_raw       = a.get("guid")
                rating_key     = a.get("ratingKey")
                view_offset    = a.get("viewOffset")
                duration       = a.get("duration")
                user_rating_lv = a.get("userRating")

                # I track how much of the song was played, mostly for logging
                try:
                    frac = float(view_offset or 0) / float(duration or 1)
                except Exception:
                    frac = 0.0

                # I pick a session ID. ratingKey is ideal if it's present.
                sid = rating_key or f"{artist}-{title}"

                CURRENT_TRACKS[sid] = {
                    "artist": artist,
                    "album": album_title,
                    "track": title,
                    "guid": guid_raw,
                    "ratingKey": rating_key,
                    "progress_fraction": frac,
                    "user_rating_raw": user_rating_lv,
                }
                seen_now.add(sid)

                log.debug(
                    f"{artist} - {title} "
                    f"({frac*100:.0f}% played, rating={user_rating_lv})"
                )

            # Anything in last_active but not in seen_now just finished.
            ended = last_active - seen_now
            for e in ended:
                snap = CURRENT_TRACKS.get(e)
                if not snap:
                    continue
                RECENTLY_FINISHED[e] = snap
                log.info(
                    f"Session ended for {e}: {snap.get('artist')} - {snap.get('track')} "
                    f"(progress={snap.get('progress_fraction'):.2f})"
                )

            last_active = seen_now

            if not seen_now:
                log.debug("No active tracks.")

        except Exception:
            log.exception("Error in poll_plex_sessions")

        time.sleep(5)

# ==============================
# FLASK API
# ==============================
# This gives me a little HTTP interface into my state so I can
# check what I've liked/disliked or what's currently playing.
app = Flask(__name__)

@app.route("/liked")
def liked():
    return jsonify(_load_json_file(LIKED_PATH))

@app.route("/disliked")
def disliked():
    return jsonify(_load_json_file(DISLIKED_PATH))

@app.route("/now")
def now():
    return jsonify({
        "live": CURRENT_TRACKS,
        "saved": _load_json_file(NOW_PLAYING_PATH)
    })

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat()
    })

# ==============================
# ENTRYPOINT
# ==============================
# When the container runs:
# - I start the plex poller thread
# - I start the finished_watcher thread
# - I launch the Flask API on 0.0.0.0:7000
if __name__ == "__main__":
    threading.Thread(
        target=poll_plex_sessions,
        name="plex_poller",
        daemon=True
    ).start()

    threading.Thread(
        target=finished_watcher_loop,
        name="finished_watcher",
        daemon=True
    ).start()

    log.info("All background threads launched.")
    app.run(host="0.0.0.0", port=7000, debug=False)

