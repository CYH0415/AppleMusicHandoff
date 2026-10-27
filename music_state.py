#!/usr/bin/env python3
"""Music state sampler and read-only HTTP API. Python standard library only."""
import argparse
from collections import deque
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'capture.js'
ICLOUD = Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs/MusicHandoff'
HANDOFF_FILENAME = 'phone_handoff.json'
PHONE_EVENT_FILENAME = 'phone_last_event_id.txt'
TRACKING_FILENAME = 'handoff-track.json'
HANDOFF_SCHEMA = 3
HANDOFF_FIELDS = {'event_id', 'started_at', 'position', 'rate', 'track', 'artist',
                  'playlist', 'playlist_index', 'shuffle', 'repeat'}
SEEK_TOLERANCE_SECONDS = 2.0
HANDOFF_PAUSE_DRIFT_SECONDS = 5.0
SHUFFLE_VALUES = {'songs': 'Songs', 'albums': 'Albums'}
REPEAT_VALUES = {'off': 'None', 'one': 'One', 'all': 'All'}

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    os.replace(tmp, path)

def ensure_phone_event_file(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / PHONE_EVENT_FILENAME
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write('none')

def capture_unix(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()

def iso_from_unix(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def shortcut_shuffle(state):
    if not state.get('shuffle_enabled'):
        return 'Off'
    return SHUFFLE_VALUES.get(str(state.get('shuffle_mode') or '').lower(), 'Off')

def shortcut_repeat(state):
    return REPEAT_VALUES.get(str(state.get('repeat_mode') or '').lower(), 'None')

def shortcut_rate(state):
    return 1 if state.get('player_state') == 'playing' else 0

def run_capture(mode='state', timeout=8):
    result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', str(SCRIPT), mode],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'Music automation failed')
    return result.stdout

def fingerprint(tracks):
    # Mac-local IDs are excluded, allowing the phone to compute the same fingerprint.
    content = [[t.get('name'), t.get('artist'), t.get('album'),
                round(t.get('duration_seconds') or 0), t.get('disc_number'),
                t.get('track_number')] for t in tracks]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def parse_playlist(xml, expected_id):
    data = plistlib.loads(xml.encode())
    playlists = [p for p in data.get('Playlists', []) if p.get('Playlist Persistent ID') == expected_id]
    if len(playlists) != 1:
        raise ValueError('Playlist changed during export')
    p = playlists[0]
    tracks = []
    for index, item in enumerate(p.get('Playlist Items', []), 1):
        source = data.get('Tracks', {}).get(str(item['Track ID']))
        if source is None:
            raise ValueError('Playlist export contains an unresolved track')
        tracks.append(dict(position=index, name=source.get('Name'), artist=source.get('Artist'),
                           album=source.get('Album'), album_artist=source.get('Album Artist'),
                           duration_seconds=source.get('Total Time', 0)/1000,
                           disc_number=source.get('Disc Number'), track_number=source.get('Track Number'),
                           local_persistent_id=source.get('Persistent ID'),
                           local_database_id=source.get('Track ID')))
    return dict(name=p.get('Name'), local_persistent_id=expected_id,
                description=p.get('Description'), track_count=len(tracks), tracks=tracks,
                content_fingerprint=fingerprint(tracks), order_source='Music XML Playlist Items',
                captured_at=utc_now())

def match_positions(track, playlist):
    if not track or not playlist:
        return []
    return [t['position'] for t in playlist['tracks']
            if t.get('local_persistent_id') == track.get('local_persistent_id')]

class Monitor:
    def __init__(self, directory, interval=1, refresh=30, sync_dir=None):
        self.directory, self.interval, self.refresh = directory, interval, refresh
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.state, self.playlist, self.last_playable = None, None, None
        self.last_success, self.last_error = None, None
        self.history = deque(maxlen=15)
        self.exported_at = 0
        self.sync_dir = sync_dir
        self.last_sync, self.sync_error = 0, None
        self.handoff_event_id, self.handoff_identity = None, None
        self.handoff_anchor_unix, self.handoff_position = None, None
        self.handoff_state = self.handoff_shuffle = self.handoff_repeat = None
        self.handoff_schema = None
        self.load_handoff_tracking()
        for name in ('state', 'playlist', 'last_playable'):
            try:
                setattr(self, name, json.loads((directory / (name+'.json')).read_text()))
            except (OSError, ValueError):
                pass

    def sample(self):
        started = time.monotonic()
        state = json.loads(run_capture())
        playlist_id = (state.get('playlist') or {}).get('local_persistent_id')
        with self.lock:
            cached = copy.deepcopy(self.playlist)
        needs_export = playlist_id and (not cached or cached['local_persistent_id'] != playlist_id or
                                       cached.get('track_count') != state['playlist'].get('track_count') or
                                       started - self.exported_at >= self.refresh)
        if needs_export:
            try:
                cached = parse_playlist(run_capture('playlist', timeout=20), playlist_id)
                atomic_json(self.directory / 'playlist.json', cached)
                with self.lock:
                    self.playlist = cached
                self.exported_at = started
            except Exception as error:
                state.setdefault('warnings', []).append('playlist_export: ' + str(error))
        if cached and cached['local_persistent_id'] != playlist_id:
            cached = None
        positions = match_positions(state.get('track'), cached)
        if state.get('playlist'):
            state['playlist'].update(snapshot_available=cached is not None,
                snapshot_captured_at=cached.get('captured_at') if cached else None,
                content_fingerprint=cached.get('content_fingerprint') if cached else None,
                current_track_positions=positions,
                current_track_position=positions[0] if len(positions) == 1 else None)
        state.update(schema_version=1,
                     sample_duration_ms=round((time.monotonic()-started)*1000, 1))
        # Optional explicit mapping supplied later by the phone or the user.
        try:
            mapping = json.loads((self.directory / 'identity-map.json').read_text())
        except (OSError, ValueError):
            mapping = {}
        for kind in ('track', 'playlist'):
            if state.get(kind):
                state[kind]['remote_identity'] = mapping.get(kind+'s', {}).get(state[kind]['local_persistent_id'])
        atomic_json(self.directory / 'state.json', state)
        playable = bool(state.get('track') and state.get('position_seconds') is not None and
                        state.get('player_state') in ('playing', 'paused'))
        if playable:
            atomic_json(self.directory / 'last_playable.json', state)
        with self.lock:
            self.state = state
            if playable:
                self.last_playable = state
                sample_unix = datetime.fromisoformat(state['captured_at'].replace('Z','+00:00')).timestamp()
                self.history.append(dict(sample_unix=sample_unix, state=copy.deepcopy(state), playlist=copy.deepcopy(cached)))
            self.last_success, self.last_error = time.monotonic(), None
        if playable:
            self.publish(state)

    def load_handoff_tracking(self):
        try:
            value = json.loads((self.directory / TRACKING_FILENAME).read_text())
            self.handoff_event_id = value.get('event_id')
            self.handoff_identity = tuple(value['identity'])
            self.handoff_anchor_unix = float(value['anchor_unix'])
            self.handoff_position = float(value['position'])
            self.handoff_state = value.get('state')
            self.handoff_shuffle = value.get('shuffle')
            self.handoff_repeat = value.get('repeat')
            self.handoff_schema = value.get('schema')
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save_handoff_tracking(self):
        if self.handoff_event_id is None or self.handoff_identity is None:
            return
        atomic_json(self.directory / TRACKING_FILENAME, dict(
            event_id=self.handoff_event_id,
            identity=list(self.handoff_identity),
            anchor_unix=self.handoff_anchor_unix,
            position=self.handoff_position,
            state=self.handoff_state,
            shuffle=self.handoff_shuffle,
            repeat=self.handoff_repeat,
            schema=HANDOFF_SCHEMA,
        ))

    def build_phone_handoff(self, state, event_id):
        track = state.get('track') or {}
        playlist_state = state.get('playlist') or {}
        positions = playlist_state.get('current_track_positions') or []
        current_position = playlist_state.get('current_track_position')
        if current_position is None and positions:
            current_position = positions[0]
        position = max(0.0, float(state['position_seconds']))
        started_at = iso_from_unix(capture_unix(state['captured_at']))
        return dict(
            event_id=event_id,
            started_at=started_at,
            position=round(position, 3),
            rate=shortcut_rate(state),
            track=track.get('name'),
            artist=track.get('artist'),
            playlist=playlist_state.get('name'),
            playlist_index=current_position,
            shuffle=shortcut_shuffle(state),
            repeat=shortcut_repeat(state),
        )

    def publish(self, state):
        if self.sync_dir is None:
            return
        track = state.get('track') or {}
        playlist_state = state.get('playlist') or {}
        positions = playlist_state.get('current_track_positions') or []
        current_position = playlist_state.get('current_track_position')
        if current_position is None and positions:
            current_position = positions[0]
        if state.get('position_seconds') is None or track.get('local_persistent_id') is None or current_position is None:
            return
        now = time.monotonic()
        position = max(0.0, float(state['position_seconds']))
        anchor_unix = capture_unix(state['captured_at'])
        identity = (track.get('local_persistent_id'), playlist_state.get('local_persistent_id'), current_position)
        identity_changed = identity != self.handoff_identity
        expected = self.handoff_position
        if expected is not None and self.handoff_state == 'playing' and self.handoff_anchor_unix is not None:
            expected += max(0.0, anchor_unix - self.handoff_anchor_unix)
        seek_delta = abs(position - expected) if expected is not None else None
        seek_changed = seek_delta is not None and seek_delta > SEEK_TOLERANCE_SECONDS
        shuffle = shortcut_shuffle(state)
        repeat = shortcut_repeat(state)
        handoff_pause = (self.handoff_state == 'playing' and
                         state.get('player_state') == 'paused')
        handoff_pause_drift = (handoff_pause and seek_delta is not None and
                               seek_delta <= HANDOFF_PAUSE_DRIFT_SECONDS)
        handoff_path = self.sync_dir / HANDOFF_FILENAME
        try:
            current_handoff = json.loads(handoff_path.read_text())
            current_file_valid = set(current_handoff) == HANDOFF_FIELDS
        except (OSError, ValueError, TypeError):
            current_file_valid = False
        resumed = self.handoff_state in ('paused', 'stopped') and state.get('player_state') == 'playing'
        event_changed = (identity_changed or (seek_changed and not handoff_pause_drift) or resumed or
                         shuffle != self.handoff_shuffle or repeat != self.handoff_repeat)
        if self.handoff_event_id is None or event_changed:
            event_id = str(uuid.uuid4())
        else:
            event_id = self.handoff_event_id
        changed = (identity_changed or self.handoff_anchor_unix is None or
                   not current_file_valid or self.handoff_schema != HANDOFF_SCHEMA or
                   seek_changed or
                   state.get('player_state') != self.handoff_state or
                   shuffle != self.handoff_shuffle or repeat != self.handoff_repeat or
                   self.sync_error is not None)
        if not changed:
            return
        payload = self.build_phone_handoff(state, event_id)
        try:
            ensure_phone_event_file(self.sync_dir)
            atomic_json(handoff_path, payload)
            with self.lock:
                self.last_sync, self.sync_error = now, None
                self.handoff_event_id, self.handoff_identity = event_id, identity
                self.handoff_anchor_unix, self.handoff_position = anchor_unix, position
                self.handoff_state, self.handoff_shuffle, self.handoff_repeat = (
                    state.get('player_state'), shuffle, repeat)
                self.handoff_schema = HANDOFF_SCHEMA
            self.save_handoff_tracking()
        except Exception as error:
            with self.lock:
                self.sync_error = str(error)

    def loop(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.sample()
            except Exception as error:
                with self.lock:
                    self.last_error = str(error)
            self.stop.wait(max(0.05, self.interval-(time.monotonic()-start)))

    def payload(self, endpoint):
        with self.lock:
            age = None if self.last_success is None else time.monotonic()-self.last_success
            health = dict(ok=self.last_error is None and age is not None and age < max(5, self.interval*3),
                          sample_age_seconds=round(age, 3) if age is not None else None,
                          error=self.last_error, interval_seconds=self.interval,
                          cloud_sync=dict(enabled=self.sync_dir is not None,
                              file=str(self.sync_dir / HANDOFF_FILENAME) if self.sync_dir else None,
                              last_write_age_seconds=round(time.monotonic()-self.last_sync, 3) if self.last_sync else None,
                              error=self.sync_error, status_scope='Mac file write; phone delivery requires verification'))
            if endpoint == '/health':
                return health
            if endpoint == '/playlist':
                current_id = ((self.state or {}).get('playlist') or {}).get('local_persistent_id')
                value = self.playlist if self.playlist and self.playlist['local_persistent_id'] == current_id else None
            elif endpoint == '/last-playable':
                value = self.last_playable
            else:
                value = self.state
            return dict(health=health, data=copy.deepcopy(value))

class Server(ThreadingHTTPServer):
    daemon_threads = True


def handler_for(monitor, token):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Tokens are accepted only in headers, keeping them out of URLs and logs.
            auth = self.headers.get('Authorization', '')
            if not hmac.compare_digest(auth, 'Bearer '+token):
                self.send_json(401, {'error':'Authorization: Bearer <token> required'})
                return
            path = urlsplit(self.path).path
            if path not in ('/state', '/playlist', '/last-playable', '/health'):
                self.send_json(404, {'error':'Unknown endpoint'})
                return
            self.send_json(200, monitor.payload(path))
        def send_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    return Handler

def state_dir(value):
    p = Path(value).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p

def token_for(directory):
    path = directory / 'token'
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(token+'\n')
        return token

def serve(args, directory):
    if not 0.2 <= args.interval <= 60 or args.playlist_refresh < 5:
        raise ValueError('interval must be 0.2–60 seconds; playlist-refresh must be >= 5 seconds')
    token = token_for(directory)
    monitor = Monitor(directory, args.interval, args.playlist_refresh,
                      Path(args.sync_dir).expanduser() if args.sync_dir else None)
    server = Server((args.host, args.port), handler_for(monitor, token))
    thread = threading.Thread(target=monitor.loop, daemon=True)
    thread.start()
    handoff = None
    if args.airpods:
        from handoff import Handoff
        handoff = Handoff(monitor, args.airpods,
                          Path(args.handoff_dir).expanduser() if args.handoff_dir else None, atomic_json)
        handoff.start()
    def shutdown(*_):
        monitor.stop.set()
        if handoff:
            handoff.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'Music state API: http://{args.host}:{args.port}; token file: {directory / "token"}', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        monitor.stop.set()
        if handoff:
            handoff.stop()
        thread.join(timeout=22)
        server.server_close()


def process_matches(pid):
    try:
        r = subprocess.run(['/bin/ps','-p',str(pid),'-o','command='], capture_output=True, text=True)
        return r.returncode == 0 and str(ROOT / 'music_state.py') in r.stdout and ' serve ' in r.stdout
    except OSError:
        return False

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve','start','stop','status','snapshot'])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--playlist-refresh', type=float, default=30)
    parser.add_argument('--sync-dir', default=None, help='Publish latest.json to this folder')
    parser.add_argument('--airpods', default=None, help='Paired AirPods Bluetooth address')
    parser.add_argument('--handoff-dir', default=None, help='Publish only disconnect events to this folder')
    parser.add_argument('--icloud-handoff', action='store_true', help='Publish disconnect events to iCloud Drive')
    parser.add_argument('--icloud', action='store_true', help='Publish to iCloud Drive/MusicHandoff')
    parser.add_argument('--data-dir', default=str(ROOT / 'runtime'))
    args = parser.parse_args()
    if args.icloud_handoff:
        if not ICLOUD.parent.is_dir():
            raise ValueError('iCloud Drive is unavailable')
        args.handoff_dir = str(ICLOUD)
    if args.handoff_dir and not args.airpods:
        raise ValueError('--handoff-dir requires --airpods')
    if args.airpods and not (ROOT/'bluetooth_watch').is_file():
        raise ValueError('Build bluetooth_watch.swift before starting the Bluetooth trigger')
    if args.icloud:
        if not ICLOUD.parent.is_dir():
            raise ValueError('iCloud Drive directory is unavailable on this Mac')
        args.sync_dir = str(ICLOUD)
    directory = state_dir(args.data_dir)
    pidfile = directory / 'service.pid'
    try:
        pid = int(pidfile.read_text())
    except (OSError, ValueError):
        pid = None
    alive = pid is not None and process_matches(pid)
    if args.command == 'snapshot':
        monitor = Monitor(directory, args.interval, args.playlist_refresh,
                      Path(args.sync_dir).expanduser() if args.sync_dir else None)
        monitor.sample()
        print(json.dumps(monitor.payload('/state'), ensure_ascii=False, indent=2))
    elif args.command == 'serve':
        serve(args, directory)
    elif args.command == 'start':
        if alive:
            print(f'Already running (PID {pid})')
            return
        # Verify automation permission interactively before detaching.
        run_capture()
        with open(directory / 'service.log', 'ab') as log:
            os.chmod(directory / 'service.log', 0o600)
            child = subprocess.Popen([sys.executable, str(ROOT / 'music_state.py'), 'serve',
                '--host', args.host, '--port', str(args.port), '--interval', str(args.interval),
                '--playlist-refresh', str(args.playlist_refresh), '--data-dir', str(directory)] +
                (['--sync-dir', args.sync_dir] if args.sync_dir else []) +
                (['--airpods', args.airpods] if args.airpods else []) +
                (['--handoff-dir', args.handoff_dir] if args.handoff_dir else []),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        time.sleep(0.4)
        if child.poll() is not None:
            raise RuntimeError(f'Start failed; inspect {directory / "service.log"}')
        pidfile.write_text(str(child.pid))
        os.chmod(pidfile, 0o600)
        print(f'Started (PID {child.pid}). Token: {directory / "token"}')
    elif args.command == 'stop':
        if alive:
            os.kill(pid, signal.SIGTERM)
            print('Stop requested')
        else:
            print('Service is stopped')
        pidfile.unlink(missing_ok=True)
    else:
        print('Running' if alive else 'Stopped')
        try:
            print(json.dumps(json.loads((directory/'state.json').read_text()), ensure_ascii=False, indent=2))
        except (OSError, ValueError):
            pass

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
